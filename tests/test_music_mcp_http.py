from __future__ import annotations

import json
import os
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from mcp_server.auth0 import (
    Auth0JWTVerifier,
    Auth0Settings,
    AuthConfigurationError,
    REQUIRED_SCOPE,
)
from mcp_server.eryu_music_http import HTTPSettings, build_http_app
from mcp_server.eryu_music_mcp import build_server


ISSUER = "https://eryu-test.us.auth0.com/"
RESOURCE = "https://eryu-mcp.95.169.17.214.sslip.io"
JWKS_URI = "https://eryu-test.us.auth0.com/.well-known/jwks.json"


class UnusedReadClient:
    async def get_json(self, path, query=None):  # type: ignore[no-untyped-def]
        raise AssertionError("HTTP auth tests must not reach the backend")

    async def get_bytes(self, path, query=None):  # type: ignore[no-untyped-def]
        raise AssertionError("HTTP auth tests must not reach the backend")


class StaticVerifier:
    def __init__(self, scopes: list[str] | None) -> None:
        self.scopes = scopes

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.scopes is None or token != "incoming-oauth-token":
            return None
        return AccessToken(
            token=token,
            client_id="chatgpt-public-client",
            scopes=self.scopes,
            expires_at=int(time.time()) + 300,
            resource=RESOURCE,
            subject="auth0|test-user",
            claims={"iss": ISSUER},
        )


def make_http_settings() -> HTTPSettings:
    return HTTPSettings(
        host="127.0.0.1",
        port=9091,
        auth0=Auth0Settings(
            issuer_url=ISSUER,
            audience=RESOURCE,
            resource_url=RESOURCE,
        ),
    )


class HTTPConfigurationTests(unittest.TestCase):
    def valid_environment(self) -> dict[str, str]:
        return {
            "MCP_HTTP_HOST": "127.0.0.1",
            "MCP_HTTP_PORT": "9091",
            "MCP_PUBLIC_URL": RESOURCE,
            "AUTH0_ISSUER_URL": ISSUER,
            "AUTH0_AUDIENCE": RESOURCE,
            "MCP_REQUIRED_SCOPE": REQUIRED_SCOPE,
        }

    def test_valid_public_configuration_is_exact_and_loopback_only(self) -> None:
        with patch.dict(os.environ, self.valid_environment(), clear=True):
            settings = HTTPSettings.from_environment()

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 9091)
        self.assertEqual(settings.auth0.resource_url, RESOURCE)
        self.assertEqual(settings.auth_settings().required_scopes, ["music:read"])

    def test_rejects_non_loopback_listener(self) -> None:
        environment = self.valid_environment()
        environment["MCP_HTTP_HOST"] = "0.0.0.0"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "loopback"):
                HTTPSettings.from_environment()

    def test_rejects_noncanonical_port(self) -> None:
        environment = self.valid_environment()
        environment["MCP_HTTP_PORT"] = "09091"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "canonical"):
                HTTPSettings.from_environment()

    def test_rejects_non_https_or_non_origin_resource(self) -> None:
        for value in (
            "http://eryu-mcp.example",
            "https://eryu-mcp.example/",
            "https://eryu-mcp.example/mcp",
            "https://user@example.com",
        ):
            environment = self.valid_environment()
            environment["MCP_PUBLIC_URL"] = value
            environment["AUTH0_AUDIENCE"] = value
            with self.subTest(value=value), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(AuthConfigurationError):
                    HTTPSettings.from_environment()

    def test_rejects_mismatched_audience(self) -> None:
        environment = self.valid_environment()
        environment["AUTH0_AUDIENCE"] = "https://other.example"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(AuthConfigurationError, "exactly match"):
                HTTPSettings.from_environment()

    def test_rejects_scope_override(self) -> None:
        environment = self.valid_environment()
        environment["MCP_REQUIRED_SCOPE"] = "diary:read"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(AuthConfigurationError, "music:read"):
                HTTPSettings.from_environment()

    def test_rejects_noncanonical_issuer(self) -> None:
        for issuer in (
            "http://eryu-test.us.auth0.com/",
            "https://eryu-test.us.auth0.com",
            "https://eryu-test.us.auth0.com/oauth/",
        ):
            environment = self.valid_environment()
            environment["AUTH0_ISSUER_URL"] = issuer
            with self.subTest(issuer=issuer), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(AuthConfigurationError):
                    HTTPSettings.from_environment()


