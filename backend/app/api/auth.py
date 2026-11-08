"""Optional OIDC / JWT bearer authentication for the API (O4 Stage A; used in Stage B behind Entra ID).

OFF by default (`BIG360_AUTH=none`): nothing is installed and local use is unchanged; `get_principal()` then returns
the local single user with the admin role. With `BIG360_AUTH=oidc` every request under `auth_protected_prefixes`
(default `/api/`) needs `Authorization: Bearer <access token>`; `/health`, `/ready`, `/metrics`, `/docs`,
`/openapi.json` and the static UI shell stay public (configurable). CORS preflights (OPTIONS) are never challenged.

Token validation (`JwtValidator`), fail-closed:
* header `alg` must be in `auth_algorithms` (default RS256; `none` and HMAC algorithms are always refused), `kid`
  must be in the issuer's JWKS (fetched from `auth_jwks_url` or discovered from `<issuer>/.well-known/
  openid-configuration`; cached `auth_jwks_cache_s`, refreshed at most once a minute on an unknown kid - key rollover);
* RSA signature (RS256 / RS384 / RS512, keys >= 2048 bits);
* `exp` required, `nbf` / `iat` honoured, `auth_leeway_s` clock skew; `iss` in `auth_issuer`; `aud` intersects
  `auth_audience`.

Crypto backend: PyJWT's RSAAlgorithm (needs `PyJWT[crypto]`, i.e. the `cryptography` package) when importable,
otherwise a built-in PKCS#1 v1.5 verifier in pure Python (public-key operation only: modular exponentiation and a
constant-time comparison against the re-encoded DigestInfo - no ASN.1 parsing of the signature). For a production
deployment install `PyJWT[crypto]>=2.8` (docs/it/SECURITY.md); the claim checks are the same either way.

Roles: the `auth_roles_claim` (default `roles`, Entra app roles; a dotted path such as `realm_access.roles` and a
space-separated string such as `scp` also work) is mapped through `auth_role_map` (default BiG360.Viewer / .Analyst
/ .Admin) to viewer < analyst < admin. Policy (`required_role`):
* viewer  - every GET / HEAD, plus the read-only POST computations (screener query / export, backtest validate /
            compare, studio preview / compare / leaderboard, supply-chain shock / portfolio exposure, macro scenario run);
* analyst - every other write (watchlists, alerts, theses, studies, backtests, scenarios ...);
* admin   - operations: POST /system/jobs/{id}/trigger, PUT /system/schedule, model train / promote.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.api.auth")

ROLE_RANK = {"viewer": 1, "analyst": 2, "admin": 3}
SUPPORTED_ALGS = {"RS256": "sha256", "RS384": "sha384", "RS512": "sha512"}
MIN_RSA_BITS = 2048
_DIGESTINFO = {  # DER prefix of DigestInfo (RFC 8017 9.2 note 1)
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}
#: POST endpoints that only compute and return a result (no state change): open to viewers
READ_ONLY_POSTS = (
    r"/screener/query", r"/screener/export", r"/backtest/validate", r"/backtest/compare", r"/backtest/from-screen",
    r"/studio/datasets/preview", r"/studio/compare", r"/studio/leaderboard", r"/studio/guide/suggest", r"/studio/lint",
    r"/supply-chain/shock",
    r"/supply-chain/portfolio-exposure", r"/macro/v4/scenarios/run",
)
ADMIN_PATTERNS = (
    ("POST", r"/system/jobs/[^/]+/trigger"), ("PUT", r"/system/schedule"), ("POST", r"/models/train"),
    ("POST", r"/models/runs/[^/]+/promote"),
)


class AuthError(Exception):
    def __init__(self, status: int, error: str, description: str):
        super().__init__(description)
        self.status, self.error, self.description = status, error, description


@dataclass
class Principal:
    subject: str
    name: str | None
    roles: frozenset[str]
    claims: dict[str, Any] = field(default_factory=dict)
    auth: str = "oidc"

    @property
    def role(self) -> str | None:
        return max(self.roles, key=lambda r: ROLE_RANK.get(r, 0)) if self.roles else None

    def has(self, role: str) -> bool:
        return any(ROLE_RANK.get(r, 0) >= ROLE_RANK[role] for r in self.roles)


LOCAL_PRINCIPAL = Principal(subject="local", name="local user", roles=frozenset({"admin"}), auth="none")


# ------------------------------------------------------------------------------------------------ helpers
def b64url_decode(s: str) -> bytes:
    s = s.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]*", s):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64_int(s: str) -> int:
    return int.from_bytes(b64url_decode(s), "big")


def rsa_pkcs1v15_verify(n: int, e: int, message: bytes, signature: bytes, hash_name: str) -> bool:
    """RSASSA-PKCS1-v1_5 verification (RFC 8017 8.2.2) by re-encoding EM and comparing in constant time."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _DIGESTINFO[hash_name] + hashlib.new(hash_name, message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def _pyjwt_verify(jwk: dict[str, Any], alg: str, message: bytes, signature: bytes) -> bool | None:
    """Verify with PyJWT[crypto] when installed; None = backend unavailable."""
    try:
        from jwt.algorithms import RSAAlgorithm, has_crypto  # type: ignore[import-not-found]  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    if not has_crypto:
        return None
    algo = RSAAlgorithm(getattr(RSAAlgorithm, f"SHA{alg[2:]}"))
    key = algo.from_jwk(json.dumps(jwk))
    return bool(algo.verify(message, key, signature))


