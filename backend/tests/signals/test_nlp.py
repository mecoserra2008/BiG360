"""signals-alt NLP: language detection, cached sentiment scoring (mocked models), relevance, GDELT linking,
Lazy Prices measures, EWM / sentiment aggregation. One network+slow test loads the real Hugging Face models."""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.nlp import aggregate as AG
from app.nlp import gdelt_link as GL
from app.nlp import lazy_prices as LP
from app.nlp import sentiment as SM
from app.nlp.common import ewm_by_group

# ------------------------------------------------------------------------------------------------ sentiment


def test_detect_language_and_clean_text():
    assert SM.detect_language("Toyota cuts outlook as yen surges") == "en"
    assert SM.detect_language("トヨタ、業績予想を下方修正") == "ja"
    assert SM.detect_language("삼성전자 실적 발표") == "ko"
    assert SM.detect_language("贵州茅台股价大涨") == "zh"
    assert SM.detect_language("Die Aktie fällt nach der Prognose für das Jahr") == "de"
    assert SM.detect_language("whatever", hint="pt") == "pt"
    assert SM.clean_text("Q3 &amp; outlook &#x2013; strong") == "Q3 & outlook – strong"


class FakeModel:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self.calls = 0

    def predict(self, texts, batch_size=64):
        self.calls += len(texts)
        return np.array([[0.1, 0.2, 0.7] if "beat" in t else [0.8, 0.15, 0.05] for t in texts], dtype=np.float32)


def test_scorer_routes_by_language_and_caches(tmp_data):
    en, xx = FakeModel(SM.EN_MODEL), FakeModel(SM.ML_MODEL)
    sc = SM.SentimentScorer(tmp_data, models={"en": en, "xx": xx})
    out = sc.score(["Acme beat estimates", "Acme misses", "Acme beat estimates", "Acme verfehlt Ziele"],
                   ["en", "en", "en", "de"])
    assert out["sentiment_label"].to_list() == ["positive", "negative", "positive", "negative"]
    assert out["sentiment_score"][0] == pytest.approx(0.6, abs=1e-6)
    assert out["sentiment_model"].to_list()[3] == SM.ML_MODEL
    assert en.calls == 2 and xx.calls == 1  # duplicate text scored once
    sc2 = SM.SentimentScorer(tmp_data, models={"en": FakeModel(SM.EN_MODEL), "xx": FakeModel(SM.ML_MODEL)})
    sc2.score(["Acme beat estimates"], ["en"])
    assert sc2.stats["cache_hits"] == 1 and sc2.stats["scored"] == 0  # persistent on-disk cache


def test_relevance_rules_and_provider_score():
    secmap = pl.DataFrame({"issuer_id": ["IA", "IB"], "security_id": ["SA", "SB"], "ticker": ["AAPL", "TGT"],
                           "name": ["Apple Inc.", "Target Corporation"], "short_name": ["Apple", "Target"],
                           "sector_id": [None, None], "region": ["US", "US"], "country": ["US", "US"]})
    items = pl.DataFrame({
        "item_id": ["1", "2", "3"], "title": ["Apple shares jump after iPhone launch", "Market wrap", "Missile strike"],
        "snippet": [None, "Target raised guidance", None], "issuer_ids": [["IA"], ["IA", "IB"], ["IB"]],
        "tickers": [[], ["AAPL"], []], "entities": [["Apple"], [], ["Target"]], "themes": [[], [], ["MILITARY"]],
        "source_id": ["gdelt_gkg", "polygon_news", "gdelt_gkg"]})
    linker = GL.OrgLinker.from_rows([{"org": "Target", "issuer_id": "IB", "kind": "b_alias", "name": "Target Corporation",
                                      "ticker": "TGT"}])
    rel = SM.relevance_frame(items, secmap, linker)
    r = {x["item_id"]: x for x in rel.iter_rows(named=True)}
    assert r["1"]["issuer_relevance"] == [1.0]
    assert r["2"]["issuer_relevance"] == [0.85, 0.7]
    assert r["3"]["relevance"] < 0.1  # ambiguous 'target' contradicted by context
    ps = '[{"ticker":"AAPL","sentiment":"positive"},{"ticker":"MSFT","sentiment":"negative"}]'
    assert SM.provider_score(ps, ["AAPL"]) == 1.0 and SM.provider_score(ps) == 0.0


