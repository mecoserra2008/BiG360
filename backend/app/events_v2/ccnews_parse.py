"""N1 CC-NEWS worker side: one Common Crawl news WARC (multi-member gzip) -> derived article rows. Pure stdlib.

Runs in the CC-NEWS process pool (app.events_v2.ccnews), so it imports neither polars nor torch (small workers).

* `GzipMembers` splits a gzip stream into its members incrementally (every WARC record is its own gzip member). Only
  the first `keep` decompressed bytes of a member are kept, so memory per worker stays a few MB; `consumed` is the
  compressed offset of the last complete member, used to resume an interrupted download with an HTTP Range request.
* `parse_record` reads WARC + HTTP headers and the HTML <head> (bytes regexes; only the small matched spans are
  decoded): og:title / twitter:title / <title>, og:description / description (<= 300 chars), publication time
  (article:published_time, datePublished, pubdate ... or JSON-LD), language (<html lang>, og:locale,
  Content-Language, script / stop-word fallback). The article body is never decoded nor returned.
* `label_row` = the events-v2 labels for free text: issuer links (alias n-grams of the title / description against
  the shared alias table with free-text rules: capitalised mention, short aliases upper-case, ambiguous aliases need a
  finance / brand cue, explicit exchange:ticker tags), market themes (English headline rules), sectors (issuers |
  theme -> sector), countries (country names in the text, else issuer countries, else the publisher's ccTLD, else
  GLOBAL). A row is kept iff it has a theme or an issuer link ("label, then forget").
* `parse_stream` / `parse_file` drive it over chunks / a local file and return (rows, stats).

Row tuple (ROW_FIELDS): url, url_hash, domain, lang, ts_epoch_s, ts_source (0 page meta, 1 WARC fetch time), title,
description, theme_mask, sector_mask, countries [ISO2], issuer_ids [iid], issuer_conf [x100], crawl_epoch_s.
"""

from __future__ import annotations

import hashlib
import html
import pickle
import re
import time
import unicodedata
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

HEAD_MAX = 262_144            # payload bytes inspected per record (the <head> almost always fits)
TEXT_MAX = 300                # stored title / description length
STALE_DAYS = 30               # published more than this before the crawl -> not news any more (dropped)
MIN_LINK = 0.5
ROW_FIELDS = ("url", "url_hash", "domain", "lang", "ts", "ts_source", "title", "description", "theme_mask",
              "sector_mask", "countries", "issuer_ids", "issuer_conf", "crawl_ts")


# ================================================================================================ url key / hash
_SCHEME_RX = re.compile(r"^[a-z]+://(www[0-9]*\.)?")
_QF_RX = re.compile(r"[?#].*$", re.S)


def url_key(url: str) -> str:
    """Same normalisation as app.events_v2.parse.url_key_expr (lower, scheme / www / query / fragment / trailing /)."""
    k = _SCHEME_RX.sub("", url.lower(), count=1)
    k = _QF_RX.sub("", k, count=1)
    return k.rstrip("/")


def hash64(s: str | None) -> int | None:
    """Identical to app.events_v2.parse.hash64 (blake2b-64, little-endian signed)."""
    if not s:
        return None
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8", "ignore"), digest_size=8).digest(), "little", signed=True)


def url_hash(url: str) -> int | None:
    return hash64(url_key(url))


_HOST_RX = re.compile(r"^[a-z]+://([^/:?#]+)", re.I)


def domain_of(url: str) -> str | None:
    m = _HOST_RX.match(url)
    if not m:
        return None
    h = m.group(1).lower().rstrip(".")
    return re.sub(r"^(?:www\d*|m|amp|mobile)\.", "", h) or None


# ================================================================================================ gzip members
_GZ_MAGIC = b"\x1f\x8b\x08"
_HEAD_MARKS = (b"</head", b"</HEAD", b"<body", b"<BODY")
HEAD_MARGIN = 2048            # bytes kept after the end of the <head>


def _head_end(buf: bytes | bytearray, start: int) -> int:
    """Position of the first </head / <body marker at or after `start` (-1 when none yet)."""
    best = -1
    for m in _HEAD_MARKS:
        i = buf.find(m, start)
        if i >= 0 and (best < 0 or i < best):
            best = i
    return best


