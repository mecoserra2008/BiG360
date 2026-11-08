"""Level-type taxonomy for the technicals package (owner: technicals).

A *level key* is ``"{level_type}@{timeframe}"`` (tech_levels.level_key). It is the unit that the level-type backtest
(level_type_stats) ranks per ticker and that confluence weights by reliability.

Families (coarse groups used by the UI and for zone diversity):
    period_ohlc       prior period open/high/low/close/mid and the current period open ("open-candle" levels)
    opening_range     high/low of the first bars of the current period
    floor_pivot       classic floor pivots P, R1-R3, S1-S3 of the prior period
    camarilla         Camarilla H3-H5 / L3-L5 of the prior period
    fib_pivot         Fibonacci pivots R1-R3 / S1-S3 of the prior period (P == floor_p)
    swing_pivot       fractal N-bar swing highs/lows (k = 2, 3, 5, 10 daily; k = 2 weekly)
    market_structure  ZigZag(3 ATR) pivots labelled HH/HL/LH/LL + Break-of-Structure / Change-of-Character levels
    fibonacci         retracements / extensions of the last ZigZag leg
    anchored_vwap     AVWAPs anchored at YTD/QTD start, last swing high/low, last filing, 52w high/low
    volume_profile    volume-at-price POC / value-area edges / high-volume nodes over 6M and 1Y windows
    extreme           52-week high/low, all-time high
    round_number      psychological round numbers (major and minor grid)
    moving_average    SMA 50 / SMA 200
    gap               near edge of an unfilled price gap
"""

from __future__ import annotations

from dataclasses import dataclass

TIMEFRAMES = ("D", "W", "M", "Q", "Y", "NA")

# role: +1 = naturally support (formed below price, e.g. swing low), -1 = naturally resistance, 0 = either
_PERIOD_TYPES_ALL_TF = {  # type -> (family, role)
    "prev_open": ("period_ohlc", 0),
    "prev_high": ("period_ohlc", -1),
    "prev_low": ("period_ohlc", 1),
    "prev_close": ("period_ohlc", 0),
    "prev_mid": ("period_ohlc", 0),
}
_PERIOD_TYPES_HTF = {  # only W/M/Q/Y
    "cur_open": ("period_ohlc", 0),
    "or_high": ("opening_range", -1),
    "or_low": ("opening_range", 1),
    "floor_p": ("floor_pivot", 0),
    "floor_r1": ("floor_pivot", -1),
    "floor_r2": ("floor_pivot", -1),
    "floor_r3": ("floor_pivot", -1),
    "floor_s1": ("floor_pivot", 1),
    "floor_s2": ("floor_pivot", 1),
    "floor_s3": ("floor_pivot", 1),
    "cam_h3": ("camarilla", -1),
    "cam_h4": ("camarilla", -1),
    "cam_h5": ("camarilla", -1),
    "cam_l3": ("camarilla", 1),
    "cam_l4": ("camarilla", 1),
    "cam_l5": ("camarilla", 1),
    "fibp_r1": ("fib_pivot", -1),
    "fibp_r2": ("fib_pivot", -1),
    "fibp_r3": ("fib_pivot", -1),
    "fibp_s1": ("fib_pivot", 1),
    "fibp_s2": ("fib_pivot", 1),
    "fibp_s3": ("fib_pivot", 1),
}
SWING_ORDERS_D = (2, 3, 5, 10)
SWING_ORDERS_W = (2,)
_NA_TYPES = {
    "ms_hh": ("market_structure", -1),
    "ms_lh": ("market_structure", -1),
    "ms_hl": ("market_structure", 1),
    "ms_ll": ("market_structure", 1),
    "ms_bos": ("market_structure", 0),
    "ms_choch": ("market_structure", 0),
    "fib_236": ("fibonacci", 0),
    "fib_382": ("fibonacci", 0),
    "fib_500": ("fibonacci", 0),
    "fib_618": ("fibonacci", 0),
    "fib_786": ("fibonacci", 0),
    "fib_ext_1272": ("fibonacci", 0),
    "fib_ext_1618": ("fibonacci", 0),
    "avwap_ytd": ("anchored_vwap", 0),
    "avwap_qtd": ("anchored_vwap", 0),
    "avwap_swing_high": ("anchored_vwap", 0),
    "avwap_swing_low": ("anchored_vwap", 0),
    "avwap_earnings": ("anchored_vwap", 0),
    "avwap_52w_high": ("anchored_vwap", 0),
    "avwap_52w_low": ("anchored_vwap", 0),
    "vp_poc_1y": ("volume_profile", 0),
    "vp_poc_6m": ("volume_profile", 0),
    "vp_hvn_1y": ("volume_profile", 0),
    "vp_hvn_6m": ("volume_profile", 0),
    "vp_vah_1y": ("volume_profile", 0),
    "vp_val_1y": ("volume_profile", 0),
    "high_52w": ("extreme", -1),
    "low_52w": ("extreme", 1),
    "all_time_high": ("extreme", -1),
    "round_major": ("round_number", 0),
    "round_minor": ("round_number", 0),
    "sma_50": ("moving_average", 0),
    "sma_200": ("moving_average", 0),
    "gap_up": ("gap", 1),
    "gap_down": ("gap", -1),
}

