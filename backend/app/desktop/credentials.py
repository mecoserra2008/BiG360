"""API keys in Windows Credential Manager (DPAPI-protected, per user) with `.env` as the development fallback.

Storage: one generic credential per secret, target name ``BiG360/<field>`` (e.g. ``BiG360/polygon_apikey``), visible
under Control Panel > Credential Manager > Windows Credentials. Pure ctypes (advapi32 CredReadW / CredWriteW /
CredDeleteW / CredEnumerateW); `keyring` is used instead only off Windows (or with BIG360_CRED_BACKEND=keyring).

How keys reach the app: the supervisor (app.ops.supervisor) reads the stored keys once and passes them to its
children (API, worker, filler) as environment variables. pydantic-settings gives environment variables precedence
over `.env`, so  explicit env var > Credential Manager > .env.  `app.core.config` is unchanged.

CLI (never prints a secret value):
    python -m app.desktop.credentials status                 # which keys are stored / in .env (booleans only)
    python -m app.desktop.credentials migrate [--overwrite]  # copy keys from <root>/.env into Credential Manager
    python -m app.desktop.credentials verify                 # stored value == .env value? (match / differs)
    python -m app.desktop.credentials set polygon_apikey     # prompt (no echo)
    python -m app.desktop.credentials delete polygon_apikey
    python -m app.desktop.credentials exec -- python -m app.cli jobs run-dag nightly   # run with stored keys
The `.env` file is never modified; remove keys from it by hand once `verify` reports a match.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import os
import subprocess
import sys
from collections.abc import MutableMapping
from ctypes import wintypes
from pathlib import Path
from typing import Any

from app.core.config import SECRET_FIELDS

DEFAULT_PREFIX = "BiG360/"
IS_WINDOWS = sys.platform == "win32"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CredentialError(RuntimeError):
    pass


def default_prefix() -> str:
    return os.environ.get("BIG360_CRED_PREFIX") or DEFAULT_PREFIX


# ------------------------------------------------------------------------------------------------ win32 backend

if IS_WINDOWS:
    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", _FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _PCRED = ctypes.POINTER(_CREDENTIALW)

    def _advapi() -> Any:
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        a.CredWriteW.argtypes = [_PCRED, wintypes.DWORD]
        a.CredWriteW.restype = wintypes.BOOL
        a.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_PCRED)]
        a.CredReadW.restype = wintypes.BOOL
        a.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        a.CredDeleteW.restype = wintypes.BOOL
        a.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                     ctypes.POINTER(ctypes.POINTER(_PCRED))]
        a.CredEnumerateW.restype = wintypes.BOOL
        a.CredFree.argtypes = [ctypes.c_void_p]
        a.CredFree.restype = None
        return a


class WinCredStore:
    backend = "wincred"

    def __init__(self) -> None:
        if not IS_WINDOWS:  # pragma: no cover
            raise CredentialError("Windows Credential Manager is only available on Windows")
        self._a = _advapi()

    def get(self, target: str) -> str | None:
        p = _PCRED()
        if not self._a.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"CredReadW failed ({err})")
        try:
            c = p.contents
            raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize) if c.CredentialBlobSize else b""
            return raw.decode("utf-16-le")
        finally:
            self._a.CredFree(p)

    def set(self, target: str, value: str, username: str = "", comment: str = "BiG Equity 360") -> None:
        blob = value.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else None
        c = _CREDENTIALW()
        c.Type = CRED_TYPE_GENERIC
        c.TargetName = target
        c.Comment = comment
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)) if buf is not None else None
        c.Persist = CRED_PERSIST_LOCAL_MACHINE
        c.UserName = username or target
        if not self._a.CredWriteW(ctypes.byref(c), 0):
            raise CredentialError(f"CredWriteW failed ({ctypes.get_last_error()})")

    def delete(self, target: str) -> bool:
        if self._a.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            return True
        err = ctypes.get_last_error()
        if err == ERROR_NOT_FOUND:
            return False
        raise CredentialError(f"CredDeleteW failed ({err})")

    def targets(self, prefix: str) -> list[str]:
        n = wintypes.DWORD(0)
        arr = ctypes.POINTER(_PCRED)()
        if not self._a.CredEnumerateW(prefix + "*", 0, ctypes.byref(n), ctypes.byref(arr)):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:
                return []
            raise CredentialError(f"CredEnumerateW failed ({err})")
        try:
            return sorted(arr[i].contents.TargetName for i in range(n.value))
        finally:
            self._a.CredFree(arr)


class KeyringStore:  # pragma: no cover - only off Windows / on request
    backend = "keyring"
    SERVICE = "BiG Equity 360"

    def __init__(self) -> None:
        import keyring  # noqa: PLC0415

        self._k = keyring

    def get(self, target: str) -> str | None:
        return self._k.get_password(self.SERVICE, target)

    def set(self, target: str, value: str, username: str = "", comment: str = "") -> None:
        self._k.set_password(self.SERVICE, target, value)

    def delete(self, target: str) -> bool:
        try:
            self._k.delete_password(self.SERVICE, target)
            return True
        except Exception:
            return False

    def targets(self, prefix: str) -> list[str]:
        return [f"{prefix}{f}" for f in SECRET_FIELDS if self.get(f"{prefix}{f}") is not None]


def store() -> Any:
    if os.environ.get("BIG360_CRED_BACKEND", "").lower() == "keyring" or not IS_WINDOWS:
        return KeyringStore()
    return WinCredStore()


# ------------------------------------------------------------------------------------------------ secrets API


def _field(name: str) -> str:
    f = name.strip().lower()
    if f not in SECRET_FIELDS:
        raise CredentialError(f"unknown secret {name!r} (known: {', '.join(SECRET_FIELDS)})")
    return f


def get_secret(name: str, prefix: str | None = None, backend: Any = None) -> str | None:
    v = (backend or store()).get((prefix or default_prefix()) + _field(name))
    return v if v else None


def set_secret(name: str, value: str, prefix: str | None = None, backend: Any = None) -> None:
    f = _field(name)
    if not value or not value.strip():
        raise CredentialError("empty value")
    (backend or store()).set((prefix or default_prefix()) + f, value.strip(), username=f)


def delete_secret(name: str, prefix: str | None = None, backend: Any = None) -> bool:
    return (backend or store()).delete((prefix or default_prefix()) + _field(name))


def stored_fields(prefix: str | None = None, backend: Any = None) -> list[str]:
    pre = prefix or default_prefix()
    out = []
    for t in (backend or store()).targets(pre):
        f = t[len(pre):].lower() if t.startswith(pre) else ""
        if f in SECRET_FIELDS:
            out.append(f)
    return sorted(set(out))


def stored_secrets(prefix: str | None = None, backend: Any = None) -> dict[str, str]:
    """{field: value} of every stored key. Callers must never log the values."""
    b = backend or store()
    out = {}
    for f in stored_fields(prefix, b):
        v = get_secret(f, prefix, b)
        if v:
            out[f] = v
    return out


def load_into_env(env: MutableMapping[str, str], prefix: str | None = None, backend: Any = None,
                  override: bool = False) -> list[str]:
    """Put stored keys into `env` as upper-case variables (POLYGON_APIKEY=...). An explicit variable already present
    (any case) wins unless override=True. Returns the field names loaded (never values). Never raises."""
    try:
        secrets = stored_secrets(prefix, backend)
    except Exception:
        return []
    present = {k.lower() for k in env}
    loaded = []
    for f, v in secrets.items():
        if f in present and not override:
            continue
        env[f.upper()] = v
        loaded.append(f)
    return sorted(loaded)


def dotenv_secrets(env_file: Path) -> dict[str, str]:
    """{field: value} of the secrets in a .env file (keys matched case-insensitively, like app.core.config)."""
    if not env_file.is_file():
        return {}
    from dotenv import dotenv_values  # noqa: PLC0415

    out: dict[str, str] = {}
    for k, v in dotenv_values(env_file).items():
        f = (k or "").strip().lower()
        if f in SECRET_FIELDS and v and v.strip():
            out[f] = v.strip()
    return out


def migrate_from_dotenv(env_file: Path, prefix: str | None = None, backend: Any = None,
                        overwrite: bool = False) -> dict[str, list[str]]:
    """Copy the secrets of `env_file` into the credential store. Returns field-name lists only:
    {"migrated": [...], "unchanged": [...], "already_stored": [...], "updated": [...]} (unchanged = same value already
    stored; already_stored = a different value is stored and kept because overwrite is False). .env is not modified."""
    b = backend or store()
    src = dotenv_secrets(env_file)
    have = set(stored_fields(prefix, b))
    rep: dict[str, list[str]] = {"migrated": [], "unchanged": [], "already_stored": [], "updated": []}
    for f, v in sorted(src.items()):
        if f in have:
            if get_secret(f, prefix, b) == v:
                rep["unchanged"].append(f)
                continue
            if not overwrite:
                rep["already_stored"].append(f)
                continue
            set_secret(f, v, prefix, b)
            rep["updated"].append(f)
            continue
        set_secret(f, v, prefix, b)
        rep["migrated"].append(f)
    return rep


def verify_against_dotenv(env_file: Path, prefix: str | None = None, backend: Any = None) -> dict[str, str]:
    """{field: match | differs | only_env | only_store} - no values."""
    b = backend or store()
    src = dotenv_secrets(env_file)
    st = stored_secrets(prefix, b)
    out = {}
    for f in sorted(set(src) | set(st)):
        if f in src and f in st:
            out[f] = "match" if src[f] == st[f] else "differs"
        else:
            out[f] = "only_env" if f in src else "only_store"
    return out


# ------------------------------------------------------------------------------------------------ CLI


def _env_file(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    from app.core.config import PROJECT_ROOT  # noqa: PLC0415

    return PROJECT_ROOT / ".env"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.desktop.credentials", description="BiG360 API keys in Credential Manager")
    ap.add_argument("--prefix", default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    m = sub.add_parser("migrate")
    m.add_argument("--env-file")
    m.add_argument("--overwrite", action="store_true", help="replace values already stored")
    v = sub.add_parser("verify")
    v.add_argument("--env-file")
    s = sub.add_parser("set")
    s.add_argument("name")
    d = sub.add_parser("delete")
    d.add_argument("name")
    e = sub.add_parser("exec")
    e.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            st = set(stored_fields(a.prefix))
            env = set(dotenv_secrets(_env_file(None)))
            print(f"backend: {store().backend}  target prefix: {a.prefix or default_prefix()}")
            print(f"{'key':<24}{'credential manager':<22}{'.env':<6}")
            for f in SECRET_FIELDS:
                print(f"{f:<24}{'stored' if f in st else '-':<22}{'yes' if f in env else '-':<6}")
            return 0
        if a.cmd == "migrate":
            path = _env_file(a.env_file)
            if not path.is_file():
                print(f"no .env file at {path}")
                return 1
            rep = migrate_from_dotenv(path, a.prefix, overwrite=a.overwrite)
            print(f"migrated ({len(rep['migrated'])}): {', '.join(rep['migrated']) or '-'}")
            if rep["unchanged"]:
                print(f"already stored, same value ({len(rep['unchanged'])}): {', '.join(rep['unchanged'])}")
            if rep["updated"]:
                print(f"updated ({len(rep['updated'])}): {', '.join(rep['updated'])}")
            if rep["already_stored"]:
                print(f"kept, already stored ({len(rep['already_stored'])}): {', '.join(rep['already_stored'])} "
                      "(use --overwrite to replace)")
            print(f"{path} was not modified; keep it as the dev fallback or remove the keys after `verify`.")
            return 0
        if a.cmd == "verify":
            res = verify_against_dotenv(_env_file(a.env_file), a.prefix)
            for f, r in res.items():
                print(f"{f:<24}{r}")
            return 0 if all(r in ("match", "only_store") for r in res.values()) else 1
        if a.cmd == "set":
            f = _field(a.name)
            val = getpass.getpass(f"value for {f} (input hidden): ")
            set_secret(f, val, a.prefix)
            print(f"stored {f}")
            return 0
        if a.cmd == "delete":
            print(("deleted " if delete_secret(a.name, a.prefix) else "not stored: ") + _field(a.name))
            return 0
        if a.cmd == "exec":
            cmd = [c for c in a.command if c != "--"] if a.command[:1] == ["--"] else a.command
            if not cmd:
                print("usage: exec -- <command ...>")
                return 2
            env = dict(os.environ)
            loaded = load_into_env(env, a.prefix)
            print(f"[credentials] {len(loaded)} key(s) from Credential Manager", file=sys.stderr)
            return subprocess.call(cmd, env=env)
    except CredentialError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