class GzipMembers:
    """Incremental splitter of a multi-member gzip stream (one WARC record per member).

    feed(chunk) -> decompressed prefixes of the members completed so far. Head-only mode (default): a member is
    decompressed only until its HTML head ended (</head> / <body> + HEAD_MARGIN bytes) or `keep` bytes; the rest of
    the member (the article body) is NOT decompressed - the next member is located by scanning the compressed bytes
    for the gzip magic and verified by decompressing its first bytes into a 'WARC/' header. full=True decompresses
    every member completely (reference mode, used by the tests). `consumed` = absolute compressed offset of the next
    member (HTTP Range-resume point)."""

    def __init__(self, keep: int = HEAD_MAX + 16_384, full: bool = False):
        self.keep = keep
        self.full = full
        self.consumed = 0
        self.bytes_out = 0
        self.skipped_in = 0
        self._buf = bytearray()      # compressed bytes from the start of the current member
        self._reset()

    def _reset(self) -> None:
        self._d = zlib.decompressobj(31)
        self._out = bytearray()
        self._fed = 0                # bytes of _buf given to the decompressor
        self._done_head = False
        self._scan = 0               # where the magic scan resumes
        self._mark_from = 0

    def _finish_member(self, length: int) -> bytes:
        rec = bytes(self._out)
        self.bytes_out += len(self._out)
        self.consumed += length
        del self._buf[:length]
        self._reset()
        return rec

    def _verify(self, at: int) -> bool | None:
        """True / False = WARC member starts at `at`; None = not enough bytes yet."""
        probe = bytes(self._buf[at:at + 4096])
        try:
            d = zlib.decompressobj(31)
            head = d.decompress(probe, 16)
        except zlib.error:
            return False
        if len(head) < 5:
            return None if len(probe) < 4096 else False
        return head.startswith(b"WARC/")

    def _step(self) -> bytes | None:
        if not self._done_head:
            while self._fed < len(self._buf) or self._d.unconsumed_tail:
                if self._d.unconsumed_tail:
                    data = self._d.unconsumed_tail
                else:  # bounded input pieces: no copy of the whole chunk per member
                    data = bytes(self._buf[self._fed:self._fed + 32_768])
                    self._fed += len(data)
                room = self.keep - len(self._out)
                if room > 0:
                    chunk = self._d.decompress(data, min(room, 65_536))
                    self._out += chunk
                else:
                    chunk = self._d.decompress(data, 1 << 20)
                    self.bytes_out += len(chunk)
                if self._d.eof:
                    used = self._fed - len(self._d.unused_data)
                    return self._finish_member(used)
                if not self.full:
                    hit = _head_end(self._out, self._mark_from)
                    if (hit >= 0 and len(self._out) >= hit + HEAD_MARGIN) or len(self._out) >= self.keep:
                        self._done_head = True
                        self._scan = max(self._fed - len(self._d.unconsumed_tail), 10)
                        break
                    self._mark_from = max(0, len(self._out) - 8)
            if not self._done_head:
                return None
        # skip the body: find the next member's gzip magic in the compressed bytes
        while True:
            i = self._buf.find(_GZ_MAGIC, self._scan)
            if i < 0:
                self._scan = max(self._scan, len(self._buf) - 2)
                return None
            ok = self._verify(i)
            if ok is None:
                self._scan = i
                return None
            if ok:
                self.skipped_in += i - self._fed if i > self._fed else 0
                return self._finish_member(i)
            self._scan = i + 1

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while True:
            r = self._step()
            if r is None:
                return out
            out.append(r)

    def close(self) -> list[bytes]:
        """End of stream: the last member (head-only mode never sees its end) is complete."""
        if self._out and (self._done_head or self.full):
            return [self._finish_member(len(self._buf))]
        return []

    def reset_partial(self) -> None:
        """Drop the member in progress (connection lost): resume the download from `consumed`."""
        self._buf = bytearray()
        self._reset()


# ================================================================================================ record parsing
_META_RX = re.compile(rb"<meta\s[^>]{0,2000}?>", re.I)
_ATTR_RX = re.compile(rb"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_TITLE_RX = re.compile(rb"<title[^>]*>(.{1,2000}?)</title", re.I | re.S)
_HTML_LANG_RX = re.compile(rb"<html[^>]{0,500}?\blang\s*=\s*[\"']?([a-zA-Z]{2,3})(?:[-_][a-zA-Z0-9]+)?", re.I)
_HEAD_END_RX = re.compile(rb"</head\s*>|<body[\s>]", re.I)
_CHARSET_RX = re.compile(rb"charset\s*=\s*[\"']?([a-zA-Z0-9_.:-]{2,40})", re.I)
_JSONLD_DATE_RX = re.compile(rb'"datePublished"\s*:\s*"([^"]{8,40})"')
_TAG_RX = re.compile(r"<[^>]{0,500}>")
_WS_RX = re.compile(r"\s+")
_PUB_KEYS = ("article:published_time", "og:article:published_time", "datepublished", "article.published",
             "pubdate", "publishdate", "publish-date", "publish_date", "date", "dc.date", "dc.date.issued",
             "dcterms.created", "dcterms.issued", "sailthru.date", "parsely-pub-date", "article:published",
             "og:published_time", "published_time", "datecreated", "release_date", "originalpublicationdate")
_TITLE_KEYS = ("og:title", "twitter:title")
_DESC_KEYS = ("og:description", "description", "twitter:description")


@dataclass
class Record:
    url: str
    crawl: datetime
    status: int
    ctype: str
    head: bytes
    payload: bytes
    content_lang: str | None = None
    charset: str | None = None


def _hdr_lines(block: bytes) -> dict[bytes, bytes]:
    out: dict[bytes, bytes] = {}
    for line in block.split(b"\r\n")[1:]:
        k, sep, v = line.partition(b":")
        if sep:
            out[k.strip().lower()] = v.strip()
    return out


def _parse_iso(v: str) -> datetime | None:
    v = v.strip()
    if not v:
        return None
    try:
        if re.fullmatch(r"\d{10}(\d{3})?", v):  # epoch seconds / ms
            x = int(v)
            return datetime.fromtimestamp(x / 1000 if x > 10**11 else x, UTC)
        s = v.replace("Z", "+00:00").replace("z", "+00:00")
        s = re.sub(r"\s*\+(\d{2})(\d{2})$", r"+\1:\2", s)
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)
        d = datetime.fromisoformat(s.replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2} \d", s) else s)
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        d = parsedate_to_datetime(v)
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def split_record(rec: bytes) -> Record | None:
    """WARC response record (decompressed prefix) -> Record, or None for other record types / non-HTML / non-200."""
    i = rec.find(b"\r\n\r\n")
    if i < 0:
        return None
    wh = rec[:i]
    if b"WARC-Type: response" not in wh and b"warc-type: response" not in wh.lower():
        return None
    w = _hdr_lines(wh)
    url = w.get(b"warc-target-uri", b"").decode("utf-8", "replace").strip("<> ")
    if not url.startswith("http"):
        return None
    crawl = _parse_iso(w.get(b"warc-date", b"").decode("ascii", "replace")) or datetime(1970, 1, 1, tzinfo=UTC)
    body = rec[i + 4:]
    j = body.find(b"\r\n\r\n")
    if j < 0:
        return None
    hh = body[:j]
    sl = hh.split(b"\r\n", 1)[0].split()
    try:
        status = int(sl[1]) if len(sl) > 1 else 0
    except ValueError:
        status = 0
    h = _hdr_lines(hh)
    ctype = h.get(b"content-type", b"").decode("ascii", "replace").lower()
    if status != 200 or ("html" not in ctype and ctype):
        return Record(url, crawl, status, ctype or "?", b"", b"")
    payload = body[j + 4:j + 4 + HEAD_MAX]
    m = _HEAD_END_RX.search(payload)
    head = payload[: m.start()] if m else payload
    cs = _CHARSET_RX.search(h.get(b"content-type", b""))
    cl = h.get(b"content-language", b"").decode("ascii", "replace")[:2].lower() or None
    return Record(url, crawl, status, ctype or "text/html", head, payload, cl, cs.group(1).decode("ascii").lower() if cs else None)


