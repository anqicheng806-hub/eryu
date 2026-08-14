from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from mcp import Client, StdioServerParameters, stdio_client

from mcp_server.eryu_music_mcp import EryuReadClient, EryuUnavailable, build_server


FRESH_PRESENCE = {
    "ok": True,
    "freshness": {
        "state": "fresh",
        "ageSeconds": 0.4,
        "staleAfterSeconds": 10,
        "receivedAt": "2026-08-14T06:00:00Z",
    },
    "presence": {
        "song": {
            "songId": "12345",
            "name": "Test Song",
            "artist": "Test Artist",
            "album": "Test Album",
            "cover": "https://example.invalid/cover.jpg",
        },
        "playback": {
            "status": "playing",
            "playing": True,
            "positionSeconds": 75.0,
            "durationSeconds": 180.0,
            "progressRatio": 75 / 180,
        },
        "lyrics": {
            "songId": "12345",
            "status": "ready",
            "currentIndex": 2,
            "current": {
                "index": 2,
                "timeSeconds": 70.0,
                "text": "current line",
                "translation": "current translation",
            },
            "previous": [
                {
                    "index": 1,
                    "timeSeconds": 60.0,
                    "text": "previous line",
                    "translation": "",
                },
            ],
            "next": [
                {
                    "index": 3,
                    "timeSeconds": 80.0,
                    "text": "next line",
                    "translation": "",
                },
            ],
        },
    },
}

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class FakeReadClient:
    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def get_json(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("GET", path, dict(query or {})))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    async def get_bytes(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> bytes:
        self.calls.append(("GET", path, dict(query or {})))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, bytes):
            raise TypeError("fake binary response must be bytes")
        return response


