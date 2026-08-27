from __future__ import annotations

import asyncio
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
        "schemaVersion": 1,
        "clientSessionId": "test-session-a",
        "sequence": 7,
        "reportedAt": "2026-08-14T06:00:00Z",
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


class ControlledAnalysisReadClient(FakeReadClient):
    def __init__(
        self,
        presence: Mapping[str, Any],
        status: Mapping[str, Any] | Exception,
        image: bytes | None = None,
    ) -> None:
        super().__init__({})
        self.current_presence = deepcopy(presence)
        self.status = deepcopy(status)
        self.image = image
        self.status_started = asyncio.Event()
        self.status_release = asyncio.Event()
        self.image_started = asyncio.Event()
        self.image_release = asyncio.Event()
        self.recheck_started = {1: asyncio.Event(), 2: asyncio.Event()}
        self.recheck_release = {1: asyncio.Event(), 2: asyncio.Event()}
        self._presence_calls = 0

    async def get_json(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("GET", path, dict(query or {})))
        if path == "/music/presence":
            self._presence_calls += 1
            if self._presence_calls > 1:
                recheck = self._presence_calls - 1
                self.recheck_started[recheck].set()
                await self.recheck_release[recheck].wait()
            if isinstance(self.current_presence, Exception):
                raise self.current_presence
            return deepcopy(self.current_presence)
        if path == "/music/analyze/status":
            self.status_started.set()
            await self.status_release.wait()
            if isinstance(self.status, Exception):
                raise self.status
            return deepcopy(self.status)
        raise AssertionError(f"unexpected JSON GET: {path}")

    async def get_bytes(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> bytes:
        self.calls.append(("GET", path, dict(query or {})))
        if path != "/music/analyze/spectrogram" or self.image is None:
            raise AssertionError(f"unexpected bytes GET: {path}")
        self.image_started.set()
        await self.image_release.wait()
        return self.image


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

    async def call_controlled_analysis(
        self,
        fake: ControlledAnalysisReadClient,
        drive,
    ):
        server = build_server(fake)
        async with Client(server, raise_exceptions=True) as client:
            task = asyncio.create_task(client.call_tool("music_analysis", {}))
            try:
                await asyncio.wait_for(drive(fake), timeout=1.0)
                result = await asyncio.wait_for(task, timeout=1.0)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(result.is_error)
        return result

    def changed_presence(
        self,
        *,
        song_id: str = "67890",
        sequence: int = 8,
        session_id: str = "test-session-a",
    ) -> dict[str, Any]:
        payload = deepcopy(FRESH_PRESENCE)
        payload["freshness"]["receivedAt"] = "2026-08-14T06:00:01Z"
        payload["presence"]["clientSessionId"] = session_id
        payload["presence"]["sequence"] = sequence
        payload["presence"]["song"]["songId"] = song_id
        payload["presence"]["lyrics"]["songId"] = song_id
        return payload

    def ready_analysis_status(self, *, spectrogram: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "ready",
            "analysis": {
                "songId": "12345",
                "duration": 180.0,
                "bpm": 128,
                "key": "C#",
                "segments": [],
                "spectrogramAvailable": spectrogram,
            },
        }

    def assert_current_changed(self, result) -> None:
        expected = {
            "ok": True,
            "available": False,
            "state": "current_changed",
            "reason": "analysis_current_changed",
        }
        self.assertEqual(result.structured_content, expected)
        self.assertEqual([item.type for item in result.content], ["text"])
        self.assertEqual(json.loads(result.content[0].text), expected)
        serialized = json.dumps(
            {
                "structured": result.structured_content,
                "text": result.content[0].text,
            }
        )
        for forbidden in (
            '"song"',
            '"analysis"',
            '"catalog"',
            '"clientSessionId"',
            '"sequence"',
            "revision",
            "12345",
            "Test Song",
            "old-song-image",
        ):
            self.assertNotIn(forbidden, serialized)

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
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/spectrogram", {"id": "12345"}),
                ("GET", "/music/presence", {}),
            ],
        )

    async def test_analysis_fails_closed_when_song_switches_during_status(self) -> None:
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            self.ready_analysis_status(),
        )

        async def drive(controlled):
            await controlled.status_started.wait()
            controlled.current_presence = self.changed_presence()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.recheck_release[1].set()

        result = await self.call_controlled_analysis(fake, drive)

        self.assert_current_changed(result)
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/status", {"id": "12345"}),
                ("GET", "/music/presence", {}),
            ],
        )

    async def test_analysis_fails_closed_when_song_switches_after_status(self) -> None:
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            self.ready_analysis_status(),
        )

        async def drive(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.current_presence = self.changed_presence()
            controlled.recheck_release[1].set()

        result = await self.call_controlled_analysis(fake, drive)

        self.assert_current_changed(result)
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/status", {"id": "12345"}),
                ("GET", "/music/presence", {}),
            ],
        )

    async def test_analysis_revision_compares_session_and_song_id_independently(
        self,
    ) -> None:
        replacements: list[tuple[str, dict[str, Any]]] = []
        session_changed = deepcopy(FRESH_PRESENCE)
        session_changed["presence"]["clientSessionId"] = "test-session-b"
        replacements.append(("session", session_changed))

        song_changed = deepcopy(FRESH_PRESENCE)
        song_changed["presence"]["song"]["songId"] = "67890"
        song_changed["presence"]["lyrics"]["songId"] = "67890"
        replacements.append(("song", song_changed))

        for label, replacement in replacements:
            with self.subTest(field=label):
                fake = ControlledAnalysisReadClient(
                    FRESH_PRESENCE,
                    self.ready_analysis_status(),
                )

                async def drive(controlled):
                    await controlled.status_started.wait()
                    controlled.status_release.set()
                    await controlled.recheck_started[1].wait()
                    controlled.current_presence = replacement
                    controlled.recheck_release[1].set()

                result = await self.call_controlled_analysis(fake, drive)

                self.assert_current_changed(result)
                self.assertEqual(
                    fake.calls,
                    [
                        ("GET", "/music/presence", {}),
                        ("GET", "/music/analyze/status", {"id": "12345"}),
                        ("GET", "/music/presence", {}),
                    ],
                )

    async def test_analysis_discards_image_when_song_switches_after_png(self) -> None:
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            self.ready_analysis_status(spectrogram=True),
            image=b"old-song-image",
        )

        async def drive(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.recheck_release[1].set()
            await controlled.image_started.wait()
            controlled.image_release.set()
            await controlled.recheck_started[2].wait()
            controlled.current_presence = self.changed_presence()
            controlled.recheck_release[2].set()

        result = await self.call_controlled_analysis(fake, drive)

        self.assert_current_changed(result)
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/status", {"id": "12345"}),
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/spectrogram", {"id": "12345"}),
                ("GET", "/music/presence", {}),
            ],
        )

    async def test_analysis_discards_failed_image_when_revision_changes(self) -> None:
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            self.ready_analysis_status(spectrogram=True),
        )

        async def drive(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.recheck_release[1].set()
            await controlled.recheck_started[2].wait()
            controlled.current_presence = self.changed_presence()
            controlled.recheck_release[2].set()

        result = await self.call_controlled_analysis(fake, drive)

        self.assert_current_changed(result)
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/status", {"id": "12345"}),
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/spectrogram", {"id": "12345"}),
                ("GET", "/music/presence", {}),
            ],
        )

    async def test_analysis_fails_closed_when_recheck_is_stale(self) -> None:
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            {"ok": True, "status": "none"},
        )
        stale = deepcopy(FRESH_PRESENCE)
        stale["freshness"] = {
            "state": "stale",
            "ageSeconds": 11.0,
            "receivedAt": FRESH_PRESENCE["freshness"]["receivedAt"],
        }

        async def drive(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.current_presence = stale
            controlled.recheck_release[1].set()

        result = await self.call_controlled_analysis(fake, drive)

        self.assert_current_changed(result)

    async def test_analysis_fails_closed_when_recheck_is_absent_or_idle(self) -> None:
        absent = {
            "ok": True,
            "presence": None,
            "freshness": {"state": "absent", "stale": True},
        }
        idle = deepcopy(FRESH_PRESENCE)
        idle["presence"]["song"] = None

        for label, replacement in (("absent", absent), ("idle", idle)):
            with self.subTest(state=label):
                fake = ControlledAnalysisReadClient(
                    FRESH_PRESENCE,
                    {"ok": True, "status": "none"},
                )

                async def drive(controlled):
                    await controlled.status_started.wait()
                    controlled.status_release.set()
                    await controlled.recheck_started[1].wait()
                    controlled.current_presence = replacement
                    controlled.recheck_release[1].set()

                result = await self.call_controlled_analysis(fake, drive)

                self.assert_current_changed(result)
                self.assertEqual(
                    fake.calls,
                    [
                        ("GET", "/music/presence", {}),
                        ("GET", "/music/analyze/status", {"id": "12345"}),
                        ("GET", "/music/presence", {}),
                    ],
                )

    async def test_analysis_revision_includes_received_at_but_not_age_seconds(self) -> None:
        received_at_changed = deepcopy(FRESH_PRESENCE)
        received_at_changed["freshness"]["receivedAt"] = "2026-08-14T06:00:01Z"
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            {"ok": True, "status": "none"},
        )

        async def change_received_at(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.current_presence = received_at_changed
            controlled.recheck_release[1].set()

        changed_result = await self.call_controlled_analysis(fake, change_received_at)
        self.assert_current_changed(changed_result)

        age_changed = deepcopy(FRESH_PRESENCE)
        age_changed["freshness"]["ageSeconds"] = 0.9
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            {"ok": True, "status": "none"},
        )

        async def change_age_only(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.current_presence = age_changed
            controlled.recheck_release[1].set()

        unchanged_result = await self.call_controlled_analysis(fake, change_age_only)
        self.assertEqual(
            unchanged_result.structured_content,
            {
                "ok": True,
                "available": False,
                "state": "none",
                "reason": "analysis_none",
                "song": {
                    "songId": "12345",
                    "name": "Test Song",
                    "artist": "Test Artist",
                    "album": "Test Album",
                    "cover": "https://example.invalid/cover.jpg",
                },
            },
        )

    async def test_analysis_fails_closed_on_same_song_aba_revision(self) -> None:
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            self.ready_analysis_status(),
        )
        returned_a = deepcopy(FRESH_PRESENCE)
        returned_a["presence"]["sequence"] = 9

        async def drive(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.current_presence = self.changed_presence(sequence=8)
            controlled.current_presence = returned_a
            controlled.recheck_release[1].set()

        result = await self.call_controlled_analysis(fake, drive)

        self.assert_current_changed(result)

    async def test_analysis_fails_closed_when_revision_recheck_errors(self) -> None:
        fake = ControlledAnalysisReadClient(
            FRESH_PRESENCE,
            {"ok": True, "status": "none"},
        )

        async def drive(controlled):
            await controlled.status_started.wait()
            controlled.status_release.set()
            await controlled.recheck_started[1].wait()
            controlled.current_presence = EryuUnavailable("synthetic recheck failure")
            controlled.recheck_release[1].set()

        result = await self.call_controlled_analysis(fake, drive)

        self.assert_current_changed(result)

    async def test_analysis_invalid_initial_revision_stops_before_status(self) -> None:
        invalid_payloads: list[tuple[str, dict[str, Any]]] = []
        for label, parent, field in (
            ("session", "presence", "clientSessionId"),
            ("sequence", "presence", "sequence"),
            ("received_at", "freshness", "receivedAt"),
        ):
            payload = deepcopy(FRESH_PRESENCE)
            del payload[parent][field]
            invalid_payloads.append((label, payload))

        for label, value in (
            ("session_characters", "bad session"),
            ("session_length", "a" * 129),
        ):
            payload = deepcopy(FRESH_PRESENCE)
            payload["presence"]["clientSessionId"] = value
            invalid_payloads.append((label, payload))
        for label, value in (
            ("sequence_boolean", True),
            ("sequence_too_large", 2**53),
        ):
            payload = deepcopy(FRESH_PRESENCE)
            payload["presence"]["sequence"] = value
            invalid_payloads.append((label, payload))

        for label, payload in invalid_payloads:
            with self.subTest(field=label):
                fake = FakeReadClient({"/music/presence": payload})
                result = await self.call_tool(fake, "music_analysis")

                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], "analysis_unavailable")
                self.assertEqual(fake.calls, [("GET", "/music/presence", {})])

    async def test_analysis_rejects_ready_result_without_song_id(self) -> None:
        status = self.ready_analysis_status()
        del status["analysis"]["songId"]
        fake = FakeReadClient(
            {
                "/music/presence": FRESH_PRESENCE,
                "/music/analyze/status": status,
            }
        )

        result = await self.call_tool(fake, "music_analysis")

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "analysis_song_mismatch")

    async def test_analysis_rejects_non_string_result_song_id(self) -> None:
        for label, song_id in (("integer", 12345), ("other_song", "67890")):
            with self.subTest(value=label):
                status = self.ready_analysis_status()
                status["analysis"]["songId"] = song_id
                fake = FakeReadClient(
                    {
                        "/music/presence": FRESH_PRESENCE,
                        "/music/analyze/status": status,
                    }
                )

                result = await self.call_tool(fake, "music_analysis")

                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], "analysis_song_mismatch")
                self.assertEqual(
                    fake.calls,
                    [
                        ("GET", "/music/presence", {}),
                        ("GET", "/music/analyze/status", {"id": "12345"}),
                        ("GET", "/music/presence", {}),
                    ],
                )

    async def test_analysis_status_failure_is_rechecked_before_unavailable(self) -> None:
        fake = FakeReadClient(
            {
                "/music/presence": FRESH_PRESENCE,
                "/music/analyze/status": EryuUnavailable("synthetic failure"),
            }
        )

        result = await self.call_tool(fake, "music_analysis")

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "analysis_unavailable")
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/status", {"id": "12345"}),
                ("GET", "/music/presence", {}),
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
        self.assertEqual(
            fake.calls,
            [
                ("GET", "/music/presence", {}),
                ("GET", "/music/analyze/status", {"id": "12345"}),
                ("GET", "/music/presence", {}),
            ],
        )

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
                        "firstListened": "2026-08-27T09:00:00+00:00",
                        "lastListened": "2026-08-27T09:30:00+00:00",
                        "analyzed": False,
                        "_listenEventIds": ["listen:internal"],
                        "catalog": {"provider": "netease", "songId": "789"},
                        "internalSecret": "must not escape",
                    },
                },
            }
        )
        result = await self.call_tool(fake, "music_memory")
        self.assertTrue(result["available"])
        self.assertEqual(result["memory"]["notes"], "existing note")
        self.assertFalse(result["memory"]["analyzed"])
        self.assertEqual(result["memory"]["listenCount"], 4)
        self.assertEqual(
            result["memory"]["lastListened"],
            "2026-08-27T09:30:00+00:00",
        )
        self.assertNotIn("_listenEventIds", result["memory"])
        self.assertNotIn("catalog", result["memory"])
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
