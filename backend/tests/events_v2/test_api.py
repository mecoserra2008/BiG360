"""E7 API: every /events/v2 operation against a synthetic serving lake (the real story / aggregate code writes the
v2 tables), validated against contracts/api.yaml; 404 / 422 paths; serving tables present."""

from __future__ import annotations

import os
import random
from datetime import timedelta
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.api.deps import snapshots
from app.core import config as config_mod
from app.core import schema as schema_mod
from tests.api_contract import validate


def _add_events_v2(s: Any) -> dict[str, Any]:
    from app.core.storage import scan_table
    from app.events_v2 import dicts, store
    from app.events_v2 import taxonomy as tx
    from app.events_v2.parse import hash64
    from app.events_v2.pipeline import day_aggregates, recompute_z
    from app.events_v2.stories import MEMBER_SCHEMA, build_day_stories

    dicts.ensure_static(s)
    idict = dicts.sync_issuers(s)
    iids = idict.get_column("id").to_list()[:12]
    names = {int(r["id"]): (dicts.norm_org(r["name"] or ""), "") for r in idict.iter_rows(named=True)}
    as_of = scan_table("prices_daily", s).select(pl.col("date").max()).collect().item()
    rnd = random.Random(3)
    ctys = [tx.COUNTRY_ID[c] for c in ("US", "CN", "DE", "UA", "GB")]
    themes = [tx.THEME_BIT[t] for t in ("conflict_military", "inflation", "oil_gas", "capital_markets", "m_and_a")]
    heads = ["{} shares jump after earnings beat", "Oil prices surge as {} supply worries grow",
             "{} faces antitrust probe in Europe", "Central bank holds rates, {} reacts", "Protests spread across {}"]
    for k in range(45, -1, -1):
        d = as_of - timedelta(days=k)
        mem, evs = [], []
        n = 40 if k else 160                                   # last day: attention shock
        for i in range(n):
            iid = rnd.choice(iids) if rnd.random() < 0.5 else None
            c = rnd.choice(ctys)
            tb = rnd.choice(themes)
            topic = names.get(iid, ("acme", ""))[0] if iid else rnd.choice(["china", "germany", "ukraine"])
            title = rnd.choice(heads).format(topic.title()) + ("" if rnd.random() < 0.6 else f" - Site{i % 3}")
            mem.append({"url_hash": hash64(f"{d}{i}"), "url": f"https://n{i % 7}.com/{d}/{i}", "title": title,
                        "minute": rnd.randrange(1440), "domain_id": None, "lang_id": None, "source_id": 0,
                        "tone_x100": rnd.randint(-600, 300), "theme_mask": 1 << tb,
                        "sector_mask": tx.sector_mask(["FINS"]) if iid else 0, "countries": [c],
                        "issuer_ids": [iid] if iid else [], "issuer_conf": [95] if iid else [], "n_events": 1})
            evs.append({"event_id": int(d.strftime("%Y%m%d")) * 1000 + i, "date": d, "n_mentions": 5, "n_articles": 2,
                        "quad": rnd.choice([1, 4]), "goldstein_x10": -50, "tone_x100": -200, "countries": [c],
                        "theme_mask": 1 << tb, "sector_mask": 0, "issuer_ids": [iid] if iid else []})
        dom = dicts.assign_ids("domain", [f"n{i}.com" for i in range(7)], s)
        mdf = pl.DataFrame(mem, schema=MEMBER_SCHEMA).with_columns(
            pl.col("url").str.extract(r"https://([^/]+)/", 1).replace_strict(dom, return_dtype=pl.Int32).alias("domain_id"))
        nb = store.read_range("news_stories", d - timedelta(days=3), d + timedelta(days=3), s,
                              columns=["date", "story_id", "title", "n_articles"]).filter(pl.col("date") != d)
        st, sm = build_day_stories(d, mdf, nb, names)
        store.write_day("news_stories", d, st, s)
        store.write_day("story_members", d, sm, s)
        ev = pl.DataFrame(evs).with_columns(pl.col("countries").cast(pl.List(pl.Int16)),
                                            pl.col("issuer_ids").cast(pl.List(pl.Int32)),
                                            pl.col("goldstein_x10").cast(pl.Int8), pl.col("tone_x100").cast(pl.Int16),
                                            pl.col("sector_mask").cast(pl.Int16), pl.col("quad").cast(pl.Int8))
        arts = mdf.select("tone_x100", "theme_mask", "sector_mask", "countries", "issuer_ids")
        store.write_day("event_daily_agg", d, day_aggregates(d, ev, arts), s)
    recompute_z(as_of - timedelta(days=45), as_of, s)
    return {"as_of": as_of, "iids": iids}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("ev2") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    snapshots.reset()
    s = config_mod.get_settings()
    s.ensure_dirs()
    from app.api.serving import build_serving_db
    from tests.fixtures.serving_small import build_lake

    build_lake(s, n_securities=30, years=4)
    info = _add_events_v2(s)
    rep = build_serving_db(s)
    from app.api.main import create_app

    yield {"client": TestClient(create_app()), "info": info, "settings": s, "rep": rep}
    snapshots.reset()
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