class MusicMcpTests(unittest.IsolatedAsyncioTestCase):
    async def call_tool_result(self, fake: FakeReadClient, name: str):
        server = build_server(fake)
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool(name, {})
        self.assertFalse(result.is_error)
        return result

    async def call_tool(self, fake: FakeReadClient, name: str) -> dict[str, Any]:
        result = await self.call_tool_result(fake, name)
        self.assertIsInstance(result.structured_content, dict)
        return result.structured_content

    async def test_tools_list_has_exactly_four_parameterless_read_only_tools(self) -> None:
        fake = FakeReadClient({"/music/presence": FRESH_PRESENCE})
        server = build_server(fake)
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()

        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(
            set(tools),
            {
                "music_now_playing",
                "music_lyrics_window",
                "music_analysis",
                "music_memory",
            },
        )
        for tool in tools.values():
            self.assertIsNotNone(tool.annotations)
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertEqual(tool.input_schema.get("properties", {}), {})
            self.assertEqual(tool.input_schema.get("required", []), [])

    async def test_absent_presence_fails_closed(self) -> None:
        fake = FakeReadClient(
            {
                "/music/presence": {
                    "ok": True,
                    "presence": None,
                    "freshness": {"state": "absent", "stale": True},
                }
            }
        )
        result = await self.call_tool(fake, "music_now_playing")
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "absent")
        self.assertEqual(fake.calls, [("GET", "/music/presence", {})])

    async def test_playback_statuses_are_not_flattened_to_paused(self) -> None:
        for status in ("loading", "paused", "ended", "error"):
            with self.subTest(status=status):
                payload = deepcopy(FRESH_PRESENCE)
                payload["presence"]["playback"].update(
                    {"status": status, "playing": False}
                )
                result = await self.call_tool(
                    FakeReadClient({"/music/presence": payload}),
                    "music_now_playing",
                )
                self.assertEqual(result["state"], status)
                self.assertEqual(result["playbackStatus"], status)

    async def test_stale_presence_short_circuits_analysis(self) -> None:
        stale = deepcopy(FRESH_PRESENCE)
        stale["freshness"] = {"state": "stale", "ageSeconds": 11.0}
        fake = FakeReadClient({"/music/presence": stale})
        result = await self.call_tool(fake, "music_analysis")
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "stale")
        self.assertEqual(fake.calls, [("GET", "/music/presence", {})])

    async def test_lyrics_window_uses_presence_only(self) -> None:
        fake = FakeReadClient({"/music/presence": FRESH_PRESENCE})
        result = await self.call_tool(fake, "music_lyrics_window")
        self.assertTrue(result["available"])
        self.assertEqual(result["currentLyric"]["text"], "current line")
        self.assertEqual(len(result["nearbyLyrics"]), 3)
        self.assertEqual(fake.calls, [("GET", "/music/presence", {})])

    async def test_lyrics_window_preserves_loading_none_and_error(self) -> None:
        for status in ("loading", "none", "error"):
            with self.subTest(status=status):
                payload = deepcopy(FRESH_PRESENCE)
                payload["presence"]["lyrics"].update(
                    {
                        "status": status,
                        "current": None,
                        "previous": [],
                        "next": [],
                    }
                )
                result = await self.call_tool(
                    FakeReadClient({"/music/presence": payload}),
                    "music_lyrics_window",
                )
                self.assertFalse(result["available"])
                self.assertEqual(result["state"], status)

    async def test_analysis_reads_existing_result_and_selects_energy_segment(self) -> None:
        absolute_spectrogram = r"C:\private\server\data\12345_analysis.png"
        fake = FakeReadClient(
            {
                "/music/presence": FRESH_PRESENCE,
                "/music/analyze/status": {
                    "ok": True,
                    "status": "ready",
                    "analysis": {
                        "songId": "12345",
                        "duration": 180.0,
                        "bpm": 128,
                        "key": "C#",
                        "segments": [
                            {"start": 0, "end": 60, "avgEnergy": 0.1, "maxEnergy": 0.2},
                            {"start": 60, "end": 120, "avgEnergy": 0.3, "maxEnergy": 0.5},
                            {"start": 120, "end": 180, "avgEnergy": 0.2, "maxEnergy": 0.4},
                        ],
                        "spectrogram": absolute_spectrogram,
                    },
                },
                "/music/analyze/spectrogram": b"synthetic-png-bytes",
            }
        )
        tool_result = await self.call_tool_result(fake, "music_analysis")
        result = tool_result.structured_content
        self.assertIsInstance(result, dict)
        self.assertTrue(result["available"])
        self.assertEqual(result["analysis"]["bpm"], 128.0)
        self.assertEqual(result["analysis"]["currentEnergySegment"]["avgEnergy"], 0.3)
        self.assertTrue(result["analysis"]["spectrogramAvailable"])
        self.assertTrue(result["analysis"]["spectrogramIncluded"])
        self.assertEqual([item.type for item in tool_result.content], ["text", "image"])
        self.assertEqual(tool_result.content[1].mime_type, "image/png")
        self.assertNotIn(absolute_spectrogram, json.dumps(result))
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/status", {"id": "12345"}),
                ("GET", "/music/analyze/spectrogram", {"id": "12345"}),
            ],
        )

    async def test_analysis_error_is_sanitized_and_never_triggers_analysis(self) -> None:
        raw_error = "error: /private/path decoder failed"
        fake = FakeReadClient(
            {
                "/music/presence": FRESH_PRESENCE,
                "/music/analyze/status": {"ok": True, "status": raw_error},
            }
        )
        result = await self.call_tool(fake, "music_analysis")
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "error")
        self.assertNotIn(raw_error, json.dumps(result))
        self.assertEqual(fake.calls[-1], ("GET", "/music/analyze/status", {"id": "12345"}))

    async def test_analysis_accepts_sanitized_spectrogram_availability(self) -> None:
        fake = FakeReadClient(
            {
                "/music/presence": FRESH_PRESENCE,
                "/music/analyze/status": {
                    "ok": True,
                    "status": "ready",
                    "analysis": {
                        "songId": "12345",
                        "segments": [],
                        "spectrogramAvailable": True,
                    },
                },
                "/music/analyze/spectrogram": EryuUnavailable("not found"),
            }
        )
        result = await self.call_tool(fake, "music_analysis")
        self.assertTrue(result["analysis"]["spectrogramAvailable"])
        self.assertFalse(result["analysis"]["spectrogramIncluded"])
        self.assertNotIn("spectrogram", result["analysis"])

    async def test_memory_reads_only_current_song_with_id_and_sanitizes_fields(self) -> None:
        fake = FakeReadClient(
            {
                "/music/presence": FRESH_PRESENCE,
                "/music/memory": {
                    "ok": True,
                    "memory": {
                        "songId": 12345,
                        "name": "Test Song",
                        "notes": "existing note",
                        "feeling": "calm",
                        "favoriteLines": ["one line"],
                        "tags": ["night"],
                        "listenCount": 4,
                        "togetherCount": 2,
                        "analyzed": False,
                        "internalSecret": "must not escape",
                    },
                },
            }
        )
        result = await self.call_tool(fake, "music_memory")
        self.assertTrue(result["available"])
        self.assertEqual(result["memory"]["notes"], "existing note")
        self.assertFalse(result["memory"]["analyzed"])
        self.assertNotIn("internalSecret", result["memory"])
        self.assertNotIn("must not escape", json.dumps(result))
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/memory", {"id": "12345"}),
            ],
        )