def _decode(b: bytes, charset: str | None) -> str:
    if not b:
        return ""
    for cs in (charset, "utf-8"):
        if not cs:
            continue
        try:
            return b.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return b.decode("cp1252", "replace")


def clean_text(s: str | None, n: int = TEXT_MAX) -> str | None:
    if not s:
        return None
    t = html.unescape(html.unescape(s))
    t = _TAG_RX.sub(" ", t)
    t = _WS_RX.sub(" ", t).strip()
    if len(t) > n:
        cut = t[:n]
        sp = cut.rfind(" ")
        t = (cut[:sp] if sp > n * 0.6 else cut).rstrip(" ,;:-") + "…"
    return t or None


def head_fields(r: Record) -> dict[str, Any]:
    """title, description, published (datetime | None), lang (2 letters | None) from the HTML head."""
    head = r.head
    charset = r.charset
    if not charset:
        m = _CHARSET_RX.search(head[:8192])
        charset = m.group(1).decode("ascii").lower() if m else None
    metas: dict[str, bytes] = {}
    for m in _META_RX.finditer(head):
        attrs: dict[bytes, bytes] = {}
        for a in _ATTR_RX.finditer(m.group(0)):
            attrs[a.group(1).lower()] = a.group(2) if a.group(2) is not None else (
                a.group(3) if a.group(3) is not None else a.group(4))
        key = attrs.get(b"property") or attrs.get(b"name") or attrs.get(b"itemprop") or attrs.get(b"http-equiv")
        if key is None or b"content" not in attrs:
            continue
        k = key.decode("ascii", "replace").strip().lower()
        if k not in metas:
            metas[k] = attrs[b"content"]
    title = None
    for k in _TITLE_KEYS:
        if metas.get(k):
            title = clean_text(_decode(metas[k], charset))
            if title:
                break
    if not title:
        m = _TITLE_RX.search(head)
        if m:
            title = clean_text(_decode(m.group(1), charset))
    desc = None
    for k in _DESC_KEYS:
        if metas.get(k):
            desc = clean_text(_decode(metas[k], charset))
            if desc:
                break
    pub = None
    for k in _PUB_KEYS:
        v = metas.get(k)
        if v:
            pub = _parse_iso(v.decode("ascii", "replace"))
            if pub:
                break
    if pub is None:
        m = _JSONLD_DATE_RX.search(r.payload)
        if m:
            pub = _parse_iso(m.group(1).decode("ascii", "replace"))
    lang = None
    m = _HTML_LANG_RX.search(r.payload[:4096])
    if m:
        lang = m.group(1).decode("ascii").lower()[:2]
    if not lang and metas.get("og:locale"):
        lang = metas["og:locale"].decode("ascii", "replace")[:2].lower()
    if not lang and metas.get("content-language"):
        lang = metas["content-language"].decode("ascii", "replace")[:2].lower()
    if not lang and r.content_lang:
        lang = r.content_lang
    if lang and not lang.isalpha():
        lang = None
    return {"title": title, "description": desc, "published": pub, "lang": lang}


# ================================================================================================ language fallback
_SCRIPTS = [("ko", re.compile(r"[가-힯ᄀ-ᇿ]")), ("ja", re.compile(r"[぀-ヿ]")), ("zh", re.compile(r"[一-鿿]")),
            ("ru", re.compile(r"[Ѐ-ӿ]")), ("ar", re.compile(r"[؀-ۿ]")), ("el", re.compile(r"[Ͱ-Ͽ]")),
            ("th", re.compile(r"[฀-๿]")), ("he", re.compile(r"[֐-׿]")), ("hi", re.compile(r"[ऀ-ॿ]"))]