def crypto_backend() -> str:
    try:
        from jwt.algorithms import has_crypto  # type: ignore[import-not-found]  # noqa: PLC0415

        return "pyjwt" if has_crypto else "builtin"
    except Exception:  # noqa: BLE001
        return "builtin"


# ------------------------------------------------------------------------------------------------ JWKS
class JwksProvider:
    """JWKS by kid. Either a static dict (tests, air-gapped config) or fetched from a URL with caching."""

    def __init__(self, url: str | None = None, jwks: dict[str, Any] | None = None, issuer: str | None = None,
                 cache_s: float = 3600.0, min_refresh_s: float = 60.0, fetch: Callable[[str], dict[str, Any]] | None = None):
        self.url, self.issuer = url, issuer
        self.cache_s, self.min_refresh_s = cache_s, min_refresh_s
        self._fetch = fetch or _http_json
        self._lock = threading.Lock()
        self._keys: dict[str, dict[str, Any]] = {}
        self._loaded_at = 0.0
        self._static = jwks is not None
        if jwks is not None:
            self._set(jwks)

    def _set(self, jwks: dict[str, Any]) -> None:
        keys = {}
        for k in jwks.get("keys") or []:
            if k.get("kty") == "RSA" and k.get("kid") and (k.get("use") in (None, "sig")):
                keys[str(k["kid"])] = k
        self._keys = keys
        self._loaded_at = time.monotonic()

    def _resolve_url(self) -> str:
        if self.url:
            return self.url
        if not self.issuer:
            raise AuthError(500, "server_error", "auth misconfigured: no JWKS URL and no issuer")
        conf = self._fetch(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
        self.url = str(conf["jwks_uri"])
        return self.url

    def refresh(self) -> None:
        self._set(self._fetch(self._resolve_url()))

    def get(self, kid: str) -> dict[str, Any] | None:
        with self._lock:
            if self._static:
                return self._keys.get(kid)
            age = time.monotonic() - self._loaded_at
            if not self._keys or age > self.cache_s or (kid not in self._keys and age > self.min_refresh_s):
                try:
                    self.refresh()
                except AuthError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("JWKS refresh failed: %s", e)
                    if not self._keys:
                        raise AuthError(503, "temporarily_unavailable", "signing keys unavailable") from e
            return self._keys.get(kid)


def _http_json(url: str) -> dict[str, Any]:
    if not url.lower().startswith("https://") and not url.lower().startswith("http://127.0.0.1") \
            and not url.lower().startswith("http://localhost"):
        raise AuthError(500, "server_error", "JWKS / discovery URL must be https")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "BiG360-API"})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - https enforced above; proxy from env
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------------------------------------ validator
class JwtValidator:
    def __init__(self, issuers: list[str], audiences: list[str], jwks: JwksProvider, algorithms: list[str] | None = None,
                 leeway_s: int = 60, clock: Callable[[], float] = time.time, use_pyjwt: bool | None = None):
        if not issuers or not audiences:
            raise ValueError("JwtValidator needs at least one issuer and one audience")
        self.issuers, self.audiences = list(issuers), list(audiences)
        algs = [a for a in (algorithms or ["RS256"]) if a in SUPPORTED_ALGS]
        if not algs:
            raise ValueError(f"no supported algorithm in {algorithms}; supported: {sorted(SUPPORTED_ALGS)}")
        self.algorithms = algs
        self.jwks, self.leeway, self.clock = jwks, int(leeway_s), clock
        self.use_pyjwt = (crypto_backend() == "pyjwt") if use_pyjwt is None else use_pyjwt

    def _verify_sig(self, jwk: dict[str, Any], alg: str, signing_input: bytes, sig: bytes) -> bool:
        n, e = _b64_int(jwk["n"]), _b64_int(jwk["e"])
        if n.bit_length() < MIN_RSA_BITS:
            raise AuthError(401, "invalid_token", "signing key too short")
        if self.use_pyjwt:
            r = _pyjwt_verify(jwk, alg, signing_input, sig)
            if r is not None:
                return r
        return rsa_pkcs1v15_verify(n, e, signing_input, sig, SUPPORTED_ALGS[alg])

    def validate(self, token: str) -> dict[str, Any]:
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError(401, "invalid_token", "malformed token")
        try:
            header = json.loads(b64url_decode(parts[0]))
            claims = json.loads(b64url_decode(parts[1]))
            sig = b64url_decode(parts[2])
        except Exception as e:  # noqa: BLE001
            raise AuthError(401, "invalid_token", "malformed token") from e
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthError(401, "invalid_token", "malformed token")
        alg = header.get("alg")
        if alg not in self.algorithms:
            raise AuthError(401, "invalid_token", f"algorithm not allowed: {alg}")
        if header.get("crit"):
            raise AuthError(401, "invalid_token", "unsupported critical header")
        kid = header.get("kid")
        if not kid:
            raise AuthError(401, "invalid_token", "token has no kid")
        jwk = self.jwks.get(str(kid))
        if jwk is None:
            raise AuthError(401, "invalid_token", "unknown signing key")
        if jwk.get("alg") and jwk["alg"] != alg:
            raise AuthError(401, "invalid_token", "key / algorithm mismatch")
        if not self._verify_sig(jwk, alg, f"{parts[0]}.{parts[1]}".encode("ascii"), sig):
            raise AuthError(401, "invalid_token", "bad signature")
        now = self.clock()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)):
            raise AuthError(401, "invalid_token", "token has no exp")
        if now > exp + self.leeway:
            raise AuthError(401, "invalid_token", "token expired")
        nbf = claims.get("nbf")
        if isinstance(nbf, (int, float)) and now + self.leeway < nbf:
            raise AuthError(401, "invalid_token", "token not yet valid")
        iat = claims.get("iat")
        if isinstance(iat, (int, float)) and now + self.leeway < iat:
            raise AuthError(401, "invalid_token", "token issued in the future")
        if claims.get("iss") not in self.issuers:
            raise AuthError(401, "invalid_token", "wrong issuer")
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else list(aud or []) if isinstance(aud, list) else []
        if not any(a in self.audiences for a in auds):
            raise AuthError(401, "invalid_token", "wrong audience")
        return claims


