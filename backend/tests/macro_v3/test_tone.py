"""W3.6 tone pipeline with the classifier mocked (no model download, no network)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.macro.cb import tone

FX = Path(__file__).parent / "fixtures"


class FakeModel:
    """Hawkish if the sentence mentions 'raise', dovish if 'lower', else neutral."""

    def __init__(self):
        self.calls = 0

    def predict(self, texts):
        self.calls += len(texts)
        out = []
        for t in texts:
            low = t.lower()
            out.append([0.1, 0.8, 0.1] if "raise" in low else [0.1, 0.1, 0.8] if "lower" in low else [0.9, 0.05, 0.05])
        return np.asarray(out)


class MemCache(tone.ScoreCache):
    def __init__(self):  # noqa: D107 - in-memory only
        self.data, self.dirty = {}, False


def test_fomc_statement_fixture_extraction_and_votes():
    html = (FX / "fomc_statement_20260916.htm").read_bytes()
    paras = tone.extract_paragraphs(html)
    sents = tone.sentences(paras)
    assert len(sents) >= 5 and any("federal funds rate" in s for s in sents)
    assert not any(s.lower().startswith("voting for") for s in sents)
    n, txt = tone.vote_split(paras)
    assert txt is not None and n is not None and n >= 0


def test_vote_split_variants():
    assert tone.vote_split(["the MPC voted by a majority of 6–3 to maintain Bank Rate"]) == (3, "6-3")
    assert tone.vote_split(["the Policy Board decided, by a 7-2 majority vote, to set"]) == (2, "7-2")
    assert tone.vote_split(["approved for release by a 12 – 0 vote:"]) == (0, "12-0")
    assert tone.vote_split(["Voting against the action were A. Person and B. Person."]) == (2, "2 against")
    assert tone.vote_split(["no vote information here at all, just text."]) == (None, None)


def test_lexicon_direction():
    assert tone.lexicon_score(["The Committee decided to raise the target range given persistent inflation."]) > 0
    assert tone.lexicon_score(["Downside risks and a weakening labour market support easing."]) < 0
    assert tone.lexicon_score(["The meeting took place in Washington."]) is None


def test_score_doc_with_mocked_model_and_cache():
    sents = ["The Committee decided to raise the target range by 25 basis points today.",
             "Members judged it appropriate to lower rates further in the coming months.",
             "The Committee will continue to monitor incoming information carefully."]
    m, c = FakeModel(), MemCache()
    r = tone.score_doc(sents, m, c)
    assert r["tone_model"] == pytest.approx(0.0, abs=1e-9) and r["hawkish_share"] == pytest.approx(1 / 3)
    assert m.calls == 3
    tone.score_doc(sents, m, c)  # cached -> no new model calls
    assert m.calls == 3
    r2 = tone.score_doc(sents[:1], m, c)
    assert r2["tone_model"] == pytest.approx(0.7) and r2["n_sentences"] == 1
    assert tone.score_doc(sents, None, None)["tone_model"] is None  # lexicon only


def test_doc_urls():
    from datetime import date

    assert tone.doc_url("fed", date(2026, 9, 16), None).endswith("monetary20260916a.htm")
    assert tone.doc_url("boe", date(2026, 9, 17), None).endswith("/2026/september-2026")
    assert tone.doc_url("boc", date(2026, 9, 2), None).endswith("/2026/09/fad-press-release-2026-09-02/")
    assert tone.doc_url("ecb", date(2026, 9, 10), "https://www.ecb.europa.eu/press/calendars/x.html") is None
