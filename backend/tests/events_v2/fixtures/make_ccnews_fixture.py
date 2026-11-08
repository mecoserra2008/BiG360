"""Builds fixtures/ccnews_sample.warc.gz: a small CC-NEWS-style WARC (one gzip member per record, warcinfo /
request / response records, like data.commoncrawl.org/crawl-data/CC-NEWS). Synthetic pages, recorded once:

    python tests/events_v2/fixtures/make_ccnews_fixture.py

Every article body contains BODY_SENTINEL so tests can assert that no body text reaches any stored table.
"""

from __future__ import annotations

import gzip
from pathlib import Path

OUT = Path(__file__).with_name("ccnews_sample.warc.gz")
BODY_SENTINEL = "ZQXBODYSENTINEL"
CRAWL = "2026-09-15T08:14:23Z"


def _rec(headers: dict[str, str], block: bytes) -> bytes:
    h = "WARC/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + f"Content-Length: {len(block)}\r\n\r\n"
    return gzip.compress(h.encode() + block + b"\r\n\r\n")


def page(title: str, desc: str | None, pub: str | None, lang: str = "en", og: bool = True, charset: str = "utf-8",
         body_extra: str = "") -> bytes:
    metas = []
    if og:
        metas.append(f'<meta property="og:title" content="{title}">')
    if desc:
        metas.append(f'<meta name="description" content="{desc}">')
    if pub:
        metas.append(f'<meta property="article:published_time" content="{pub}">')
    html = (f'<!doctype html>\n<html lang="{lang}"><head><meta charset="{charset}">'
            f'<script>var x = "<meta name=fake>";</script>{"".join(metas)}<title>{title} - Example News</title></head>'
            f'<body><article><p>{BODY_SENTINEL} The full article text must never be stored. {body_extra}</p>'
            f'<p>{BODY_SENTINEL} second paragraph.</p></article></body></html>')
    return html.encode(charset)


def response(url: str, payload: bytes, status: str = "200 OK", ctype: str = "text/html; charset=utf-8",
             date: str = CRAWL) -> bytes:
    http = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nX-Crawler-Content-Encoding: gzip\r\n\r\n").encode() + payload
    return _rec({"WARC-Type": "response", "WARC-Date": date, "WARC-Target-URI": url,
                 "Content-Type": "application/http; msgtype=response"}, http)


def request(url: str) -> bytes:
    return _rec({"WARC-Type": "request", "WARC-Date": CRAWL, "WARC-Target-URI": url,
                 "Content-Type": "application/http; msgtype=request"},
                f"GET / HTTP/1.1\r\nUser-Agent: CCBot/2.0\r\nHost: {url.split('/')[2]}\r\n\r\n".encode())


ARTICLES = [
    # (url, title, description, published, lang, extra kwargs)
    ("https://www.example.com/markets/nvidia-record-revenue",
     "Nvidia shares jump after record quarterly revenue",
     "Nvidia reported record data-center revenue on Wednesday, beating analyst estimates as AI demand stayed strong.",
     "2026-09-15T06:30:00+00:00", "en", {}),
    ("https://www.lemonde-example.fr/economie/carrefour-acquisition",
     "Carrefour annonce une acquisition majeure en Espagne",
     "Le distributeur Carrefour a annoncé mardi le rachat d'une chaîne espagnole.",
     "2026-09-15T07:05:00+02:00", "fr", {}),
    ("https://news.example.org/world/eu-sanctions-russian-oil",
     "EU imposes new sanctions on Russian oil exports",
     None, None, "en", {"og": False}),
    ("https://www.example.com/local/bakery-award",
     "Local bakery wins regional award for sourdough",
     "A small family bakery was honoured at the county fair.", "2026-09-15T05:00:00Z", "en", {}),
    ("https://www.example.com/old/delta-flights-2026",
     "Delta Air Lines cancels flights after storm",
     "Delta Air Lines cancelled hundreds of flights.", "2026-07-01T05:00:00Z", "en", {}),
    ("https://www.gdelt-dup.com/fed-holds-rates",
     "Fed holds rates steady as inflation cools",
     "The Federal Reserve kept interest rates unchanged.", "2026-09-15T04:00:00Z", "en", {}),
    ("https://www.another-paper.com/economy/fed-steady",
     "Fed holds interest rates steady as inflation cools",
     "Policymakers kept rates on hold on Wednesday.", "2026-09-15T04:20:00Z", "en", {}),
    ("https://www.example.de/wirtschaft/chevron",
     "Chevron Corporation kauft Ölfelder in Texas",
     "Der Ölkonzern Chevron Corporation übernimmt Förderrechte für 2 Milliarden Dollar.",
     "2026-09-14T22:15:00+02:00", "de", {"charset": "iso-8859-1"}),
]


def build() -> bytes:
    out = [_rec({"WARC-Type": "warcinfo", "WARC-Date": CRAWL, "WARC-Filename": "CC-NEWS-20260915081423-00001.warc.gz",
                 "Content-Type": "application/warc-fields"}, b"isPartOf: CC-NEWS\r\npublisher: Common Crawl Foundation\r\n")]
    for url, title, desc, pub, lang, kw in ARTICLES:
        out.append(request(url))
        cs = kw.get("charset", "utf-8")
        out.append(response(url, page(title, desc, pub, lang, og=kw.get("og", True), charset=cs),
                            ctype=f"text/html; charset={cs}"))
    out.append(request("https://www.example.com/report.pdf"))
    out.append(response("https://www.example.com/report.pdf", b"%PDF-1.4 " + BODY_SENTINEL.encode(),
                        ctype="application/pdf"))
    out.append(response("https://www.example.com/missing", page("Not found", None, None), status="404 Not Found"))
    return b"".join(out)


if __name__ == "__main__":
    OUT.write_bytes(build())
    print(OUT, OUT.stat().st_size)
