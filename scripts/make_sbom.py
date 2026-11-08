"""Generate a CycloneDX 1.5 JSON SBOM for BiG Equity 360 (O4, docs/it/SECURITY.md).

Sources:
  * the Python environment that runs this script (importlib.metadata: every installed distribution, its licence
    metadata, and Requires-Dist edges resolved against what is installed);
  * frontend/package-lock.json (npm lockfile v2/v3: every package, its integrity hash, dev flag and dependency edges).

No third-party tool needed (cyclonedx-py / cyclonedx-npm / syft are not installed on this PC). When the bank's
pipeline has them, prefer:  cyclonedx-py environment <venv>\\Scripts\\python.exe -o sbom-python.json  and
cyclonedx-npm --package-lock-only -o sbom-npm.json, then merge with cyclonedx-cli.

    .venv\\Scripts\\python.exe scripts\\make_sbom.py                    # -> docs/it/sbom.json
    .venv\\Scripts\\python.exe scripts\\make_sbom.py --out x.json --no-npm
"""

from __future__ import annotations

import argparse
import base64
import importlib.metadata as md
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _licence(dist: md.Distribution) -> list[dict]:
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return [{"expression": expr}]
    out = []
    for c in meta.get_all("Classifier") or []:
        if c.startswith("License :: OSI Approved :: "):
            out.append({"license": {"name": c.removeprefix("License :: OSI Approved :: ")}})
        elif c.startswith("License :: ") and "OSI Approved" not in c:
            out.append({"license": {"name": c.removeprefix("License :: ")}})
    if not out:
        lic = (meta.get("License") or "").strip()
        if lic and len(lic) <= 120 and "\n" not in lic:
            out.append({"license": {"name": lic}})
    return out


def python_components() -> tuple[list[dict], list[dict]]:
    dists = {}
    for d in md.distributions():
        name = d.metadata.get("Name")
        if name:
            dists.setdefault(_norm(name), d)
    comps, deps = [], []
    for key, d in sorted(dists.items()):
        name, ver = d.metadata["Name"], d.version
        ref = f"pkg:pypi/{_norm(name)}@{quote(ver, safe='.-_~!')}"
        c = {"type": "library", "bom-ref": ref, "name": name, "version": ver, "purl": ref}
        lic = _licence(d)
        if lic:
            c["licenses"] = lic
        summary = (d.metadata.get("Summary") or "").strip()
        if summary:
            c["description"] = summary[:300]
        home = d.metadata.get("Home-page") or next((u.split(",", 1)[-1].strip() for u in d.metadata.get_all("Project-URL") or []
                                                    if u.lower().startswith(("homepage", "source", "repository"))), None)
        if home:
            c["externalReferences"] = [{"type": "website", "url": home}]
        comps.append(c)
        edges = set()
        for req in d.requires or []:
            if ";" in req and "extra ==" in req.split(";", 1)[1]:
                continue  # optional extra
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)", req)
            if m and _norm(m.group(1)) in dists:
                t = dists[_norm(m.group(1))]
                edges.add(f"pkg:pypi/{_norm(t.metadata['Name'])}@{quote(t.version, safe='.-_~!')}")
        deps.append({"ref": ref, "dependsOn": sorted(edges)})
    return comps, deps


def _npm_purl(name: str, ver: str) -> str:
    if name.startswith("@"):
        scope, pkg = name[1:].split("/", 1)
        return f"pkg:npm/{quote('@' + scope, safe='')}/{pkg}@{quote(ver, safe='.-_~!')}"
    return f"pkg:npm/{name}@{quote(ver, safe='.-_~!')}"


