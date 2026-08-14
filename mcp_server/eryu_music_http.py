#!/usr/bin/env python3
"""Authenticated Streamable HTTP entry for ChatGPT and other remote clients."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from .auth0 import Auth0JWTVerifier, Auth0Settings
from .eryu_music_mcp import EryuReadClient, ReadClient, build_server


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 9091
STREAMABLE_HTTP_PATH = "/mcp"
MAX_REQUEST_BODY_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class HTTPSettings:
    """Listener and public OAuth settings for the reverse-proxied MCP server."""

    host: str
    port: int
    auth0: Auth0Settings

    @classmethod
    def from_environment(cls) -> "HTTPSettings":
        host = os.environ.get("MCP_HTTP_HOST", DEFAULT_HTTP_HOST)
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("MCP_HTTP_HOST must be a numeric loopback address")
        raw_port = os.environ.get("MCP_HTTP_PORT", str(DEFAULT_HTTP_PORT))
        if not raw_port.isascii() or not raw_port.isdigit() or raw_port != str(int(raw_port)):
            raise ValueError("MCP_HTTP_PORT must be a canonical decimal integer")
        port = int(raw_port)
        if not 1024 <= port <= 65535:
            raise ValueError("MCP_HTTP_PORT must be between 1024 and 65535")
        return cls(host=host, port=port, auth0=Auth0Settings.from_environment())

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.auth0.issuer_url,
            resource_server_url=self.auth0.resource_url,
            required_scopes=[self.auth0.required_scope],
        )

    def transport_security(self) -> TransportSecuritySettings:
        public = urllib.parse.urlsplit(self.auth0.resource_url)
        public_origin = f"{public.scheme}://{public.netloc}"
        public_host = public.netloc
        local_host = f"[{self.host}]" if ":" in self.host else self.host
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                public_host,
                f"{public_host}:443",
                f"{local_host}:{self.port}",
            ],
            allowed_origins=[public_origin, "https://chatgpt.com"],
        )


def build_http_app(
    settings: HTTPSettings,
    *,
    client: ReadClient | None = None,
    verifier: Auth0JWTVerifier | None = None,
):
    """Build the ASGI app without opening a socket, primarily for local tests."""

    token_verifier = verifier if verifier is not None else Auth0JWTVerifier(settings.auth0)
    read_client = client if client is not None else EryuReadClient.from_environment()
    server = build_server(
        read_client,
        auth=settings.auth_settings(),
        token_verifier=token_verifier,
    )
    return server.streamable_http_app(
        streamable_http_path=STREAMABLE_HTTP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        transport_security=settings.transport_security(),
        host=settings.host,
    )


def main() -> None:
    """Run behind an HTTPS reverse proxy; the listener itself remains loopback-only."""

    settings = HTTPSettings.from_environment()
    verifier = Auth0JWTVerifier(settings.auth0)
    server = build_server(
        auth=settings.auth_settings(),
        token_verifier=verifier,
    )
    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=STREAMABLE_HTTP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        transport_security=settings.transport_security(),
    )


if __name__ == "__main__":
    main()


__all__ = ["HTTPSettings", "build_http_app", "main"]
