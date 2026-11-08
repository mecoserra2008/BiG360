"""China A-share full statements via AKShare (secondary CN source) -> filings_index + fundamentals_raw.

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §3.1 (live-tested 2026-10-10, akshare 1.19.1).
  stock_financial_report_sina(stock='sh600519', symbol='资产负债表'|'利润表'|'现金流量表')  ~100 periods x 70-150 labels,
      incl. 公告日期 (announcement date), 币种, 类型 (合并期末 = consolidated), 报告日 (period end)
  stock_zh_a_disclosure_report_cninfo(symbol, market='沪深京', category='年报'|'半年报'|'一季报'|'三季报', start_date, end_date)
      periodic-report announcements with exact 公告时间 (filings_index)
Hard rules from the research: AKShare has NO timeouts (one call hung > 25 min) and Eastmoney endpoints hang/disconnect
from EU IPs, so every call runs in a killable child process (HardTimeoutRunner, default 60 s) and only the allow-listed
Sina / THS / CNINFO functions below can be called (no *_em functions).
PIT: knowledge_date = 公告日期 of the row (restated rows carry the restatement date => conservative); rows without it get
an imputed date (period_end + 60/120 d, knowledge_date_estimated=true). Accounting standard CAS.
Concepts: the Chinese statement label exactly as AKShare returns it (canonical_line_items.yaml 'cn' lists), taxonomy
'cas_label'. Income/cash-flow values are YTD cumulative from 1 Jan (CN fiscal year = calendar year).
source_entity_id = 'SSE:600519' style (normalize_id('cn_code')).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (CallTimeout, HardTimeoutRunner, fact_row, filing_row, frame,
                                          hkt_to_utc, impute_knowledge_date, parse_num, run_adapter_job, safe_call,
                                          seed_universe, to_date)
from app.ingestion.baostock_cn import cn_entity, to_bs_code
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

ALLOWED = {"stock_financial_report_sina", "stock_financial_abstract_ths", "stock_zh_a_disclosure_report_cninfo",
           "stock_industry_change_cninfo", "stock_share_change_cninfo", "stock_info_a_code_name"}
STATEMENTS = {"资产负债表": "BS", "利润表": "IS", "现金流量表": "CF"}
META_COLS = {"报告日", "数据源", "是否审计", "公告日期", "币种", "类型", "更新日期"}
CNINFO_CATS = {"年报": ("annual", "FY"), "半年报": ("semiannual", "H1"), "一季报": ("quarterly", "Q1"), "三季报": ("quarterly", "Q3")}


def ak_call(func: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Runs inside the HardTimeoutRunner child process. Allow-listed AKShare functions only (no Eastmoney)."""
    if func not in ALLOWED or func.endswith("_em"):
        raise ValueError(f"akshare function {func!r} not allowed")
    os.environ["TQDM_DISABLE"] = "1"
    import contextlib
    import io

    import akshare as ak

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        df = getattr(ak, func)(**kwargs)
    df = df.astype(str)
    return df.to_dict(orient="records")


def cninfo_original_dates(cninfo: dict[str, list[dict[str, Any]]]) -> dict[date, date]:
    """period_end -> earliest periodic-report announcement date, parsed from CNINFO titles ('2024年年度报告')."""
    out: dict[date, date] = {}
    md = {"年报": (12, 31), "半年报": (6, 30), "一季报": (3, 31), "三季报": (9, 30)}
    for cat, recs in (cninfo or {}).items():
        if cat not in md:
            continue
        for r in recs:
            m = re.search(r"(\d{4})\s*年", r.get("公告标题") or "")
            d = to_date(r.get("公告时间"))
            if not m or d is None or any(k in (r.get("公告标题") or "") for k in ("摘要", "英文", "取消")):
                continue
            pe = date(int(m.group(1)), *md[cat])
            if d >= pe and (pe not in out or d < out[pe]):
                out[pe] = d
    return out