_STOP = {  # same lists as app.nlp.sentiment (stop-word vote among Latin-script languages)
    "en": "the of and to in for on with is are at from by as this that will after over new says",
    "de": "der die das und mit für nicht von den ist im zu auf bei nach über eine ein wird",
    "fr": "le la les des et pour avec dans sur est une un du au aux par qui sont pas",
    "es": "el la los las y para con en del por una un que se es más sobre tras",
    "pt": "o a os as e para com em do da dos das por uma um que se não mais após",
    "it": "il lo la gli le e per con del della nel che una un sono più dopo alla",
    "nl": "de het een en van in op met voor is niet aan bij naar door zijn",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOP.items()}


def detect_language(text: str | None) -> str | None:
    if not text:
        return None
    for lang, rx in _SCRIPTS:
        n = len(rx.findall(text))
        if n >= max(2, 0.2 * len(text.replace(" ", ""))) or (lang in ("ko", "ja") and n):
            return lang
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    votes = {k: sum(w in s for w in words) for k, s in _STOPSETS.items()}
    best = max(votes, key=lambda k: (votes[k], k == "en"))
    return best if votes[best] > 0 else None


# ================================================================================================ labelling context
@dataclass
class LabelCtx:
    """Plain-Python labelling context shipped to the workers (built in the main process from the events-v2 alias
    table / issuer dictionary / taxonomy; see app.events_v2.ccnews.build_label_ctx)."""
    aliases: dict[str, tuple[tuple[int, ...], float, bool]]     # org_norm -> (iids, base conf, ambiguous)
    names: dict[int, tuple[str, str]]                             # iid -> (normalised name, ticker lower)
    issuer_country: dict[int, str]
    issuer_sector: dict[int, int]
    tickers: dict[str, tuple[int, ...]]                           # ticker upper -> iids
    theme_rx: list[tuple[int, re.Pattern[str]]]                   # (bit, English headline rule)
    theme_sector: list[tuple[int, int]]                           # (theme bit mask, sector mask)
    countries: dict[str, str]                                     # normalised country name / demonym -> ISO2
    tld: dict[str, str]                                           # ccTLD -> ISO2
    finance_cues: re.Pattern[str] | None = None
    brand_cues: dict[str, re.Pattern[str]] = field(default_factory=dict)
    contra_cues: dict[str, re.Pattern[str]] = field(default_factory=dict)
    common_words: frozenset[str] = frozenset()                    # dictionary words (aliases made of them need cues)
    lower_share: dict[str, float] = field(default_factory=dict)  # observed mid-sentence lower-case share per word
    max_ngram: int = 5

    def first_tokens(self) -> frozenset[str]:
        """First words of all aliases (n-grams starting elsewhere cannot match; cached per process)."""
        ft = self.__dict__.get("_first")
        if ft is None:
            ft = frozenset(k.split(" ", 1)[0] for k in self.aliases)
            self.__dict__["_first"] = ft
        return ft

    def dump(self, path: str) -> None:
        with open(path, "wb") as fh:
            pickle.dump(self, fh, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: str) -> LabelCtx:
        with open(path, "rb") as fh:
            return pickle.load(fh)  # noqa: S301 - own file


_SUFFIX_WORDS = frozenset("inc incorporated corp corporation co company cos ltd limited plc llc sa ag nv se asa ab spa "
                          "holdings holding group oyj tbk bhd berhad gmbh bv kgaa".split())
_LEGAL_FORMS = frozenset("inc incorporated corp corporation ltd limited plc llc ag nv asa spa oyj tbk bhd berhad gmbh "
                          "kgaa".split())  # not 'co' / 'se' / 'sa' (co-founder; Spanish / Portuguese 'se')
_VENUES = frozenset("stadium arena park center centre field dome stadion estadio stade stadio coliseum amphitheater "
                     "amphitheatre pavilion ballpark speedway".split())
_NORM_RX = re.compile(r"[^a-z0-9 ]")


def norm_text(s: str) -> str:
    """Accent fold, lower, punctuation -> space (the alias-table normaliser without legal-suffix stripping)."""
    if s.isascii():
        s = s.lower()
    else:
        s = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return _WS_RX.sub(" ", _NORM_RX.sub(" ", s)).strip()


_TICKER_TAG_RX = re.compile(r"(?:\((?:NYSE|NASDAQ|Nasdaq|NYSE American|AMEX|TSX|TSXV|LSE|ASX|OTC\w*)\s*:\s*([A-Z][A-Z0-9.\-]{0,6})\)"
                            r"|\b(?:NYSE|NASDAQ|Nasdaq)\s*:\s*([A-Z][A-Z0-9.\-]{0,6})\b|\$([A-Z]{2,5})\b)")
# organisation context in English free text (FINANCE_CUES of app.nlp.gdelt_link is added by the context builder)
ORG_CUES = re.compile(
    r"\b(compan(?:y|ies)|firms?|group|makers?|manufacturers?|retailers?|brands?|banks?|insurers?|carriers?|airlines?|"
    r"start-?ups?|subsidiar(?:y|ies)|conglomerates?|chains?|operators?|developers?|producers?|suppliers?|ceo|"
    r"chief executive|chairman|founders?|deals?|stake|bids?|takeovers?|contracts?|orders?|plants?|factor(?:y|ies)|"
    r"launch(?:es|ed)?|unveil(?:s|ed)?|listed|stocks?|shares?|share price|results|quarter(?:ly)?|q[1-4]|fy\d{2,4}|"
    r"sells?|sale|buys?|invest(?:s|ed|ment)?|layoffs?|jobs?|profits?|revenues?|sales|earnings|dividends?|ipo)\b", re.I)
