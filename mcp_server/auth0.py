"""Fail-closed Auth0/OIDC access-token verification for the HTTP MCP entry.

Only public OAuth configuration is read from the environment.  The MCP
resource server does not need, read, or log an OAuth client secret.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken


REQUIRED_SCOPE = "music:read"
JWKS_TIMEOUT_SECONDS = 3.0
JWKS_CACHE_SECONDS = 300.0
UNKNOWN_KID_REFRESH_SECONDS = 30.0
MAX_METADATA_BYTES = 262_144
MAX_TOKEN_BYTES = 16_384


class AuthConfigurationError(RuntimeError):
    """A public OAuth setting is absent, unsafe, or incompatible."""


JsonFetcher = Callable[[str, float, int], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class Auth0Settings:
    """Canonical public settings shared by OAuth discovery and JWT checks."""

    issuer_url: str
    audience: str
    resource_url: str
    required_scope: str = REQUIRED_SCOPE

    @classmethod
    def from_environment(cls) -> "Auth0Settings":
        issuer_url = _validate_issuer_url(os.environ.get("AUTH0_ISSUER_URL", ""))
        resource_url = _validate_resource_url(os.environ.get("MCP_PUBLIC_URL", ""))
        audience = _validate_resource_url(os.environ.get("AUTH0_AUDIENCE", ""))
        required_scope = os.environ.get("MCP_REQUIRED_SCOPE", REQUIRED_SCOPE)
        if required_scope != REQUIRED_SCOPE:
            raise AuthConfigurationError("MCP_REQUIRED_SCOPE must be exactly music:read")
        if audience != resource_url:
            raise AuthConfigurationError("AUTH0_AUDIENCE must exactly match MCP_PUBLIC_URL")
        return cls(
            issuer_url=issuer_url,
            audience=audience,
            resource_url=resource_url,
            required_scope=required_scope,
        )


def _validate_issuer_url(value: str) -> str:
    candidate = value.strip()
    parsed = urllib.parse.urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/"
    ):
        raise AuthConfigurationError(
            "AUTH0_ISSUER_URL must be a canonical HTTPS origin ending in /"
        )
    try:
        port = parsed.port
    except ValueError:
        raise AuthConfigurationError("AUTH0_ISSUER_URL contains an invalid port") from None
    if port not in {None, 443}:
        raise AuthConfigurationError("AUTH0_ISSUER_URL must use the standard HTTPS port")
    if candidate != f"https://{parsed.netloc}/":
        raise AuthConfigurationError("AUTH0_ISSUER_URL is not canonical")
    return candidate


def _validate_resource_url(value: str) -> str:
    candidate = value.strip()
    parsed = urllib.parse.urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise AuthConfigurationError(
            "MCP_PUBLIC_URL and AUTH0_AUDIENCE must be canonical HTTPS origins"
        )
    try:
        port = parsed.port
    except ValueError:
        raise AuthConfigurationError("MCP public URL contains an invalid port") from None
    if port not in {None, 443}:
        raise AuthConfigurationError("MCP public URL must use the standard HTTPS port")
    if candidate != f"https://{parsed.netloc}":
        raise AuthConfigurationError("MCP public URL is not canonical")
    return candidate


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _download_json(url: str, timeout: float, max_bytes: int) -> Mapping[str, Any]:
    """Fetch one HTTPS JSON document without redirects or credential headers."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("metadata URL must use HTTPS without user information")
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise ValueError("metadata redirects are not accepted")
            raw = response.read(max_bytes + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("metadata is unavailable") from None
    if not raw or len(raw) > max_bytes:
        raise ValueError("metadata response has an invalid size")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("metadata response is not valid JSON") from None
    if not isinstance(result, dict):
        raise ValueError("metadata response is not a JSON object")
    return result


class Auth0JWTVerifier:
    """Verify Auth0 RS256 access tokens using cached OIDC discovery/JWKS data."""

    def __init__(
        self,
        settings: Auth0Settings,
        *,
        json_fetcher: JsonFetcher = _download_json,
        clock: Callable[[], float] = time.time,
        cache_seconds: float = JWKS_CACHE_SECONDS,
        timeout: float = JWKS_TIMEOUT_SECONDS,
    ) -> None:
        if cache_seconds <= 0 or timeout <= 0:
            raise ValueError("cache_seconds and timeout must be positive")
        self.settings = settings
        self._json_fetcher = json_fetcher
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._timeout = timeout
        self._keys: dict[str, Any] = {}
        self._cache_expires_at = 0.0
        self._last_refresh_at = 0.0
        self._last_forced_refresh_attempt_at = 0.0
        self._next_fetch_allowed_at = 0.0
        self._lock = threading.Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return SDK access information, or ``None`` for every invalid token."""

        try:
            return await asyncio.to_thread(self._verify_token_sync, token)
        except Exception:
            # Fail closed and deliberately avoid logging tokens, claims, or fetch errors.
            return None

    def _verify_token_sync(self, token: str) -> AccessToken | None:
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > MAX_TOKEN_BYTES
            or any(character.isspace() for character in token)
        ):
            return None

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not (1 <= len(kid) <= 256):
            return None

        key = self._signing_key(kid)
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer_url,
                leeway=0,
                options={
                    "require": ["iss", "aud", "sub", "exp", "iat"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.PyJWTError:
            return None

        subject = claims.get("sub")
        expires_at = claims.get("exp")
        issued_at = claims.get("iat")
        if (
            not isinstance(subject, str)
            or not subject
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
        ):
            return None

        scope_claim = claims.get("scope", "")
        if not isinstance(scope_claim, str):
            return None
        scopes = [scope for scope in scope_claim.split(" ") if scope]
        if len(scopes) != len(set(scopes)):
            return None

        client_id = claims.get("azp", claims.get("client_id"))
        if not isinstance(client_id, str) or not client_id:
            return None

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(expires_at),
            resource=self.settings.resource_url,
            subject=subject,
            claims={"iss": self.settings.issuer_url},
        )

    def _signing_key(self, kid: str) -> Any | None:
        keys = self._load_keys(force=False)
        key = keys.get(kid)
        if key is not None:
            return key
        # A new kid is the normal Auth0 rotation signal. Refresh once, then fail.
        return self._load_keys(force=True).get(kid)

    def _load_keys(self, *, force: bool) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            if not force and self._keys and now < self._cache_expires_at:
                return dict(self._keys)
            if now < self._next_fetch_allowed_at:
                # Never fall back to an expired key set.  A short negative
                # cache prevents an Auth0 outage from becoming an outbound
                # request amplifier for arbitrary unauthenticated JWTs.
                return dict(self._keys) if now < self._cache_expires_at else {}
            if force:
                # An untrusted JWT controls ``kid``.  Limit all unknown-kid
                # refreshes globally so random values cannot amplify outbound
                # traffic to Auth0, while still permitting prompt key rotation.
                last_attempt = max(self._last_refresh_at, self._last_forced_refresh_attempt_at)
                if now - last_attempt < UNKNOWN_KID_REFRESH_SECONDS:
                    return dict(self._keys)
                self._last_forced_refresh_attempt_at = now

            try:
                loaded = self._fetch_signing_keys()
            except Exception:
                self._next_fetch_allowed_at = now + UNKNOWN_KID_REFRESH_SECONDS
                raise
            self._keys = loaded
            self._cache_expires_at = now + self._cache_seconds
            self._last_refresh_at = now
            self._next_fetch_allowed_at = 0.0
            return dict(self._keys)

    def _fetch_signing_keys(self) -> dict[str, Any]:
        discovery_url = f"{self.settings.issuer_url}.well-known/openid-configuration"
        discovery = self._json_fetcher(discovery_url, self._timeout, MAX_METADATA_BYTES)
        if discovery.get("issuer") != self.settings.issuer_url:
            raise ValueError("OIDC discovery issuer does not match")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise ValueError("OIDC discovery has no JWKS URI")
        self._validate_jwks_uri(jwks_uri)
        jwks = self._json_fetcher(jwks_uri, self._timeout, MAX_METADATA_BYTES)
        raw_keys = jwks.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise ValueError("JWKS has no keys")

        loaded: dict[str, Any] = {}
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                continue
            key_id = raw_key.get("kid")
            if (
                raw_key.get("kty") != "RSA"
                or raw_key.get("alg") not in {None, "RS256"}
                or raw_key.get("use") not in {None, "sig"}
                or not isinstance(key_id, str)
                or not (1 <= len(key_id) <= 256)
                or key_id in loaded
            ):
                continue
            try:
                loaded[key_id] = jwt.algorithms.RSAAlgorithm.from_jwk(raw_key)
            except (KeyError, TypeError, ValueError):
                continue
        if not loaded:
            raise ValueError("JWKS has no usable RS256 signing keys")
        return loaded

    def _validate_jwks_uri(self, value: str) -> None:
        issuer = urllib.parse.urlsplit(self.settings.issuer_url)
        jwks = urllib.parse.urlsplit(value)
        if (
            jwks.scheme != "https"
            or not jwks.hostname
            or jwks.username
            or jwks.password
            or jwks.query
            or jwks.fragment
            or jwks.hostname.lower() != issuer.hostname.lower()
            or jwks.port != issuer.port
        ):
            raise ValueError("JWKS URI must be an HTTPS URL on the issuer origin")


__all__ = [
    "Auth0JWTVerifier",
    "Auth0Settings",
    "AuthConfigurationError",
    "REQUIRED_SCOPE",
]
