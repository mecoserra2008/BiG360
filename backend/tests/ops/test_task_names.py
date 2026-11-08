"""Filler task ids must map to valid Windows file names (2026-10-20: 'gap_prices.CN_*' crashed every tick)."""

from app.ops.tasks import safe_name


def test_safe_name_strips_windows_reserved_chars():
    for raw in ("gap_prices.CN_*", r"gap:a|b/c\d?e", 'x<y>"z"', "trailing. "):
        out = safe_name(raw)
        assert not any(c in out for c in '<>:"/\\|?*'), out
        assert out and not out.endswith((".", " "))
    assert safe_name("gap_prices.CN_*") == "gap_prices.CN__"
    assert safe_name("campaign:gdelt_e6") == "campaign_gdelt_e6"