@pytest.mark.network
@pytest.mark.slow
def test_real_models_load_and_score():
    m = SM.HFSentiment(SM.EN_MODEL, threads=4)
    p = m.predict(["Company beats earnings estimates and raises guidance", "Company files for bankruptcy"])
    assert p[0, 2] > p[0, 0] and p[1, 0] > p[1, 2]
    x = SM.HFSentiment(SM.ML_MODEL, threads=4).predict(["La empresa reporta pérdidas récord"])
    assert x.shape == (1, 3) and abs(x.sum() - 1) < 1e-4


# ------------------------------------------------------------------------------------------------ GDELT linking


def _linker() -> GL.OrgLinker:
    return GL.OrgLinker.from_rows([
        {"org": "Amazon.com, Inc.", "issuer_id": "IAMZN", "kind": "a_legal", "name": "Amazon.com, Inc.", "ticker": "AMZN"},
        {"org": "Amazon", "issuer_id": "IAMZN", "kind": "b_alias", "name": None, "ticker": None},
        {"org": "Apple", "issuer_id": "IAAPL", "kind": "b_alias", "name": "Apple Inc.", "ticker": "AAPL"},
        {"org": "Nvidia Corporation", "issuer_id": "INVDA", "kind": "a_legal", "name": "NVIDIA Corporation", "ticker": "NVDA"},
        {"org": "Delta", "issuer_id": "ID1", "kind": "b_alias", "name": "Delta Air Lines", "ticker": "DAL"},
        {"org": "Delta", "issuer_id": "ID2", "kind": "b_alias", "name": "Delta Electronics", "ticker": None},
    ])


def test_org_linker_context_rules():
    lk = _linker()
    assert lk.confidence("amazon", "Fires rage across the Amazon rainforest")[1] == pytest.approx(0.05)
    assert lk.confidence("amazon", "Amazon AWS outage hits Prime customers")[1] >= 0.9
    assert lk.confidence("apple", "Apple unveils new iPhone")[1] >= 0.9
    assert lk.confidence("apple", "Best apple pie recipes for the harvest")[1] == pytest.approx(0.05)
    iid, conf, meth = lk.confidence("nvidia")
    assert iid == "INVDA" and conf >= 0.93 and meth == "exact"
    assert lk.confidence("nvidiaa")[0] == "INVDA" and lk.confidence("nvidiaa")[2] == "fuzzy"
    assert lk.confidence("delta")[0] is None  # alias shared by two issuers


def test_fips_china_is_cn_and_headline_from_url():
    from app.ingestion.gdelt_events import FIPS_TO_ISO2

    assert FIPS_TO_ISO2["CH"] == "CN" and FIPS_TO_ISO2["UK"] == "GB" and FIPS_TO_ISO2["GM"] == "DE"
    assert GL.headline_from_url("https://x.com/news/2026/10/09/fed-holds-rates-steady-again.html") == \
        "Fed Holds Rates Steady Again"  # title case (phase-3 slug cleaner)


def test_company_daily_aggregates_and_events():
    t = datetime(2026, 10, 9, 12, tzinfo=UTC)
    m = pl.DataFrame({
        "gkg_record_id": ["r1", "r2", "r3", "r4"], "published_at": [t] * 4,
        "publisher": ["a.com", "b.com", "a.com", "c.com"],
        "url": ["https://a.com/x", "https://b.com/y", "https://a.com/z", "https://c.com/w"],
        "org_norm": ["nvidia", "nvidia", "amazon", "amazon"], "issuer_id": ["INVDA", "INVDA", "IAMZN", "IAMZN"],
        "tone": [2.0, -4.0, 1.0, -1.0], "themes": [["ECON_STOCKMARKET"], ["TECH"], [], ["ENV_DEFORESTATION"]],
        "countries": [["US"], ["CN"], [], ["BR"]],
        "title": ["Nvidia shares rise", "Nvidia &amp; China", "Amazon Prime Day", "Amazon rainforest fires"]})
    m = GL.link_mentions(m, _linker())
    ev = pl.DataFrame({"global_event_id": [1, 2], "source_url": ["https://www.a.com/x", "https://q.com/n"],
                       "goldstein": [-5.0, 3.0], "num_articles": [10, 3], "issuer_ids": [None, None]},
                      schema_overrides={"issuer_ids": pl.List(pl.Utf8)})
    d = GL.company_daily(m, ev)
    by = {r["issuer_id"]: r for r in d.iter_rows(named=True)}
    assert by["INVDA"]["mentions"] == 2 and by["INVDA"]["neg_share"] == 0.5
    assert by["INVDA"]["linked_event_ids"] == ["1"] and set(by["INVDA"]["country_exposure"]) == {"US", "CN"}
    assert by["IAMZN"]["mentions"] == 1  # rainforest article dropped
    upd = GL.event_issuer_links(m, ev)
    assert upd.filter(pl.col("global_event_id") == 1)["issuer_ids"].to_list()[0] == ["INVDA"]
    assert upd.filter(pl.col("global_event_id") == 1)["headline"][0] == "Nvidia shares rise"