class Auth0JWTVerifierTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_numbers = cls.private_key.public_key().public_numbers()
        cls.jwk = {
            "kty": "RSA",
            "kid": "test-key-1",
            "use": "sig",
            "alg": "RS256",
            "n": jwt.utils.base64url_encode(
                public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
            ).decode("ascii"),
            "e": jwt.utils.base64url_encode(
                public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")
            ).decode("ascii"),
        }

    def setUp(self) -> None:
        self.fetch_calls: list[str] = []
        self.discovery = {"issuer": ISSUER, "jwks_uri": JWKS_URI}
        self.jwks = {"keys": [deepcopy(self.jwk)]}
        self.settings = Auth0Settings(
            issuer_url=ISSUER,
            audience=RESOURCE,
            resource_url=RESOURCE,
        )

    def fetch_json(self, url: str, timeout: float, max_bytes: int):  # type: ignore[no-untyped-def]
        self.fetch_calls.append(url)
        self.assertEqual(timeout, 3.0)
        self.assertEqual(max_bytes, 262_144)
        if url.endswith("openid-configuration"):
            return deepcopy(self.discovery)
        if url == JWKS_URI:
            return deepcopy(self.jwks)
        raise AssertionError(f"unexpected metadata URL: {url}")

    def token(self, *, kid: str = "test-key-1", **overrides):  # type: ignore[no-untyped-def]
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": "auth0|test-user",
            "azp": "chatgpt-public-client",
            "iat": now - 5,
            "nbf": now - 5,
            "exp": now + 300,
            "scope": "openid music:read",
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="RS256",
            headers={"kid": kid, "typ": "at+jwt"},
        )

    def verifier(self) -> Auth0JWTVerifier:
        return Auth0JWTVerifier(self.settings, json_fetcher=self.fetch_json)

    async def test_accepts_rs256_token_and_caches_discovery_and_jwks(self) -> None:
        verifier = self.verifier()
        first = await verifier.verify_token(self.token())
        second = await verifier.verify_token(self.token())

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        self.assertEqual(first.client_id, "chatgpt-public-client")
        self.assertEqual(first.subject, "auth0|test-user")
        self.assertEqual(first.resource, RESOURCE)
        self.assertIn("music:read", first.scopes)
        self.assertEqual(len(self.fetch_calls), 2)

    async def test_rejects_wrong_issuer_audience_expiry_and_future_nbf(self) -> None:
        now = int(time.time())
        cases = {
            "issuer": self.token(iss="https://other.us.auth0.com/"),
            "audience": self.token(aud="https://other.example/mcp"),
            "expired": self.token(exp=now - 1),
            "future_nbf": self.token(nbf=now + 60),
        }
        for name, token in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(await self.verifier().verify_token(token))

    async def test_rejects_token_signed_by_untrusted_key(self) -> None:
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": RESOURCE,
                "sub": "auth0|test-user",
                "azp": "chatgpt-public-client",
                "iat": now - 5,
                "exp": now + 300,
                "scope": "music:read",
            },
            other_key,
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )
        self.assertIsNone(await self.verifier().verify_token(token))

    async def test_unknown_kid_refresh_is_throttled_but_allows_rotation(self) -> None:
        fake_now = [1_000.0]
        verifier = Auth0JWTVerifier(
            self.settings,
            json_fetcher=self.fetch_json,
            clock=lambda: fake_now[0],
        )
        rotated_token = self.token(kid="rotated-key")

        # The initial fetch is already fresh, so an unknown kid cannot trigger
        # an immediate duplicate discovery/JWKS request.
        self.assertIsNone(await verifier.verify_token(rotated_token))
        self.assertEqual(len(self.fetch_calls), 2)

        # After the small global cooldown, one refresh can observe a real key
        # rotation and authenticate the token.
        fake_now[0] += 31
        self.jwks["keys"][0]["kid"] = "rotated-key"
        self.assertIsNotNone(await verifier.verify_token(rotated_token))
        self.assertEqual(len(self.fetch_calls), 4)

        # Further random kids during the cooldown do not cause more traffic.
        self.assertIsNone(await verifier.verify_token(self.token(kid="random-attacker-key")))
        self.assertEqual(len(self.fetch_calls), 4)

    async def test_initial_discovery_or_jwks_failure_has_global_backoff(self) -> None:
        for failure_point, calls_per_attempt in (("discovery", 1), ("jwks", 2)):
            with self.subTest(failure_point=failure_point):
                fake_now = [2_000.0]
                fetch_calls: list[str] = []

                def failing_fetch(url: str, timeout: float, max_bytes: int):  # type: ignore[no-untyped-def]
                    fetch_calls.append(url)
                    if failure_point == "discovery":
                        raise ValueError("synthetic discovery outage")
                    if url.endswith("openid-configuration"):
                        return deepcopy(self.discovery)
                    raise ValueError("synthetic JWKS outage")

                verifier = Auth0JWTVerifier(
                    self.settings,
                    json_fetcher=failing_fetch,
                    clock=lambda: fake_now[0],
                )
                token = self.token()

                self.assertIsNone(await verifier.verify_token(token))
                self.assertIsNone(await verifier.verify_token(token))
                self.assertEqual(len(fetch_calls), calls_per_attempt)

                fake_now[0] += 31
                self.assertIsNone(await verifier.verify_token(token))
                self.assertEqual(len(fetch_calls), calls_per_attempt * 2)

    async def test_permissions_do_not_replace_requested_scope_claim(self) -> None:
        access = await self.verifier().verify_token(
            self.token(scope="openid", permissions=["music:read"])
        )
        self.assertIsNotNone(access)
        assert access is not None
        self.assertNotIn("music:read", access.scopes)

    async def test_fails_closed_on_discovery_mismatch_or_cross_origin_jwks(self) -> None:
        self.discovery["issuer"] = "https://other.us.auth0.com/"
        self.assertIsNone(await self.verifier().verify_token(self.token()))

        self.discovery = {"issuer": ISSUER, "jwks_uri": "https://attacker.invalid/jwks.json"}
        self.assertIsNone(await self.verifier().verify_token(self.token()))

    async def test_does_not_send_access_token_to_metadata_fetcher(self) -> None:
        incoming_token = self.token()
        await self.verifier().verify_token(incoming_token)
        self.assertTrue(self.fetch_calls)
        self.assertTrue(all(incoming_token not in value for value in self.fetch_calls))