def sina_rows_to_facts(records: list[dict[str, Any]], statement: str, entity: str, *, today: date,
                       source_id: str = "akshare", original_dates: dict[date, date] | None = None
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """公告日期 of comparative/restated rows can be a year or more after the original publication. When it is more than
    400 days after period end the row is re-dated to the original CNINFO announcement date (or period_end + lag) and
    flagged knowledge_date_estimated=true (values may include the later restatement; dq penalty applies).
    Rows re-published > 30 days after a known original announcement are likewise re-dated and flagged."""
    kind = STATEMENTS[statement]
    facts, fil = [], {}
    for rec in records:
        pe = to_date(rec.get("报告日"))
        if pe is None:
            continue
        ann = to_date(rec.get("公告日期"))
        q = (pe.month - 1) // 3 + 1
        orig = (original_dates or {}).get(pe)
        if ann is not None and ann >= pe and orig is not None and (ann - orig).days > 30:
            est, kd = True, orig  # comparative/restated row re-published later: date it at the original report
        elif ann is None or ann < pe or (ann - pe).days > 400:
            est = True
            kd = orig if orig is not None else impute_knowledge_date(pe, q == 4, "CN", today)
        else:
            est, kd = False, ann
        native = f"{entity}:{pe.isoformat()}:{kd.isoformat()}"
        fid = f"{source_id}:{native}"
        typ = (rec.get("类型") or "").strip()
        consolidated = not typ.startswith("母公司")
        ccy = (rec.get("币种") or "CNY").strip() or "CNY"
        if ccy in ("nan", "None"):
            ccy = "CNY"
        if fid not in fil:
            fil[fid] = filing_row(source_id=source_id, native_key=native, source_entity_id=entity, form_type="CN_PERIODIC",
                                  form_class={4: "annual", 2: "semiannual"}.get(q, "quarterly"), knowledge_date=kd,
                                  period_end=pe, fiscal_year=pe.year, fiscal_period={1: "Q1", 2: "H1", 3: "Q3", 4: "FY"}[q],
                                  language="zh", items=["accounting_standard=CAS", f"source=sina:{rec.get('数据源', '')}",
                                                        f"audited={rec.get('是否审计', '')}"])
        fp = {1: "Q1", 2: "H1", 3: "9M", 4: "FY"}[q]
        for label, v in rec.items():
            if label in META_COLS:
                continue
            val = parse_num(v)
            if val is None:
                continue
            per_share = "每股" in label
            facts.append(fact_row(
                source_id=source_id, source_entity_id=entity, filing_id=fid, form_type="CN_PERIODIC", taxonomy="cas_label",
                concept=label, label=f"{kind}:{label}", period_start=None if kind == "BS" else date(pe.year, 1, 1),
                period_end=pe, value=val, unit=f"{ccy}/shares" if per_share else ccy, knowledge_date=kd,
                fiscal_year=pe.year, fiscal_period=None if kind == "BS" else fp, consolidated=consolidated,
                knowledge_date_estimated=est))
    return list(fil.values()), facts


def cninfo_rows_to_filings(records: list[dict[str, Any]], entity: str, category: str,
                           source_id: str = "akshare") -> list[dict[str, Any]]:
    fclass, fp = CNINFO_CATS[category]
    out = []
    for r in records:
        title = r.get("公告标题") or ""
        if any(k in title for k in ("摘要", "英文", "取消")):
            continue
        ts_s = r.get("公告时间") or ""
        d = to_date(ts_s)
        if d is None:
            continue
        link = r.get("公告链接") or ""
        key = link.split("announcementId=")[-1].split("&")[0] if "announcementId=" in link else f"{entity}:{ts_s}:{title}"[:120]
        out.append(filing_row(source_id=source_id, native_key=f"cninfo:{key}", source_entity_id=entity,
                              form_type=f"CNINFO_{category}", form_class=fclass, knowledge_date=d,
                              knowledge_ts=hkt_to_utc(d, ts_s[11:16]) if len(ts_s) >= 16 else None, fiscal_period=fp,
                              is_amendment="更正" in title or "修订" in title, language="zh", url=link or None,
                              items=[title[:200]]))
    return out


@register_adapter
class AkshareCnAdapter(SourceAdapter):
    source_id = "akshare"
    regions = ("CN",)
    produces = ("filings_index", "fundamentals_raw")
    rate_limit = RateLimit(1.0, 1)

    def __init__(self, *args: Any, timeout_s: float = 60.0, cninfo: bool = True, **kw: Any):
        super().__init__(*args, **kw)
        self.timeout_s = float(timeout_s)
        self.cninfo = str(cninfo).lower() not in ("0", "false")

    def discover(self) -> pl.DataFrame:
        with HardTimeoutRunner(180) as r:
            recs = r.call("app.ingestion.cn_akshare:ak_call", func="stock_info_a_code_name")
        rows = [{"source_entity_id": cn_entity(to_bs_code(x["code"])), "cn_code": cn_entity(to_bs_code(x["code"])),
                 "name": x["name"], "name_local": x["name"], "country": "CN", "ticker": x["code"], "is_active": True}
                for x in recs]
        return self.discover_frame(rows, self.source_id)

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        codes = [to_bs_code(e) for e in (entities or seed_universe(self.settings, countries=["CN"]).filter(
            pl.col("mic").is_in(["XSHG", "XSHE"])).get_column("ticker").to_list())]
        since_d = to_date(since)
        today = datetime.now(UTC).date()
        landed = []
        with HardTimeoutRunner(self.timeout_s) as runner:
            timeouts = 0
            for code in codes:
                if timeouts >= 3:
                    res.entities_failed[code] = "skipped: akshare unreachable (3 consecutive timeouts)"
                    continue
                sina = code.replace(".", "")
                num = code.split(".")[1]

                def one(code=code, sina=sina, num=num) -> None:
                    nonlocal timeouts
                    payload: dict[str, Any] = {"code": code, "sina": {}, "cninfo": {}}
                    for st in STATEMENTS:
                        self.http.limiter.acquire()
                        try:
                            payload["sina"][st] = runner.call("app.ingestion.cn_akshare:ak_call",
                                                              func="stock_financial_report_sina", stock=sina, symbol=st)
                            timeouts = 0
                        except CallTimeout:
                            timeouts += 1
                            raise
                    if self.cninfo:
                        start = (since_d or today - timedelta(days=730)).strftime("%Y%m%d")
                        for cat in CNINFO_CATS:
                            self.http.limiter.acquire()
                            try:
                                payload["cninfo"][cat] = runner.call(
                                    "app.ingestion.cn_akshare:ak_call", func="stock_zh_a_disclosure_report_cninfo",
                                    symbol=num, market="沪深京", category=cat, start_date=start,
                                    end_date=today.strftime("%Y%m%d"))
                            except Exception as e:  # noqa: BLE001  (CNINFO is best-effort)
                                payload["cninfo"][cat] = []
                                payload.setdefault("errors", []).append(f"cninfo {cat}: {type(e).__name__}")
                    landed.append(self.save_raw("statements", code.replace(".", "_"), payload))
                safe_call(one, res, code, self.log)
        res.raw_paths = landed
        res.meta["since"] = since_d.isoformat() if since_d else None
        res.watermark = today.isoformat()
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        fil, facts = [], []
        today = datetime.now(UTC).date()
        since = to_date(fetched.meta.get("since"))
        for p in fetched.raw_paths:
            payload = self.load_raw_json(p)
            ent = cn_entity(payload["code"])
            orig = cninfo_original_dates(payload.get("cninfo") or {})
            for st, recs in payload["sina"].items():
                f, x = sina_rows_to_facts(recs, st, ent, today=today, source_id=self.source_id, original_dates=orig)
                fil += f
                facts += x
            for cat, recs in (payload.get("cninfo") or {}).items():
                fil += cninfo_rows_to_filings(recs, ent, cat, self.source_id)
        if since:
            fil = [r for r in fil if r["knowledge_date"] >= since]
            facts = [r for r in facts if r["knowledge_date"] >= since]
        return {"filings_index": frame(fil, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw")}


def run(ctx: Any) -> Any:
    return run_adapter_job(AkshareCnAdapter, ctx)