class ReadClientSecurityTests(unittest.TestCase):
    def test_token_must_be_strong_and_contain_no_whitespace(self) -> None:
        for token in ("", "x" * 31, "x" * 31 + " ", "x" * 16 + "\n" + "y" * 16):
            with self.subTest(token_length=len(token)):
                with self.assertRaises(EryuUnavailable):
                    EryuReadClient("http://127.0.0.1:9090", token)

    def test_plain_http_is_loopback_only_and_base_url_cannot_hold_credentials(self) -> None:
        token = "t" * 32
        with self.assertRaises(EryuUnavailable):
            EryuReadClient("http://example.com:9090", token)
        with self.assertRaises(EryuUnavailable):
            EryuReadClient("https://user:secret@example.com", token)
        self.assertEqual(
            EryuReadClient("https://music.example.com", token).base_url,
            "https://music.example.com",
        )

    def test_get_client_rejects_secret_or_unexpected_query_parameters(self) -> None:
        client = EryuReadClient("http://127.0.0.1:9090", "t" * 32)
        with self.assertRaises(EryuUnavailable):
            client._get_json_sync("/music/presence", {"token": "must-not-enter-url"})
        with self.assertRaises(EryuUnavailable):
            client._get_json_sync("/music/memory", {})
        with self.assertRaises(EryuUnavailable):
            client._get_json_sync("/music/analyze/status", {"id": "../secret"})
        with self.assertRaises(EryuUnavailable):
            client._get_bytes_sync("/music/analyze/spectrogram", {"id": "../secret"})

    def test_real_client_uses_get_and_keeps_token_out_of_url(self) -> None:
        token = "t" * 32
        client = EryuReadClient("http://127.0.0.1:9090", token)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true, "presence": null}'
        with patch.object(client._opener, "open", return_value=response) as urlopen:
            client._get_json_sync("/music/presence", None)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertNotIn(token, request.full_url)
        self.assertEqual(request.get_header("X-auth-token"), token)

    def test_real_client_reads_only_png_from_authenticated_endpoint(self) -> None:
        token = "t" * 32
        client = EryuReadClient("http://127.0.0.1:9090", token)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"synthetic-png"
        response.headers.get_content_type.return_value = "image/png"
        with patch.object(client._opener, "open", return_value=response) as urlopen:
            raw = client._get_bytes_sync(
                "/music/analyze/spectrogram", {"id": "12345"}
            )

        self.assertEqual(raw, b"synthetic-png")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertNotIn(token, request.full_url)
        self.assertEqual(request.get_header("X-auth-token"), token)

    def test_internal_client_refuses_every_http_redirect(self) -> None:
        client = EryuReadClient("http://127.0.0.1:9090", "t" * 32)
        redirect_handlers = [
            handler
            for handler in client._opener.handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        request = urllib.request.Request(
            "http://127.0.0.1:9090/music/presence",
            headers={"X-Auth-Token": "t" * 32},
        )
        self.assertIsNone(
            redirect_handlers[0].redirect_request(
                request,
                None,
                302,
                "Found",
                {"Location": "https://attacker.invalid/"},
                "https://attacker.invalid/",
            )
        )


class MusicMcpStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_subprocess_handshake_and_tool_list(self) -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(REPOSITORY_ROOT / "mcp_server" / "eryu_music_mcp.py")],
            cwd=REPOSITORY_ROOT,
            env={
                "ERYU_MCP_READ_TOKEN": "t" * 32,
                "ERYU_BASE_URL": "http://127.0.0.1:9090",
            },
        )
        async with Client(
            stdio_client(parameters),
            raise_exceptions=True,
            read_timeout_seconds=10,
        ) as client:
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

if __name__ == "__main__":
    unittest.main()
