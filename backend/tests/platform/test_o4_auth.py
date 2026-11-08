"""O4: OIDC / JWT bearer validator (app.api.auth) with a locally generated RSA key and JWKS.

No crypto dependency: the test key is generated with a pure-Python Miller-Rabin prime search and tokens are signed
with RSASSA-PKCS1-v1_5 / SHA-256 by modular exponentiation. When PyJWT[crypto] is installed the validator also runs
through PyJWT's verifier (test_pyjwt_backend_if_installed).
"""

from __future__ import annotations

import hashlib
import json
import random
import time

import pytest

from app.api import auth as auth_mod
from app.api.auth import (
    AuthError,
    JwksProvider,
    JwtValidator,
    b64url_encode,
    map_roles,
    required_role,
    rsa_pkcs1v15_verify,
)

ISS = "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000001/v2.0"
AUD = "api://big360-test"
_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def _is_probable_prime(n: int, rng: random.Random, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = rng.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        c = rng.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_probable_prime(c, rng):
            return c


class RsaKey:
    def __init__(self, bits: int = 2048, seed: int = 7, kid: str = "test-kid-1"):
        rng = random.Random(seed)
        e = 65537
        while True:
            p, q = _prime(bits // 2, rng), _prime(bits // 2, rng)
            phi = (p - 1) * (q - 1)
            if p != q and phi % e and (p * q).bit_length() == bits:
                break
        self.n, self.e, self.d, self.kid = p * q, e, pow(e, -1, phi), kid

    def jwk(self) -> dict:
        k = (self.n.bit_length() + 7) // 8
        return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
                "n": b64url_encode(self.n.to_bytes(k, "big")), "e": b64url_encode(self.e.to_bytes(3, "big"))}

    def sign(self, msg: bytes) -> bytes:
        k = (self.n.bit_length() + 7) // 8
        t = auth_mod._DIGESTINFO["sha256"] + hashlib.sha256(msg).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")

    def token(self, claims: dict, header: dict | None = None) -> str:
        h = {"alg": "RS256", "typ": "JWT", "kid": self.kid, **(header or {})}
        si = b64url_encode(json.dumps(h).encode()) + "." + b64url_encode(json.dumps(claims).encode())
        return si + "." + b64url_encode(self.sign(si.encode()))


@pytest.fixture(scope="module")
def key() -> RsaKey:
    return RsaKey()


@pytest.fixture(scope="module")
def other_key() -> RsaKey:
    return RsaKey(seed=11, kid="test-kid-1")  # same kid, different key: forged signature


def _claims(**kw) -> dict:
    now = int(time.time())
    base = {"iss": ISS, "aud": AUD, "sub": "user-1", "oid": "oid-1", "preferred_username": "analyst@big.pt",
            "iat": now - 10, "nbf": now - 10, "exp": now + 3600, "roles": ["BiG360.Analyst"]}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def _validator(key: RsaKey, **kw) -> JwtValidator:
    return JwtValidator([ISS], [AUD], JwksProvider(jwks={"keys": [key.jwk()]}), use_pyjwt=False, **kw)


def test_valid_token_and_roles(key):
    v = _validator(key)
    claims = v.validate(key.token(_claims()))
    assert claims["preferred_username"] == "analyst@big.pt"
    roles = map_roles(claims, "roles", {"BiG360.Viewer": "viewer", "BiG360.Analyst": "analyst", "BiG360.Admin": "admin"})
    assert roles == {"analyst"}
    # audience as a list, case-insensitive role mapping, space-separated scope claim, dotted claim path
    assert v.validate(key.token(_claims(aud=["other", AUD])))
    assert map_roles({"roles": ["big360.admin"]}, "roles", {"BiG360.Admin": "admin"}) == {"admin"}
    assert map_roles({"scp": "read BiG360.Viewer"}, "scp", {"BiG360.Viewer": "viewer"}) == {"viewer"}
    assert map_roles({"realm_access": {"roles": ["x"]}}, "realm_access.roles", {"x": "viewer"}) == {"viewer"}
    assert map_roles({"roles": ["unknown"]}, "roles", {}, default_role="viewer") == {"viewer"}
    assert map_roles({"roles": ["unknown"]}, "roles", {}) == frozenset()


@pytest.mark.parametrize("mutate, why", [
    (lambda c: c.update(exp=int(time.time()) - 3600), "expired"),
    (lambda c: c.update(nbf=int(time.time()) + 3600), "not yet valid"),
    (lambda c: c.update(iat=int(time.time()) + 3600), "future"),
    (lambda c: c.update(iss="https://evil.example/v2.0"), "issuer"),
    (lambda c: c.update(aud="api://someone-else"), "audience"),
    (lambda c: c.pop("exp"), "no exp"),
])
def test_claim_checks(key, mutate, why):
    c = _claims()
    mutate(c)
    with pytest.raises(AuthError) as ei:
        _validator(key).validate(key.token(c))
    assert ei.value.status == 401 and why in ei.value.description


def test_leeway(key):
    c = _claims(exp=int(time.time()) - 30)
    assert _validator(key, leeway_s=60).validate(key.token(c))
    with pytest.raises(AuthError):
        _validator(key, leeway_s=0).validate(key.token(c))


def test_signature_and_header_attacks(key, other_key):
    v = _validator(key)
    good = key.token(_claims())
    h, p, s = good.split(".")
    # tampered payload (role escalation) with the original signature
    evil = b64url_encode(json.dumps(_claims(roles=["BiG360.Admin"])).encode())
    with pytest.raises(AuthError, match="bad signature"):
        v.validate(f"{h}.{evil}.{s}")
    # signed with another key under the same kid
    with pytest.raises(AuthError, match="bad signature"):
        v.validate(other_key.token(_claims()))
    # alg=none / HS256 (key confusion) / unknown kid / missing kid / malformed
    for hdr, msg in (({"alg": "none"}, "algorithm"), ({"alg": "HS256"}, "algorithm"), ({"kid": "nope"}, "unknown signing key")):
        with pytest.raises(AuthError, match=msg):
            v.validate(key.token(_claims(), header=hdr))
    hdr_nokid = b64url_encode(json.dumps({"alg": "RS256"}).encode())
    with pytest.raises(AuthError, match="no kid"):
        v.validate(f"{hdr_nokid}.{p}.{s}")
    with pytest.raises(AuthError, match="malformed"):
        v.validate("abc.def")
    with pytest.raises(AuthError, match="malformed"):
        v.validate("!!.@@.##")
    with pytest.raises(AuthError, match="malformed"):
        v.validate(f"{h}.{p}.{s}=")  # padding / non-base64url
    # truncated signature
    with pytest.raises(AuthError, match="bad signature"):
        v.validate(f"{h}.{p}.{s[:-4]}")


def test_rejects_short_keys():
    small = RsaKey(bits=1024, seed=3, kid="small")
    v = JwtValidator([ISS], [AUD], JwksProvider(jwks={"keys": [small.jwk()]}), use_pyjwt=False)
    with pytest.raises(AuthError, match="too short"):
        v.validate(small.token(_claims()))


def test_pkcs1_verify_primitive(key):
    msg = b"hello"
    sig = key.sign(msg)
    assert rsa_pkcs1v15_verify(key.n, key.e, msg, sig, "sha256")
    assert not rsa_pkcs1v15_verify(key.n, key.e, b"hellO", sig, "sha256")
    assert not rsa_pkcs1v15_verify(key.n, key.e, msg, sig[1:], "sha256")


def test_jwks_provider_fetch_and_rollover(key):
    k2 = RsaKey(seed=21, kid="test-kid-2")
    calls: list[str] = []
    current = {"keys": [key.jwk()]}

    def fetch(url: str) -> dict:
        calls.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return {"jwks_uri": "https://login.example/keys"}
        return current

    prov = JwksProvider(issuer=ISS, fetch=fetch, min_refresh_s=0)
    v = JwtValidator([ISS], [AUD], prov, use_pyjwt=False)
    assert v.validate(key.token(_claims()))
    assert calls == [ISS + "/.well-known/openid-configuration", "https://login.example/keys"]
    current = {"keys": [key.jwk(), k2.jwk()]}  # key rollover: unknown kid -> one refresh
    assert v.validate(k2.token(_claims()))
    assert calls[-1] == "https://login.example/keys" and len(calls) == 3


def test_required_role_policy():
    assert required_role("GET", "/api/v1/companies/S1/overview") == "viewer"
    assert required_role("POST", "/api/v1/screener/query") == "viewer"
    assert required_role("POST", "/api/v1/supply-chain/shock") == "viewer"
    assert required_role("POST", "/api/v1/watchlists") == "analyst"
    assert required_role("DELETE", "/api/v1/theses/T1") == "analyst"
    assert required_role("POST", "/api/v1/system/jobs/moat/trigger") == "admin"
    assert required_role("PUT", "/api/v1/system/schedule") == "admin"
    assert required_role("POST", "/api/v1/models/runs/R1/promote") == "admin"
    assert required_role("POST", "/api/v1/models/train") == "admin"


def test_pyjwt_backend_if_installed(key):
    if auth_mod.crypto_backend() != "pyjwt":
        pytest.skip("PyJWT[crypto] not installed: the built-in verifier is used (covered above)")
    v = JwtValidator([ISS], [AUD], JwksProvider(jwks={"keys": [key.jwk()]}), use_pyjwt=True)
    assert v.validate(key.token(_claims()))


def test_api_with_auth_enabled(tmp_data, monkeypatch, key):
    """create_app with BIG360_AUTH=oidc: 401 / 403 / 200 by role; probes, docs and preflights stay public."""
    from fastapi.testclient import TestClient

    from app.api.deps import snapshots
    from app.core import config as config_mod

    monkeypatch.setenv("BIG360_AUTH", "oidc")
    monkeypatch.setenv("BIG360_AUTH_ISSUER", ISS)
    monkeypatch.setenv("BIG360_AUTH_AUDIENCE", AUD)
    monkeypatch.setenv("BIG360_AUTH_JWKS_URL", "https://login.example/keys")
    monkeypatch.setenv("BIG360_API_LOG_FILE", "")
    monkeypatch.setattr(auth_mod, "_http_json", lambda url: {"keys": [key.jwk()]})
    config_mod.reset_settings_cache()
    snapshots.reset()
    from app.api.main import create_app

    try:
        c = TestClient(create_app())
        assert c.get("/health").status_code == 200
        assert c.get("/ready").status_code in (200, 503)
        assert c.get("/metrics").status_code == 200
        assert c.get("/openapi.json").status_code == 200
        r = c.get("/api/v1/system/status")
        assert r.status_code == 401 and r.headers["content-type"].startswith("application/problem+json")
        assert r.headers["www-authenticate"].startswith("Bearer") and r.headers.get("x-request-id")
        assert c.get("/api/v1/system/status", headers={"Authorization": "Bearer junk"}).status_code == 401
        pre = c.options("/api/v1/system/status", headers={"Origin": "http://localhost:5173",
                                                         "Access-Control-Request-Method": "GET"})
        assert pre.status_code == 200

        def hdr(*roles: str) -> dict:
            return {"Authorization": "Bearer " + key.token(_claims(roles=list(roles)))}

        assert c.get("/api/v1/system/status", headers=hdr("BiG360.Viewer")).status_code == 200
        r = c.post("/api/v1/watchlists", json={"name": "x"}, headers=hdr("BiG360.Viewer"))
        assert r.status_code == 403 and "analyst" in r.json()["detail"]
        assert c.post("/api/v1/watchlists", json={"name": "x"}, headers=hdr("BiG360.Analyst")).status_code in (200, 201)
        r = c.post("/api/v1/system/jobs/moat/trigger", json={}, headers=hdr("BiG360.Analyst"))
        assert r.status_code == 403
        assert c.get("/api/v1/system/status", headers=hdr("Unmapped.Role")).status_code == 403
    finally:
        snapshots.reset()
        config_mod.reset_settings_cache()


def test_auth_off_by_default(tmp_data, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.deps import snapshots
    from app.core import config as config_mod

    monkeypatch.delenv("BIG360_AUTH", raising=False)
    monkeypatch.setenv("BIG360_API_LOG_FILE", "")
    config_mod.reset_settings_cache()
    snapshots.reset()
    from app.api.main import create_app

    try:
        assert config_mod.get_settings().auth_mode == "none"
        c = TestClient(create_app())
        assert c.get("/api/v1/system/status").status_code == 200
        assert not any(getattr(m, "cls", None) is auth_mod.AuthMiddleware for m in c.app.user_middleware)
    finally:
        snapshots.reset()
        config_mod.reset_settings_cache()


def test_misconfigured_oidc_fails_closed(tmp_data, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setenv("BIG360_AUTH", "oidc")
    monkeypatch.delenv("BIG360_AUTH_ISSUER", raising=False)
    config_mod.reset_settings_cache()
    try:
        from fastapi import FastAPI

        with pytest.raises(RuntimeError, match="BIG360_AUTH_ISSUER"):
            auth_mod.install_auth(FastAPI(), config_mod.get_settings())
    finally:
        config_mod.reset_settings_cache()
