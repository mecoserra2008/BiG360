"""Down-convert contracts/api.yaml (OpenAPI 3.1) to OpenAPI 3.0.3 for code generators with partial 3.1 support
(NSwag / NJsonSchema, older Kiota, AutoRest). The source contract stays 3.1; this writes a derived file.

    .venv\\Scripts\\python.exe scripts\\openapi_31_to_30.py                       # -> integration/dotnet/api.v30.json
    .venv\\Scripts\\python.exe scripts\\openapi_31_to_30.py --in contracts/api.yaml --out x.json

Rewrites (semantics preserved for client generation):
  type: [T, "null"]                 -> type: T, nullable: true
  type: [T1, T2] (no null)          -> no type (any) + x-oneOf-types: [T1, T2]
  oneOf/anyOf [{$ref: X}, {type: null}] -> allOf: [{$ref: X}], nullable: true
  enum [..., null]                  -> enum without null, nullable: true
  const: v                          -> enum: [v]
  examples: [v, ...] (schemas)      -> example: v
  exclusiveMinimum / exclusiveMaximum: number -> minimum / maximum + exclusive*: true
  contentMediaType / contentEncoding / $schema / unevaluatedProperties -> dropped
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DROP = {"contentMediaType", "contentEncoding", "$schema", "unevaluatedProperties", "$id", "$comment"}


def _is_null_schema(s: Any) -> bool:
    return isinstance(s, dict) and (s.get("type") == "null" or s.get("type") == ["null"]) and len(s) == 1


def convert(node: Any, in_schema: bool = False) -> Any:
    if isinstance(node, list):
        return [convert(x, in_schema) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in DROP:
            continue
        child_schema = in_schema or k in ("schema", "schemas", "items", "properties", "additionalProperties", "allOf",
                                          "oneOf", "anyOf", "not")
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: convert(pv, True) for pk, pv in v.items()}
        elif k == "schemas" and isinstance(v, dict):
            out[k] = {sk: convert(sv, True) for sk, sv in v.items()}
        else:
            out[k] = convert(v, child_schema)
    t = out.get("type")
    if isinstance(t, list):
        types = [x for x in t if x != "null"]
        if len(types) < len(t):
            out["nullable"] = True
        if len(types) == 1:
            out["type"] = types[0]
        else:
            out.pop("type")
            if types:
                out["x-oneOf-types"] = types
    for comb in ("oneOf", "anyOf"):
        alts = out.get(comb)
        if isinstance(alts, list) and any(_is_null_schema(a) for a in alts):
            rest = [a for a in alts if not _is_null_schema(a)]
            out.pop(comb)
            out["nullable"] = True
            if len(rest) == 1:
                out["allOf"] = rest
            elif rest:
                out[comb] = rest
    if isinstance(out.get("enum"), list) and None in out["enum"]:
        out["enum"] = [e for e in out["enum"] if e is not None]
        out["nullable"] = True
    if "const" in out:
        out["enum"] = [out.pop("const")]
    if in_schema and isinstance(out.get("examples"), list):
        ex = out.pop("examples")
        if ex:
            out["example"] = ex[0]
    for kx, kb in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        if isinstance(out.get(kx), (int, float)) and not isinstance(out.get(kx), bool):
            out[kb] = out.pop(kx)
            out[kx] = True
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OpenAPI 3.1 -> 3.0.3")
    ap.add_argument("--in", dest="src", default=str(ROOT / "contracts" / "api.yaml"))
    ap.add_argument("--out", default=str(ROOT / "integration" / "dotnet" / "api.v30.json"))
    a = ap.parse_args(argv)
    spec = yaml.safe_load(Path(a.src).read_text(encoding="utf-8"))
    conv = convert(spec)
    conv["openapi"] = "3.0.3"
    conv.pop("jsonSchemaDialect", None)
    conv.pop("webhooks", None)
    conv.setdefault("info", {})["x-source-openapi"] = spec.get("openapi")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(conv, indent=1, ensure_ascii=False), encoding="utf-8")
    left = json.dumps(conv).count('"type": [')
    print(f"wrote {out} (OpenAPI 3.0.3 from {spec.get('openapi')} {spec['info']['version']}; {left} list-typed schemas left)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