# company / market words in other languages (non-English single-word aliases need one of them)
INTL_ORG_CUES = re.compile(
    r"\b(aktien?|börse|konzern|unternehmen|gmbh|acci[oó]n(?:es)?|empresa|compañ[ií]a|bolsa|cotizaci[oó]n|azion[ei]|"
    r"azienda|societ[àa]|borsa|gruppo|grupo|groupe|soci[ée]t[ée]|entreprise|bourse|action(?:s|naires)?|ações|"
    r"companhia|aandeel|aandelen|beurs|bedrijf|akcie|spółka|akcje|giełda|şirket|hisse|borsası|компани[яи]|акци[ия]|"
    r"бирж|концерн|kurs|gewinn|umsatz|ventas|beneficio|ricavi|utile|fatturato|chiffre d'affaires|lucro|receita|"
    r"quartal|trimestre|dividend[eo]s?|übernahme|adquisici[oó]n|acquisizione|rachat|fusion|fusione|fusi[oó]n)", re.I)
_INDEX_RX = re.compile(r"^(?:s p|dow jones|nasdaq|ftse|stoxx|euro stoxx|nikkei|topix|hang seng|kospi|sensex|nifty|msci|"
                       r"russell|cac|dax|ibex|asx|tsx)(?: (?:index|composite|\d+))*$|\b(?:500|100|200|600|225|40|50)$|"
                       r"^(?:no|nr|number|top|level|phase|day|vol|the|a)(?: \d+)+$|"
                       r"^dow jones (?:industrial|transportation|utility|composite|u s)")
_TRAIL_PUB_RX = re.compile(r"\s+(?:[|–—]|-|·|::)\s+[^|–—]{1,48}$")
_SENT_START_RX = re.compile(r"(?:^|[.!?:|•]\s+|[\"'“‘(\[]\s*)$")


def strip_publisher(title: str) -> str:
    """'Headline words here - Publisher' -> 'Headline words here' (only when >= 4 words remain)."""
    t = title
    for _ in range(2):
        m = _TRAIL_PUB_RX.search(t)
        if m and len(t[: m.start()].split()) >= 4:
            t = t[: m.start()]
    return t


def common_word(w: str, base: float, ctx: LabelCtx) -> bool:
    """True for words that are usually lower-case in running English text ('billion', 'level', 'target'): measured
    share >= 0.5 (ccnews_case_words.tsv); unobserved words fall back to the dictionary list for weak aliases."""
    if len(w) < 4 or w.isdigit():
        return True
    sh = ctx.lower_share.get(w)
    if sh is not None:
        return sh >= 0.5
    return w in ctx.common_words and base < 0.85


def load_case_words(path: str | None = None) -> dict[str, float]:
    import os  # noqa: PLC0415

    p = path or os.path.join(os.path.dirname(__file__), "ccnews_case_words.tsv")
    out: dict[str, float] = {}
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) == 3:
                    lo, up = int(parts[1]), int(parts[2])
                    if lo + up:
                        out[parts[0]] = lo / (lo + up)
    except OSError:
        pass
    return out


_FUNCTION_WORDS = frozenset("of on in the and to for at by as with from into over after before amid says said is are was "
                            "were be has have had will would could its their his her this that than but not new".split())


def informative_case(text: str, lower_share: dict[str, float] | None = None) -> bool:
    """False for Title Case / ALL CAPS text, where capitalisation says nothing about proper nouns. Judged on words
    that are normally lower-case in running text (function words, measured lower-case share >= 0.8): in sentence
    case they stay lower-case even when the text is full of proper nouns."""
    toks = re.findall(r"[^\W\d_]+", text)
    if len(toks) < 3:
        return False
    if sum(1 for w in toks if len(w) >= 3 and w.isupper()) >= 0.6 * len([w for w in toks if len(w) >= 3]):
        return False
    probe = [w for w in toks[1:] if w.lower() in _FUNCTION_WORDS
             or (lower_share is not None and len(w) >= 4 and lower_share.get(w.lower(), 0.0) >= 0.8)]
    if len(probe) >= 2:
        return sum(1 for w in probe if w[0].isupper()) / len(probe) <= 0.34
    words = [w for w in toks if len(w) >= 4]
    if len(words) < 3:
        return False
    return sum(1 for w in words if w[0].isupper()) / len(words) <= 0.5


def capital_evidence(alias: str, texts: list[str], lower_share: dict[str, float] | None = None) -> str:
    """'yes' = alias capitalised mid-sentence in a sentence-case text; 'no' = found in lower case there;
    'unknown' = only sentence-initial / Title-Case / ALL-CAPS occurrences."""
    words = alias.split()
    rx = re.compile(r"(?<![\w])" + r"[\W_]{1,3}".join(re.escape(w) for w in words) + r"(?![\w])", re.I)
    res = "unknown"
    for text in texts:
        if not text:
            continue
        info = informative_case(text, lower_share)
        for m in rx.finditer(text):
            if not info:
                continue
            if _SENT_START_RX.search(text[: m.start()]):
                continue
            span = m.group(0)
            if len(span) >= 5 and span.isupper():  # ALL-CAPS emphasis / acronym: no proper-noun evidence
                continue
            caps = [p[:1].isupper() or not p[:1].isalpha() for p in re.findall(r"[^\W_]+", span)]
            if all(caps):
                return "yes"
            res = "no"
    return res