#: legacy enum values kept in the contract (v1) but not produced any more
LEGACY_TYPES = ("swing_high", "swing_low", "round_number")


@dataclass(frozen=True)
class LevelKey:
    key: str
    level_type: str
    timeframe: str
    family: str
    role: int


def _build() -> list[LevelKey]:
    out: list[LevelKey] = []
    for tf in ("D", "W", "M", "Q", "Y"):
        for t, (fam, role) in _PERIOD_TYPES_ALL_TF.items():
            out.append(LevelKey(f"{t}@{tf}", t, tf, fam, role))
        if tf != "D":
            for t, (fam, role) in _PERIOD_TYPES_HTF.items():
                out.append(LevelKey(f"{t}@{tf}", t, tf, fam, role))
    for k in SWING_ORDERS_D:
        out.append(LevelKey(f"swing_high_k{k}@D", f"swing_high_k{k}", "D", "swing_pivot", -1))
        out.append(LevelKey(f"swing_low_k{k}@D", f"swing_low_k{k}", "D", "swing_pivot", 1))
    for k in SWING_ORDERS_W:
        out.append(LevelKey(f"swing_high_k{k}@W", f"swing_high_k{k}", "W", "swing_pivot", -1))
        out.append(LevelKey(f"swing_low_k{k}@W", f"swing_low_k{k}", "W", "swing_pivot", 1))
    for t, (fam, role) in _NA_TYPES.items():
        out.append(LevelKey(f"{t}@NA", t, "NA", fam, role))
    return out


LEVEL_KEYS: tuple[LevelKey, ...] = tuple(_build())
KEY_ID: dict[str, int] = {k.key: i for i, k in enumerate(LEVEL_KEYS)}
KEY_BY_ID: tuple[str, ...] = tuple(k.key for k in LEVEL_KEYS)
N_KEYS = len(LEVEL_KEYS)
LEVEL_TYPES: tuple[str, ...] = tuple(dict.fromkeys(k.level_type for k in LEVEL_KEYS))
FAMILY_OF_TYPE: dict[str, str] = {k.level_type: k.family for k in LEVEL_KEYS}
FAMILIES: tuple[str, ...] = tuple(dict.fromkeys(k.family for k in LEVEL_KEYS))

#: frontend LevelType (frontend/src/api/types.ts) for each family — used by the API to map LevelTypeStat.type
FRONTEND_TYPE_OF_FAMILY: dict[str, str] = {
    "period_ohlc": "prior_high_low",
    "opening_range": "prior_high_low",
    "floor_pivot": "prior_high_low",
    "camarilla": "prior_high_low",
    "fib_pivot": "fibonacci",
    "swing_pivot": "swing_pivot",
    "market_structure": "swing_pivot",
    "fibonacci": "fibonacci",
    "anchored_vwap": "anchored_vwap",
    "volume_profile": "volume_profile",
    "extreme": "prior_high_low",
    "round_number": "round_number",
    "moving_average": "ma_200",
    "gap": "gap",
}

#: timeframe weight used by confluence (higher timeframe heavier)
TF_WEIGHT: dict[str, float] = {"D": 0.55, "W": 0.8, "M": 1.0, "Q": 1.15, "Y": 1.3, "NA": 1.0}

STRUCTURE_LABELS = ("HH", "HL", "LH", "LL")


def key_id(level_type: str, timeframe: str) -> int:
    return KEY_ID[f"{level_type}@{timeframe}"]


def split_key(key: str) -> tuple[str, str]:
    t, tf = key.rsplit("@", 1)
    return t, tf


def family_of_key(key: str) -> str:
    return LEVEL_KEYS[KEY_ID[key]].family


def contract_level_types() -> list[str]:
    """Enum values for tech_levels.level_type: v1 values (kept) + every type produced now."""
    v1 = ["swing_high", "swing_low", "prev_open", "prev_high", "prev_low", "prev_close", "cur_open", "floor_p",
          "floor_r1", "floor_r2", "floor_r3", "floor_s1", "floor_s2", "floor_s3", "cam_h3", "cam_h4", "cam_h5",
          "cam_l3", "cam_l4", "cam_l5", "fib_236", "fib_382", "fib_500", "fib_618", "fib_786", "fib_ext_1272",
          "fib_ext_1618", "avwap_ytd", "avwap_qtd", "avwap_swing_high", "avwap_swing_low", "avwap_earnings",
          "avwap_52w_high", "avwap_52w_low", "high_52w", "low_52w", "all_time_high", "round_number", "vp_poc_1y"]
    return v1 + [t for t in LEVEL_TYPES if t not in v1]
