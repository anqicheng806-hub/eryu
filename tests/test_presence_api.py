from __future__ import annotations

import copy
import http.client
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from server.eryu import (
    EryuHandler,
    MAX_JSON_NESTING_DEPTH,
    ServerState,
    ThreadingHTTPServer,
    _parse_allowed_origin,
    _load_server_port,
    _parse_data_dir,
    _parse_server_host,
    _parse_server_port,
)
from server.presence import PresenceValidationError, parse_presence_ttl, validate_presence_payload


FULL_TOKEN = "full-access-token-for-tests-only-000001"
READ_TOKEN = "read-only-token-for-tests-only-000002"


class MutableClock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def lyric_line(index: int, text: str = "line") -> dict:
    return {
        "index": index,
        "timeSeconds": float(index * 5),
        "text": text,
        "translation": "",
    }


def playing_presence() -> dict:
    return {
        "schemaVersion": 1,
        "clientSessionId": "test-session-1",
        "sequence": 7,
        "reportedAt": "2026-08-14T06:00:00.000Z",
        "song": {
            "songId": 123456,
            "name": "Test Song",
            "artist": "Test Artist",
            "album": "Test Album",
            "cover": "https://example.invalid/cover.jpg",
        },
        "playback": {
            "status": "playing",
            "playing": True,
            "positionSeconds": 12.5,
            "durationSeconds": 240,
            "progressRatio": 12.5 / 240,
        },
        "lyrics": {
            "songId": 123456,
            "status": "ready",
            "currentIndex": 1,
            "current": lyric_line(1, "current"),
            "previous": [lyric_line(0, "previous")],
            "next": [lyric_line(2, "next")],
        },
    }


def idle_presence() -> dict:
    return {
        "schemaVersion": 1,
        "clientSessionId": "test-session-idle",
        "sequence": 0,
        "reportedAt": "2026-08-14T06:00:00Z",
        "song": None,
        "playback": {
            "status": "idle",
            "playing": False,
            "positionSeconds": 0,
            "durationSeconds": 0,
            "progressRatio": 0,
        },
        "lyrics": {
            "songId": None,
            "status": "idle",
            "currentIndex": -1,
            "current": None,
            "previous": [],
            "next": [],
        },
    }


def playing_presence_for_song(
    song_id: int,
    *,
    session_id: str = "test-current-song",
    sequence: int = 1,
) -> dict:
    payload = playing_presence()
    payload["clientSessionId"] = session_id
    payload["sequence"] = sequence
    payload["song"]["songId"] = song_id
    payload["lyrics"]["songId"] = song_id
    return payload


def playing_presence_v2(
    public_song_id: int,
    catalog_song_id: str | None,
    *,
    session_id: str = "test-current-song-v2",
    sequence: int = 1,
) -> dict:
    payload = playing_presence_for_song(
        public_song_id,
        session_id=session_id,
        sequence=sequence,
    )
    payload["schemaVersion"] = 2
    payload["song"]["catalog"] = (
        None
        if catalog_song_id is None
        else {"provider": "netease", "songId": catalog_song_id}
    )
    return payload


class PresenceApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ERYU_AUTH_TOKEN": FULL_TOKEN,
                "ERYU_MCP_READ_TOKEN": READ_TOKEN,
                "MUSIC_PRESENCE_TTL_SECONDS": "10",
            },
            clear=False,
        )
        self.environment.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.state = ServerState(
            0,
            data_dir=Path(self.temp_dir.name),
            presence_clock=self.clock,
            presence_utcnow=lambda: datetime(2026, 8, 14, 6, 30, tzinfo=timezone.utc),
        )
        EryuHandler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), EryuHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()
        self.environment.stop()

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: object | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], dict]:
        headers = dict(extra_headers or {})
        if token is not None:
            headers["X-Auth-Token"] = token
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = content_type
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            parsed = json.loads(response_body) if response_body else {}
            return response.status, dict(response.getheaders()), parsed
        finally:
            connection.close()

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = dict(extra_headers or {})
        if token is not None:
            headers["X-Auth-Token"] = token
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_health_is_generic_plain_text_without_server_metadata(self) -> None:
        status, headers, body = self.request_bytes("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(headers.get("Content-Length"), "2")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertNotIn("Server", headers)
        self.assertNotIn("Date", headers)
        self.assertNotIn(b"version", body.lower())
        self.assertNotIn(b"service", body.lower())

    def test_absent_post_fresh_and_stale_response_contract(self) -> None:
        status, headers, body = self.request("GET", "/music/presence", token=READ_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(
            body,
            {
                "ok": True,
                "presence": None,
                "freshness": {
                    "state": "absent",
                    "stale": True,
                    "ageSeconds": None,
                    "staleAfterSeconds": 10.0,
                    "receivedAt": None,
                },
            },
        )

        status, headers, body = self.request(
            "POST", "/music/presence", token=FULL_TOKEN, payload=playing_presence()
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(set(body), {"ok", "presence", "freshness"})
        self.assertEqual(body["presence"]["song"]["songId"], "123456")
        self.assertEqual(body["presence"]["lyrics"]["songId"], "123456")
        self.assertEqual(body["freshness"]["state"], "fresh")
        self.assertFalse(body["freshness"]["stale"])
        self.assertEqual(body["freshness"]["ageSeconds"], 0.0)
        self.assertEqual(body["freshness"]["receivedAt"], "2026-08-14T06:30:00Z")

        self.clock.advance(9.999)
        body = self.request("GET", "/music/presence", token=READ_TOKEN)[2]
        self.assertEqual(body["freshness"]["state"], "fresh")
        self.clock.advance(0.001)
        body = self.request("GET", "/music/presence", token=READ_TOKEN)[2]
        self.assertEqual(body["freshness"]["state"], "stale")
        self.assertTrue(body["freshness"]["stale"])
        self.assertEqual(body["freshness"]["ageSeconds"], 10.0)

    def test_idle_null_song_and_minus_one_current_index_are_valid(self) -> None:
        status, _, body = self.request(
            "POST", "/music/presence", token=FULL_TOKEN, payload=idle_presence()
        )
        self.assertEqual(status, 200)
        self.assertIsNone(body["presence"]["song"])
        self.assertEqual(body["presence"]["lyrics"]["currentIndex"], -1)
        self.assertIsNone(body["presence"]["lyrics"]["current"])

    def test_presence_v2_requires_and_normalizes_catalog_without_changing_v1(self) -> None:
        v1 = validate_presence_payload(playing_presence())
        self.assertEqual(v1["schemaVersion"], 1)
        self.assertNotIn("catalog", v1["song"])

        for catalog_song_id in ("789", None):
            with self.subTest(catalog_song_id=catalog_song_id):
                normalized = validate_presence_payload(
                    playing_presence_v2(123, catalog_song_id)
                )
                self.assertEqual(normalized["schemaVersion"], 2)
                self.assertEqual(normalized["song"]["songId"], "123")
                expected = (
                    None
                    if catalog_song_id is None
                    else {"provider": "netease", "songId": catalog_song_id}
                )
                self.assertEqual(normalized["song"]["catalog"], expected)

        idle_v2 = idle_presence()
        idle_v2["schemaVersion"] = 2
        self.assertIsNone(validate_presence_payload(idle_v2)["song"])

    def test_presence_versions_reject_cross_version_or_invalid_catalog_shapes(self) -> None:
        invalid_payloads = []

        v1_with_catalog = playing_presence()
        v1_with_catalog["song"]["catalog"] = None
        invalid_payloads.append(v1_with_catalog)

        v2_without_catalog = playing_presence()
        v2_without_catalog["schemaVersion"] = 2
        invalid_payloads.append(v2_without_catalog)

        for catalog in (
            {},
            {"provider": "spotify", "songId": "789"},
            {"provider": "netease", "songId": 789},
            {"provider": "netease", "songId": "0"},
            {"provider": "netease", "songId": "00789"},
            {"provider": "netease", "songId": "../789"},
            {"provider": "netease", "songId": "789", "extra": True},
        ):
            payload = playing_presence_v2(123, None)
            payload["song"]["catalog"] = catalog
            invalid_payloads.append(payload)

        unsupported_version = playing_presence()
        unsupported_version["schemaVersion"] = 3
        invalid_payloads.append(unsupported_version)

        non_integer_version = playing_presence()
        non_integer_version["schemaVersion"] = 2.0
        invalid_payloads.append(non_integer_version)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(PresenceValidationError):
                    validate_presence_payload(payload)

    def test_sequence_must_increase_per_session_without_refreshing_rejections(self) -> None:
        initial = playing_presence()
        self.assertEqual(
            self.request("POST", "/music/presence", token=FULL_TOKEN, payload=initial)[0],
            200,
        )

        self.clock.advance(3)
        for rejected_sequence in (7, 6):
            replay = playing_presence()
            replay["sequence"] = rejected_sequence
            status, _, body = self.request(
                "POST", "/music/presence", token=FULL_TOKEN, payload=replay
            )
            self.assertEqual(status, 409)
            self.assertEqual(
                body,
                {"ok": False, "error": "presence sequence must strictly increase"},
            )

        current = self.request("GET", "/music/presence", token=READ_TOKEN)[2]
        self.assertEqual(current["presence"]["sequence"], 7)
        self.assertEqual(current["freshness"]["ageSeconds"], 3.0)

        replacement_session = playing_presence()
        replacement_session["clientSessionId"] = "replacement-session"
        replacement_session["sequence"] = 0
        self.assertEqual(
            self.request(
                "POST", "/music/presence", token=FULL_TOKEN, payload=replacement_session
            )[0],
            200,
        )

        # Remember prior sessions too: switching sessions must not permit a replay.
        replay = playing_presence()
        replay["sequence"] = 7
        self.assertEqual(
            self.request("POST", "/music/presence", token=FULL_TOKEN, payload=replay)[0],
            409,
        )
        current = self.request("GET", "/music/presence", token=READ_TOKEN)[2]
        self.assertEqual(current["presence"]["clientSessionId"], "replacement-session")

    def test_auth_is_header_only_and_logs_redact_query(self) -> None:
        marker = "query-token-must-not-appear-in-logs"
        with self.assertLogs("eryu", level=logging.INFO) as captured:
            status, _, _ = self.request(
                "GET", f"/music/presence?token={marker}"
            )
        self.assertEqual(status, 403)
        self.assertNotIn(marker, "\n".join(captured.output))

        status, _, _ = self.request(
            "GET",
            "/music/presence",
            extra_headers={"X-Auth": FULL_TOKEN},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", "/music/presence")[0], 403)
        self.assertEqual(
            self.request("GET", "/music/presence", token="wrong-token")[0], 403
        )

    def test_mcp_token_is_limited_to_four_fresh_current_song_read_routes(self) -> None:
        self.assertEqual(self.request("GET", "/music/presence", token=READ_TOKEN)[0], 200)
        for path in (
            "/music/analyze/status?id=123",
            "/music/analyze/spectrogram?id=123",
            "/music/memory?id=123",
        ):
            with self.subTest(path=path, state="absent"):
                self.assertEqual(self.request("GET", path, token=READ_TOKEN)[0], 403)

        self.assertEqual(
            self.request(
                "POST",
                "/music/presence",
                token=FULL_TOKEN,
                payload=playing_presence_for_song(123),
            )[0],
            200,
        )
        self.assertEqual(
            self.request("GET", "/music/analyze/status?id=123", token=READ_TOKEN)[0], 200
        )
        self.assertEqual(
            self.request("GET", "/music/analyze/spectrogram?id=123", token=READ_TOKEN)[0], 404
        )
        self.assertEqual(
            self.request("GET", "/music/memory?id=123", token=READ_TOKEN)[0], 200
        )
        for path in (
            "/music/analyze/status?id=124",
            "/music/analyze/spectrogram?id=124",
            "/music/memory?id=124",
        ):
            with self.subTest(path=path, state="wrong_song"):
                self.assertEqual(self.request("GET", path, token=READ_TOKEN)[0], 403)
        self.assertEqual(self.request("GET", "/music/memory", token=READ_TOKEN)[0], 403)
        self.assertEqual(
            self.request("GET", "/music/memory?id=not-numeric", token=READ_TOKEN)[0], 403
        )
        self.assertEqual(self.request("GET", "/music/search?q=x", token=READ_TOKEN)[0], 403)
        self.assertEqual(
            self.request("POST", "/music/presence", token=READ_TOKEN, payload=idle_presence())[0],
            403,
        )

        self.clock.advance(10)
        for path in (
            "/music/analyze/status?id=123",
            "/music/analyze/spectrogram?id=123",
            "/music/memory?id=123",
        ):
            with self.subTest(path=path, state="stale"):
                self.assertEqual(self.request("GET", path, token=READ_TOKEN)[0], 403)

    def test_v2_mcp_analysis_uses_catalog_cache_and_rebinds_public_song_id(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        provider_song_id = "789"
        public_song_id = 123
        (cache_dir / f"{provider_song_id}_preanalysis.json").write_text(
            json.dumps(
                {
                    "songId": provider_song_id,
                    "name": "Mapped Song",
                    "artist": "Mapped Artist",
                    "duration": 180,
                    "bpm": 128,
                    "key": "C#",
                    "segments": [],
                }
            ),
            encoding="utf-8",
        )
        image = b"\x89PNG\r\n\x1a\nmapped-image"
        (cache_dir / f"{provider_song_id}_analysis.png").write_bytes(image)
        self.assertEqual(
            self.request(
                "POST",
                "/music/presence",
                token=FULL_TOKEN,
                payload=playing_presence_v2(public_song_id, provider_song_id),
            )[0],
            200,
        )

        status, _, body = self.request(
            "GET",
            f"/music/analyze/status?id={public_song_id}",
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["analysis"]["songId"], str(public_song_id))
        self.assertTrue(body["analysis"]["spectrogramAvailable"])
        self.assertNotIn(provider_song_id, json.dumps(body))

        status, _, response_image = self.request_bytes(
            "GET",
            f"/music/analyze/spectrogram?id={public_song_id}",
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_image, image)

        full_status = self.request(
            "GET",
            f"/music/analyze/status?id={provider_song_id}",
            token=FULL_TOKEN,
        )[2]
        self.assertEqual(full_status["status"], "ready")
        self.assertEqual(full_status["analysis"]["songId"], provider_song_id)
        self.assertEqual(
            self.request(
                "GET",
                f"/music/analyze/status?id={public_song_id}",
                token=FULL_TOKEN,
            )[2],
            {"ok": True, "status": "none"},
        )

    def test_v2_null_catalog_hides_public_id_analysis_from_mcp_read(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        public_song_id = "123"
        (cache_dir / f"{public_song_id}_preanalysis.json").write_text(
            json.dumps({"songId": public_song_id, "segments": []}),
            encoding="utf-8",
        )
        image = b"\x89PNG\r\n\x1a\npublic-id-image"
        (cache_dir / f"{public_song_id}_analysis.png").write_bytes(image)
        self.assertEqual(
            self.request(
                "POST",
                "/music/presence",
                token=FULL_TOKEN,
                payload=playing_presence_v2(int(public_song_id), None),
            )[0],
            200,
        )

        self.assertEqual(
            self.request(
                "GET",
                f"/music/analyze/status?id={public_song_id}",
                token=READ_TOKEN,
            )[2],
            {"ok": True, "status": "none"},
        )
        status, _, body = self.request(
            "GET",
            f"/music/analyze/spectrogram?id={public_song_id}",
            token=READ_TOKEN,
        )
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "spectrogram not found"})

        self.assertEqual(
            self.request(
                "GET",
                f"/music/analyze/status?id={public_song_id}",
                token=FULL_TOKEN,
            )[2]["status"],
            "ready",
        )
        self.assertEqual(
            self.request_bytes(
                "GET",
                f"/music/analyze/spectrogram?id={public_song_id}",
                token=FULL_TOKEN,
            )[2],
            image,
        )

    def test_analysis_result_song_id_must_match_the_resolved_cache_id(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        (cache_dir / "789_preanalysis.json").write_text(
            json.dumps({"songId": "790", "bpm": 120, "segments": []}),
            encoding="utf-8",
        )
        self.assertEqual(
            self.request(
                "POST",
                "/music/presence",
                token=FULL_TOKEN,
                payload=playing_presence_v2(123, "789"),
            )[0],
            200,
        )

        for token, request_song_id in ((READ_TOKEN, "123"), (FULL_TOKEN, "789")):
            with self.subTest(token=token, request_song_id=request_song_id):
                status, _, body = self.request(
                    "GET",
                    f"/music/analyze/status?id={request_song_id}",
                    token=token,
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, {"ok": True, "status": "error"})
                self.assertNotIn("790", json.dumps(body))

    def test_mcp_analysis_rechecks_fresh_current_song_after_auth(self) -> None:
        self.state.presence.update(
            playing_presence_v2(123, "789", session_id="race-old")
        )
        old_current = self.state.presence.read()
        self.state.presence.update(
            playing_presence_v2(124, "790", session_id="race-new")
        )
        new_current = self.state.presence.read()

        for path in (
            "/music/analyze/status?id=123",
            "/music/analyze/spectrogram?id=123",
        ):
            with self.subTest(path=path):
                with mock.patch.object(
                    self.state.presence,
                    "read",
                    side_effect=[old_current, new_current],
                ) as read:
                    status, _, body = self.request("GET", path, token=READ_TOKEN)
                self.assertEqual(read.call_count, 2)
                self.assertEqual(status, 403)
                self.assertEqual(body, {"error": "auth required"})

    def test_invalid_presence_payloads_are_rejected(self) -> None:
        cases = []

        unknown = playing_presence()
        unknown["control"] = {"pause": True}
        cases.append(unknown)

        invalid_status = playing_presence()
        invalid_status["playback"]["status"] = "seeking"
        cases.append(invalid_status)

        conflicting_playing = playing_presence()
        conflicting_playing["playback"]["playing"] = False
        cases.append(conflicting_playing)

        invalid_number = playing_presence()
        invalid_number["playback"]["positionSeconds"] = math.nan
        cases.append(invalid_number)

        overflowing_number = playing_presence()
        overflowing_number["playback"]["positionSeconds"] = 10**400
        cases.append(overflowing_number)

        mismatched_song = playing_presence()
        mismatched_song["lyrics"]["songId"] = 999
        cases.append(mismatched_song)

        null_with_index = idle_presence()
        null_with_index["lyrics"]["currentIndex"] = 0
        cases.append(null_with_index)

        too_many_lines = playing_presence()
        too_many_lines["lyrics"]["next"] = [lyric_line(i + 2) for i in range(6)]
        cases.append(too_many_lines)

        for payload in cases:
            with self.subTest(payload=payload):
                status, _, body = self.request(
                    "POST", "/music/presence", token=FULL_TOKEN, payload=payload
                )
                self.assertEqual(status, 400)
                self.assertEqual(body, {"ok": False, "error": "invalid presence payload"})

    def test_json_errors_content_type_and_body_limit_are_safe(self) -> None:
        self.assertEqual(
            self.request(
                "POST", "/music/presence", token=FULL_TOKEN, raw_body=b"{"
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "POST", "/music/presence", token=FULL_TOKEN, payload=["not", "an", "object"]
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/music/presence",
                token=FULL_TOKEN,
                raw_body=b"{}",
                content_type="text/plain",
            )[0],
            415,
        )
        # Declare an oversized body without transmitting it. The handler must
        # reject from Content-Length before attempting a read.
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        try:
            connection.putrequest("POST", "/music/presence")
            connection.putheader("X-Auth-Token", FULL_TOKEN)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(64 * 1024 + 1))
            connection.endheaders()
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 413)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
        finally:
            connection.close()

        self.assertEqual(MAX_JSON_NESTING_DEPTH, 64)
        at_limit_objects = (
            '{"a":' * MAX_JSON_NESTING_DEPTH
            + "0"
            + "}" * MAX_JSON_NESTING_DEPTH
        ).encode("utf-8")
        at_limit_arrays = (
            b'{"a":'
            + b"[" * (MAX_JSON_NESTING_DEPTH - 1)
            + b"0"
            + b"]" * (MAX_JSON_NESTING_DEPTH - 1)
            + b"}"
        )
        quoted_delimiters = json.dumps(
            {"a": '\\"{}[]' * (MAX_JSON_NESTING_DEPTH + 1)}
        ).encode("utf-8")
        utf16_delimiters = json.dumps(
            {"a": '\\"{}[]' * (MAX_JSON_NESTING_DEPTH + 1)}
        ).encode("utf-16")
        for raw_body in (
            at_limit_objects,
            at_limit_arrays,
            quoted_delimiters,
            utf16_delimiters,
        ):
            with self.subTest(boundary="allowed", raw_body=raw_body[:32]):
                status, _, body = self.request(
                    "POST",
                    "/music/presence",
                    token=FULL_TOKEN,
                    raw_body=raw_body,
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    body, {"ok": False, "error": "invalid presence payload"}
                )

        too_deep_objects = (
            '{"a":' * (MAX_JSON_NESTING_DEPTH + 1)
            + "0"
            + "}" * (MAX_JSON_NESTING_DEPTH + 1)
        ).encode("utf-8")
        too_deep_arrays = (
            b'{"a":'
            + b"[" * MAX_JSON_NESTING_DEPTH
            + b"0"
            + b"]" * MAX_JSON_NESTING_DEPTH
            + b"}"
        )
        for raw_body in (too_deep_objects, too_deep_arrays):
            with self.subTest(boundary="rejected", raw_body=raw_body[:32]):
                status, _, body = self.request(
                    "POST",
                    "/music/presence",
                    token=FULL_TOKEN,
                    raw_body=raw_body,
                )
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "invalid JSON body"})

        deeply_nested = ('{"a":' * 5000 + "0" + "}" * 5000).encode("utf-8")
        status, _, body = self.request(
            "POST",
            "/music/presence",
            token=FULL_TOKEN,
            raw_body=deeply_nested,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "invalid JSON body"})

    def test_public_music_file_route_only_serves_numeric_mp3_with_no_store(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        audio = b"0123456789"
        (cache_dir / "123.mp3").write_bytes(audio)
        forbidden_files = {
            "123_preanalysis.json": b'{"private":"C:/server/cache"}',
            "123.lrc": b"private lyrics",
            "123_analyze_error.txt": b"private dependency path",
            "123.analyzing": b'{"name":"private"}',
            "123_analysis.png": b"private image",
        }
        for filename, content in forbidden_files.items():
            (cache_dir / filename).write_bytes(content)

        status, headers, body = self.request_bytes("GET", "/music/file/123.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(body, audio)
        self.assertEqual(headers.get("Content-Type"), "audio/mpeg")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")

        status, headers, body = self.request_bytes(
            "GET", "/music/file/123.mp3", extra_headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, b"2345")
        self.assertEqual(headers.get("Content-Range"), "bytes 2-5/10")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

        denied_names = [
            *forbidden_files,
            "0.mp3",
            "abc.mp3",
            "123.MP3",
            "123.mp3/extra",
            "../123.mp3",
        ]
        for filename in denied_names:
            with self.subTest(filename=filename):
                status, _, body = self.request("GET", f"/music/file/{filename}")
                self.assertEqual(status, 404)
                self.assertEqual(body, {"error": "not found"})

    def test_music_search_adds_safely_converted_duration_seconds(self) -> None:
        raw_songs = [
            {
                "id": 1,
                "name": "Valid",
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
                "duration": 123456,
            },
            {
                "id": 2,
                "name": "Missing",
                "artists": [],
                "album": {},
            },
            {
                "id": 3,
                "name": "String",
                "artists": [],
                "album": {},
                "duration": "123456",
            },
            {
                "id": 4,
                "name": "Negative",
                "artists": [],
                "album": {},
                "duration": -1,
            },
            {
                "id": 5,
                "name": "Nonfinite",
                "artists": [],
                "album": {},
                "duration": math.inf,
            },
            {
                "id": 6,
                "name": "Boolean",
                "artists": [],
                "album": {},
                "duration": True,
            },
        ]

        def fake_netease_request(
            handler,
            url: str,
            data: bytes | None = None,
            extra_headers: dict[str, str] | None = None,
            timeout: int = 10,
        ) -> dict:
            del handler, data, extra_headers, timeout
            if "/api/search/get" in url:
                return {"result": {"songs": raw_songs}}
            if "/api/song/detail" in url:
                return {"songs": []}
            raise AssertionError(f"unexpected URL: {url}")

        with mock.patch.object(
            EryuHandler,
            "_netease_request",
            new=fake_netease_request,
        ):
            status, _, body = self.request(
                "GET",
                "/music/search?q=duration",
                token=FULL_TOKEN,
            )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(
            [song["durationSeconds"] for song in body["songs"]],
            [123.456, 0.0, 0.0, 0.0, 0.0, 0.0],
        )

    def test_analysis_id_and_response_are_sanitized(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        result_path = cache_dir / "123_preanalysis.json"
        result_path.write_text(
            json.dumps(
                {
                    "songId": "123",
                    "name": "Song",
                    "artist": "Artist",
                    "duration": 120.5,
                    "bpm": 100,
                    "key": "C",
                    "segments": [
                        {"start": 0, "end": 20, "avgEnergy": 0.1, "maxEnergy": 0.2}
                    ],
                    "spectrogram": "C:/private/server/cache/123_analysis.png",
                    "unexpected": "must not escape",
                }
            ),
            encoding="utf-8",
        )
        (cache_dir / "123_analysis.png").write_bytes(b"test image marker")

        status, _, body = self.request(
            "GET", "/music/analyze/status?id=123", token=FULL_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")
        self.assertTrue(body["analysis"]["spectrogramAvailable"])
        self.assertNotIn("spectrogram", body["analysis"])
        self.assertNotIn("unexpected", body["analysis"])
        self.assertNotIn("C:/private", json.dumps(body))

        for invalid_id in ("../123", "123/../../x", "abc", "-1"):
            with self.subTest(invalid_id=invalid_id):
                status, _, _ = self.request(
                    "GET", f"/music/analyze/status?id={invalid_id}", token=FULL_TOKEN
                )
                self.assertEqual(status, 400)

        (cache_dir / "456_analyze_error.txt").write_text(
            "private dependency error at C:/private/path", encoding="utf-8"
        )
        status, _, body = self.request(
            "GET", "/music/analyze/status?id=456", token=FULL_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "status": "error"})
        self.assertNotIn("private", json.dumps(body))

    def test_analysis_subprocess_uses_the_active_virtual_environment_python(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        (cache_dir / "789.mp3").write_bytes(b"synthetic audio placeholder")

        child_secret_values = {
            "MUSIC_U": "m" * 40,
            "ERYU_AUTH_TOKEN": "f" * 40,
            "ERYU_MCP_READ_TOKEN": "r" * 40,
        }
        with mock.patch.dict(os.environ, child_secret_values, clear=False):
            with mock.patch("server.eryu.subprocess.Popen") as popen:
                status, _, body = self.request(
                    "POST",
                    "/music/analyze",
                    token=FULL_TOKEN,
                    payload={"songId": 789, "name": "Song", "artist": "Artist"},
                )

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "status": "started"})
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]).name, "analyze_song.py")
        self.assertEqual(command[2:5], ["789", "Song", "Artist"])
        self.assertEqual(Path(command[5]), cache_dir)
        self.assertIs(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        child_environment = popen.call_args.kwargs["env"]
        self.assertEqual(child_environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(Path(child_environment["MPLCONFIGDIR"]).parent, cache_dir)
        self.assertEqual(Path(child_environment["NUMBA_CACHE_DIR"]).parent, cache_dir)
        self.assertEqual(Path(child_environment["HOME"]).parents[1], cache_dir)
        self.assertEqual(Path(child_environment["XDG_CACHE_HOME"]).parents[1], cache_dir)
        for secret_name, secret_value in child_secret_values.items():
            self.assertNotIn(secret_name, child_environment)
            self.assertNotIn(secret_value, child_environment.values())

    def test_spectrogram_is_authenticated_current_song_png_without_path_leak(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        image = b"\x89PNG\r\n\x1a\nsynthetic-test-image"
        (cache_dir / "123_preanalysis.json").write_text(
            json.dumps({"songId": "123", "segments": []}),
            encoding="utf-8",
        )
        (cache_dir / "123_analysis.png").write_bytes(image)
        self.assertEqual(
            self.request(
                "POST",
                "/music/presence",
                token=FULL_TOKEN,
                payload=playing_presence_for_song(123, session_id="spectrogram-test"),
            )[0],
            200,
        )

        status, headers, body = self.request_bytes(
            "GET", "/music/analyze/spectrogram?id=123", token=READ_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, image)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

        self.assertEqual(
            self.request("GET", "/music/analyze/spectrogram?id=123")[0], 403
        )
        self.assertEqual(
            self.request(
                "GET", "/music/analyze/spectrogram?id=456", token=READ_TOKEN
            )[0],
            403,
        )
        for path in (
            "/music/analyze/spectrogram",
            "/music/analyze/spectrogram?id=abc",
            "/music/analyze/spectrogram?id=123&extra=1",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, token=FULL_TOKEN)[0], 400)

        (cache_dir / "123_analysis.png").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        self.assertEqual(
            self.request(
                "GET", "/music/analyze/spectrogram?id=123", token=READ_TOKEN
            )[0],
            413,
        )

    def test_spectrogram_requires_a_result_with_the_exact_cache_song_id(self) -> None:
        cache_dir = self.state.data_dir / "music_cache"
        (cache_dir / "789_analysis.png").write_bytes(
            b"\x89PNG\r\n\x1a\norphan-image"
        )
        self.assertEqual(
            self.request(
                "POST",
                "/music/presence",
                token=FULL_TOKEN,
                payload=playing_presence_v2(123, "789"),
            )[0],
            200,
        )

        for token, request_song_id in ((READ_TOKEN, "123"), (FULL_TOKEN, "789")):
            with self.subTest(token=token, state="orphan"):
                status, _, body = self.request(
                    "GET",
                    f"/music/analyze/spectrogram?id={request_song_id}",
                    token=token,
                )
                self.assertEqual(status, 404)
                self.assertEqual(body, {"error": "spectrogram not found"})

        (cache_dir / "789_preanalysis.json").write_text(
            json.dumps({"songId": "790", "segments": []}),
            encoding="utf-8",
        )
        for token, request_song_id in ((READ_TOKEN, "123"), (FULL_TOKEN, "789")):
            with self.subTest(token=token, state="mismatch"):
                status, _, body = self.request(
                    "GET",
                    f"/music/analyze/spectrogram?id={request_song_id}",
                    token=token,
                )
                self.assertEqual(status, 404)
                self.assertEqual(body, {"error": "spectrogram not found"})


class PresenceConfigurationTest(unittest.TestCase):
    def make_state(self, environment: dict[str, str]) -> ServerState:
        with mock.patch.dict(os.environ, environment, clear=True):
            with tempfile.TemporaryDirectory() as temp_dir:
                return ServerState(0, data_dir=Path(temp_dir))

    def test_tokens_are_required_strong_and_distinct(self) -> None:
        valid = {
            "ERYU_AUTH_TOKEN": FULL_TOKEN,
            "ERYU_MCP_READ_TOKEN": READ_TOKEN,
        }
        self.make_state(valid)

        invalid_environments = [
            {},
            {"ERYU_AUTH_TOKEN": FULL_TOKEN},
            {"ERYU_AUTH_TOKEN": "short", "ERYU_MCP_READ_TOKEN": READ_TOKEN},
            {"ERYU_AUTH_TOKEN": FULL_TOKEN, "ERYU_MCP_READ_TOKEN": "short"},
            {"ERYU_AUTH_TOKEN": "x" * 31 + " ", "ERYU_MCP_READ_TOKEN": READ_TOKEN},
            {"ERYU_AUTH_TOKEN": FULL_TOKEN, "ERYU_MCP_READ_TOKEN": "x" * 16 + " " + "y" * 16},
            {"ERYU_AUTH_TOKEN": FULL_TOKEN, "ERYU_MCP_READ_TOKEN": FULL_TOKEN},
        ]
        for environment in invalid_environments:
            with self.subTest(environment=set(environment)):
                with self.assertRaises(RuntimeError):
                    self.make_state(environment)

    def test_ttl_parser_is_strict(self) -> None:
        self.assertEqual(parse_presence_ttl(None), 10.0)
        self.assertEqual(parse_presence_ttl("10"), 10.0)
        self.assertEqual(parse_presence_ttl("0.5"), 0.5)
        for value in ("", " 10", "10 ", "+10", "1e1", "0", "-1", "NaN", "inf", "3601"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_presence_ttl(value)

    def test_listen_configuration_is_loopback_only_and_supports_legacy_port(self) -> None:
        self.assertEqual(_parse_server_host(None), "127.0.0.1")
        self.assertEqual(_parse_server_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(_parse_server_port(None), 9090)
        self.assertEqual(_parse_server_port("9090"), 9090)

        with mock.patch.dict(os.environ, {"PORT": "8181"}, clear=True):
            self.assertEqual(_load_server_port(), 8181)
        with mock.patch.dict(
            os.environ, {"PORT": "8181", "ERYU_PORT": "9191"}, clear=True
        ):
            self.assertEqual(_load_server_port(), 9191)

        for value in ("", " 9090", "9090 ", "+9090", "0", "65536", "1.5"):
            with self.subTest(port=value):
                with self.assertRaises(ValueError):
                    _parse_server_port(value)
        for value in (
            "",
            " localhost",
            "localhost",
            "0.0.0.0",
            "127.0.0.2",
            "::1",
            "127.0.0.1\n",
        ):
            with self.subTest(host=value):
                with self.assertRaises(ValueError):
                    _parse_server_host(value)

    def test_data_directory_environment_must_be_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = _parse_data_dir(temp_dir)
            self.assertTrue(configured.is_absolute())
            self.assertEqual(configured, Path(temp_dir).resolve())

        for value in ("", "relative/data", " relative/data", "relative/data "):
            with self.subTest(data_dir=value):
                with self.assertRaises(ValueError):
                    _parse_data_dir(value)

        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "ERYU_AUTH_TOKEN": FULL_TOKEN,
                "ERYU_MCP_READ_TOKEN": READ_TOKEN,
                "ERYU_HOST": "127.0.0.1",
                "ERYU_DATA_DIR": temp_dir,
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                state = ServerState(9090)
            self.assertEqual(state.host, "127.0.0.1")
            self.assertEqual(state.data_dir, Path(temp_dir).resolve())

    def test_cors_origin_is_exact_in_production_but_local_default_is_compatible(self) -> None:
        production_origin = "https://eryu.95.169.17.214.sslip.io"
        self.assertEqual(_parse_allowed_origin(None), "*")
        self.assertEqual(_parse_allowed_origin("*"), "*")
        self.assertEqual(_parse_allowed_origin(production_origin), production_origin)
        self.assertEqual(_parse_allowed_origin("http://127.0.0.1:9090"), "http://127.0.0.1:9090")
        for value in (
            "",
            " https://example.com",
            "https://example.com/",
            "https://example.com/path",
            "https://user@example.com",
            "file://example.com",
            "https://example.com:99999",
        ):
            with self.subTest(origin=value):
                with self.assertRaises(ValueError):
                    _parse_allowed_origin(value)

    def test_music_u_is_read_only_from_environment(self) -> None:
        handler = object.__new__(EryuHandler)
        with mock.patch.dict(os.environ, {"MUSIC_U": "test-cookie-value"}, clear=False):
            self.assertEqual(handler._netease_cookie(), "MUSIC_U=test-cookie-value")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(handler._netease_cookie(), "")

        source = Path(__file__).resolve().parents[1] / "server" / "eryu.py"
        source_text = source.read_text(encoding="utf-8")
        self.assertNotIn(".netease_cred", source_text)
        self.assertNotIn(".secret", source_text)

    def test_validator_returns_an_independent_normalized_snapshot(self) -> None:
        payload = playing_presence()
        snapshot = validate_presence_payload(payload)
        self.assertEqual(snapshot["song"]["songId"], "123456")
        payload["song"]["name"] = "mutated"
        self.assertEqual(snapshot["song"]["name"], "Test Song")

        invalid = playing_presence()
        invalid["reportedAt"] = "2026-08-14 06:00:00"
        with self.assertRaises(PresenceValidationError):
            validate_presence_payload(invalid)


if __name__ == "__main__":
    unittest.main()