def link_issuers(title: str | None, desc: str | None, ctx: LabelCtx, lang: str | None,
                 trace: list | None = None) -> list[tuple[int, int]]:
    """[(iid, conf x100)] best first (<= 10). Free-text rules (no NER available, unlike GDELT V1Organizations):
    * alias n-grams (longest first, non-overlapping) of the normalised title (publisher suffix stripped) and
      description; index names (S&P 500, Nikkei 225 ...) never link an issuer;
    * capitalisation evidence comes only from sentence-case text (not Title Case / ALL CAPS, not sentence-initial):
      a lower-case occurrence ('u.s. gold holdings', 'toll plaza') rejects the alias; aliases < 4 chars must appear
      upper-case (BP, IBM);
    * aliases made only of common dictionary words (bert vocabulary; 'also', 'base', 'mint', 'universal'):
      single-word -> capitalised mid-sentence AND an organisation cue (company, shares, deal ...); multi-word ->
      capitalised, or a strong alias (base >= 0.9) with an organisation cue;
    * ambiguous aliases (curated brand words, low-confidence names) -> a brand cue, the issuer's full name / ticker,
      or capitalised + organisation cue; contra cues (river, visa applications ...) reject;
    * non-English text: single-word aliases must be distinctive (not dictionary words, >= 5 chars, base >= 0.8);
    * confidence: title multi-word 0.9 / single-word >= max(base, 0.75); description only <= 0.7; strong cue
      (brand cue / full name / exchange tag) >= 0.85; exchange tags '(NYSE: XYZ)', '$XYZ' -> 0.92 (unique ticker)."""
    best: dict[int, float] = {}
    t = strip_publisher(title or "")
    d = desc or ""
    full = f"{t} {d}"
    cue_text = full.lower()
    fin = bool(ctx.finance_cues.search(full)) if ctx.finance_cues else False
    org = fin or bool(ORG_CUES.search(full))
    en = (lang or "en") == "en"
    tags = {(m.group(1) or m.group(2) or m.group(3) or "").upper() for m in _TICKER_TAG_RX.finditer(full)}
    norm_full = " " + norm_text(full) + " "
    for part, original, in_title in ((norm_text(t), t, True), (norm_text(d), d, False)):
        if not part:
            continue
        toks = part.split()
        used = [False] * len(toks)
        first = ctx.first_tokens()
        starts = [i for i, w in enumerate(toks) if w in first]
        for n in range(min(ctx.max_ngram, len(toks)), 0, -1):
            for i in starts:
                if i + n > len(toks) or any(used[i:i + n]):
                    continue
                key = " ".join(toks[i:i + n])
                hit = ctx.aliases.get(key)
                if hit is None:
                    continue
                iids, base, amb = hit
                if (n == 1 and key in _SUFFIX_WORDS) or _INDEX_RX.search(key):
                    continue
                contra = ctx.contra_cues.get(key)
                if contra is not None and contra.search(cue_text):
                    continue
                brand = ctx.brand_cues.get(key)
                # a brand cue must be something else than the alias itself ('facebook' cue for alias 'facebook')
                strong = bool(brand is not None and brand.search(re.sub(r"\b" + re.escape(key) + r"\b", " ", cue_text)))
                for iid in iids:
                    nm, tk = ctx.names.get(iid, ("", ""))
                    if (nm and nm != key and len(nm) >= 8 and " " in nm and f" {nm} " in norm_full) or \
                            (tk and tk.upper() in tags):
                        strong = True
                if i + n < len(toks) and toks[i + n] in _VENUES:
                    continue  # 'M&T Bank Stadium', 'Daikin Park': a venue named after the company, not company news
                if len(key) >= 4 and i + n < len(toks) and toks[i + n] in _LEGAL_FORMS:
                    strong = True  # 'Chevron Corporation', 'Scout24 SE', 'Pentair plc': a company in any language
                is_common = all(common_word(w, base, ctx) for w in key.split())
                vocab = n == 1 and key in ctx.common_words
                if len(key) < 4:
                    if not re.search(r"(?<![\w])" + re.escape(key.upper()) + r"(?![\w])", original):
                        continue
                    if not (org or strong):
                        continue
                    cap = "yes"
                else:
                    cap = capital_evidence(key, [t, d], ctx.lower_share)
                if cap == "no" and not strong:
                    continue
                if not strong:
                    if amb:
                        if base < 0.6 or not in_title or cap != "yes" or not fin:
                            continue
                    elif not en:
                        if n == 1 and (is_common or vocab or len(key) < 5 or base < 0.8 or not in_title
                                       or not INTL_ORG_CUES.search(full)):
                            continue
                        if n > 1 and (base < 0.9 or all(len(w) < 4 or w.isdigit() for w in key.split())):
                            continue
                    elif n == 1 and not in_title and cap != "yes":  # description: sentence case expected
                        continue
                    elif n == 1 and len(key) >= 4:
                        # dictionary / first-name / surname tokens ('universal', 'graham', 'boeing') need organisation
                        # context; lower-case-common words ('billion', 'level') also a mid-sentence capital
                        if is_common and not (cap == "yes" and org and base >= 0.75):
                            continue
                        if vocab and not (fin and base >= 0.75):  # dictionary / name tokens: a finance cue
                            continue
                        if base < 0.85 and not org:  # weaker single-word names ('Seer', 'Brookfield') need context
                            continue
                    elif n > 1:
                        if all(len(w) < 4 or w.isdigit() for w in key.split()):  # 'a plus', 'no 1', 'da li'
                            continue
                        if any(w in ctx.countries for w in key.split()) and not (base >= 0.9 and org):
                            continue  # place-name aliases ('central china', 'bank of america' passes via base)
                        if is_common and not (cap == "yes" and base >= 0.85):
                            continue
                        if not is_common and base < 0.85 and cap != "yes":
                            continue
                conf = float(base)
                if in_title:
                    conf = max(conf, 0.9 if n > 1 else 0.75)
                else:
                    conf = min(max(conf, 0.6 if n > 1 else 0.55), 0.7)
                if strong:
                    conf = max(conf, 0.85)
                for k in range(i, i + n):
                    used[k] = True
                if trace is not None:
                    trace.append((key, round(base, 2), amb, is_common, cap, strong, in_title, round(conf, 2)))
                for iid in iids:
                    best[iid] = max(best.get(iid, 0.0), conf)
    for m in _TICKER_TAG_RX.finditer(full):
        tk = (m.group(1) or m.group(2) or m.group(3) or "").upper()
        iids = ctx.tickers.get(tk)
        if iids and len(iids) == 1:
            best[iids[0]] = max(best.get(iids[0], 0.0), 0.92)
    out = [(i, int(round(min(1.0, c) * 100))) for i, c in best.items() if c >= MIN_LINK]
    out.sort(key=lambda x: -x[1])
    return out[:10]


