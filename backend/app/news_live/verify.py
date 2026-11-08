"""Verify the curated feeds: `python -m app.news_live.verify [--ids a,b] [--all]`.

Per feed: one plain GET (no validators) -> HTTP status, content type, entries parsed, newest entry age, ETag /
Last-Modified support; robots.txt of the host (urllib.robotparser, our User-Agent) -> allowed / disallowed /
no_robots / unknown; the terms page and the RSS-intended flag come from the config (curated by hand). Writes
data/state/news_feeds_verify.json (read by GET /news/feeds and Data Health) and prints a table. A feed counts as
verified when HTTP 200, >= 1 entry parsed and robots does not disallow the feed URL.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.robotparser
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.core.config import get_settings
from app.news_live.feeds import FeedSpec, load_feeds, parse_feed, user_agent, verify_path


def robots_status(client: Any, url: str, ua: str, cache: dict[str, Any]) -> str:
    p = urlparse(url)
    host = f"{p.scheme}://{p.netloc}"
    if host not in cache:
        try:
            r = client.get(host + "/robots.txt", timeout=15)
            if r.status_code >= 400:
                cache[host] = "no_robots"
            else:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                cache[host] = rp
        except Exception:  # noqa: BLE001
            cache[host] = "unknown"
    rp = cache[host]
    if isinstance(rp, str):
        return rp
    return "allowed" if rp.can_fetch(ua, url) else "disallowed"


def verify_feed(client: Any, spec: FeedSpec, ua: str, rcache: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    rec: dict[str, Any] = {"id": spec.id, "url": spec.url, "publisher": spec.publisher, "kind": spec.kind,
                           "tier": spec.tier, "config_status": spec.status, "terms": spec.terms,
                           "rss_intended": spec.rss_intended, "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}
    try:
        r = client.get(spec.url, timeout=25)
        rec["http_status"] = r.status_code
        rec["final_url"] = str(r.url)
        rec["content_type"] = r.headers.get("content-type")
        rec["etag"] = bool(r.headers.get("etag"))
        rec["last_modified"] = bool(r.headers.get("last-modified"))
        rec["bytes"] = len(r.content)
        if r.status_code == 200:
            now = datetime.now(UTC)
            items = parse_feed(spec, r.content, now)
            rec["entries"] = len(items)
            if items:
                newest = max(i["published_at"] for i in items)
                rec["newest_age_h"] = round((now - newest).total_seconds() / 3600, 1)
                rec["newest_title"] = items[0]["title"][:120]
                rec["dated_share"] = round(sum(1 for i in items if not i["published_estimated"]) / len(items), 2)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"[:200]
    rec["robots"] = robots_status(client, spec.url, ua, rcache)
    rec["seconds"] = round(time.perf_counter() - t0, 2)
    ok = rec.get("http_status") == 200 and rec.get("entries", 0) > 0 and rec["robots"] != "disallowed"
    rec["verified"] = bool(ok)
    if not ok:
        rec["reason"] = ("robots.txt disallows the feed" if rec["robots"] == "disallowed" else
                         rec.get("error") or (f"HTTP {rec.get('http_status')}" if rec.get("http_status") != 200
                                              else "no entries parsed"))
    return rec


def main(argv: list[str] | None = None) -> int:
    import httpx  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="app.news_live.verify")
    ap.add_argument("--ids", help="comma list of feed ids")
    ap.add_argument("--all", action="store_true", help="include feeds marked rejected")
    a = ap.parse_args(argv)
    s = get_settings()
    feeds = load_feeds(s, include_inactive=True)
    if a.ids:
        want = set(a.ids.split(","))
        feeds = [f for f in feeds if f.id in want]
    elif not a.all:
        feeds = [f for f in feeds if f.status != "rejected" or True]
    ua = user_agent(s)
    rcache: dict[str, Any] = {}
    out = []
    with httpx.Client(headers={"User-Agent": ua}, follow_redirects=True) as c:
        last_host: dict[str, float] = {}
        for f in feeds:
            wait = 2.0 - (time.time() - last_host.get(f.host, 0.0))
            if wait > 0:
                time.sleep(wait)
            last_host[f.host] = time.time()
            rec = verify_feed(c, f, ua, rcache)
            out.append(rec)
            print(f"{'OK ' if rec['verified'] else 'BAD'} {f.id:28} {str(rec.get('http_status')):>4} "
                  f"{rec.get('entries', 0):>4} items  newest {rec.get('newest_age_h', '-')!s:>6} h  robots "
                  f"{rec['robots']:10} etag={rec.get('etag')} lm={rec.get('last_modified')}  {rec.get('reason') or ''}")
    p = verify_path(s)
    prev: dict[str, Any] = {}
    try:
        prev = {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8")).get("feeds", [])}
    except (OSError, ValueError):
        pass
    prev.update({r["id"]: r for r in out})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"verified_at": datetime.now(UTC).isoformat(timespec="seconds"), "user_agent": ua,
                             "feeds": sorted(prev.values(), key=lambda r: r["id"])}, indent=1), encoding="utf-8")
    print(f"{sum(r['verified'] for r in out)}/{len(out)} verified -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