# ------------------------------------------------------------------------------------------------ Lazy Prices


def test_lazy_prices_measures():
    txt = "We face significant competition in our markets. Our revenue may decline if customers leave us. " * 5
    same = LP.pair_metrics(txt, txt)
    assert same["jaccard"] == 1.0 and same["min_edit_ratio"] == 1.0 and same["words_added_pct"] == 0.0
    new = txt + " A material weakness and litigation could result in substantial losses and penalties for us."
    diff = LP.pair_metrics(new, txt)
    assert diff["words_added_pct"] > 0 and diff["neg_word_delta"] > 0


def test_lazy_prices_pairing_and_cosine(tmp_data):
    root = tmp_data.raw_dir / "sec_text"
    rows = []
    texts = {"k1": "risk alpha beta gamma delta " * 50, "k2": "risk alpha beta gamma delta " * 50,
             "q1": "quarter one sales grew strongly " * 40, "q2": "lawsuit penalty impairment default " * 40}
    meta = {"k1": ("10-K", date(2024, 12, 31), date(2025, 2, 10)), "k2": ("10-K", date(2025, 12, 31), date(2026, 2, 9)),
            "q1": ("10-Q", date(2025, 3, 31), date(2025, 5, 1)), "q2": ("10-Q", date(2026, 3, 31), date(2026, 5, 1))}
    for k, (form, pe, kd) in meta.items():
        p = root / "0000000001" / k / "risk_factors.txt.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(texts[k].encode()))
        rows.append({"filing_id": f"sec_edgar:{k}", "section": "risk_factors", "cik": "0000000001", "form_type": form,
                     "knowledge_date": kd, "period_end": pe, "path": p.relative_to(tmp_data.data_dir).as_posix(),
                     "n_words": 200})
    idx = pl.DataFrame(rows)
    pairs = LP.make_pairs(idx)
    got = dict(zip(pairs["filing_id"].to_list(), pairs["prev_filing_id"].to_list(), strict=True))
    assert got == {"sec_edgar:k2": "sec_edgar:k1", "sec_edgar:q2": "sec_edgar:q1"}
    pairs = pairs.sort("filing_id")
    idf = LP.build_idf(pairs, str(tmp_data.data_dir), workers=1)
    LP._init_worker(idf)
    cos = [LP._pair_worker((r["path"], r["prev_path"], str(tmp_data.data_dir), 0, r["section"]))["cosine_tfidf"]
           for r in pairs.iter_rows(named=True)]
    assert cos[0] == pytest.approx(1.0, abs=1e-6) and cos[1] < 0.2


# ------------------------------------------------------------------------------------------------ aggregation


def test_ewm_missing_days_are_not_negative():
    df = pl.DataFrame({"g": ["a"] * 3, "d": [date(2026, 1, 1), date(2026, 1, 5), date(2026, 1, 9)],
                       "num": [0.5, 0.0, 0.0], "den": [1.0, 0.0, 0.0]})
    out = ewm_by_group(df, "g", "d", "num", "den", 7, "e", "w")
    assert out["e"].to_list() == pytest.approx([0.5, 0.5, 0.5])  # no news != bad news
    assert out["w"][2] < out["w"][0]


def test_source_baseline_and_coverage_flag():
    d0 = date(2026, 9, 1)
    rows = []
    for i in range(40):
        n = 2 if i == 39 else 20  # volume collapse on the last day
        for j in range(n):
            rows.append({"item_id": f"{i}-{j}", "source_id": "gdelt_gkg", "knowledge_date": d0 + timedelta(days=i),
                         "item_score": -0.2, "relevance": 1.0, "issuer_id": "I1", "provider_score": None})
    ex = pl.DataFrame(rows)
    base, cov = AG.source_baselines(ex, ex.select("item_id", "source_id", "knowledge_date"))
    last = cov.filter(pl.col("knowledge_date") == d0 + timedelta(days=39))["coverage_ratio"][0]
    assert last < 0.5
    daily = AG.daily_frame(ex, base, cov)
    # per-source de-meaning: a constant -0.2 tone level becomes ~0 once the baseline exists
    v = daily.filter(pl.col("date") == d0 + timedelta(days=20)).select(pl.col("__num") / pl.col("__den")).item()
    assert abs(v) < 1e-9