def claim_values(claims: dict[str, Any], path: str) -> list[str]:
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, dict):
            return []
        node = node.get(part)
    if isinstance(node, str):
        return [x for x in node.split() if x]
    if isinstance(node, list):
        return [str(x) for x in node]
    return []


def map_roles(claims: dict[str, Any], roles_claim: str, role_map: dict[str, str], default_role: str | None = None) -> frozenset[str]:
    out = set()
    lower = {k.lower(): v for k, v in role_map.items()}
    for v in claim_values(claims, roles_claim):
        r = role_map.get(v) or lower.get(v.lower())
        if r in ROLE_RANK:
            out.add(r)
    if not out and default_role in ROLE_RANK:
        out.add(default_role)  # type: ignore[arg-type]
    return frozenset(out)


def principal_from_claims(claims: dict[str, Any], s: Settings) -> Principal:
    roles = map_roles(claims, s.auth_roles_claim, s.auth_role_map, s.auth_default_role)
    name = claims.get("preferred_username") or claims.get("upn") or claims.get("name") or claims.get("appid") \
        or claims.get("azp")
    subject = str(claims.get("oid") or claims.get("sub") or "")
    return Principal(subject=subject, name=str(name) if name else None, roles=roles, claims=claims)


def required_role(method: str, path: str) -> str:
    m = method.upper()
    p = re.sub(r"^/api/v\d+", "", path.rstrip("/") or "/")
    if m in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    for meth, rx in ADMIN_PATTERNS:
        if m == meth and re.fullmatch(rx, p):
            return "admin"
    if m == "POST" and any(re.fullmatch(rx, p) for rx in READ_ONLY_POSTS):
        return "viewer"
    return "analyst"


