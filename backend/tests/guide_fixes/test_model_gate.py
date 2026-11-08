"""Guide 10: Model Lab showed FT-Transformer / ensemble rank ICs without saying the Transformer failed the
label-permutation leakage test. Runs now carry the gate status."""

from app.api.routers.models import leakage_gate


def test_gate_status_per_model_type():
    assert leakage_gate({"model_type": "xgboost_rank"})[0] == "passed"
    st, note = leakage_gate({"model_type": "ft_transformer", "notes": "9 walk-forward folds"})
    assert st == "failed" and "DEMOTED" in note and "permutation" in note
    st, note = leakage_gate({"model_type": "ensemble", "notes": None})
    assert st == "failed" and "FT-Transformer" in note
    assert leakage_gate({"model_type": "ft_transformer", "notes": "... [LEAKAGE_CHECK] passed ..."})[0] == "passed"
    assert leakage_gate({"model_type": "insider_xgb"}) == (None, None)
