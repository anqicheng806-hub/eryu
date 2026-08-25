#!/usr/bin/env python3
"""Read-only MCP tools backed by eryu's authenticated HTTP API.

The server intentionally exposes no playback controls and defaults to stdio.
It reads its backend URL and token only from the local process environment.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations


DEFAULT_BASE_URL = "http://127.0.0.1:9090"
HTTP_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 1_048_576
MAX_IMAGE_BYTES = 8 * 1_048_576
SONG_ID_RE = re.compile(r"^[0-9]{1,20}$")
CLIENT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_PRESENCE_SEQUENCE = 2**53 - 1
ALLOWED_PATHS = frozenset(
    {
        "/music/presence",
        "/music/analyze/status",
        "/music/analyze/spectrogram",
        "/music/memory",
    }
)


class EryuUnavailable(RuntimeError):
    """A sanitized backend/configuration failure safe to handle internally."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the internal read token on the one configured Eryu origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class ReadClient(Protocol):
    """Narrow interface used by tools and fakes; it has no write operation."""

    async def get_json(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def get_bytes(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> bytes: ...


PresenceRevision = tuple[str, int, str, str]


class EryuReadClient:
    """Header-authenticated, GET-only client with no retry behavior."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if (
            not token
            or token != token.strip()
            or len(token) < 32
            or any(char.isspace() for char in token)
        ):
            raise EryuUnavailable("ERYU_MCP_READ_TOKEN must be a non-whitespace value of at least 32 characters")
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @classmethod
    def from_environment(cls) -> "EryuReadClient":
        return cls(
            os.environ.get("ERYU_BASE_URL", DEFAULT_BASE_URL),
            os.environ.get("ERYU_MCP_READ_TOKEN", ""),
        )

    async def get_json(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_json_sync, path, query)

    async def get_bytes(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> bytes:
        return await asyncio.to_thread(self._get_bytes_sync, path, query)

    def _get_json_sync(
        self,
        path: str,
        query: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        if path not in ALLOWED_PATHS:
            raise EryuUnavailable("backend path is not allowed")
        validated_query = _validate_query(path, query)

        url = f"{self.base_url}{path}"
        if validated_query:
            url = f"{url}?{urllib.parse.urlencode(validated_query)}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "X-Auth-Token": self._token,
            },
        )

        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise EryuUnavailable(f"backend returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise EryuUnavailable("backend is unavailable") from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise EryuUnavailable("backend response is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EryuUnavailable("backend returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise EryuUnavailable("backend returned an invalid object")
        return payload

    def _get_bytes_sync(
        self,
        path: str,
        query: Mapping[str, str] | None,
    ) -> bytes:
        if path != "/music/analyze/spectrogram" or path not in ALLOWED_PATHS:
            raise EryuUnavailable("backend image path is not allowed")
        validated_query = _validate_query(path, query)
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(validated_query)}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "image/png",
                "X-Auth-Token": self._token,
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                content_type = response.headers.get_content_type()
                raw = response.read(MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise EryuUnavailable(f"backend returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise EryuUnavailable("backend is unavailable") from None
        if content_type != "image/png" or len(raw) > MAX_IMAGE_BYTES or not raw:
            raise EryuUnavailable("backend returned an invalid spectrogram")
        return raw


def _validate_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EryuUnavailable("ERYU_BASE_URL must be an HTTP(S) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EryuUnavailable("ERYU_BASE_URL must not contain credentials or a query")
    if parsed.path not in {"", "/"}:
        raise EryuUnavailable("ERYU_BASE_URL must not contain a path")
    try:
        parsed.port
    except ValueError:
        raise EryuUnavailable("ERYU_BASE_URL contains an invalid port") from None
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise EryuUnavailable("plain HTTP is allowed only for a loopback ERYU_BASE_URL")
    return candidate


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _nonnegative_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None


def _bounded_text(value: Any, limit: int = 2_000) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _song_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not SONG_ID_RE.fullmatch(value) or int(value) <= 0:
        return None
    return value


def _validate_query(
    path: str,
    query: Mapping[str, str] | None,
) -> dict[str, str]:
    values = dict(query or {})
    if path == "/music/presence":
        if values:
            raise EryuUnavailable("presence GET does not accept query parameters")
        return {}
    if set(values) != {"id"} or _song_id(values.get("id")) is None:
        raise EryuUnavailable("backend GET requires one numeric song id")
    return {"id": str(values["id"])}


def _freshness_state(payload: Mapping[str, Any], presence: Mapping[str, Any] | None) -> str:
    freshness = payload.get("freshness")
    state: Any = None
    stale: Any = payload.get("stale")

    if isinstance(freshness, Mapping):
        state = freshness.get("state") or freshness.get("status")
        if stale is None:
            stale = freshness.get("stale")
    elif isinstance(freshness, str):
        state = freshness

    if state is None:
        candidate = payload.get("status")
        if candidate in {"absent", "fresh", "stale"}:
            state = candidate

    if presence is not None:
        if stale is None:
            stale = presence.get("stale")
        if state is None and presence.get("fresh") is True:
            state = "fresh"

    # The real backend represents an absent snapshot as state=absent and
    # stale=true.  Explicit absence wins; a contradictory fresh+stale response
    # still fails closed as stale.
    if state == "absent":
        return "absent"
    if state == "stale" or stale is True:
        return "stale"
    if state == "fresh":
        return "fresh"
    if presence is None:
        return "absent"
    if stale is False:
        return "fresh"
    return "unknown"


def _freshness_metadata(payload: Mapping[str, Any], state: str) -> dict[str, Any]:
    source = payload.get("freshness")
    source = source if isinstance(source, Mapping) else payload
    result: dict[str, Any] = {"state": state}
    for key in ("ageSeconds", "staleAfterSeconds"):
        value = _nonnegative_number(source.get(key))
        if value is not None:
            result[key] = value
    received_at = _bounded_text(source.get("receivedAt"), 100)
    if received_at is not None:
        result["receivedAt"] = received_at
    return result


def _sanitize_line(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    index = value.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        result["index"] = index
    time_value = value.get("timeSeconds", value.get("time"))
    time_number = _nonnegative_number(time_value)
    if time_number is not None:
        result["timeSeconds"] = time_number
    text = _bounded_text(value.get("text"), 1_000)
    if text is not None:
        result["text"] = text
    translation = _bounded_text(value.get("translation"), 1_000)
    if translation is not None:
        result["translation"] = translation
    if value.get("current") is True:
        result["current"] = True
    relation = value.get("relation")
    if isinstance(relation, int) and not isinstance(relation, bool):
        result["relation"] = max(-20, min(20, relation))
    return result if "text" in result or "timeSeconds" in result else None


def _sanitize_lyrics(
    presence: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    lyrics = presence.get("lyrics")
    lyrics = lyrics if isinstance(lyrics, Mapping) else {}
    raw_status = lyrics.get("status")
    status = raw_status if raw_status in {"idle", "loading", "ready", "none", "error"} else "unknown"
    current_raw = presence.get("currentLyric", lyrics.get("current"))
    nearby_raw = presence.get("nearbyLyrics")
    if nearby_raw is None:
        nearby_raw = lyrics.get("nearby", lyrics.get("window", lyrics.get("lines")))
    if nearby_raw is None:
        previous = lyrics.get("previous") if isinstance(lyrics.get("previous"), list) else []
        following = lyrics.get("next") if isinstance(lyrics.get("next"), list) else []
        nearby_raw = [*previous, *([current_raw] if current_raw is not None else []), *following]

    current = _sanitize_line(current_raw)
    nearby: list[dict[str, Any]] = []
    if isinstance(nearby_raw, list):
        for item in nearby_raw[:21]:
            line = _sanitize_line(item)
            if line is not None:
                nearby.append(line)
    if current is None:
        current = next((line for line in nearby if line.get("current") is True), None)
    if status == "unknown" and (current is not None or nearby):
        status = "ready"
    return status, current, nearby


def _sanitize_song(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    sid = _song_id(value.get("songId"))
    if sid is None:
        return None
    result: dict[str, Any] = {"songId": sid}
    for key, limit in (("name", 500), ("artist", 500), ("album", 500), ("cover", 2_000)):
        text = _bounded_text(value.get(key), limit)
        if text is not None:
            result[key] = text
    return result


def _normalize_presence(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is not True:
        return {
            "ok": False,
            "available": False,
            "state": "unavailable",
            "reason": "presence_unavailable",
        }

    raw_presence = payload.get("presence")
    presence = raw_presence if isinstance(raw_presence, Mapping) else None
    freshness_state = _freshness_state(payload, presence)
    freshness = _freshness_metadata(payload, freshness_state)

    if freshness_state != "fresh":
        state = freshness_state if freshness_state in {"absent", "stale"} else "unavailable"
        return {
            "ok": True,
            "available": False,
            "state": state,
            "reason": f"presence_{state}",
            "freshness": freshness,
        }
    if presence is None:
        return {
            "ok": True,
            "available": False,
            "state": "absent",
            "reason": "presence_absent",
            "freshness": {"state": "absent"},
        }

    song = _sanitize_song(presence.get("song"))
    if presence.get("song") is None:
        return {
            "ok": True,
            "available": False,
            "state": "idle",
            "reason": "no_current_song",
            "freshness": freshness,
        }
    if song is None:
        return {
            "ok": True,
            "available": False,
            "state": "unavailable",
            "reason": "invalid_current_song",
            "freshness": freshness,
        }

    playback = presence.get("playback")
    playback = playback if isinstance(playback, Mapping) else {}
    playing = presence.get("playing", playback.get("playing"))
    if not isinstance(playing, bool):
        status = playback.get("state", playback.get("status"))
        playing = True if status == "playing" else False if status == "paused" else None

    raw_playback_status = playback.get("status", playback.get("state"))
    playback_status = (
        raw_playback_status
        if raw_playback_status in {"idle", "loading", "playing", "paused", "ended", "error"}
        else "playing" if playing is True else "paused" if playing is False else "unknown"
    )

    current_time = _nonnegative_number(
        presence.get("currentTime", playback.get("currentTime", playback.get("positionSeconds")))
    )
    duration = _nonnegative_number(presence.get("duration", playback.get("duration", playback.get("durationSeconds"))))
    progress = _finite_number(
        presence.get("progress", playback.get("progress", playback.get("progressRatio")))
    )
    if progress is not None:
        progress = max(0.0, min(1.0, progress))
    elif current_time is not None and duration:
        progress = max(0.0, min(1.0, current_time / duration))

    lyrics_status, current_lyric, nearby_lyrics = _sanitize_lyrics(presence)
    return {
        "ok": True,
        "available": True,
        "state": playback_status,
        "freshness": freshness,
        "song": song,
        "playing": playing,
        "playbackStatus": playback_status,
        "currentTime": current_time,
        "duration": duration,
        "progress": progress,
        "currentLyric": current_lyric,
        "nearbyLyrics": nearby_lyrics,
        "lyricsStatus": lyrics_status,
    }


def _presence_revision(
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> PresenceRevision | None:
    """Extract an internal-only identity for one exact fresh presence snapshot."""

    if normalized.get("available") is not True:
        return None
    raw_presence = payload.get("presence")
    raw_freshness = payload.get("freshness")
    song = normalized.get("song")
    if (
        not isinstance(raw_presence, Mapping)
        or not isinstance(raw_freshness, Mapping)
        or not isinstance(song, Mapping)
    ):
        return None

    client_session_id = raw_presence.get("clientSessionId")
    sequence = raw_presence.get("sequence")
    received_at = raw_freshness.get("receivedAt")
    public_song_id = _song_id(song.get("songId"))
    if (
        not isinstance(client_session_id, str)
        or CLIENT_SESSION_ID_RE.fullmatch(client_session_id) is None
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or sequence > MAX_PRESENCE_SEQUENCE
        or not isinstance(received_at, str)
        or not received_at
        or len(received_at) > 100
        or public_song_id is None
    ):
        return None
    return client_session_id, sequence, received_at, public_song_id


async def _current_presence_with_revision(
    client: ReadClient,
) -> tuple[dict[str, Any], PresenceRevision | None]:
    try:
        payload = await client.get_json("/music/presence")
    except Exception:
        return {
            "ok": False,
            "available": False,
            "state": "unavailable",
            "reason": "presence_unavailable",
        }, None
    if not isinstance(payload, Mapping):
        return {
            "ok": False,
            "available": False,
            "state": "unavailable",
            "reason": "presence_unavailable",
        }, None
    normalized = _normalize_presence(payload)
    return normalized, _presence_revision(payload, normalized)


async def _current_presence(client: ReadClient) -> dict[str, Any]:
    presence, _revision = await _current_presence_with_revision(client)
    return presence


def _sanitize_segments(value: Any) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    if not isinstance(value, list):
        return result
    for raw in value[:128]:
        if not isinstance(raw, Mapping):
            continue
        start = _nonnegative_number(raw.get("start"))
        end = _nonnegative_number(raw.get("end"))
        avg = _nonnegative_number(raw.get("avgEnergy"))
        maximum = _nonnegative_number(raw.get("maxEnergy"))
        if start is None or end is None or avg is None or maximum is None or end < start:
            continue
        result.append(
            {
                "start": start,
                "end": end,
                "avgEnergy": avg,
                "maxEnergy": maximum,
            }
        )
    return result


def _current_energy_segment(
    segments: list[dict[str, float]],
    current_time: float | None,
) -> dict[str, float] | None:
    if current_time is None:
        return None
    for segment in segments:
        if segment["start"] <= current_time <= segment["end"]:
            return segment
    return None


def _sanitize_memory(value: Any, expected_song_id: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    memory_song_id = value.get("songId")
    if memory_song_id is not None and _song_id(memory_song_id) != expected_song_id:
        return None

    result: dict[str, Any] = {"songId": expected_song_id}
    for key, limit in (("name", 500), ("artist", 500), ("notes", 8_000), ("feeling", 4_000)):
        text = _bounded_text(value.get(key), limit)
        if text is not None:
            result[key] = text
    for key in ("listenCount", "togetherCount"):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[key] = count
    for key in ("firstListened", "lastListened"):
        text = _bounded_text(value.get(key), 100)
        if text is not None:
            result[key] = text
    if isinstance(value.get("analyzed"), bool):
        result["analyzed"] = value["analyzed"]
    for key in ("bpm", "duration"):
        number = _nonnegative_number(value.get(key))
        if number is not None:
            result[key] = number
    for key, limit in (("favoriteLines", 100), ("tags", 100)):
        items = value.get(key)
        if isinstance(items, list):
            result[key] = [text[:1_000] for text in items[:limit] if isinstance(text, str)]
    return result


def _readonly_annotations() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )


def _oauth_security_meta(enabled: bool) -> dict[str, Any] | None:
    """Advertise the same OAuth scope enforced by the HTTP route middleware."""

    if not enabled:
        return None
    return {"securitySchemes": [{"type": "oauth2", "scopes": ["music:read"]}]}


def _analysis_result(
    structured: dict[str, Any],
    spectrogram: bytes | None = None,
) -> CallToolResult:
    content: list[Any] = [
        TextContent(
            type="text",
            text=json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
        )
    ]
    if spectrogram is not None:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(spectrogram).decode("ascii"),
                mime_type="image/png",
            )
        )
    return CallToolResult(content=content, structured_content=structured)


def _analysis_current_changed() -> CallToolResult:
    return _analysis_result(
        {
            "ok": True,
            "available": False,
            "state": "current_changed",
            "reason": "analysis_current_changed",
        }
    )


def _analysis_unavailable(presence: Mapping[str, Any]) -> CallToolResult:
    return _analysis_result(
        {
            "ok": False,
            "available": False,
            "state": "unavailable",
            "reason": "analysis_unavailable",
            "song": presence["song"],
        }
    )


async def _analysis_revision_is_current(
    client: ReadClient,
    expected: PresenceRevision,
) -> bool:
    current, revision = await _current_presence_with_revision(client)
    return current.get("available") is True and revision == expected


def build_server(
    client: ReadClient | None = None,
    *,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Build the four-tool server; optional auth is used only by the HTTP entry."""

    read_client = client if client is not None else EryuReadClient.from_environment()
    server = MCPServer(
        "eryu-music-presence",
        instructions=(
            "Read-only access to the current eryu listening presence. "
            "This server cannot pause, seek, skip, queue, analyze, or write memories."
        ),
        auth=auth,
        token_verifier=token_verifier,
    )

    @server.tool(
        name="music_now_playing",
        description="Read the fresh current song, playback position, and current lyric. Never controls playback.",
        annotations=_readonly_annotations(),
        meta=_oauth_security_meta(auth is not None),
    )
    async def music_now_playing() -> dict[str, Any]:
        return await _current_presence(read_client)

    @server.tool(
        name="music_lyrics_window",
        description="Read the current and nearby lyrics already reported by the player.",
        annotations=_readonly_annotations(),
        meta=_oauth_security_meta(auth is not None),
    )
    async def music_lyrics_window() -> dict[str, Any]:
        presence = await _current_presence(read_client)
        if not presence.get("available"):
            return presence
        lyrics_status = presence.get("lyricsStatus", "unknown")
        if lyrics_status != "ready":
            state = lyrics_status if lyrics_status in {"idle", "loading", "none", "error"} else "no_lyrics"
            return {
                "ok": True,
                "available": False,
                "state": state,
                "reason": f"lyrics_{state}",
                "freshness": presence["freshness"],
                "song": presence["song"],
            }
        current = presence.get("currentLyric")
        nearby = presence.get("nearbyLyrics") or []
        if current is None and not nearby:
            return {
                "ok": True,
                "available": False,
                "state": "no_lyrics",
                "reason": "lyrics_unavailable",
                "freshness": presence["freshness"],
                "song": presence["song"],
            }
        return {
            "ok": True,
            "available": True,
            "state": "ready",
            "lyricsStatus": "ready",
            "freshness": presence["freshness"],
            "song": presence["song"],
            "currentTime": presence.get("currentTime"),
            "currentLyric": current,
            "nearbyLyrics": nearby,
        }

    @server.tool(
        name="music_analysis",
        description="Read existing BPM, key, energy, and the available spectrogram image. Never starts analysis.",
        annotations=_readonly_annotations(),
        meta=_oauth_security_meta(auth is not None),
    )
    async def music_analysis() -> CallToolResult:
        presence, revision = await _current_presence_with_revision(read_client)
        if not presence.get("available"):
            return _analysis_result(presence)
        if revision is None:
            return _analysis_unavailable(presence)
        sid = presence["song"]["songId"]
        payload: Any = None
        status_failed = False
        try:
            payload = await read_client.get_json("/music/analyze/status", {"id": sid})
        except Exception:
            status_failed = True

        if not await _analysis_revision_is_current(read_client, revision):
            return _analysis_current_changed()
        if status_failed:
            return _analysis_unavailable(presence)
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return _analysis_unavailable(presence)

        raw_status = payload.get("status")
        status = raw_status if raw_status in {"ready", "running", "none"} else "error" if isinstance(raw_status, str) and raw_status.startswith("error") else "unavailable"
        if status != "ready":
            return _analysis_result({
                "ok": True,
                "available": False,
                "state": status,
                "reason": f"analysis_{status}",
                "song": presence["song"],
            })

        raw_analysis = payload.get("analysis")
        if not isinstance(raw_analysis, Mapping):
            return _analysis_unavailable(presence)
        analysis_song_id = raw_analysis.get("songId")
        if analysis_song_id != sid:
            return _analysis_result({
                "ok": False,
                "available": False,
                "state": "unavailable",
                "reason": "analysis_song_mismatch",
                "song": presence["song"],
            })

        segments = _sanitize_segments(raw_analysis.get("segments"))
        analysis: dict[str, Any] = {
            "songId": sid,
            "segments": segments,
            "spectrogramAvailable": bool(
                raw_analysis.get("spectrogramAvailable") or raw_analysis.get("spectrogram")
            ),
        }
        for key in ("duration", "bpm"):
            number = _nonnegative_number(raw_analysis.get(key))
            if number is not None:
                analysis[key] = number
        key_name = _bounded_text(raw_analysis.get("key"), 32)
        if key_name is not None:
            analysis["key"] = key_name
        analysis["currentEnergySegment"] = _current_energy_segment(
            segments,
            presence.get("currentTime"),
        )
        spectrogram: bytes | None = None
        if analysis["spectrogramAvailable"]:
            try:
                spectrogram = await read_client.get_bytes(
                    "/music/analyze/spectrogram", {"id": sid}
                )
            except Exception:
                spectrogram = None
            if not await _analysis_revision_is_current(read_client, revision):
                return _analysis_current_changed()
        analysis["spectrogramIncluded"] = spectrogram is not None
        result = {
            "ok": True,
            "available": True,
            "state": "ready",
            "freshness": presence["freshness"],
            "song": presence["song"],
            "analysis": analysis,
        }
        return _analysis_result(result, spectrogram)

    @server.tool(
        name="music_memory",
        description="Read existing notes and listening statistics for only the fresh current song. Never writes memory.",
        annotations=_readonly_annotations(),
        meta=_oauth_security_meta(auth is not None),
    )
    async def music_memory() -> dict[str, Any]:
        presence = await _current_presence(read_client)
        if not presence.get("available"):
            return presence
        sid = presence["song"]["songId"]
        try:
            payload = await read_client.get_json("/music/memory", {"id": sid})
        except Exception:
            return {
                "ok": False,
                "available": False,
                "state": "unavailable",
                "reason": "memory_unavailable",
                "song": presence["song"],
            }
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return {
                "ok": False,
                "available": False,
                "state": "unavailable",
                "reason": "memory_unavailable",
                "song": presence["song"],
            }
        if payload.get("memory") is None:
            return {
                "ok": True,
                "available": False,
                "state": "none",
                "reason": "memory_not_found",
                "freshness": presence["freshness"],
                "song": presence["song"],
            }
        memory = _sanitize_memory(payload.get("memory"), sid)
        if memory is None:
            return {
                "ok": False,
                "available": False,
                "state": "unavailable",
                "reason": "memory_invalid",
                "song": presence["song"],
            }
        return {
            "ok": True,
            "available": True,
            "state": "ready",
            "freshness": presence["freshness"],
            "song": presence["song"],
            "memory": memory,
        }

    return server


def main() -> None:
    """Run locally over stdio; no unauthenticated incoming HTTP listener exists."""

    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
