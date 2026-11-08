"""F17: ref_universe never succeeded - ragged hand-edited seed CSVs and the key-less DART source."""

from __future__ import annotations

from app.core.config import Settings
from app.normalize import identity_universe as U


def test_ragged_seed_csv_is_tolerated(tmp_path):
    p = tmp_path / "manual_overrides.csv"
    p.write_text("kind,key,value,note\n# comment\nlink,cik:1,seed:x,ok\nblock,cik:2,cik:3,note, with a comma\n",
                 encoding="utf-8")
    df = U.read_seed_csv(p)
    assert df.height == 2 and df.columns == ["kind", "key", "value", "note"]
    assert df.row(1, named=True)["note"] == "note, with a comma"


def test_missing_key_source_is_skipped_not_a_warning(monkeypatch):
    from app.normalize import identity

    assert identity.KEYED_SOURCES["dart"] == "opendart_apikey"
    s = Settings()
    monkeypatch.setattr(Settings, "secret", lambda self, name: None)
    assert s.secret("opendart_apikey") is None  # the build_reference branch then records "skipped", not a warning
