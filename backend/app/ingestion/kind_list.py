"""Korea KRX KIND listed-company list (KSIC industry names, market, FY-end month) -> reference hand-off file.

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §2.2 (live-tested 2026-10-10, no key).
  https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13  -> HTML table served as .xls (EUC-KR)
Writes no contract table. Output for ref-identity: data/raw/kind_list/latest/kind_listed.csv with columns
  name, market, stock_code (6-digit), ksic_industry, products, listing_date, fy_end_month, ceo, homepage, region
Numeric KSIC codes come from OpenDART company.json (opendart adapter, needs OPENDART_APIKEY).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

URL = "https://kind.krx.co.kr/corpgeneral/corpList.do"
COLS = {"회사명": "name", "시장구분": "market", "종목코드": "stock_code", "업종": "ksic_industry", "주요제품": "products",
        "상장일": "listing_date", "결산월": "fy_end_month", "대표자명": "ceo", "홈페이지": "homepage", "지역": "region"}
MARKET_MIC = {"유가증권": "XKRX", "코스닥": "XKOS", "코넥스": "XKON"}


def parse_kind(content: bytes) -> pl.DataFrame:
    tables = pd.read_html(io.StringIO(content.decode("euc-kr", errors="replace")), converters={"종목코드": str})
    df = tables[0]
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        raise ValueError(f"KIND list columns changed, missing {missing}")
    df = df.rename(columns=COLS)[list(COLS.values())].astype(str)
    out = pl.from_pandas(df.replace({"nan": None}))
    return out.with_columns(pl.col("stock_code").str.zfill(6),
                            pl.col("fy_end_month").str.replace("월", "").str.strip_chars())


@register_adapter
class KindListAdapter(SourceAdapter):
    source_id = "kind_list"
    regions = ("KR",)
    produces = ()
    rate_limit = RateLimit(0.5, 1)

    def __init__(self, *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self.http._client.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

    def latest_path(self):
        p = self.settings.raw_dir / self.source_id / "latest" / "kind_listed.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        r = self.http.get(URL, params={"method": "download", "searchType": "13"}, cache_ttl=86400)
        df = parse_kind(r.content)
        res.raw_paths.append(self.save_raw("xls_html", "corpList", r.content))
        df.write_csv(self.latest_path())
        res.entities_ok.append("kind_list")
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
        rows = []
        for r in df.iter_rows(named=True):
            mic = MARKET_MIC.get(r["market"] or "", None)
            suffix = ".KS" if mic == "XKRX" else (".KQ" if mic == "XKOS" else "")
            rows.append({"source_entity_id": r["stock_code"], "kr_stock_code": r["stock_code"], "name": r["name"],
                         "name_local": r["name"], "country": "KR", "mic": mic, "ticker": r["stock_code"],
                         "yf_ticker": f"{r['stock_code']}{suffix}" if suffix else None,
                         "industry_code": r["ksic_industry"], "industry_scheme": "ksic_name", "is_active": True})
        return self.discover_frame(rows, self.source_id)


def run(ctx: Any) -> Any:
    from app.ingestion._global_common import run_adapter_job

    return run_adapter_job(KindListAdapter, ctx)