class HTTPRouteTests(unittest.TestCase):
    def request_headers(self) -> dict[str, str]:
        return {
            "host": "eryu-mcp.95.169.17.214.sslip.io",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }

    def test_protected_resource_metadata_is_public_and_exact(self) -> None:
        app = build_http_app(
            make_http_settings(),
            client=UnusedReadClient(),
            verifier=StaticVerifier(["music:read"]),  # type: ignore[arg-type]
        )
        with TestClient(app) as client:
            response = client.get(
                "/.well-known/oauth-protected-resource",
                headers={"host": "eryu-mcp.95.169.17.214.sslip.io"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["resource"], RESOURCE)
        self.assertEqual(payload["authorization_servers"], [ISSUER])
        self.assertEqual(payload["scopes_supported"], ["music:read"])

    def test_missing_or_invalid_bearer_is_401_with_metadata_pointer(self) -> None:
        app = build_http_app(
            make_http_settings(),
            client=UnusedReadClient(),
            verifier=StaticVerifier(["music:read"]),  # type: ignore[arg-type]
        )
        with TestClient(app) as client:
            missing = client.post("/mcp", headers=self.request_headers(), json={})
            invalid_headers = self.request_headers()
            invalid_headers["authorization"] = "Bearer wrong-token"
            invalid = client.post("/mcp", headers=invalid_headers, json={})

        for response in (missing, invalid):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"], "invalid_token")
            self.assertIn(
                "https://eryu-mcp.95.169.17.214.sslip.io/.well-known/oauth-protected-resource",
                response.headers["www-authenticate"],
            )

    def test_valid_token_without_music_scope_is_403(self) -> None:
        app = build_http_app(
            make_http_settings(),
            client=UnusedReadClient(),
            verifier=StaticVerifier(["openid"]),  # type: ignore[arg-type]
        )
        headers = self.request_headers()
        headers["authorization"] = "Bearer incoming-oauth-token"
        with TestClient(app) as client:
            response = client.post("/mcp", headers=headers, json={})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "insufficient_scope")
        self.assertIn("music:read", response.json()["error_description"])

    def test_valid_auth_still_rejects_host_and_origin_mismatch(self) -> None:
        app = build_http_app(
            make_http_settings(),
            client=UnusedReadClient(),
            verifier=StaticVerifier(["music:read"]),  # type: ignore[arg-type]
        )
        headers = self.request_headers()
        headers["authorization"] = "Bearer incoming-oauth-token"
        with TestClient(app) as client:
            wrong_host = dict(headers, host="attacker.invalid")
            host_response = client.post("/mcp", headers=wrong_host, json={})
            wrong_origin = dict(headers, origin="https://attacker.invalid")
            origin_response = client.post("/mcp", headers=wrong_origin, json={})

        self.assertEqual(host_response.status_code, 421)
        self.assertEqual(origin_response.status_code, 403)

    def test_authenticated_streamable_http_handshake_and_tool_list(self) -> None:
        app = build_http_app(
            make_http_settings(),
            client=UnusedReadClient(),
            verifier=StaticVerifier(["music:read"]),  # type: ignore[arg-type]
        )
        headers = self.request_headers()
        headers["authorization"] = "Bearer incoming-oauth-token"
        with TestClient(app) as client:
            initialized = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "local-http-test", "version": "1"},
                    },
                },
            )
            session_id = initialized.headers.get("mcp-session-id")
            session_headers = {
                **headers,
                "mcp-protocol-version": "2025-11-25",
            }
            notification = client.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
            )
            listed = client.post(
                "/mcp",
                headers=session_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )

        self.assertEqual(initialized.status_code, 200)
        self.assertIsNone(session_id)
        self.assertEqual(notification.status_code, 202)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.headers["content-type"].split(";", 1)[0], "application/json")
        payload = listed.json()
        tools = payload["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "music_now_playing",
                "music_lyrics_window",
                "music_analysis",
                "music_memory",
            },
        )
        self.assertTrue(
            all(
                tool["_meta"]["securitySchemes"]
                == [{"type": "oauth2", "scopes": ["music:read"]}]
                for tool in tools
            )
        )


class HTTPToolMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_server_has_four_oauth_scoped_tools(self) -> None:
        settings = make_http_settings()
        server = build_server(
            UnusedReadClient(),
            auth=settings.auth_settings(),
            token_verifier=StaticVerifier(["music:read"]),  # type: ignore[arg-type]
        )
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()

        self.assertEqual(
            {tool.name for tool in listed.tools},
            {
                "music_now_playing",
                "music_lyrics_window",
                "music_analysis",
                "music_memory",
            },
        )
        for tool in listed.tools:
            self.assertEqual(
                tool.meta,
                {"securitySchemes": [{"type": "oauth2", "scopes": ["music:read"]}]},
            )

    async def test_stdio_server_does_not_advertise_http_oauth(self) -> None:
        server = build_server(UnusedReadClient())
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()

        self.assertTrue(all(tool.meta is None for tool in listed.tools))


if __name__ == "__main__":
    unittest.main()
