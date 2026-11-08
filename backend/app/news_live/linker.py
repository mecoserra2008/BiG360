"""Headline -> issuer linking for live news items (roadmap v3 N2.4), reusing the events_v2 entity linker.

The events_v2 labelling context (alias table of ~23k normalised issuer names / short names / curated aliases with a
base confidence and an ambiguity flag, plus the issuer meta) is the same one the GDELT parser uses for GKG
organisations; here the candidates are the word n-grams (1..6) of the normalised headline instead of GKG's
V1Organizations, and each candidate goes through the same context rule (app.events_v2.parse.final_conf: headline
confirmation by full name / cash-tag / brand cue / finance cue, ambiguous short aliases capped). Links >= 0.5 are
kept (the GDELT threshold). Explicit tickers ("$AAPL", "(NASDAQ: AAPL)") link directly at 0.95.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logsetup import get_logger

log = get_logger(__name__)

MIN_LINK = 0.5
MAX_NGRAM = 6
_CASHTAG = re.compile(r"\$([A-Z][A-Z0-9]{0,5})\b")
_EXCH = re.compile(r"\((?:NYSE|NASDAQ|Nasdaq|NYSE American|NYSE Arca|TSX|TSXV|LSE|ASX|OTC|OTCQX|Euronext|XETRA)\s*:\s*"
                   r"([A-Z][A-Z0-9.]{0,6})\)")


def _common() -> frozenset[str]:
    from app.events_v2.dicts import _common_words  # noqa: PLC0415

    try:
        return _common_words()
    except Exception:  # noqa: BLE001  # pragma: no cover
        return frozenset()


COMMON = _common()


class HeadlineLinker:
    def __init__(self, wctx: Any):
        self.ctx = wctx
        self.alias: dict[str, tuple[list[int], float, bool]] = {}
        for org, ids, conf, amb in wctx.aliases.select("org_norm", "issuer_ids", "conf", "ambiguous").iter_rows():
            if org:
                self.alias[org] = (list(ids or []), float(conf or 0.0), bool(amb))
        iss = wctx.issuers
        cols = iss.columns
        self.issuer_id: dict[int, str] = {}
        self.country: dict[int, str] = {}
        self.sector_mask: dict[int, int] = {}
        self.ticker_iids: dict[str, list[int]] = {}
        for r in iss.iter_rows(named=True):
            iid = int(r["iid"])
            if "issuer_id" in cols and r.get("issuer_id"):
                self.issuer_id[iid] = r["issuer_id"]
            if r.get("country"):
                self.country[iid] = r["country"]
            self.sector_mask[iid] = int(r.get("sector_mask") or 0)
            t = (r.get("ticker") or "").upper()
            if t and len(t) >= 2:
                self.ticker_iids.setdefault(t, []).append(iid)

    @staticmethod
    def norm(s: str) -> str:
        from app.events_v2.dicts import norm_org  # noqa: PLC0415

        return norm_org(s)

    def link(self, title: str | None, language: str | None = "en") -> list[tuple[int, float]]:
        """[(iid, confidence)] best first, confidence >= MIN_LINK."""
        if not title:
            return []
        from app.events_v2.parse import final_conf  # noqa: PLC0415

        best: dict[int, float] = {}
        for rx in (_CASHTAG, _EXCH):
            for m in rx.finditer(title):
                for iid in self.ticker_iids.get(m.group(1).split(".")[0], [])[:3]:
                    best[iid] = max(best.get(iid, 0.0), 0.95)
        text = _EXCH.sub(" ", _CASHTAG.sub(" ", title))
        caps = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9&'-]*", text) if w[0].isupper()}
        long_words = [w for w in re.findall(r"[A-Za-z]{4,}", text)]
        title_case = bool(long_words) and sum(w[0].isupper() for w in long_words) >= 0.7 * len(long_words)
        words = self.norm(text).split()
        used: set[int] = set()       # word positions covered by a longer match
        n = len(words)
        for k in range(min(MAX_NGRAM, n), 0, -1):
            for i in range(0, n - k + 1):
                if any(j in used for j in range(i, i + k)):
                    continue
                org = " ".join(words[i:i + k])
                hit = self.alias.get(org)
                if hit is None or (k == 1 and len(org) < 3):
                    continue
                ids, base, amb = hit
                conf = final_conf(org, base, title, None, ids, False, self.ctx, amb)
                if k == 1 and org in COMMON:
                    # a common word ("cars", "target", "shell") names a company only as a proper noun
                    if org not in caps or (title_case and conf < 0.9):
                        continue
                if conf < MIN_LINK:
                    continue
                used.update(range(i, i + k))
                for iid in ids[:3]:
                    best[iid] = max(best.get(iid, 0.0), conf)
        return sorted(best.items(), key=lambda kv: -kv[1])[:6]

    def issuer_ids(self, links: list[tuple[int, float]]) -> list[str]:
        return [self.issuer_id[i] for i, _ in links if i in self.issuer_id]
