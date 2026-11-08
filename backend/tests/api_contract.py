"""Helpers to validate API responses against contracts/api.yaml (JSON Schema 2020-12 / OpenAPI 3.1)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).resolve().parents[2] / "contracts" / "api.yaml"


@lru_cache(maxsize=1)
def spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


_HTTP = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def operations() -> dict[tuple[str, str], dict[str, Any]]:
    out = {}
    for path, ops in spec()["paths"].items():
        for method, op in ops.items():
            if method.lower() not in _HTTP:   # path-level keys (parameters, summary, ...) are not operations
                continue
            out[(method.upper(), path)] = op
    return out


def _resolve(obj: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in obj:
        ref = obj["$ref"]
        node: Any = spec()
        for part in ref.removeprefix("#/").split("/"):
            node = node[part]
        return node
    return obj


@lru_cache(maxsize=256)
def _validator(method: str, path: str, status: str) -> Draft202012Validator | None:
    op = operations()[(method, path)]
    if status not in op["responses"]:
        if int(status) >= 400:  # undeclared error statuses must still be Problem documents
            root = {"components": spec()["components"], "$ref": "#/components/schemas/Problem"}
            return Draft202012Validator(root)
        raise AssertionError(f"{method} {path}: status {status} not declared in contracts/api.yaml")
    resp = _resolve(op["responses"][status])
    content = resp.get("content") or {}
    media = content.get("application/json") or content.get("application/problem+json")
    if media is None:
        return None
    root = {"components": spec()["components"], **media["schema"]}
    return Draft202012Validator(root, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate(method: str, path: str, status: int, body: Any) -> None:
    v = _validator(method.upper(), path, str(status))
    if v is None:
        return
    errors = sorted(v.iter_errors(body), key=lambda e: list(e.absolute_path))
    if errors:
        msgs = [f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message[:200]}" for e in errors[:15]]
        raise AssertionError(f"{method} {path} -> {status}: {len(errors)} schema errors\n  " + "\n  ".join(msgs))


def template_of(url: str) -> str | None:
    """Map a concrete URL path to its contract template."""
    p = url.split("?")[0]
    for (_, tpl) in operations():
        rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+", tpl) + "$"
        if re.match(rx, p):
            return tpl
    return None