def text_countries(title: str | None, desc: str | None, ctx: LabelCtx) -> list[str]:
    """ISO2 codes of countries named in the title (first) and description, by first mention, <= 3."""
    seen: dict[str, int] = {}
    pos = 0
    for part, original in ((title or "", title or ""), (desc or "", desc or "")):
        toks = norm_text(part).split()
        for n in (3, 2, 1):
            for i in range(len(toks) - n + 1):
                key = " ".join(toks[i:i + n])
                iso = ctx.countries.get(key)
                if iso is None:
                    continue
                if key in ("us", "uk") and not re.search(r"\b(?:U\.?S\.?A?|U\.?K\.?)(?![a-z])", original):
                    continue
                seen.setdefault(iso, pos + i)
        pos += 10_000
    return [k for k, _ in sorted(seen.items(), key=lambda kv: kv[1])][:3]


def tld_country(domain: str | None, ctx: LabelCtx) -> str | None:
    if not domain or "." not in domain:
        return None
    return ctx.tld.get(domain.rsplit(".", 1)[1])


def theme_mask(title: str | None, ctx: LabelCtx) -> int:
    if not title:
        return 0
    m = 0
    for b, rx in ctx.theme_rx:
        if rx.search(title):
            m |= 1 << b
    return m


def label_row(title: str | None, desc: str | None, lang: str | None, domain: str | None,
              ctx: LabelCtx) -> tuple[int, int, list[str], list[int], list[int]] | None:
    """(theme_mask, sector_mask, countries, issuer_ids, issuer_conf) or None when the row gets no market label."""
    links = link_issuers(title, desc, ctx, lang)
    tm = theme_mask(title, ctx) if (lang or "en") == "en" else 0
    if not links and not tm:
        return None
    sm = 0
    for tb, s in ctx.theme_sector:
        if tm & tb:
            sm |= s
    ids = [i for i, _ in links]
    for i in ids:
        sm |= int(ctx.issuer_sector.get(i) or 0)
    cs = text_countries(title, desc, ctx)
    if not cs:
        cs = list(dict.fromkeys(c for c in (ctx.issuer_country.get(i) for i in ids) if c))[:3]
    if not cs:
        c = tld_country(domain, ctx)
        cs = [c] if c else []
    return tm, sm, cs, ids, [c for _, c in links]


# ================================================================================================ drivers
def new_stats() -> Counter:
    return Counter()


def parse_records(members: Iterable[bytes], ctx: LabelCtx, stats: Counter) -> Iterator[tuple]:
    for rec in members:
        stats["records"] += 1
        r = split_record(rec)
        if r is None:
            continue
        stats["responses"] += 1
        if not r.head:
            stats["non_html"] += 1
            continue
        stats["html"] += 1
        f = head_fields(r)
        title = f["title"]
        if not title:
            stats["untitled"] += 1
            continue
        stats["titled"] += 1
        lang = f["lang"] or detect_language(title) or None
        if not f["lang"]:
            stats["lang_detected"] += 1
        elif lang == "en":  # many CMS templates hard-code lang="en": trust a clear stop-word vote for another language
            alt = detect_language(f"{title} {f['description'] or ''}")
            if alt and alt != "en":
                lang = alt
                stats["lang_overridden"] += 1
        pub = f["published"]
        ts, src = r.crawl, 1
        if pub is not None:
            if pub < r.crawl - timedelta(days=STALE_DAYS):
                stats["stale"] += 1
                continue
            if pub <= r.crawl + timedelta(days=1):
                ts, src = pub, 0
            else:
                stats["future_pubtime"] += 1
        else:
            stats["no_pubtime"] += 1
        stats[f"lang:{lang or 'xx'}"] += 1
        dom = domain_of(r.url)
        lab = label_row(title, f["description"], lang, dom, ctx)
        if lab is None:
            continue
        tm, sm, cs, ids, conf = lab
        stats["kept"] += 1
        if ids:
            stats["linked"] += 1
        yield (r.url, url_hash(r.url), dom, lang, int(ts.timestamp()), src, title, f["description"], tm, sm, cs, ids,
               conf, int(r.crawl.timestamp()))


