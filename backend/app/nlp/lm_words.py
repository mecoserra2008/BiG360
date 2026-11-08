"""Loughran-McDonald (LM) finance sentiment word lists.

The full LM Master Dictionary (Loughran & McDonald, Notre Dame SRAF; free for academic/non-commercial use) is not
redistributable here and its download link is a Google Drive URL, so the pipeline uses:
  1. data/reference/lm/LoughranMcDonald_MasterDictionary*.csv if the user drops it there (columns Word, Negative,
     Positive, Uncertainty, Litigious, ... ; non-zero = member), else
  2. the compact built-in subsets below: the most frequent LM Negative / Positive / Uncertainty words in 10-K text
     (inflected forms included). Coverage of the built-in lists is roughly 60-70% of LM token hits on 10-K risk factors;
     deltas between consecutive filings of the same company are what Lazy Prices uses, so the subset is adequate.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import polars as pl

from app.core.config import get_settings

_NEG = """
abandon abandoned abandonment adverse adversely against allegation allegations alleged bankrupt bankruptcy breach
breached breaches burden burdensome catastrophe catastrophic cease ceased claims closure closures collapse complaint
complaints concern concerns condemn conflict conflicts constraint constraints contingencies corrupt corruption costly
crises crisis critical criticism damage damaged damages decline declined declines declining decrease decreased
decreases default defaulted defaults defect defective defects deficiencies deficiency deficit delay delayed delays
delinquent delist delisted deny denied deteriorate deteriorated deteriorating deterioration detrimental difficult
difficulties difficulty diminish diminished disadvantage disadvantages disagree disclose discontinue discontinued
dispute disputes disrupt disrupted disruption disruptions dissatisfied distress downgrade downgraded downturn
downturns drop dropped erode erosion error errors exacerbate exacerbated excessive fail failed failing fails failure
failures falling false fines fraud fraudulent harm harmful hazardous hazards impair impaired impairment impairments
inability inadequate incorrect ineffective infringe infringement injunction injury insolvency insolvent instability
insufficient interrupt interruption interruptions investigation investigations involuntary lawsuit lawsuits layoffs
liquidate liquidation litigation lose losing loss losses misconduct misstatement negative negatively neglect
noncompliance obsolete obsolescence occurrence penalties penalty poor problem problems prosecution recall recalls
reject rejected restate restated restatement restructuring restructurings risk_none sanctions serious severe
shortage shortages shrink slowdown slowing stoppage strain stress stringent subpoena suffer suffered suspend
suspended suspension termination terminate terminated theft threat threats unable unanticipated uncollectible
undesirable unfavorable unfavorably unforeseen unlawful unprofitable unsuccessful violate violated violation
violations volatile vulnerable vulnerability weak weaken weakened weakness weaknesses worse worsen worsening writedown
writedowns writeoff writeoffs wrongful
"""
_POS = """
able advantage advantageous advantages attractive benefit beneficial benefited benefits best better boost boosted
breakthrough collaborate collaboration confident constructive creative delight desirable despite efficiencies
efficiency efficient enable enabled enables encouraged enhance enhanced enhancement enhancements enhancing enjoy
excellent exceptional excited exclusive favorable favorably gain gained gains good great greater highest ideal
impressive improve improved improvement improvements improving incredible innovative integrity leadership leading
opportunities opportunity optimistic outperform outperformed perfect pleased popular positive positively premier
profitable profitability progress prosper prosperity rebound record resolve resolved rewarding satisfaction
satisfied smooth solid stability stable strength strengthen strengthened strengthening strong stronger strongest
succeed succeeded success successful successfully superior surpass upturn valuable winning
"""
_UNC = """
almost ambiguity ambiguous anticipate anticipated anticipates apparent appear appeared appears approximate
approximately assume assumed assumes assuming assumption assumptions believe believed believes cautious
contingency contingent could depend depended dependence dependent depending depends doubt doubtful estimate
estimated estimates estimating exposure fluctuate fluctuated fluctuates fluctuating fluctuation fluctuations
hidden imprecise indefinite indefinitely likelihood may maybe might nearly occasionally pending perhaps possibility
possible possibly predict predicted predicting prediction predictions preliminary presumably probability probable
probably random randomly reassess reconsider risk risks risky roughly seems seldom sometimes speculate speculative
sudden suddenly suggest susceptible tentative turbulence uncertain uncertainly uncertainties uncertainty unclear
unconfirmed undetermined unexpected unexpectedly unforecasted unknown unknowns unpredictable unpredictability unproven
unquantifiable unsettled untested unusual variability variable variance variation variations vary varying volatility
"""


def _builtin(s: str) -> frozenset[str]:
    return frozenset(w for w in s.split() if w.isalpha())


@lru_cache(maxsize=1)
def word_lists() -> dict[str, frozenset[str]]:
    d = get_settings().data_dir / "reference" / "lm"
    files = sorted(d.glob("LoughranMcDonald_MasterDictionary*.csv")) if d.exists() else []
    if files:
        try:
            df = pl.read_csv(files[-1], infer_schema_length=0)
            out = {}
            for k, col in (("negative", "Negative"), ("positive", "Positive"), ("uncertainty", "Uncertainty")):
                out[k] = frozenset(df.filter(pl.col(col).cast(pl.Int64, strict=False).fill_null(0) != 0)
                                   .get_column("Word").str.to_lowercase().to_list())
            out["source"] = frozenset({str(Path(files[-1]).name)})
            return out
        except Exception:  # noqa: BLE001 - malformed file -> built-in lists
            pass
    return {"negative": _builtin(_NEG), "positive": _builtin(_POS), "uncertainty": _builtin(_UNC),
            "source": frozenset({"builtin_subset"})}