def call(env: dict[str, Any], tpl: str, path: dict[str, str] | None = None, params: Any = None, status: int = 200) -> Any:
    url = tpl
    for k, v in (path or {}).items():
        url = url.replace("{" + k + "}", v)
    r = env["client"].get(url, params=params)
    assert r.status_code == status, f"GET {url} -> {r.status_code}: {r.text[:600]}"
    body = r.json()
    validate("GET", tpl, status, body)
    return body


def _sec(env: dict[str, Any]) -> str:
    import duckdb

    con = duckdb.connect(str(env["rep"].path), read_only=True)
    try:
        return con.execute("SELECT security_id FROM ev2_issuer WHERE security_id IS NOT NULL AND iid IN (SELECT iid FROM "
                           "ev2_story_issuer) ORDER BY iid LIMIT 1").fetchone()[0]
    finally:
        con.close()


def test_serving_tables(env):
    t = env["rep"].tables
    for name in ("ev2_dict", "ev2_issuer", "ev2_agg", "ev2_stories", "ev2_story_issuer", "ev2_members", "ev2_domains"):
        assert name in t
    assert t["ev2_stories"] > 0 and t["ev2_agg"] > 0


def test_taxonomy_and_map(env):
    tx = call(env, "/api/v1/events/v2/taxonomy")
    assert len(tx["themes"]) >= 35 and tx["countries"][0]["id"] == "GLOBAL" and tx["data_status"] == "ok"
    m = call(env, "/api/v1/events/v2/map", params={"days": 7})
    assert m["countries"] and {c["country"] for c in m["countries"]} <= {"US", "CN", "DE", "UA", "GB"}
    assert m["timeline"] and max(c["intensity"] for c in m["countries"]) == 1.0
    mt = call(env, "/api/v1/events/v2/map", params={"days": 7, "theme": ["oil_gas", "inflation"]})
    assert mt["themes"] == ["inflation", "oil_gas"]
    assert sum(c["events"] for c in mt["countries"]) < sum(c["events"] for c in m["countries"])
    call(env, "/api/v1/events/v2/map", params={"theme": ["nope"]}, status=422)
    # v1.20.0: coverage + long windows bucketed weekly
    assert tx["coverage"]["first_date"] <= tx["coverage"]["last_date"] and tx["coverage"]["n_days"] > 0
    assert m["coverage"] == tx["coverage"] and m["timeline_freq"] == "day"
    ml = call(env, "/api/v1/events/v2/map", params={"days": 2555, "slider_days": 2555})
    assert ml["timeline_freq"] == "week" and len(ml["timeline"]) <= 366
    assert all(__import__("datetime").date.fromisoformat(r["date"]).weekday() == 0 for r in ml["timeline"])
    call(env, "/api/v1/events/v2/map", params={"days": 2601}, status=422)


def test_timeline(env):
    t = call(env, "/api/v1/events/v2/timeline", params={"label_type": "country", "label_id": "us", "days": 30})
    assert t["label"]["id"] == "US" and t["series"][0]["theme"] == "all" and len(t["series"]) > 1
    assert len(t["series"][0]["points"]) >= 20
    w = call(env, "/api/v1/events/v2/timeline", params={"label_type": "sector", "label_id": "FINS", "freq": "week",
                                                         "theme": ["capital_markets"]})
    assert w["freq"] == "week" and w["series"][0]["theme"] == "capital_markets"
    i = call(env, "/api/v1/events/v2/timeline", params={"label_type": "issuer", "label_id": _sec(env)})
    assert i["label"]["security"]["security_id"] and [s["theme"] for s in i["series"]] == ["all"]
    call(env, "/api/v1/events/v2/timeline", params={"label_type": "country", "label_id": "XX"}, status=404)