def parse_stream(chunks: Iterable[bytes], ctx: LabelCtx, stats: Counter | None = None) -> tuple[list[tuple], Counter]:
    """Chunks of a .warc.gz -> (kept rows, stats)."""
    st = stats if stats is not None else new_stats()
    gz = GzipMembers()
    rows: list[tuple] = []
    t0 = time.process_time()
    for ch in chunks:
        st["bytes_in"] += len(ch)
        rows.extend(parse_records(gz.feed(ch), ctx, st))
    rows.extend(parse_records(gz.close(), ctx, st))
    st["bytes_out"] += gz.bytes_out
    st["cpu_s"] += time.process_time() - t0
    return rows, st


def parse_file(path: str, ctx: LabelCtx, chunk: int = 1 << 20) -> tuple[list[tuple], Counter]:
    def gen() -> Iterator[bytes]:
        with open(path, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b:
                    return
                yield b
    return parse_stream(gen(), ctx)


# ================================================================================================ pool worker
BASE_URL = "https://data.commoncrawl.org/"
USER_AGENT = "BiG-Equity-360/1.0 (+local research; CC-NEWS derived metadata only)"
RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_TRIES = 8
_W: dict[str, Any] = {}


def init_worker(ctx_path: str) -> None:
    """Process-pool initialiser: labelling context + one HTTP client per worker."""
    _W["ctx"] = LabelCtx.load(ctx_path)
    _W["client"] = None


def _client() -> Any:
    import httpx  # noqa: PLC0415

    c = _W.get("client")
    if c is None:
        c = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(60.0, read=180.0),
                         follow_redirects=True)
        _W["client"] = c
    return c


class _Retry(Exception):
    pass


def fetch_and_parse(task: dict[str, Any]) -> dict[str, Any]:
    """Stream one WARC from data.commoncrawl.org (or a local `file`) through the parser, in memory.

    Polite: one connection per worker, exponential back-off with jitter on 429 / 5xx / transport errors (Common
    Crawl's CloudFront answers 503 'Slow Down' under load), resumed with a Range request at the last complete gzip
    member, so an interrupted 1 GB file is not downloaded twice. Nothing is written to disk."""
    import random  # noqa: PLC0415

    ctx: LabelCtx = _W.get("ctx") or LabelCtx.load(task["ctx_path"])
    t0 = time.perf_counter()
    st = new_stats()
    rows: list[tuple] = []
    if task.get("file"):
        rows, st = parse_file(task["file"], ctx)
        return {**{k: task[k] for k in ("month", "idx", "path")}, "rows": rows, "stats": dict(st), "error": None,
                "seconds": time.perf_counter() - t0, "retries": 0}
    import httpx  # noqa: PLC0415

    url = task.get("url") or (BASE_URL + task["path"])
    from app.core.robots_policy import guard  # noqa: PLC0415

    guard(url)  # A9 2026-11-07: data.commoncrawl.org is robots-disallowed -> SourceDisallowed before any request
    gz = GzipMembers()
    tries = 0
    err = None
    while True:
        off = gz.consumed
        try:
            headers = {"Range": f"bytes={off}-"} if off else {}
            with _client().stream("GET", url, headers=headers) as r:
                if r.status_code in RETRY_STATUS:
                    raise _Retry(f"HTTP {r.status_code}")
                if r.status_code == 404:
                    err = "HTTP 404"
                    break
                r.raise_for_status()
                if off and r.status_code != 206:  # range ignored: start over
                    gz, rows, st = GzipMembers(), [], new_stats()
                for ch in r.iter_raw(1 << 20):
                    st["bytes_in"] += len(ch)
                    c0 = time.process_time()
                    rows.extend(parse_records(gz.feed(ch), ctx, st))
                    st["cpu_s"] += time.process_time() - c0
            rows.extend(parse_records(gz.close(), ctx, st))
            break
        except (httpx.TransportError, httpx.HTTPStatusError, _Retry) as e:  # noqa: PERF203
            tries += 1
            st["retries"] += 1
            if tries > MAX_TRIES:
                err = f"{type(e).__name__}: {e}"[:300]
                break
            gz.reset_partial()
            time.sleep(min(300.0, 4.0 * 2 ** tries) * (0.75 + 0.5 * random.random()))
    st["bytes_out"] += gz.bytes_out
    return {**{k: task[k] for k in ("month", "idx", "path")}, "rows": rows if err is None else [], "stats": dict(st),
            "error": err, "seconds": time.perf_counter() - t0, "retries": tries}