def npm_components(lock_path: Path) -> tuple[list[dict], list[dict], list[str]]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    pkgs = lock.get("packages") or {}
    by_path: dict[str, tuple[str, str]] = {}
    for path, p in pkgs.items():
        if not path or p.get("link"):
            continue
        name = p.get("name") or path.split("node_modules/")[-1]
        if p.get("version"):
            by_path[path] = (name, p["version"])
    comps, deps, seen = [], [], set()
    for path, (name, ver) in sorted(by_path.items(), key=lambda x: (x[1][0], x[1][1])):
        ref = _npm_purl(name, ver)
        p = pkgs[path]
        if ref not in seen:
            seen.add(ref)
            c = {"type": "library", "bom-ref": ref, "name": name, "version": ver, "purl": ref}
            if p.get("license"):
                lic = p["license"]
                c["licenses"] = [{"expression": lic}] if any(x in lic for x in (" OR ", " AND ", "(")) else [{"license": {"id": lic}}]
            integ = p.get("integrity") or ""
            if "-" in integ:
                alg, b64 = integ.split("-", 1)
                alg_map = {"sha512": "SHA-512", "sha384": "SHA-384", "sha256": "SHA-256", "sha1": "SHA-1"}
                if alg in alg_map:
                    try:
                        c["hashes"] = [{"alg": alg_map[alg], "content": base64.b64decode(b64).hex()}]
                    except ValueError:
                        pass
            if p.get("resolved"):
                c["externalReferences"] = [{"type": "distribution", "url": p["resolved"]}]
            props = []
            if p.get("dev"):
                props.append({"name": "cdx:npm:package:development", "value": "true"})
                c["scope"] = "excluded"  # build-time only: not shipped in the built UI bundle
            if props:
                c["properties"] = props
            comps.append(c)
        # dependency edges: resolve each dependency the npm way (nearest node_modules up the tree)
        edges = set()
        for dn in {**(p.get("dependencies") or {}), **(p.get("optionalDependencies") or {})}:
            base = path
            while True:
                cand = (base + "/node_modules/" + dn) if base else ("node_modules/" + dn)
                if cand in by_path:
                    edges.add(_npm_purl(*by_path[cand]))
                    break
                if not base:
                    break
                base = base.rsplit("/node_modules/", 1)[0] if "/node_modules/" in base else ""
        deps.append({"ref": ref, "dependsOn": sorted(edges)})
    root = pkgs.get("") or {}
    direct = [_npm_purl(*by_path["node_modules/" + n]) for n in sorted({**(root.get("dependencies") or {}),
                                                                         **(root.get("devDependencies") or {})})
              if "node_modules/" + n in by_path]
    # merge duplicate refs (same name@version installed at several paths)
    merged: dict[str, set[str]] = {}
    for d in deps:
        merged.setdefault(d["ref"], set()).update(d["dependsOn"])
    return comps, [{"ref": k, "dependsOn": sorted(v)} for k, v in sorted(merged.items())], direct


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "docs" / "it" / "sbom.json"))
    ap.add_argument("--no-npm", action="store_true")
    ap.add_argument("--lock", default=str(ROOT / "frontend" / "package-lock.json"))
    a = ap.parse_args(argv)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"
    py_comps, py_deps = python_components()
    comps, deps = list(py_comps), list(py_deps)
    app_ref = f"big360@{version}"
    direct = []
    req = ROOT / "backend" / "requirements.txt"
    names = {_norm(m.group(1)) for line in req.read_text(encoding="utf-8").splitlines()
             if (m := re.match(r"\s*([A-Za-z0-9_.\-]+)", line)) and not line.strip().startswith(("#", "-"))} if req.exists() else set()
    direct += [c["bom-ref"] for c in py_comps if _norm(c["name"]) in names]
    n_npm = 0
    if not a.no_npm and Path(a.lock).exists():
        npm_c, npm_d, npm_direct = npm_components(Path(a.lock))
        comps += npm_c
        deps += npm_d
        direct += npm_direct
        n_npm = len(npm_c)
    deps.insert(0, {"ref": app_ref, "dependsOn": sorted(set(direct))})
    bom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "tools": {"components": [{"type": "application", "name": "big360-make-sbom", "version": "1.0",
                                      "description": "scripts/make_sbom.py (importlib.metadata + package-lock.json)"}]},
            "component": {"type": "application", "bom-ref": app_ref, "name": "BiG Equity 360", "version": version,
                          "description": "Local equity analytics dashboard (FastAPI + DuckDB/Parquet + React)"},
            "properties": [{"name": "python.version", "value": sys.version.split()[0]},
                           {"name": "python.executable", "value": Path(sys.executable).name},
                           {"name": "components.python", "value": str(len(py_comps))},
                           {"name": "components.npm", "value": str(n_npm)}],
        },
        "components": comps,
        "dependencies": deps,
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bom, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out} - {len(py_comps)} Python + {n_npm} npm components, {out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
