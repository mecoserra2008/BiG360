"""Registry of the central banks covered by the monitor (W3.1) - all inputs are official sources.

Series specs are ``(kind, id)``:
  lake      series already ingested into macro_observations (catalog-driven official fetchers)
  fred      FRED/ALFRED (St. Louis Fed) series fetched directly
  ecb       ECB Data Portal SDMX key (flow/key)
  riksbank  Riksbank SWEA API series
  norges    Norges Bank SDMX key (flow/key)
  valet     Bank of Canada Valet series
  bis       BIS central bank policy rate database (WS_CBPOL, daily) - official compilation from the central banks;
            used as cross-check everywhere and as the daily series where the bank's own daily series is not
            machine-readable (BoJ, SNB daily, Banxico/BCCh/RBI/BoK without API keys, BCRP daily)
  lpr       China Loan Prime Rate from the National Interbank Funding Center (chinamoney.com.cn), the PBoC-authorised
            publisher of the LPR (only the last 12 months are queryable; history from BIS CN = LPR 1Y since 2019-08-20)

Inflation targets: official mandates as published by each bank (url).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

Spec = tuple[str, str]


@dataclass(frozen=True)
class Bank:
    code: str
    name: str
    country: str
    currency: str
    rate_name: str
    policy: tuple[Spec, ...]                 # daily policy-rate series in priority order (first = primary)
    bis: str                                 # BIS WS_CBPOL REF_AREA
    corridor: dict[str, Spec] = field(default_factory=dict)  # lower/upper/deposit/lending/overnight
    meeting_source: str | None = None        # app.macro.calendar_sources source id (fetch_<x>)
    target: float | None = None              # inflation target (point / midpoint), percent
    target_text: str = ""
    target_measure: str = ""
    target_url: str = ""
    step_bp: float = 25.0
    url: str = ""
    notes: str = ""
    decisions_since: date = date(1990, 1, 1)  # start of the policy-target regime (earlier data = market rates)

    def to_dict(self) -> dict[str, Any]:
        return {"bank": self.code, "name": self.name, "country": self.country, "currency": self.currency,
                "rate_name": self.rate_name, "inflation_target": self.target, "inflation_target_text": self.target_text,
                "inflation_target_measure": self.target_measure, "inflation_target_url": self.target_url,
                "meeting_calendar": self.meeting_source, "url": self.url, "notes": self.notes,
                "decisions_since": self.decisions_since.isoformat(),
                "policy_sources": [f"{k}:{v}" for k, v in self.policy] + [f"bis:WS_CBPOL.D.{self.bis}"]}


BANKS: tuple[Bank, ...] = (
    Bank("fed", "Federal Reserve (FOMC)", "US", "USD", "Federal funds target range (midpoint)",
         policy=(("fed_mid", "DFEDTARL|DFEDTARU|DFEDTAR"),), bis="US",
         corridor={"lower": ("fred", "DFEDTARL"), "upper": ("fred", "DFEDTARU"), "deposit": ("fred", "IORB"),
                   "lending": ("fred", "RRPONTSYAWARD"), "overnight": ("fred", "EFFR")},
         meeting_source="fomc", target=2.0, target_text="2% PCE inflation over the longer run",
         target_measure="PCE price index",
         target_url="https://www.federalreserve.gov/monetarypolicy/statement-on-longer-run-goals-monetary-policy-strategy.htm",
         url="https://www.federalreserve.gov/monetarypolicy.htm",
         notes="deposit = interest on reserve balances (IORB); lending column = ON RRP award rate (floor)"),
    Bank("ecb", "European Central Bank", "EA", "EUR", "Deposit facility rate",
         policy=(("ecb", "FM/D.U2.EUR.4F.KR.DFR.LEV"),), bis="XM",
         corridor={"deposit": ("ecb", "FM/D.U2.EUR.4F.KR.DFR.LEV"), "lower": ("ecb", "FM/D.U2.EUR.4F.KR.DFR.LEV"),
                   "upper": ("ecb", "FM/D.U2.EUR.4F.KR.MLFR.LEV"), "lending": ("ecb", "FM/D.U2.EUR.4F.KR.MLFR.LEV"),
                   "mro": ("ecb", "FM/D.U2.EUR.4F.KR.MRR_FR.LEV"), "overnight": ("lake", "ecb:EST.B.EU000A2X2A25.WT")},
         meeting_source="ecb_gc", target=2.0, target_text="2% HICP inflation over the medium term (symmetric)",
         target_measure="HICP", target_url="https://www.ecb.europa.eu/mopo/strategy/html/index.en.html",
         url="https://www.ecb.europa.eu/mopo/decisions/html/index.en.html",
         notes="steering rate = DFR since 2024-09-18 (MRO before the floor system; BIS compilation notes the switch)"),
    Bank("boe", "Bank of England (MPC)", "GB", "GBP", "Bank Rate",
         policy=(("lake", "boe:IUDBEDR"),), bis="GB", corridor={"overnight": ("lake", "boe:IUDSOIA")},
         meeting_source="boe_mpc", target=2.0, target_text="2% CPI inflation (HM Treasury remit)",
         target_measure="CPI", target_url="https://www.bankofengland.co.uk/monetary-policy/inflation",
         url="https://www.bankofengland.co.uk/monetary-policy/the-interest-rate-bank-rate"),
    Bank("boj", "Bank of Japan", "JP", "JPY", "Uncollateralized overnight call rate target",
         policy=(("bis", "JP"),), bis="JP", corridor={"overnight": ("lake", "boj:FM01.STRDCLUCON")},
         meeting_source="boj_mpm", target=2.0, target_text="2% CPI price stability target",
         target_measure="CPI (all items less fresh food)", target_url="https://www.boj.or.jp/en/mopo/outline/index.htm",
         url="https://www.boj.or.jp/en/mopo/index.htm",
         notes="BoJ guideline is announced as text; the daily series is the BIS compilation of BoJ announcements"),
    Bank("snb", "Swiss National Bank", "CH", "CHF", "SNB policy rate",
         policy=(("bis", "CH"), ("lake", "snb:snboffzisa.LZ")), bis="CH",
         corridor={"overnight": ("lake", "snb:zimoma.SARON")}, target=1.0,
         target_text="price stability = CPI inflation between 0% and 2% (midpoint 1%)", target_measure="CPI",
         target_url="https://www.snb.ch/en/the-snb/mandates-goals/monetary-policy/strategy",
         url="https://www.snb.ch/en/the-snb/mandates-goals/monetary-policy/decisions",
         notes="SNB data portal publishes the policy rate monthly; daily change dates from the BIS compilation"),
    Bank("boc", "Bank of Canada", "CA", "CAD", "Target for the overnight rate",
         policy=(("lake", "boc:V39079"),), bis="CA",
         corridor={"upper": ("valet", "V39078"), "lending": ("valet", "V39078"), "overnight": ("lake", "boc:AVG.INTWO")},
         meeting_source="boc_rate", target=2.0, target_text="2% CPI inflation, midpoint of the 1-3% control range",
         target_measure="CPI",
         target_url="https://www.bankofcanada.ca/rates/indicators/key-variables/inflation-control-target/",
         url="https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/",
         notes="upper/lending = Bank Rate (Valet V39078)", decisions_since=date(1996, 2, 22)),
    Bank("rba", "Reserve Bank of Australia", "AU", "AUD", "Cash rate target",
         policy=(("lake", "rba:FIRMMCRTD"),), bis="AU", meeting_source="rba_board", target=2.5,
         target_text="2-3% CPI inflation (midpoint 2.5%)", target_measure="CPI",
         target_url="https://www.rba.gov.au/inflation/inflation-target.html",
         url="https://www.rba.gov.au/monetary-policy/"),
    Bank("riksbank", "Sveriges Riksbank", "SE", "SEK", "Policy rate",
         policy=(("riksbank", "SECBREPOEFF"),), bis="SE",
         corridor={"deposit": ("riksbank", "SECBDEPOEFF"), "lower": ("riksbank", "SECBDEPOEFF"),
                   "lending": ("riksbank", "SECBLENDEFF"), "upper": ("riksbank", "SECBLENDEFF")},
         target=2.0, target_text="2% CPIF inflation", target_measure="CPIF",
         target_url="https://www.riksbank.se/en-gb/monetary-policy/the-inflation-target/",
         url="https://www.riksbank.se/en-gb/monetary-policy/", decisions_since=date(1994, 6, 1)),
    Bank("norges", "Norges Bank", "NO", "NOK", "Key policy rate (sight deposit rate)",
         policy=(("norges", "IR/B.KPRA.SD.R"),), bis="NO", target=2.0, target_text="2% CPI inflation over time",
         target_measure="CPI", target_url="https://www.norges-bank.no/en/topics/Monetary-policy/Mandate-monetary-policy/",
         url="https://www.norges-bank.no/en/topics/Monetary-policy/"),
    Bank("bcb", "Banco Central do Brasil (Copom)", "BR", "BRL", "Selic target",
         policy=(("lake", "bcb:432"),), bis="BR", corridor={"overnight": ("lake", "bcb:1178")},
         meeting_source="bcb_copom", target=3.0, target_text="3% IPCA continuous target (tolerance +-1.5 pp)",
         target_measure="IPCA", target_url="https://www.bcb.gov.br/en/monetarypolicy/inflationtargeting",
         url="https://www.bcb.gov.br/en/monetarypolicy/copom", decisions_since=date(1999, 3, 5)),
    Bank("banxico", "Banco de Mexico", "MX", "MXN", "Target for the overnight interbank rate",
         policy=(("bis", "MX"),), bis="MX", target=3.0, target_text="3% CPI inflation (+-1 pp variability interval)",
         target_measure="INPC", target_url="https://www.banxico.org.mx/monetary-policy/inflation-target.html",
         url="https://www.banxico.org.mx/monetary-policy/", notes="daily series = BIS compilation (Banxico SIE needs a token); overnight-rate target since 2008-01-21",
         decisions_since=date(2008, 1, 21)),
    Bank("bcch", "Banco Central de Chile", "CL", "CLP", "Monetary policy rate (TPM)",
         policy=(("bis", "CL"),), bis="CL", target=3.0, target_text="3% CPI inflation at a two-year horizon",
         target_measure="CPI", target_url="https://www.bcentral.cl/en/web/banco-central/areas/monetary-policy",
         url="https://www.bcentral.cl/en/web/banco-central/areas/monetary-policy",
         notes="daily series = BIS compilation (BCCh BDE API needs credentials)"),
    Bank("banrep", "Banco de la Republica (Colombia)", "CO", "COP", "Policy interest rate",
         policy=(("lake", "banrep:DF_CBR_DAILY_HIST.IRCBRM01"),), bis="CO", target=3.0,
         target_text="3% CPI inflation (+-1 pp range)", target_measure="CPI",
         target_url="https://www.banrep.gov.co/en/monetary-policy", url="https://www.banrep.gov.co/en/monetary-policy"),
    Bank("bcrp", "Banco Central de Reserva del Peru", "PE", "PEN", "Reference rate",
         policy=(("bis", "PE"), ("lake", "bcrp:PD04722MM")), bis="PE", target=2.0,
         target_text="1-3% CPI inflation (midpoint 2%)", target_measure="CPI (Lima)",
         target_url="https://www.bcrp.gob.pe/en/monetary-policy.html", url="https://www.bcrp.gob.pe/en/monetary-policy.html",
         notes="BCRP series is monthly; daily change dates from the BIS compilation"),
    Bank("rbi", "Reserve Bank of India (MPC)", "IN", "INR", "Policy repo rate",
         policy=(("bis", "IN"),), bis="IN", target=4.0, target_text="4% CPI inflation (+-2 pp tolerance band)",
         target_measure="CPI (combined)", target_url="https://www.rbi.org.in/Scripts/FS_Overview.aspx?fn=2752",
         url="https://www.rbi.org.in/", notes="daily series = BIS compilation (no keyless RBI API)"),
    Bank("bok", "Bank of Korea", "KR", "KRW", "Base rate",
         policy=(("bis", "KR"),), bis="KR", target=2.0, target_text="2% CPI inflation", target_measure="CPI",
         target_url="https://www.bok.or.kr/eng/main/contents.do?menuNo=400017", url="https://www.bok.or.kr/eng/",
         notes="daily series = BIS compilation (BoK ECOS needs an API key)"),
    Bank("pboc", "People's Bank of China", "CN", "CNY", "1-year Loan Prime Rate",
         policy=(("lpr", "1Y"), ("bis", "CN")), bis="CN", corridor={"lending": ("lpr", "5Y")},
         target=None, target_text="no formal inflation target (annual government CPI goal set at the NPC)",
         target_measure="CPI", target_url="http://www.pbc.gov.cn/en/3688006/index.html",
         url="https://www.chinamoney.com.cn/english/bmklpr/", step_bp=10.0,
         notes="LPR published monthly (20th) by the National Interbank Funding Center under PBoC authorisation; "
               "lending column = 5Y LPR. The 7-day reverse repo (PBoC's main operating rate) has no machine-readable "
               "official series and is not covered."),
)

BY_CODE: dict[str, Bank] = {b.code: b for b in BANKS}
BY_COUNTRY: dict[str, Bank] = {b.country: b for b in BANKS}


def get_bank(code: str) -> Bank | None:
    return BY_CODE.get((code or "").lower())
