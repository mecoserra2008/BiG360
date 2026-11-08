"""Robots.txt compliance audit of every external host the backend requests (COVERAGE_V3 A9).

    python -m app.ops.robots_compliance scan   --out DIR     # static enumeration of hosts / path prefixes (no network)
    python -m app.ops.robots_compliance fetch  --out DIR     # one robots.txt request per host, 1 request / s
    python -m app.ops.robots_compliance report --out DIR [--doc docs/it/ROBOTS_COMPLIANCE.md]

Enumeration (`enumerate_refs`): every http(s) URL in a string literal / f-string of backend/app (docstrings and
comments skipped), the curated RSS feeds (config/news_feeds.yaml, status != rejected), the requests made inside
third-party libraries (`LIBRARY_REFS`: yfinance, AKShare) and the data-driven download URLs recorded in the adapters'
state files (`state_refs`: data/state/*.json doc_url, e.g. OAM packages). The macro catalog source URLs
(contracts/macro_catalog.yaml `sources`) are the source links shown in the UI (the fetchers' own literals are the
requests) and are listed as links. A path prefix is the literal path up to the first template field / query.
`report --doc` writes the table between the ROBOTS-TABLE markers of the document; the Action column comes from
app.core.robots_policy (blocked / listed hosts) and `ACTIONS`.
Literals that are never requested (XML namespaces, terms-of-use links, placeholders) are classified by `NON_REQUEST`
and by the link heuristic `_is_link`.

Evaluation (RFC 9309): the group of our product token (`UA_TOKEN`, case-insensitive) else `*`; the longest matching
rule wins, `allow` wins a tie; `*` and `$` wildcards. robots.txt HTTP 4xx = no restrictions; 5xx / network error =
unreachable (RFC: assume complete disallow) -> reported as `unknown`, to be re-checked. `/robots.txt` is always allowed.

Runtime enforcement lives in app.core.robots_policy (deny list checked by app.core.http before every request).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

UA_TOKEN = "BiG-Equity-360"
MAX_ROBOTS_BYTES = 512 * 1024

# =============================================================================================== RFC 9309 evaluator


@dataclass
class Rule:
    allow: bool
    pattern: str

    def regex(self) -> re.Pattern[str]:
        p = self.pattern
        anchored = p.endswith("$")
        if anchored:
            p = p[:-1]
        body = ".*".join(re.escape(_norm(x)) for x in p.split("*"))
        return re.compile("^" + body + ("$" if anchored else ""))


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _norm(s: str) -> str:
    """Percent-decoding of both sides (except an encoded '/'), so '%7E' and '~' compare equal."""
    return unquote(s.replace("%2F", "\x00").replace("%2f", "\x00")).replace("\x00", "%2F")


def parse_robots(text: str) -> list[Group]:
    groups: list[Group] = []
    cur: Group | None = None
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key in ("user-agent", "useragent", "user agent"):
            if cur is None or in_rules:
                cur = Group()
                groups.append(cur)
                in_rules = False
            cur.agents.append(val.lower())
        elif key in ("allow", "disallow"):
            if cur is None:
                continue  # rules before any user-agent line belong to no group (RFC 9309 2.2)
            in_rules = True
            if val:
                cur.rules.append(Rule(key == "allow", val))
        elif key == "crawl-delay" and cur is not None:
            in_rules = True
            try:
                cur.crawl_delay = float(val)
            except ValueError:
                pass
    return groups


def select_group(groups: list[Group], ua_token: str = UA_TOKEN) -> tuple[Group | None, str]:
    """Merged group for the product token (exact, case-insensitive), else the merged '*' group."""
    tok = ua_token.lower()
    mine = [g for g in groups if tok in g.agents]
    which = ua_token
    if not mine:
        mine = [g for g in groups if "*" in g.agents]
        which = "*"
    if not mine:
        return None, "none"
    rules = [r for g in mine for r in g.rules]
    delays = [g.crawl_delay for g in mine if g.crawl_delay is not None]
    return Group([which], rules, max(delays) if delays else None), which


def evaluate(text: str | list[Group] | None, path: str, ua_token: str = UA_TOKEN) -> tuple[bool, str | None]:
    """(allowed, deciding rule) for one path (path + '?query' allowed). No text = no restrictions."""
    if not path.startswith("/"):
        path = "/" + path
    if path == "/robots.txt" or text is None:
        return True, None
    groups = parse_robots(text) if isinstance(text, str) else text
    g, _ = select_group(groups, ua_token)
    if g is None:
        return True, None
    p = _norm(path)
    best: Rule | None = None
    best_len = -1
    for r in g.rules:
        if r.regex().match(p):
            n = len(r.pattern)
            if n > best_len or (n == best_len and r.allow and best is not None and not best.allow):
                best, best_len = r, n
    if best is None:
        return True, None
    return best.allow, f"{'Allow' if best.allow else 'Disallow'}: {best.pattern}"


def deeper_disallows(text: str | None, prefix: str, ua_token: str = UA_TOKEN) -> list[str]:
    """Disallow rules that sit below an allowed prefix (e.g. prefix /api/, rule /api/private): concrete URLs under the
    prefix may still be blocked -> reviewed by hand."""
    if not text:
        return []
    g, _ = select_group(parse_robots(text), ua_token)
    if g is None:
        return []
    out = []
    if prefix in ("", "/"):
        return out  # a bare host root (base URL of relative requests) says nothing about the concrete paths
    for r in g.rules:
        if not r.allow and len(r.pattern) > len(prefix) and r.pattern.startswith(prefix):
            out.append(r.pattern)
    return out


# =============================================================================================== enumeration

URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s\"'<>()\[\]\\]*)?")


@dataclass(frozen=True)
class Ref:
    scheme: str
    host: str
    path: str      # static prefix
    origin: str    # file:line | config:<file> | library:<name>
    kind: str = "request"   # request | link | non_request


#: hosts whose literals are never requested by the backend (namespaces, local, vendor documentation)
NON_REQUEST = {
    "www.w3.org": "XML namespace URI",
    "xbrl.org": "XBRL namespace URI",
    "www.xbrl.org": "XBRL namespace URI",
    "127.0.0.1": "local service",
    "localhost": "local service",
    "host": "placeholder",
    "login.microsoftonline.com": "enterprise SSO issuer (configuration example, user login)",
    "www.bloomberg.com": "licensed-vendor documentation link (vendors.py)",
    "www.lseg.com": "licensed-vendor documentation link (vendors.py)",
    "www.factset.com": "licensed-vendor documentation link (vendors.py)",
}
#: files whose URL literals are metadata links shown to the user (opened by the browser), not requests
LINK_FILES = {
    "app/macro/cb/banks.py": "central-bank metadata links (url / target_url) shown in the UI",
    "app/api/routers/smart_money.py": "regulator register links shown in the UI",
    "app/ingestion/vendors.py": "licensed-vendor documentation links",
    "app/ops/robots_compliance.py": "this audit (library requests are listed in LIBRARY_REFS)",
}
#: (host, path prefix) of literals that are stored document links (filings_index.url etc.), never fetched
LINK_PATHS: tuple[tuple[str, str, str], ...] = (
    ("dart.fss.or.kr", "/dsaf001/main.do", "DART document viewer link stored with each filing (opened by the user)"),
    ("dart.fss.or.kr", "/dsab007/main.do", "Referer header value of the DART search request"),
    ("opendart.fss.or.kr", "/disclosureinfo/fnltt/dwld/main.do", "Referer header value of the DART bulk list request"),
)
_LINK_WORDS = ("terms", "legal", "disclaimer", "copyright", "privacy", "policies", "licen", "about-the-website",
               "kiyaku", "conditions", "/help", "/docs/", "/developer")

#: requests made inside third-party libraries (no literal in our code)
LIBRARY_REFS: tuple[tuple[str, str, str], ...] = (
    # yfinance 1.x (prices, statements, info, earnings dates)
    ("https://fc.yahoo.com/", "yfinance", "cookie"),
    ("https://query1.finance.yahoo.com/v1/test/getcrumb", "yfinance", "crumb"),
    ("https://query2.finance.yahoo.com/v1/test/getcrumb", "yfinance", "crumb"),
    ("https://query2.finance.yahoo.com/v8/finance/chart/", "yfinance", "history"),
    ("https://query2.finance.yahoo.com/v10/finance/quoteSummary/", "yfinance", "info"),
    ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/", "yfinance", "statements"),
    ("https://query1.finance.yahoo.com/v7/finance/quote", "yfinance", "quote"),
    ("https://finance.yahoo.com/calendar/earnings", "yfinance", "get_earnings_dates (HTML page)"),
    ("https://guce.yahoo.com/consent", "yfinance", "EU consent flow"),
    # AKShare functions in app.ingestion.cn_akshare.ALLOWED and macro_catalog akshare_cn
    ("https://vip.stock.finance.sina.com.cn/corp/", "akshare", "stock_financial_report_sina"),
    ("https://quotes.sina.cn/cn/", "akshare", "stock_financial_report_sina"),
    ("https://basic.10jqka.com.cn/new/", "akshare", "stock_financial_abstract_ths"),
    ("http://www.cninfo.com.cn/new/", "akshare", "stock_zh_a_disclosure_report_cninfo"),
    ("https://webapi.cninfo.com.cn/api/", "akshare", "stock_industry_change_cninfo / stock_share_change_cninfo"),
    ("https://query.sse.com.cn/sseQuery/", "akshare", "stock_info_a_code_name"),
    ("https://www.szse.cn/api/", "akshare", "stock_info_a_code_name"),
    ("https://www.bse.cn/nqxxController/", "akshare", "stock_info_a_code_name"),
    ("https://datacenter-web.eastmoney.com/api/", "akshare", "macro_china_*"),
    ("https://datacenter.eastmoney.com/api/", "akshare", "bond_zh_us_rate"),
    ("https://cdn.jin10.com/data_center/reports/il_1.json", "akshare", "macro_china_shibor_all"),
    ("https://data.stats.gov.cn/dg/", "akshare", "macro_china_urban_unemployment"),
)


def _split(url: str) -> tuple[str, str, str] | None:
    try:
        sp = urlsplit(url)
    except ValueError:
        return None
    host = (sp.hostname or "").lower().rstrip(".")
    if not host or "." not in host and host not in NON_REQUEST:
        return None
    path = (sp.path or "/").split("{", 1)[0]
    return sp.scheme or "https", host, path or "/"


def _is_link(path: str) -> bool:
    low = path.lower()
    return any(w in low for w in _LINK_WORDS)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
        # bare string statements (section comments written as strings) are not requests either
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            ids.add(id(n.value))
    return ids


def code_refs(app_dir: Path) -> list[Ref]:
    out: list[Ref] = []
    for py in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        skip = _docstring_ids(tree)
        inner: set[int] = set()
        lits: list[tuple[str, int]] = []
        for n in ast.walk(tree):
            if isinstance(n, ast.JoinedStr):
                parts = []
                for v in n.values:
                    inner.add(id(v))
                    parts.append(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}")
                lits.append(("".join(parts), n.lineno))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip and id(n) not in inner:
                lits.append((n.value, n.lineno))
        rel = py.relative_to(app_dir.parent).as_posix()
        if rel == "app/ops/robots_compliance.py":
            continue
        for s, line in lits:
            for m in URL_RE.finditer(s):
                sp = _split(m.group(0).rstrip(".,;:"))
                if sp is None:
                    continue
                scheme, host, path = sp
                kind = ("non_request" if host in NON_REQUEST else
                        "link" if (rel in LINK_FILES or _is_link(path)
                                   or any(host == h and path.startswith(pp) for h, pp, _ in LINK_PATHS))
                        else "request")
                out.append(Ref(scheme, host, path, f"{rel}:{line}", kind))
    return out


def config_refs(project: Path) -> list[Ref]:
    import yaml  # noqa: PLC0415

    out: list[Ref] = []
    nf = project / "config" / "news_feeds.yaml"
    if nf.exists():
        doc = yaml.safe_load(nf.read_text(encoding="utf-8")) or {}
        for g in doc.get("groups") or []:
            for f in g.get("feeds") or []:
                if str(f.get("status", "")).lower() == "rejected" or not f.get("url"):
                    continue
                sp = _split(str(f["url"]))
                if sp:
                    out.append(Ref(*sp, f"config/news_feeds.yaml:{f.get('id')}"))
    mc = project / "contracts" / "macro_catalog.yaml"
    if mc.exists():
        doc = yaml.safe_load(mc.read_text(encoding="utf-8")) or {}
        for k, v in (doc.get("sources") or {}).items():
            m = URL_RE.search(str((v or {}).get("url") or ""))
            sp = _split(m.group(0)) if m else None
            if sp:
                out.append(Ref(*sp, f"contracts/macro_catalog.yaml:sources.{k}", "link"))
    return out


def library_refs() -> list[Ref]:
    out = []
    for url, lib, what in LIBRARY_REFS:
        sp = _split(url)
        if sp:
            out.append(Ref(*sp, f"library:{lib} ({what})"))
    return out


#: data-driven download URLs (no literal in the code: the source API returns them) as recorded in the adapters' state
#: files (data/state/<source>.json `doc_url`, e.g. dila_fr packages on fr.ftp.opendatasoft.com). Their `url` fields
#: are document links shown to the user (not requested) and are not enumerated.
_STATE_URL_RE = re.compile(r'"doc_url"\s*:\s*"(https?://[^"\s]+)"')
STATE_MAX_BYTES = 64 * 1024 * 1024


def _dir_prefix(path: str, depth: int) -> str:
    """'/a/b/c/file.zip' -> '/a/b/' (depth 2): directory segments only, at most `depth` of them."""
    segs = [s for s in path.split("/")[1:-1] if s][:depth]
    return "/" + "".join(s + "/" for s in segs)


def state_refs(project: Path, depth: int = 2) -> list[Ref]:
    out: dict[tuple[str, str], Ref] = {}
    sdir = project / "data" / "state"
    if not sdir.is_dir():
        return []
    for p in sorted(sdir.glob("*.json")):
        try:
            if p.stat().st_size > STATE_MAX_BYTES:
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _STATE_URL_RE.finditer(txt):
            sp = _split(m.group(1))
            if sp is None:
                continue
            scheme, host, path = sp
            prefix = _dir_prefix(path, depth)
            if (host, prefix) not in out:
                kind = "non_request" if host in NON_REQUEST else "request"
                out[(host, prefix)] = Ref(scheme, host, prefix, f"data/state/{p.name}:doc_url", kind)
    return list(out.values())


def policy_refs() -> list[Ref]:
    """Every app.core.robots_policy block (kind `policy`): listed in the table even when no code references the host
    any more (e.g. a removed feed), evaluated when its robots.txt was fetched; never fetched for that reason alone."""
    from app.core.robots_policy import BLOCKS  # noqa: PLC0415

    return [Ref("https", b.host, b.path_prefix, "app/core/robots_policy.py", "policy") for b in BLOCKS]


def enumerate_refs(project: Path) -> list[Ref]:
    return (code_refs(project / "backend" / "app") + config_refs(project) + library_refs() + state_refs(project)
            + policy_refs())


def hosts_to_check(refs: Iterable[Ref]) -> dict[str, str]:
    """host -> scheme to fetch robots.txt with (https when any reference uses it)."""
    out: dict[str, str] = {}
    for r in refs:
        if r.kind in ("non_request", "policy"):
            continue
        if out.get(r.host) != "https":
            out[r.host] = r.scheme
    return dict(sorted(out.items()))


# =============================================================================================== fetch (network)


def fetch_robots(hosts: dict[str, str], out_dir: Path, *, rate_s: float = 1.0, timeout: float = 20.0,
                 settings: Any = None, skip_existing: bool = True) -> dict[str, dict[str, Any]]:
    """One GET /robots.txt per host with our User-Agent (app.core.http default), no retries, >= rate_s apart.
    Results -> out_dir/robots/<host>.txt + out_dir/robots_index.json."""
    from app.core.http import HttpClient, HttpError  # noqa: PLC0415
    from app.core.ratelimit import RateLimit  # noqa: PLC0415

    rdir = out_dir / "robots"
    rdir.mkdir(parents=True, exist_ok=True)
    idx_p = out_dir / "robots_index.json"
    idx: dict[str, dict[str, Any]] = json.loads(idx_p.read_text(encoding="utf-8")) if idx_p.exists() else {}
    cl = HttpClient("robots_audit", settings=settings, cache=False, max_retries=0, timeout=timeout,
                    rate_limit=RateLimit(1.0 / max(rate_s, 0.1), 1))
    try:
        for host, scheme in hosts.items():
            if skip_existing and host in idx and idx[host].get("status_class") != "unreachable":
                continue
            url = f"{scheme}://{host}/robots.txt"
            rec: dict[str, Any] = {"url": url, "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}
            try:
                r = cl.get(url, cache_ttl=0, raise_for_status=False)
                rec["status"] = r.status_code
                rec["final_url"] = r.url
                body = r.content[:MAX_ROBOTS_BYTES]
                ctype = (r.headers.get("content-type") or "").lower()
                if 200 <= r.status_code < 300:
                    text = body.decode("utf-8", errors="replace")
                    rec["html_body"] = "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))
                    rec["status_class"] = "ok"
                    (rdir / f"{host}.txt").write_text(text, encoding="utf-8")
                elif 400 <= r.status_code < 500:
                    rec["status_class"] = "no_robots"
                else:
                    rec["status_class"] = "unreachable"
            except HttpError as e:
                rec.update(status=e.status, status_class="unreachable", error=str(e)[:200])
            except Exception as e:  # noqa: BLE001
                rec.update(status=None, status_class="unreachable", error=f"{type(e).__name__}: {e}"[:200])
            idx[host] = rec
            idx_p.write_text(json.dumps(idx, indent=1, sort_keys=True), encoding="utf-8")
    finally:
        cl.close()
    return idx


# =============================================================================================== report


@dataclass
class HostVerdict:
    host: str
    robots: str                       # ok | no_robots | unreachable | not_checked
    verdict: str                      # allowed | disallowed | partial | unknown | not_requested
    paths: list[dict[str, Any]] = field(default_factory=list)
    origins: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    note: str | None = None


def evaluate_refs(refs: list[Ref], out_dir: Path, ua_token: str = UA_TOKEN) -> list[HostVerdict]:
    idx_p = out_dir / "robots_index.json"
    idx = json.loads(idx_p.read_text(encoding="utf-8")) if idx_p.exists() else {}
    by_host: dict[str, list[Ref]] = {}
    for r in refs:
        by_host.setdefault(r.host, []).append(r)
    out: list[HostVerdict] = []
    for host, rs in sorted(by_host.items()):
        req = [r for r in rs if r.kind == "request"]
        if not req and (idx.get(host) or {}).get("status_class") in ("ok", "no_robots"):
            req = [r for r in rs if r.kind == "policy"]   # a blocked host no code requests any more
        if not req:
            kinds = {r.kind for r in rs}
            note = NON_REQUEST.get(host) or ("link only (terms / documentation)" if "link" in kinds else None)
            out.append(HostVerdict(host, idx.get(host, {}).get("status_class", "not_checked"), "not_requested",
                                   origins=sorted({r.origin for r in rs})[:6], note=note))
            continue
        rec = idx.get(host) or {}
        sc = rec.get("status_class", "not_checked")
        text = None
        if sc == "ok":
            p = out_dir / "robots" / f"{host}.txt"
            text = p.read_text(encoding="utf-8") if p.exists() else None
        g = select_group(parse_robots(text), ua_token)[0] if text else None
        paths: list[dict[str, Any]] = []
        for path in sorted({r.path for r in req}):
            if sc == "unreachable" or sc == "not_checked":
                paths.append({"path": path, "allowed": None, "rule": None})
                continue
            ok, rule = evaluate(text, path, ua_token)
            deep = deeper_disallows(text, path, ua_token) if ok else []
            paths.append({"path": path, "allowed": ok, "rule": rule, "deeper": deep[:5]})
        vals = [p["allowed"] for p in paths]
        if any(v is None for v in vals):
            verdict = "unknown"
        elif all(vals):
            verdict = "partial" if any(p.get("deeper") for p in paths) else "allowed"
        elif not any(vals):
            verdict = "disallowed"
        else:
            verdict = "partial"
        note = "robots.txt is an HTML page (no rules)" if rec.get("html_body") and not (g and g.rules) else None
        out.append(HostVerdict(host, sc, verdict, paths, sorted({r.origin for r in req})[:8],
                               g.crawl_delay if g else None, note))
    return out


def _compress(paths: list[str], depth: int = 3) -> list[str]:
    keep = sorted({"/".join(p.split("/")[: depth + 1]) + ("/" if p.count("/") > depth else "") for p in paths})
    return keep


#: hand-written actions for hosts without a policy block whose verdict is not a plain "allowed"
ACTIONS: dict[str, str] = {
    "api.imf.org": "kept: robots.txt answers HTTP 502 (API gateway, checked 2026-11-07 01:12 and 10:06 UTC); the IMF "
                   "SDMX API is the documented programmatic interface; re-check robots.txt",
    "servicodados.ibge.gov.br": "kept: robots.txt and the API both time out from this network (2026-11-07, twice); "
                                "ibge_cal fails until reachable; re-check robots.txt before relying on it",
    "www.sec.gov": "/cgi-bin blocked at runtime (app.core.robots_policy): the realtime EDGAR stream switched to the "
                   "XBRL RSS + .hdr.sgml headers (/Archives/edgar, allowed); the deeper /Archives/edgar/data rule is "
                   "one .gif (not used)",
    "cdn.jin10.com": "switched, not requested any more: AKShare reads it inside the library (not through app.core.http), "
                     "so macro_national never calls macro_china_shibor_all (AK_DISALLOWED); SHIBOR from CFETS / "
                     "chinamoney ShiborHis (no robots.txt)",
    "www.bankofengland.co.uk": "IADB (/boeapps/database/, /boeapps/iadb; case-insensitive) blocked at runtime: "
                               "the boe fetchers use /-/media/ files, /rss/news and /monetary-policy pages (allowed)",
}
ALLOWED_DEEPER = "none (deeper rules are not requested)"


def action_for(v: HostVerdict) -> str:
    from app.core.robots_policy import BLOCKS  # noqa: PLC0415

    if v.host in ACTIONS:
        return ACTIONS[v.host]
    bs = [b for b in BLOCKS if b.host == v.host]
    if bs:
        b = bs[0]
        pre = (f"listed only (owner {b.owner}): " if not b.enforce
               else "blocked at runtime (app.core.robots_policy): ")
        return pre + b.action
    if v.verdict == "allowed":
        return "none"
    if v.verdict == "partial" and not any(p["allowed"] is False for p in v.paths):
        return ALLOWED_DEEPER
    return "review"


def render_table(verdicts: list[HostVerdict], actions: dict[str, str] | None = None) -> str:
    actions = actions or {}
    lines = ["| Host | Paths (prefixes requested) | Allowed? | Action |", "|---|---|---|---|"]
    order = {"disallowed": 0, "partial": 1, "unknown": 2, "allowed": 3, "not_requested": 4}
    for v in sorted(verdicts, key=lambda x: (order.get(x.verdict, 9), x.host)):
        if v.verdict == "not_requested":
            continue
        bad = [p for p in v.paths if p["allowed"] is False]
        shown = _compress([p["path"] for p in v.paths])
        ps = ", ".join(f"`{p}`" for p in shown[:6]) + (f" (+{len(shown) - 6})" if len(shown) > 6 else "")
        if v.verdict == "allowed":
            a = "yes" + (" (no robots.txt)" if v.robots == "no_robots" else f" ({v.note})" if v.note else "")
        elif v.verdict == "disallowed":
            a = "**no** (" + "; ".join(sorted({p["rule"] for p in bad if p["rule"]})) + ")"
        elif v.verdict == "partial":
            if bad:
                a = "**partly** (" + "; ".join(sorted({f"{p['path']} -> {p['rule']}" for p in bad})) + ")"
            else:
                deep = sorted({d for p in v.paths for d in (p.get("deeper") or [])})
                a = "yes for the prefixes; deeper rules: " + ", ".join(f"`{d}`" for d in deep[:4])
        else:
            a = f"unknown (robots.txt {v.robots})"
        if v.crawl_delay:
            a += f"; Crawl-delay {v.crawl_delay:g}"
        act = actions.get(v.host) or action_for(v)
        lines.append(f"| {v.host} | {ps} | {a} | {act.replace('|', '/')} |")
    return "\n".join(lines)


DOC_START, DOC_END = "<!-- ROBOTS-TABLE:START -->", "<!-- ROBOTS-TABLE:END -->"


def write_doc_table(doc: Path, table: str, stamp: str | None = None) -> bool:
    """Replace the text between the ROBOTS-TABLE markers of `doc` (False when the markers are missing)."""
    txt = doc.read_text(encoding="utf-8")
    i, j = txt.find(DOC_START), txt.find(DOC_END)
    if i < 0 or j < i:
        return False
    note = f" ({stamp})" if stamp else ""
    body = f"\n<!-- generated by python -m app.ops.robots_compliance report{note} -->\n{table}\n"
    doc.write_text(txt[: i + len(DOC_START)] + body + txt[j:], encoding="utf-8")
    return True


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ops.robots_compliance")
    ap.add_argument("cmd", choices=["scan", "fetch", "report"])
    ap.add_argument("--out", required=True, help="output dir (scan JSON, robots cache)")
    ap.add_argument("--project", default=str(Path(__file__).resolve().parents[3]))
    ap.add_argument("--rate-s", type=float, default=1.0)
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--doc", default=None, help="markdown file whose ROBOTS-TABLE section is rewritten")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    refs = enumerate_refs(Path(args.project))
    (out / "refs.json").write_text(json.dumps([asdict(r) for r in refs], indent=0), encoding="utf-8")
    hosts = hosts_to_check(refs)
    if args.cmd == "scan":
        print(f"{len(refs)} references, {len(hosts)} hosts to check")
        return 0
    if args.cmd == "fetch":
        t0 = time.monotonic()
        idx = fetch_robots(hosts, out, rate_s=args.rate_s, skip_existing=not args.refetch)
        print(f"robots.txt checked for {len(idx)} hosts in {time.monotonic() - t0:.0f} s")
        return 0
    verdicts = evaluate_refs(refs, out)
    (out / "verdicts.json").write_text(json.dumps([asdict(v) for v in verdicts], indent=1), encoding="utf-8")
    table = render_table(verdicts)
    if args.doc:
        ip = out / "robots_index.json"
        idx = json.loads(ip.read_text(encoding="utf-8")) if ip.exists() else {}
        days = sorted({str(r.get("checked_at", ""))[:10] for r in idx.values() if r.get("checked_at")})
        stamp = f"robots.txt fetched {days[0]}..{days[-1]}" if days else None
        ok = write_doc_table(Path(args.doc), table, stamp)
        print(f"{'wrote' if ok else 'markers missing in'} {args.doc}")
    else:
        print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
