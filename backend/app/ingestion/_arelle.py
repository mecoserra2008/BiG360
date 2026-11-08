"""Arelle (arelle-release) wrapper: ESEF / iXBRL report packages and plain XBRL instances -> OIM xBRL-JSON.

The output is the same xBRL-JSON document that `app.ingestion._xbrl_json.parse_xbrl_json` already reads (the ESEF
adapter gets it pre-built from filings.xbrl.org). Arelle runs in a **child process** with a hard timeout:
  python -m arelle.CntlrCmdLine --plugins saveLoadableOIM -f <package> --saveLoadableOIM <out.json> --logFile <log>
so a pathological filing (huge iXBRL, taxonomy download hang) can be killed without taking the job down.

Research (docs/COMPANY_COVERAGE_OPTIONS.md §3), verified 2026-10-11 with arelle-release 2.46.1 on Python 3.13:
  * load the **zip / .xbri report package**, not the inner xhtml (otherwise concepts resolve to None);
  * the first run downloads and caches the IFRS/ESEF taxonomies (~26 MB, ~40 s), later runs take 5-10 s;
  * OIM period ends are exclusive instants (handled by _xbrl_json.parse_period); facts repeat across statements
    (deduped by parse_xbrl_json).
Packages that are not proper report packages (a zip holding a bare .xhtml/.xbrl, or a nested package zip) are
unwrapped by `pick_entry()`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from app.core.logsetup import get_logger

log = get_logger("app.ingestion.arelle")

DEFAULT_TIMEOUT_S = 420.0
_INSTANCE_EXT = (".xhtml", ".html", ".htm", ".xbrl", ".xml")


class ArelleError(RuntimeError):
    """Arelle failed, timed out or produced no numeric facts. `.log_tail` holds the last Arelle log lines."""

    def __init__(self, msg: str, log_tail: str = ""):
        super().__init__(msg)
        self.log_tail = log_tail


def arelle_available() -> bool:
    try:
        import arelle  # noqa: F401
    except ImportError:
        return False
    return True


def pick_entry(path: Path, workdir: Path) -> Path:
    """Return the file Arelle should load for `path`.

    * .xbri / zip containing META-INF/reportPackage.json or a reports/ folder -> the package itself;
    * zip containing exactly one nested .zip/.xbri -> that inner package (extracted);
    * zip containing a bare instance (.xhtml/.xbrl) -> extracted folder's instance (largest file);
    * anything else -> unchanged."""
    if not zipfile.is_zipfile(path):
        return path
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        low = [n.lower() for n in names]
        if any("meta-inf/reportpackage.json" in n or "/reports/" in n or n.startswith("reports/") for n in low) or \
                any(n.endswith("meta-inf/taxonomypackage.xml") for n in low):
            return path
        inner = [n for n in names if n.lower().endswith((".zip", ".xbri"))]
        if len(inner) == 1:
            out = workdir / Path(inner[0]).name
            out.write_bytes(z.read(inner[0]))
            return pick_entry(out, workdir)
        inst = [n for n in names if n.lower().endswith(_INSTANCE_EXT) and "meta-inf" not in n.lower()]
        if inst:
            ext_dir = workdir / "x"
            z.extractall(ext_dir)
            # prefer the inline report / instance, largest first (skips small linkbase .xml files)
            inst.sort(key=lambda n: (not n.lower().endswith((".xhtml", ".html", ".htm", ".xbrl")),
                                     -z.getinfo(n).file_size))
            return ext_dir / inst[0]
    return path


def to_xbrl_json(src: Path | bytes, *, suffix: str = ".zip", timeout_s: float = DEFAULT_TIMEOUT_S,
                 keep_dir: Path | None = None) -> dict[str, Any]:
    """Convert an ESEF/iXBRL package (or XBRL instance) to an OIM xBRL-JSON dict.

    `src` is a path or the raw bytes (then `suffix` names the file type). Raises ArelleError on failure, timeout or an
    output without facts. With `keep_dir`, the Arelle log is copied there (debugging)."""
    if not arelle_available():
        raise ArelleError("arelle-release is not installed (pip install arelle-release)")
    tmp = Path(tempfile.mkdtemp(prefix="arelle_"))
    try:
        if isinstance(src, (bytes, bytearray)):
            p = tmp / f"input{suffix}"
            p.write_bytes(src)
        else:
            p = Path(src)
        entry = pick_entry(p, tmp)
        out, logf = tmp / "out.json", tmp / "arelle.log"
        cmd = [sys.executable, "-m", "arelle.CntlrCmdLine", "--plugins", "saveLoadableOIM", "-f", str(entry),
               "--saveLoadableOIM", str(out), "--logFile", str(logf), "--logLevel", "warning"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            cp = subprocess.run(cmd, capture_output=True, timeout=timeout_s, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as e:
            raise ArelleError(f"arelle timed out after {timeout_s:.0f}s", _tail(logf)) from e
        if keep_dir is not None and logf.exists():
            keep_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(logf, keep_dir / "arelle.log")
        if not out.exists():
            raise ArelleError(f"arelle produced no output (rc={cp.returncode})",
                              _tail(logf) or cp.stderr.decode("utf-8", "replace")[-1500:])
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ArelleError(f"arelle output is not JSON: {e}", _tail(logf)) from e
        facts = doc.get("facts") or {}
        if not any((f.get("dimensions") or {}).get("unit") for f in facts.values()):
            raise ArelleError(f"no numeric facts in arelle output ({len(facts)} facts)", _tail(logf))
        return doc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tail(p: Path, n: int = 1500) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def report_period_end(doc: dict[str, Any]) -> Any:
    """Most frequent end date of ifrs-full *duration* facts (the reporting period end), or None."""
    from collections import Counter

    from app.ingestion._xbrl_json import parse_period

    c: Counter = Counter()
    for f in (doc.get("facts") or {}).values():
        d = f.get("dimensions") or {}
        if not str(d.get("concept", "")).startswith("ifrs-full:") or "/" not in str(d.get("period", "")):
            continue
        try:
            start, end = parse_period(d["period"])
        except ValueError:
            continue
        if start is not None and (end - start).days > 80:
            c[end] += 1
    if not c:
        return None
    top = max(c.values())
    return max(e for e, n in c.items() if n >= top * 0.5)  # latest end with a comparable fact count