# ------------------------------------------------------------------------------------------------ ASGI
def _is_protected(path: str, s: Settings) -> bool:
    if path in s.auth_public_paths:
        return False
    return any(path.startswith(p) for p in s.auth_protected_prefixes)


class AuthMiddleware:
    """Pure ASGI bearer-token check. Installed only when BIG360_AUTH=oidc (see install_auth)."""

    def __init__(self, app: Any, validator: JwtValidator, settings: Settings):
        self.app, self.validator, self.s = app, validator, settings

    async def _deny(self, send: Any, status: int, error: str, description: str) -> None:
        body = json.dumps({"type": "about:blank", "title": "Unauthorized" if status == 401 else
                           ("Forbidden" if status == 403 else "Service Unavailable"),
                           "status": status, "detail": description}).encode()
        headers = [(b"content-type", b"application/problem+json"), (b"content-length", str(len(body)).encode())]
        if status == 401:
            headers.append((b"www-authenticate",
                            f'Bearer realm="big360", error="{error}", error_description="{description}"'.encode()))
        elif status == 403:
            headers.append((b"www-authenticate", b'Bearer realm="big360", error="insufficient_scope"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") == "OPTIONS" or not _is_protected(scope.get("path", ""), self.s):
            await self.app(scope, receive, send)
            return
        auth = ""
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                auth = v.decode("latin-1")
                break
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            await self._deny(send, 401, "invalid_request", "bearer token required")
            return
        try:
            claims = self.validator.validate(auth[7:].strip())
        except AuthError as e:
            log.info("auth rejected %s %s: %s", scope.get("method"), scope.get("path"), e.description)
            await self._deny(send, e.status, e.error, e.description)
            return
        principal = principal_from_claims(claims, self.s)
        need = required_role(scope.get("method", "GET"), scope.get("path", ""))
        if not principal.has(need):
            log.info("auth forbidden %s %s for %s (roles %s, needs %s)", scope.get("method"), scope.get("path"),
                     principal.name or principal.subject, sorted(principal.roles), need)
            await self._deny(send, 403, "insufficient_scope", f"role {need} required")
            return
        st = scope.setdefault("state", {})
        st["principal"] = principal
        st["principal_name"] = principal.name or principal.subject
        await self.app(scope, receive, send)


def build_validator(s: Settings, jwks: dict[str, Any] | None = None) -> JwtValidator:
    if not s.auth_issuer or not s.auth_audience:
        raise RuntimeError("BIG360_AUTH=oidc needs BIG360_AUTH_ISSUER and BIG360_AUTH_AUDIENCE (docs/CONFIGURATION.md)")
    provider = JwksProvider(url=s.auth_jwks_url, jwks=jwks, issuer=s.auth_issuer[0], cache_s=s.auth_jwks_cache_s)
    return JwtValidator(s.auth_issuer, s.auth_audience, provider, s.auth_algorithms, s.auth_leeway_s)


def install_auth(app: Any, s: Settings | None = None, jwks: dict[str, Any] | None = None) -> bool:
    """Add AuthMiddleware when BIG360_AUTH=oidc. Returns True when installed. Fails closed on bad config."""
    s = s or get_settings()
    if s.auth_mode != "oidc":
        return False
    validator = build_validator(s, jwks)
    app.add_middleware(AuthMiddleware, validator=validator, settings=s)
    log.info("auth: OIDC bearer tokens required under %s (issuers %s, audiences %s, crypto %s)",
             s.auth_protected_prefixes, s.auth_issuer, s.auth_audience, crypto_backend())
    return True


def get_principal(request: Any) -> Principal:
    """FastAPI dependency: the caller (LOCAL_PRINCIPAL when auth is off)."""
    p = getattr(getattr(request, "state", None), "principal", None)
    return p if isinstance(p, Principal) else LOCAL_PRINCIPAL
