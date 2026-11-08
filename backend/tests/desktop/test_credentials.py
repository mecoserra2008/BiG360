"""Credential storage: real Windows Credential Manager under a throw-away test target prefix (never the BiG360/ keys),
plus the pure logic (migration, env loading, CLI output never containing values) on an in-memory store."""

from __future__ import annotations

import sys
import uuid

import pytest

from app.desktop import credentials as C

FAKE = "pytest-not-a-real-key-7f3a"


class MemStore:
    backend = "memory"

    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, t):
        return self.d.get(t)

    def set(self, t, v, username="", comment=""):
        self.d[t] = v

    def delete(self, t):
        return self.d.pop(t, None) is not None

    def targets(self, prefix):
        return sorted(k for k in self.d if k.startswith(prefix))


@pytest.fixture()
def prefix():
    return f"BiG360-pytest-{uuid.uuid4().hex[:8]}/"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_wincred_roundtrip(prefix):
    st = C.WinCredStore()
    try:
        assert C.get_secret("fred_apikey", prefix, st) is None
        C.set_secret("FRED_APIKEY", FAKE, prefix, st)                  # names are case-insensitive
        assert C.get_secret("fred_apikey", prefix, st) == FAKE
        C.set_secret("polygon_apikey", "unicode-ä-€-" + FAKE, prefix, st)
        assert C.get_secret("polygon_apikey", prefix, st) == "unicode-ä-€-" + FAKE
        assert C.stored_fields(prefix, st) == ["fred_apikey", "polygon_apikey"]
        assert C.delete_secret("fred_apikey", prefix, st) is True
        assert C.delete_secret("fred_apikey", prefix, st) is False
        assert C.stored_fields(prefix, st) == ["polygon_apikey"]
    finally:
        for t in st.targets(prefix):
            st.delete(t)
    assert st.targets(prefix) == []
    assert not any(t.startswith(prefix) for t in st.targets("BiG360"))


def test_unknown_and_empty_rejected():
    st = MemStore()
    with pytest.raises(C.CredentialError):
        C.set_secret("not_a_secret", "x", "P/", st)
    with pytest.raises(C.CredentialError):
        C.set_secret("fred_apikey", "   ", "P/", st)


def test_load_into_env_precedence():
    st = MemStore()
    C.set_secret("fred_apikey", FAKE + "1", "P/", st)
    C.set_secret("eia_apikey", FAKE + "2", "P/", st)
    env = {"Fred_ApiKey": "explicit"}                                  # explicit variable (any case) wins
    loaded = C.load_into_env(env, "P/", st)
    assert loaded == ["eia_apikey"] and env["EIA_APIKEY"] == FAKE + "2" and env["Fred_ApiKey"] == "explicit"
    env2 = {"FRED_APIKEY": "explicit"}
    assert C.load_into_env(env2, "P/", st, override=True) == ["eia_apikey", "fred_apikey"]

    class Broken(MemStore):
        def targets(self, prefix):
            raise C.CredentialError("boom")

    assert C.load_into_env({}, "P/", Broken()) == []                  # never raises


def test_migrate_and_verify(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text(f"Polygon_APIKEY={FAKE}a\nFRED_APIKEY={FAKE}b\nEMPTY_ONE=\nopendart_apikey=\n"
                    f"BIG360_DATA_DIR=C:/x\nNOT_A_SECRET=zzz\n", encoding="utf-8")
    st = MemStore()
    C.set_secret("fred_apikey", "older", "P/", st)
    rep = C.migrate_from_dotenv(envf, "P/", st)
    assert rep == {"migrated": ["polygon_apikey"], "unchanged": [], "already_stored": ["fred_apikey"], "updated": []}
    assert C.get_secret("fred_apikey", "P/", st) == "older"
    assert C.verify_against_dotenv(envf, "P/", st) == {"fred_apikey": "differs", "polygon_apikey": "match"}
    rep = C.migrate_from_dotenv(envf, "P/", st, overwrite=True)
    assert rep["updated"] == ["fred_apikey"] and rep["unchanged"] == ["polygon_apikey"]
    assert C.verify_against_dotenv(envf, "P/", st) == {"fred_apikey": "match", "polygon_apikey": "match"}
    assert envf.read_text(encoding="utf-8").startswith("Polygon_APIKEY=")   # .env untouched


def test_cli_never_prints_values(tmp_path, capsys, monkeypatch):
    envf = tmp_path / ".env"
    envf.write_text(f"EIA_APIKEY={FAKE}\n", encoding="utf-8")
    st = MemStore()
    monkeypatch.setattr(C, "store", lambda: st)
    monkeypatch.setattr(C, "_env_file", lambda arg: envf)              # never read the real project .env
    assert C.main(["--prefix", "P/", "migrate", "--env-file", str(envf)]) == 0
    assert C.main(["--prefix", "P/", "verify", "--env-file", str(envf)]) == 0
    assert C.main(["--prefix", "P/", "status"]) == 0
    out = capsys.readouterr()
    assert "migrated (1): eia_apikey" in out.out and "eia_apikey" in out.out
    assert FAKE not in out.out and FAKE not in out.err
    assert st.get("P/eia_apikey") == FAKE
