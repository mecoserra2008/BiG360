"""JPX listed-company list (TSE 33/17 industry codes, market segment, size class) -> reference hand-off file.

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §1.3 (live-tested 2026-10-10).
  Page https://www.jpx.co.jp/markets/statistics-equities/misc/01.html links data_j.xlsx (href scraped each run; the
  old data_j.xls is 404). Browser User-Agent required. Updated monthly (3rd business day).
Writes no contract table. Output for ref-identity (security master / TSE33 taxonomy mapping):
  data/raw/jpx_list/latest/jpx_listed.csv  (UTF-8; columns below) + dated raw xlsx under data/raw/jpx_list/xlsx/.
  columns: date, code, name, market, tse33_code, tse33_name, tse17_code, tse17_name, size_code, size_name
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
COLS = {"日付": "date", "コード": "code", "銘柄名": "name", "市場・商品区分": "market", "33業種コード": "tse33_code",
        "33業種区分": "tse33_name", "17業種コード": "tse17_code", "17業種区分": "tse17_name", "規模コード": "size_code",
        "規模区分": "size_name"}


def parse_jpx_xlsx(content: bytes) -> pl.DataFrame:
    df = pd.read_excel(io.BytesIO(content), dtype=str)
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        raise ValueError(f"JPX list columns changed, missing {missing}")
    df = df.rename(columns=COLS)[list(COLS.values())]
    out = pl.from_pandas(df.astype(str).replace({"nan": None, "-": None}))
    return out.with_columns(pl.col("code").str.strip_chars())


@register_adapter
class JpxListAdapter(SourceAdapter):
    source_id = "jpx_list"
    regions = ("JP",)
    produces = ()
    rate_limit = RateLimit(0.5, 1)

    def __init__(self, *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self.http._client.headers["User-Agent"] = BROWSER_UA

    def latest_path(self):
        p = self.settings.raw_dir / self.source_id / "latest" / "jpx_listed.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        page = self.http.get(PAGE, cache_ttl=86400)
        hrefs = re.findall(r'href="([^"]+data_j\.xlsx)"', page.text)
        if not hrefs:
            res.entities_failed["page"] = "data_j.xlsx link not found"
            return res
        url = hrefs[0] if hrefs[0].startswith("http") else "https://www.jpx.co.jp" + hrefs[0]
        x = self.http.get(url, cache_ttl=86400)
        df = parse_jpx_xlsx(x.content)
        res.raw_paths.append(self.save_raw("xlsx", "data_j", x.content, compress=False))
        df.write_csv(self.latest_path())
        res.entities_ok.append("jpx_list")
        res.meta["rows"] = df.height
        res.watermark = datetime.now(UTC).date().isoformat()
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        return {}

    def discover(self) -> pl.DataFrame:
        p = self.latest_path()
        if not p.exists():
            self.fetch()
        df = pl.read_csv(p, infer_schema_length=0)
        rows = [{"source_entity_id": r["code"], "jp_sec_code": r["code"], "name": r["name"], "country": "JP",
                 "mic": "XTKS", "ticker": r["code"], "yf_ticker": f"{r['code']}.T", "industry_code": r["tse33_code"],
                 "industry_scheme": "tse33", "is_active": True} for r in df.iter_rows(named=True)
                if r.get("tse33_code") and r["tse33_code"] not in ("-", "")]
        return self.discover_frame(rows, self.source_id)


def run(ctx: Any) -> Any:
    from app.ingestion._global_common import run_adapter_job

    return run_adapter_job(JpxListAdapter, ctx)