def test_stories_feed_and_detail(env):
    p = call(env, "/api/v1/events/v2/stories", params={"days": 7, "limit": 20})
    assert p["total"] > 0 and p["items"]
    sal = [x["salience"] for x in p["items"]]
    assert sal == sorted(sal, reverse=True)
    rec = call(env, "/api/v1/events/v2/stories", params={"days": 7, "sort": "recent", "country": ["DE"],
                                                          "theme": ["oil_gas", "inflation"], "tone_max": 0})
    for x in rec["items"]:
        assert "DE" in x["countries"] and ({"oil_gas", "inflation"} & set(x["themes"])) and x["tone"] <= 0
    sec = _sec(env)
    by = call(env, "/api/v1/events/v2/stories", params={"days": 45, "security_id": sec, "min_relevance": 0.5})
    assert by["items"] and all(any(s["security_id"] == sec for s in x["securities"]) for x in by["items"])
    big = max(p["items"], key=lambda x: x["n_articles"])
    det = call(env, "/api/v1/events/v2/stories/{story_id}", path={"story_id": big["story_id"]})
    assert det["story"]["story_id"] == big["story_id"] and sum(x["articles"] for x in det["days"]) >= 1
    if big["n_articles"] >= 2:
        assert det["members_available"] and det["sources"] and det["timeline"]
    call(env, "/api/v1/events/v2/stories/{story_id}", path={"story_id": "12345"}, status=404)
    call(env, "/api/v1/events/v2/stories/{story_id}", path={"story_id": "abc"}, status=422)
    q = call(env, "/api/v1/events/v2/stories", params={"days": 45, "q": "antitrust", "limit": 5})
    assert all("antitrust" in x["title"].lower() for x in q["items"])


def test_panels_shocks_event_dates(env):
    pc = call(env, "/api/v1/events/v2/labels/{label_type}/{label_id}", path={"label_type": "country", "label_id": "US"})
    assert pc["series"] and pc["themes"] and pc["top_stories"] and pc["latest"]["date"] == env["info"]["as_of"].isoformat()
    ps = call(env, "/api/v1/events/v2/labels/{label_type}/{label_id}", path={"label_type": "sector", "label_id": "FINS"})
    assert ps["label"]["name"]
    pi = call(env, "/api/v1/events/v2/labels/{label_type}/{label_id}", path={"label_type": "issuer", "label_id": _sec(env)})
    assert pi["label"]["type"] == "issuer" and pi["themes"] == []
    assert pc["freq"] == "day"
    pw = call(env, "/api/v1/events/v2/labels/{label_type}/{label_id}", path={"label_type": "country", "label_id": "US"},
              params={"days": 2555})
    assert pw["freq"] == "week" and len(pw["series"]) < len(pc["series"]) + 1 and pw["latest"]["date"] == pc["latest"]["date"]
    call(env, "/api/v1/events/v2/labels/{label_type}/{label_id}", path={"label_type": "issuer", "label_id": "NOPE"},
         status=404)
    sh = call(env, "/api/v1/events/v2/shocks", params={"days": 3, "min_z": 2.0})
    assert sh["items"] and sh["items"][0]["kind"] in ("attention", "tone_drop", "tone_jump")
    assert any(x["label"]["type"] == "country" and x["kind"] == "attention" for x in sh["items"])
    ed = call(env, "/api/v1/events/v2/event-dates", params={"label_type": "country", "label_id": "US", "min_z": 2.0})
    assert ed["dates"] and ed["dates"][-1]["date"] == env["info"]["as_of"].isoformat()
    ed2 = call(env, "/api/v1/events/v2/event-dates", params={"label_type": "country", "label_id": "US", "kind": "any",
                                                             "min_z": 0.5, "min_gap_days": 10})
    ds = [x["date"] for x in ed2["dates"]]
    assert ds == sorted(ds)
