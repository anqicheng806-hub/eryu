"""Windows GSMTC Reader for Eryu presence.

This module reads the current active media session from Windows GSMTC and sends
presence payloads that are fully compatible with the existing `/music/presence`
schema used by web-player and MCP tools.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import inspect
import json
import logging
import math
import os
import platform
import re
import signal
import sys
import tempfile
import unicodedata
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urlsplit

import urllib.error
import urllib.request


LOGGER = logging.getLogger("eryu.gsmtc_reader")

DEFAULT_ENDPOINT = ""
DEFAULT_HEARTBEAT_SECONDS = 2.0
DEFAULT_LOCK_FILE = Path(tempfile.gettempdir()) / "eryu-gsmtc-reader.lock"
DEFAULT_RETRY_DELAY_SECONDS = 30.0
DEFAULT_LYRICS_TIMEOUT_SECONDS = 12.0
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
NETEASE_SONG_ID_PATTERN = re.compile(r"^[0-9]{1,20}$")
LRC_TIMESTAMP_PATTERN = re.compile(
    r"\[(\d{1,3}):([0-5]?\d)(?:[.:](\d{1,3}))?\]"
)
MAX_SESSION_ID_LENGTH = 128
MAX_TEXT_LENGTH = 512
MAX_LYRIC_TEXT_LENGTH = 2000
MAX_PARSED_LYRIC_LINES = 5000
MAX_LYRICS_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_LYRICS_CACHE_ENTRIES = 32
LYRIC_WINDOW_LINES = 2
DEFAULT_IGNORED_SOURCES = (
    "applicationframehost",
    "searchui",
    "searchhost",
    "systemsettings",
    "explorer",
    "shellexperiencehost",
)
NON_SECRET_ENV_KEYS = {
    "ERYU_PRESENCE_ENDPOINT",
    "ERYU_PRESENCE_HEARTBEAT_SECONDS",
    "ERYU_GSMTC_PLAYER_PREFERENCES",
    "ERYU_GSMTC_IGNORED_SOURCES",
    "ERYU_PRESENCE_BASIC_AUTH_USER",
}


def _safe_text(value: Any, max_length: int) -> str:
    text = "" if value is None else str(value).strip()
    return text[:max_length]


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return float(default)
    return float(number)


def _coerce_status(value: Any) -> str:
    enum_name = getattr(value, "name", None)
    raw = str(enum_name if enum_name is not None else (value or "")).strip().lower()
    if not raw:
        return "idle"
    # Common values: "playing", "paused", "stopped", enum names like "Playing"
    if raw in {"playing", "paused", "stopped", "ended", "idle", "loading", "error"}:
        return "ended" if raw == "stopped" else raw
    if raw.endswith("playbackstatus.playing"):
        return "playing"
    if raw.endswith("playbackstatus.paused"):
        return "paused"
    if raw.endswith("playbackstatus.stopped"):
        return "ended"
    if raw.endswith("playbackstatus.buffering"):
        return "loading"
    if raw.endswith("playbackstatus.opened") or raw.endswith("playbackstatus.closed"):
        return "loading"
    if "pause" in raw:
        return "paused"
    if "play" in raw:
        return "playing"
    return "idle"


def _coerce_session_id(text: str) -> str:
    value = (text or "").strip()[:MAX_SESSION_ID_LENGTH]
    if not value:
        value = f"gsmtc-{os.getpid()}"
    sanitized = "".join(c for c in value if SESSION_ID_PATTERN.fullmatch(c) is not None)
    if not sanitized:
        sanitized = f"gsmtc-{os.getpid()}"
    return sanitized[:MAX_SESSION_ID_LENGTH]


def _normalize_timespan(value: Any) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "total_seconds"):
        try:
            number = float(value.total_seconds())
        except Exception:
            number = 0.0
        return _safe_number(number, 0.0)
    if isinstance(value, (int, float)):
        number = float(value)
        # WinRT TimeSpan often uses 100ns ticks.
        if abs(number) > 10_000_000:
            return number / 10_000_000.0
        # Fallback for millisecond/microsecond-like values.
        if abs(number) > 1000:
            if abs(number) >= 1000_000:
                return number / 1_000_000.0
            return number / 1000.0
        return number
    return 0.0


def _effective_position(
    position_seconds: float,
    duration_seconds: float,
    status: str,
    last_updated_time: Any,
    *,
    now: datetime | None = None,
) -> float:
    position = max(0.0, _safe_number(position_seconds))
    duration = max(0.0, _safe_number(duration_seconds))
    if _coerce_status(status) == "playing" and isinstance(last_updated_time, datetime):
        updated = last_updated_time
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        position += max(0.0, (current - updated).total_seconds())
    if duration > 0:
        position = min(position, duration)
    return position


def _to_song_id(seed: str) -> str:
    seed_value = (seed or "").strip()
    if seed_value.isdigit() and 0 < int(seed_value) <= 10**20 - 1:
        return str(seed_value)
    digest = hashlib.sha1(seed_value.encode("utf-8")).hexdigest()
    # Keep within Positive int range accepted by server validator.
    return str(int(digest[:16], 16) % (10**18) + 1)


@dataclass(frozen=True)
class PresenceSession:
    source_app_user_model_id: str
    title: str
    artist: str
    album: str
    status: str
    playing: bool
    position_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class LyricLine:
    index: int
    time_seconds: float
    text: str
    translation: str = ""


@dataclass(frozen=True)
class LyricsSnapshot:
    song_id: str | None
    status: str
    lines: tuple[LyricLine, ...] = ()
    catalog_song_id: str | None = None


@dataclass(frozen=True)
class LyricsLookupResult:
    status: str
    lines: tuple[LyricLine, ...] = ()
    catalog_song_id: str | None = None


@dataclass(frozen=True)
class _LyricsJob:
    generation: int
    key: tuple[str, str, str, str]
    song_id: str
    session: PresenceSession


@dataclass
class HttpResult:
    status: int
    body: str | None = None


def _session_key(session: PresenceSession) -> tuple[str, str, str, str]:
    return (
        session.source_app_user_model_id,
        session.title,
        session.artist,
        session.album,
    )


def _lyrics_key_digest(key: tuple[str, str, str, str]) -> str:
    encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _presence_song_id(session: PresenceSession) -> str:
    return _to_song_id("|".join(_session_key(session)))


def _normalize_metadata(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


def _valid_provider_song_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    candidate = str(value or "")
    if not NETEASE_SONG_ID_PATTERN.fullmatch(candidate) or int(candidate) <= 0:
        return None
    return str(int(candidate))


def _select_lyrics_candidate(
    session: PresenceSession,
    candidates: list[Any],
) -> str | None:
    """Return one conservative NetEase ID, or fail closed when metadata is ambiguous."""

    expected_title = _normalize_metadata(session.title)
    expected_artist = _normalize_metadata(session.artist)
    if not expected_title or not expected_artist:
        return None

    matches: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        provider_id = _valid_provider_song_id(candidate.get("id"))
        if provider_id is None:
            continue
        if _normalize_metadata(candidate.get("name")) != expected_title:
            continue
        if _normalize_metadata(candidate.get("artist")) != expected_artist:
            continue
        matches[provider_id] = candidate

    return next(iter(matches)) if len(matches) == 1 else None


def _valid_duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        duration = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _select_catalog_candidate(
    session: PresenceSession,
    candidates: list[Any],
) -> str | None:
    """Return one strictly version-matched NetEase ID, or fail closed."""

    expected_title = _normalize_metadata(session.title)
    expected_artist = _normalize_metadata(session.artist)
    expected_album = _normalize_metadata(session.album)
    expected_duration = _valid_duration_seconds(session.duration_seconds)
    if not expected_title or not expected_artist or not expected_album:
        return None
    if expected_duration is None:
        return None

    matches: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        provider_id = _valid_provider_song_id(candidate.get("id"))
        candidate_duration = _valid_duration_seconds(candidate.get("durationSeconds"))
        if provider_id is None or candidate_duration is None:
            continue
        if _normalize_metadata(candidate.get("name")) != expected_title:
            continue
        if _normalize_metadata(candidate.get("artist")) != expected_artist:
            continue
        if _normalize_metadata(candidate.get("album")) != expected_album:
            continue
        if abs(candidate_duration - expected_duration) > 2.0:
            continue
        matches.add(provider_id)

    return next(iter(matches)) if len(matches) == 1 else None


def _parse_lrc_lines(value: str) -> list[tuple[float, str]]:
    parsed: list[tuple[float, str]] = []
    for raw_line in value.splitlines():
        matches = list(LRC_TIMESTAMP_PATTERN.finditer(raw_line))
        if not matches:
            continue
        text = _safe_text(raw_line[matches[-1].end():], MAX_LYRIC_TEXT_LENGTH)
        if not text:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction_text = match.group(3) or ""
            fraction = int(fraction_text) / (10 ** len(fraction_text)) if fraction_text else 0.0
            parsed.append((round(minutes * 60 + seconds + fraction, 3), text))
            if len(parsed) >= MAX_PARSED_LYRIC_LINES:
                break
        if len(parsed) >= MAX_PARSED_LYRIC_LINES:
            break
    parsed.sort(key=lambda item: item[0])
    return parsed


def _centisecond(value: float) -> int:
    return int(max(0.0, value) * 100 + 0.5)


def _parse_lyrics(lrc: str, translated_lrc: str = "") -> tuple[LyricLine, ...]:
    base_lines = _parse_lrc_lines(lrc)
    if not base_lines:
        return ()
    translations = {
        _centisecond(time_seconds): text
        for time_seconds, text in _parse_lrc_lines(translated_lrc)
    }
    return tuple(
        LyricLine(
            index=index,
            time_seconds=time_seconds,
            text=text,
            translation=translations.get(_centisecond(time_seconds), ""),
        )
        for index, (time_seconds, text) in enumerate(base_lines)
    )


def _lyric_line_payload(line: LyricLine) -> dict[str, Any]:
    return {
        "index": line.index,
        "timeSeconds": _safe_number(line.time_seconds),
        "text": _safe_text(line.text, MAX_LYRIC_TEXT_LENGTH),
        "translation": _safe_text(line.translation, MAX_LYRIC_TEXT_LENGTH),
    }


def _build_presence_lyrics(
    song_id: str,
    position_seconds: float,
    lyrics: LyricsSnapshot | None,
) -> dict[str, Any]:
    status = "none"
    if lyrics is not None and lyrics.song_id == song_id:
        status = lyrics.status if lyrics.status in {"loading", "ready", "none", "error"} else "error"
    empty = {
        "songId": song_id,
        "status": status,
        "currentIndex": -1,
        "current": None,
        "previous": [],
        "next": [],
    }
    if status != "ready" or lyrics is None or not lyrics.lines:
        return empty

    current_position = -1
    position = _safe_number(position_seconds)
    for line_position in range(len(lyrics.lines) - 1, -1, -1):
        line = lyrics.lines[line_position]
        if position >= line.time_seconds:
            current_position = line_position
            break

    previous_start = max(0, current_position - LYRIC_WINDOW_LINES)
    previous = (
        []
        if current_position < 0
        else [
            _lyric_line_payload(line)
            for line in lyrics.lines[previous_start:current_position]
        ]
    )
    next_start = 0 if current_position < 0 else current_position + 1
    next_lines = [
        _lyric_line_payload(line)
        for line in lyrics.lines[next_start:next_start + LYRIC_WINDOW_LINES]
    ]
    current = lyrics.lines[current_position] if current_position >= 0 else None
    current_index = current.index if current is not None else -1
    return {
        "songId": song_id,
        "status": "ready",
        "currentIndex": current_index,
        "current": _lyric_line_payload(current) if current is not None else None,
        "previous": previous,
        "next": next_lines,
    }


def _select_session(
    sessions: list[PresenceSession],
    *,
    source_preference: tuple[str, ...] = (),
    ignored_sources: set[str] | None = None,
) -> PresenceSession | None:
    ignored_sources = ignored_sources or set()

    def ignored(session: PresenceSession) -> bool:
        source = session.source_app_user_model_id.lower()
        return any(value and value in source for value in ignored_sources)

    filtered = [session for session in sessions if not ignored(session)]
    if not filtered:
        return None

    # A truly playing session wins. Preferences only break ties between
    # sessions with the same playback state.
    for preference in source_preference:
        for session in filtered:
            source = session.source_app_user_model_id.lower()
            if session.playing and preference and preference in source:
                return session
    for session in filtered:
        if session.playing:
            return session
    for preference in source_preference:
        for session in filtered:
            source = session.source_app_user_model_id.lower()
            if preference and preference in source:
                return session
    return filtered[0]


def _normalize_endpoint(value: str) -> str:
    endpoint = (value or "").strip().rstrip("/")
    if not endpoint:
        raise ValueError("ERYU_PRESENCE_ENDPOINT is required")
    if any(character.isspace() for character in endpoint):
        raise ValueError("ERYU_PRESENCE_ENDPOINT must not contain whitespace")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ERYU_PRESENCE_ENDPOINT must be an http(s) base URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ERYU_PRESENCE_ENDPOINT must not contain credentials, query, or fragment")
    if parsed.scheme == "http":
        host = parsed.hostname or ""
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        if not is_loopback:
            raise ValueError("plain HTTP presence endpoints must be loopback")
    return endpoint


def _validate_token(value: str) -> str:
    token = value or ""
    if len(token) < 32 or any(character.isspace() for character in token):
        raise ValueError("presence token must be at least 32 characters with no whitespace")
    return token


class PresencePayloadBuilder:
    def __init__(self, client_session_id: str) -> None:
        self.client_session_id = _coerce_session_id(client_session_id)

    def _build_reported_at(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    def build(
        self,
        sequence: int,
        snapshot: PresenceSession | None,
        lyrics: LyricsSnapshot | None = None,
    ) -> dict[str, Any]:
        if snapshot is None:
            return {
                "schemaVersion": 2,
                "clientSessionId": self.client_session_id,
                "sequence": sequence,
                "reportedAt": self._build_reported_at(),
                "song": None,
                "playback": {
                    "status": "idle",
                    "playing": False,
                    "positionSeconds": 0.0,
                    "durationSeconds": 0.0,
                    "progressRatio": 0.0,
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

        status = _coerce_status(snapshot.status)
        playing = bool(snapshot.playing)
        if playing:
            status = "playing"
        elif status == "playing":
            status = "paused"

        position = max(_safe_number(snapshot.position_seconds), 0.0)
        duration = max(_safe_number(snapshot.duration_seconds), 0.0)
        progress_ratio = 0.0 if duration <= 0 else max(0.0, min(1.0, position / duration))
        song_id = _presence_song_id(snapshot)
        catalog: dict[str, str] | None = None
        if lyrics is not None and lyrics.song_id == song_id:
            provider_id = _valid_provider_song_id(lyrics.catalog_song_id)
            if provider_id is not None:
                catalog = {"provider": "netease", "songId": provider_id}

        return {
            "schemaVersion": 2,
            "clientSessionId": self.client_session_id,
            "sequence": sequence,
            "reportedAt": self._build_reported_at(),
            "song": {
                "songId": song_id,
                "name": _safe_text(snapshot.title, MAX_TEXT_LENGTH),
                "artist": _safe_text(snapshot.artist, MAX_TEXT_LENGTH),
                "album": _safe_text(snapshot.album, MAX_TEXT_LENGTH),
                "cover": "",
                "catalog": catalog,
            },
            "playback": {
                "status": status,
                "playing": playing,
                "positionSeconds": position,
                "durationSeconds": duration,
                "progressRatio": progress_ratio,
            },
            "lyrics": _build_presence_lyrics(song_id, position, lyrics),
        }


class GSMTCUnsupportedError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_basic_authorization(username: str, password: str) -> str:
    if bool(username) != bool(password):
        raise ValueError("Basic Auth username and password must be provided together")
    if not username:
        return ""
    if ":" in username or any(character in "\r\n" for character in username + password):
        raise ValueError("Basic Auth credentials contain unsupported characters")
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


class PresenceHttpClient:
    def __init__(
        self,
        endpoint: str,
        token: str,
        basic_auth_user: str = "",
        basic_auth_password: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.endpoint = _normalize_endpoint(endpoint)
        self.token = _validate_token(token)
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        self._basic_authorization = _build_basic_authorization(
            basic_auth_user,
            basic_auth_password,
        )

    def _request(self, payload: dict[str, Any]) -> HttpResult:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Auth-Token": self.token,
        }
        if self._basic_authorization:
            headers["Authorization"] = self._basic_authorization
        request = urllib.request.Request(
            f"{self.endpoint}/music/presence",
            data=raw,
            method="POST",
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                return HttpResult(response.status, body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return HttpResult(exc.code, body)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"presence post failed: {exc}") from exc

    async def post(self, payload: dict[str, Any]) -> HttpResult:
        return await asyncio.to_thread(self._request, payload)


class LyricsLookupError(RuntimeError):
    def __init__(self, message: str, catalog_song_id: str | None = None) -> None:
        super().__init__(message)
        self.catalog_song_id = _valid_provider_song_id(catalog_song_id)


class LyricsHttpClient:
    """Independent GET client for best-effort lyrics enrichment."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        basic_auth_user: str = "",
        basic_auth_password: str = "",
        timeout_seconds: float = DEFAULT_LYRICS_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = _normalize_endpoint(endpoint)
        self.token = _validate_token(token)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        self._basic_authorization = _build_basic_authorization(
            basic_auth_user,
            basic_auth_password,
        )

    def _request_json(self, path: str, query: dict[str, str]) -> dict[str, Any]:
        url = f"{self.endpoint}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"X-Auth-Token": self.token}
        if self._basic_authorization:
            headers["Authorization"] = self._basic_authorization
        request = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise LyricsLookupError(f"lyrics lookup HTTP status {response.status}")
                raw_length = response.headers.get("Content-Length")
                try:
                    content_length = int(raw_length) if raw_length is not None else None
                except (TypeError, ValueError):
                    content_length = None
                if content_length is not None and content_length > MAX_LYRICS_RESPONSE_BYTES:
                    raise LyricsLookupError("lyrics lookup response is too large")
                raw = response.read(MAX_LYRICS_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            finally:
                raise LyricsLookupError(f"lyrics lookup HTTP status {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LyricsLookupError("lyrics lookup network failure") from exc

        if len(raw) > MAX_LYRICS_RESPONSE_BYTES:
            raise LyricsLookupError("lyrics lookup response is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise LyricsLookupError("lyrics lookup returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LyricsLookupError("lyrics lookup returned an invalid object")
        return payload

    async def lookup(self, session: PresenceSession) -> LyricsLookupResult:
        if not _normalize_metadata(session.title) or not _normalize_metadata(session.artist):
            return LyricsLookupResult("none")
        search_query = _safe_text(
            " ".join(value for value in (session.title, session.artist) if value),
            1024,
        )
        search = await asyncio.to_thread(
            self._request_json,
            "/music/search",
            {"q": search_query},
        )
        songs = search.get("songs")
        if search.get("ok") is not True or not isinstance(songs, list):
            raise LyricsLookupError("lyrics search returned an invalid response")
        provider_id = _select_lyrics_candidate(session, songs)
        catalog_song_id = _select_catalog_candidate(session, songs)
        if provider_id is None:
            return LyricsLookupResult("none", catalog_song_id=catalog_song_id)

        try:
            lyric_response = await asyncio.to_thread(
                self._request_json,
                "/music/lyric",
                {"id": provider_id},
            )
            if lyric_response.get("ok") is not True:
                raise LyricsLookupError("lyrics endpoint returned an invalid response")
            lrc = lyric_response.get("lrc", "")
            translated_lrc = lyric_response.get("tlyric", "")
            if not isinstance(lrc, str) or not isinstance(translated_lrc, str):
                raise LyricsLookupError("lyrics endpoint returned invalid text")
            if not lrc.strip():
                return LyricsLookupResult("none", catalog_song_id=catalog_song_id)
            lines = _parse_lyrics(lrc, translated_lrc)
            if not lines:
                raise LyricsLookupError("lyrics text could not be parsed")
            return LyricsLookupResult("ready", lines, catalog_song_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, LyricsLookupError) else "lyrics retrieval failed"
            raise LyricsLookupError(message, catalog_song_id) from exc


class LyricsEnricher:
    """Single-worker, current-song-only lyrics enrichment state machine."""

    def __init__(
        self,
        client: LyricsHttpClient,
        on_change: Callable[[], None] | None = None,
        *,
        cache_entries: int = MAX_LYRICS_CACHE_ENTRIES,
    ) -> None:
        self.client = client
        self.on_change = on_change
        self.cache_entries = max(1, int(cache_entries))
        self._cache: OrderedDict[
            tuple[str, str, str, str],
            LyricsLookupResult,
        ] = OrderedDict()
        self._attempted_results: dict[str, LyricsLookupResult] = {}
        self._queue: asyncio.Queue[_LyricsJob] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._pending_before_start: _LyricsJob | None = None
        self._generation = 0
        self._current_key: tuple[str, str, str, str] | None = None
        self._state = LyricsSnapshot(None, "idle")
        self._closed = False

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        if self._closed:
            raise RuntimeError("lyrics enricher is closed")
        self._queue = asyncio.Queue(maxsize=1)
        if self._pending_before_start is not None:
            self._queue.put_nowait(self._pending_before_start)
            self._pending_before_start = None
        self._worker_task = asyncio.create_task(
            self._run_worker(),
            name="eryu-lyrics-enricher",
        )

    def _clear_pending(self) -> None:
        self._pending_before_start = None
        if self._queue is None:
            return
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    def _enqueue_latest(self, job: _LyricsJob) -> None:
        if self._queue is None:
            self._pending_before_start = job
            return
        self._clear_pending()
        self._queue.put_nowait(job)

    def _cache_get(
        self,
        key: tuple[str, str, str, str],
    ) -> LyricsLookupResult | None:
        result = self._cache.get(key)
        if result is not None:
            self._cache.move_to_end(key)
        return result

    def _cache_put(
        self,
        key: tuple[str, str, str, str],
        result: LyricsLookupResult,
    ) -> None:
        self._attempted_results[_lyrics_key_digest(key)] = LyricsLookupResult(
            result.status,
            catalog_song_id=result.catalog_song_id,
        )
        if result.status != "ready" or not result.lines:
            self._cache.pop(key, None)
            return
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_entries:
            self._cache.popitem(last=False)

    def _attempted_result(
        self,
        key: tuple[str, str, str, str],
    ) -> LyricsLookupResult | None:
        attempted = self._attempted_results.get(_lyrics_key_digest(key))
        if attempted is None:
            return None
        fallback_status = attempted.status if attempted.status in {"none", "error"} else "none"
        return LyricsLookupResult(
            fallback_status,
            catalog_song_id=attempted.catalog_song_id,
        )

    def observe(self, session: PresenceSession | None) -> None:
        if self._closed:
            return
        if session is None:
            if self._current_key is not None:
                self._generation += 1
                self._current_key = None
                self._state = LyricsSnapshot(None, "idle")
                self._clear_pending()
            return

        key = _session_key(session)
        if key == self._current_key:
            return
        self._generation += 1
        self._current_key = key
        song_id = _presence_song_id(session)
        cached = self._cache_get(key)
        if cached is not None:
            self._clear_pending()
            self._state = LyricsSnapshot(
                song_id,
                cached.status,
                cached.lines,
                cached.catalog_song_id,
            )
            return
        attempted = self._attempted_result(key)
        if attempted is not None:
            self._clear_pending()
            self._state = LyricsSnapshot(
                song_id,
                attempted.status,
                catalog_song_id=attempted.catalog_song_id,
            )
            return

        self._state = LyricsSnapshot(song_id, "loading")
        self._enqueue_latest(
            _LyricsJob(
                generation=self._generation,
                key=key,
                song_id=song_id,
                session=session,
            )
        )

    def snapshot_for(self, session: PresenceSession | None) -> LyricsSnapshot | None:
        if session is None:
            return LyricsSnapshot(None, "idle")
        if _session_key(session) != self._current_key:
            return None
        return self._state

    async def _run_worker(self) -> None:
        if self._queue is None:
            return
        while True:
            job = await self._queue.get()
            try:
                cached = self._cache_get(job.key)
                if cached is None:
                    attempted = self._attempted_result(job.key)
                    if attempted is not None:
                        result = attempted
                    else:
                        try:
                            result = await self.client.lookup(job.session)
                            if not isinstance(result, LyricsLookupResult):
                                raise TypeError("lyrics client returned an invalid result")
                            if result.status not in {"ready", "none"}:
                                raise ValueError("lyrics client returned an invalid status")
                            result = LyricsLookupResult(
                                result.status,
                                result.lines,
                                _valid_provider_song_id(result.catalog_song_id),
                            )
                            if result.status == "ready" and not result.lines:
                                result = LyricsLookupResult(
                                    "none",
                                    catalog_song_id=result.catalog_song_id,
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            LOGGER.warning(
                                "lyrics lookup failed for current song: %s",
                                type(exc).__name__,
                            )
                            result = LyricsLookupResult(
                                "error",
                                catalog_song_id=(
                                    exc.catalog_song_id
                                    if isinstance(exc, LyricsLookupError)
                                    else None
                                ),
                            )
                        self._cache_put(job.key, result)
                else:
                    result = cached

                if self._closed:
                    return
                if job.generation != self._generation or job.key != self._current_key:
                    continue
                self._state = LyricsSnapshot(
                    job.song_id,
                    result.status,
                    result.lines,
                    result.catalog_song_id,
                )
                if self.on_change is not None:
                    try:
                        self.on_change()
                    except Exception:
                        LOGGER.debug("lyrics change callback raised", exc_info=True)
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._current_key = None
        self._clear_pending()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class SingleInstanceGuard:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.path, "a+b")
        self._fd.seek(0, os.SEEK_END)
        if self._fd.tell() == 0:
            self._fd.write(b"0")
            self._fd.flush()
        self._fd.seek(0)
        if platform.system() == "Windows":
            import msvcrt

            try:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self._fd.close()
                self._fd = None
                return False
            self._fd.seek(0)
            self._fd.truncate()
            self._fd.write(str(os.getpid()).encode("ascii"))
            self._fd.flush()
            return True

        try:
            import fcntl

            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.seek(0)
            self._fd.truncate()
            self._fd.write(str(os.getpid()).encode("ascii"))
            self._fd.flush()
            return True
        except Exception:
            self._fd.close()
            self._fd = None
            return False

    def release(self) -> None:
        if self._fd is None:
            return
        if platform.system() == "Windows":
            try:
                import msvcrt

                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            self._fd.close()
        finally:
            self._fd = None


class _WindowsGSMTCAdapter:
    def __init__(
        self,
        source_preference: Iterable[str] | None = None,
        ignored_sources: Iterable[str] | None = None,
    ) -> None:
        if platform.system() != "Windows":
            raise GSMTCUnsupportedError("GSMTC reader only supported on Windows")
        self.manager = None
        self.source_preference = tuple(
            item.strip().lower() for item in (source_preference or ())
            if item.strip()
        )
        self.ignored_sources = {
            item.strip().lower() for item in (ignored_sources or ())
            if item.strip()
        } | {item.lower() for item in DEFAULT_IGNORED_SOURCES}
        self._on_change: Callable[[], None] | None = None
        self._observed_sessions: set[int] = set()
        self._manager_events_attached = False

    @staticmethod
    def _iter_sessions(obj: Any) -> list[Any]:
        try:
            return list(obj)
        except Exception:
            return []

    def _attach_event(self, target: Any, name: str, callback: Callable[[], None]) -> bool:
        add_event = getattr(target, name, None)
        if not callable(add_event):
            return False
        try:
            add_event(callback)
            return True
        except TypeError:
            # Some runtimes expose slightly different signatures.
            try:
                add_event(callback, None)
                return True
            except Exception:
                return False
        except Exception:
            return False

    async def _maybe_await(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def initialize(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager,
        )

        self.manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        if self.manager is None:
            raise RuntimeError("unable to initialize GSMTC manager")
        self._attach_manager_events()

    def attach_change_listener(self, callback: Callable[[], None]) -> None:
        self._on_change = callback
        self._attach_manager_events()

    def _attach_manager_events(self) -> None:
        if self.manager is None or self._manager_events_attached:
            return

        attached = [
            self._attach_event(self.manager, name, self._emit_change)
            for name in ("add_current_session_changed", "add_sessions_changed")
        ]
        self._manager_events_attached = any(attached)
        if not self._manager_events_attached:
            LOGGER.warning("GSMTC manager events unavailable; heartbeat polling remains active")

    def _emit_change(self, *_args: Any) -> None:
        handler = self._on_change
        if handler is None:
            return
        try:
            handler()
        except Exception:
            LOGGER.debug("change callback raised", exc_info=True)

    async def _snapshot_from_session(self, session: Any) -> PresenceSession | None:
        if session is None:
            return None
        source = str(getattr(session, "source_app_user_model_id", "") or "")
        normalized_source = source.lower()
        if any(value and value in normalized_source for value in self.ignored_sources):
            return None

        media = await self._maybe_await(session.try_get_media_properties_async())
        if media is None:
            return None
        title = _safe_text(getattr(media, "title", ""), MAX_TEXT_LENGTH)
        artist = _safe_text(getattr(media, "artist", ""), MAX_TEXT_LENGTH)
        album = _safe_text(
            getattr(media, "album_title", "")
            or getattr(media, "album", "")
            or "",
            MAX_TEXT_LENGTH,
        )
        if not title and not artist:
            return None

        playback = await self._maybe_await(session.get_playback_info())
        timeline = await self._maybe_await(session.get_timeline_properties())
        raw_status = _coerce_status(getattr(playback, "playback_status", None))

        position = _normalize_timespan(getattr(timeline, "position", 0))
        end_time = _normalize_timespan(getattr(timeline, "end_time", 0))
        start_time = _normalize_timespan(getattr(timeline, "start_time", 0))
        duration = max(0.0, end_time - start_time)
        # If a player uses absolute position instead of timespan window, keep value.
        if duration == 0 and isinstance(playback, object):
            control = getattr(playback, "controls", None)
            if control is not None:
                try:
                    duration = max(0.0, _normalize_timespan(getattr(control, "estimated_duration", 0)))
                except Exception:
                    duration = 0.0
        position = _effective_position(
            max(0.0, position - start_time),
            duration,
            raw_status,
            getattr(timeline, "last_updated_time", None),
        )

        snapshot = PresenceSession(
            source_app_user_model_id=source,
            title=title,
            artist=artist,
            album=album,
            status=raw_status,
            playing=bool(raw_status == "playing"),
            position_seconds=position,
            duration_seconds=duration,
        )

        session_identity = id(session)
        if session_identity not in self._observed_sessions:
            attached = [
                self._attach_event(session, name, self._emit_change)
                for name in (
                    "add_media_properties_changed",
                    "add_playback_info_changed",
                    "add_timeline_properties_changed",
                )
            ]
            if any(attached):
                self._observed_sessions.add(session_identity)
        return snapshot

    async def list_sessions(self) -> list[PresenceSession]:
        if self.manager is None:
            raise RuntimeError("adapter not initialized")
        result: list[PresenceSession] = []
        for session in self._iter_sessions(self.manager.get_sessions()):
            snapshot = await self._snapshot_from_session(session)
            if snapshot is not None:
                result.append(snapshot)
        return result

    async def current_session(self) -> PresenceSession | None:
        if self.manager is None:
            return None
        direct = await self._snapshot_from_session(self.manager.get_current_session())
        if direct is not None and direct.playing:
            return direct
        sessions = await self.list_sessions()
        selected = _select_session(
            sessions,
            source_preference=self.source_preference,
            ignored_sources=self.ignored_sources,
        )
        if selected is not None and selected.playing:
            return selected
        return direct or selected


class GSMTCReader:
    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        basic_auth_user: str = "",
        basic_auth_password: str = "",
        heartbeat: float = DEFAULT_HEARTBEAT_SECONDS,
        source_preference: Iterable[str] | None = None,
        ignored_sources: Iterable[str] | None = None,
        lock_path: Path = DEFAULT_LOCK_FILE,
        adapter: _WindowsGSMTCAdapter | None = None,
        http_client: PresenceHttpClient | None = None,
        lyrics_client: LyricsHttpClient | None = None,
        lyrics_enricher: LyricsEnricher | None = None,
        lock: SingleInstanceGuard | None = None,
    ) -> None:
        self.endpoint = _normalize_endpoint(endpoint)
        self.token = _validate_token(token)
        self.heartbeat = max(1.0, float(heartbeat))
        self.client = http_client or PresenceHttpClient(
            endpoint,
            token,
            basic_auth_user=basic_auth_user,
            basic_auth_password=basic_auth_password,
        )
        self.adapter = adapter or _WindowsGSMTCAdapter(
            source_preference=source_preference,
            ignored_sources=ignored_sources,
        )
        self.notify = asyncio.Event()
        self._running = True
        self._sequence = 1
        self.lock = lock or SingleInstanceGuard(lock_path)
        self.notify_loop: asyncio.AbstractEventLoop | None = None
        self._last_session_key: tuple[str, str, str, str] | None = None
        session_id = f"gsmtc:{Path(sys.executable).name}:{os.getpid()}:{uuid.uuid4()}"
        self._sequence_session = PresencePayloadBuilder(session_id)
        self.lyrics = lyrics_enricher or LyricsEnricher(
            lyrics_client
            or LyricsHttpClient(
                endpoint,
                token,
                basic_auth_user=basic_auth_user,
                basic_auth_password=basic_auth_password,
            )
        )

    def _on_changed(self) -> None:
        if self.notify_loop is None:
            return
        try:
            self.notify_loop.call_soon_threadsafe(self.notify.set)
        except RuntimeError:
            pass

    async def _publish(self, session: PresenceSession | None, *, sequence: int) -> bool:
        payload = self._sequence_session.build(
            sequence,
            session,
            self.lyrics.snapshot_for(session),
        )
        source = session.source_app_user_model_id if session else "none"
        status = _coerce_status(payload["playback"]["status"]) if session else "idle"
        try:
            result = await self.client.post(payload)
        except Exception as exc:
            LOGGER.warning("presence post failed: %s", exc)
            return False

        if result.status == 200:
            LOGGER.info(
                "presence posted: source=%s status=%s seq=%s title=%s",
                _safe_text(source, 72),
                status,
                sequence,
                _safe_text(session.title if session else "", 80),
            )
            return True
        if result.status == 409:
            LOGGER.warning(
                "presence sequence rejected (already consumed) seq=%s source=%s",
                sequence,
                _safe_text(source, 72),
            )
            return True
        LOGGER.warning(
            "presence rejected: status=%s seq=%s source=%s body=%s",
            result.status,
            sequence,
            _safe_text(source, 72),
            "present" if result.body else "empty",
        )
        return False

    async def _send_once(self, session: PresenceSession | None) -> bool:
        success = await self._publish(session, sequence=self._sequence)
        if success:
            self._sequence += 1
        return success

    def _log_selected_session(self, session: PresenceSession | None) -> None:
        key = (
            session.source_app_user_model_id if session else "",
            session.title if session else "",
            session.artist if session else "",
            session.status if session else "idle",
        )
        if key == self._last_session_key:
            return
        self._last_session_key = key
        if session is None:
            LOGGER.info("active session: none")
            return
        LOGGER.info(
            "active session: source=%s title=%s artist=%s status=%s",
            _safe_text(session.source_app_user_model_id, 72),
            _safe_text(session.title, 80),
            _safe_text(session.artist, 80),
            _coerce_status(session.status),
        )

    async def run(self) -> None:
        if not self.lock.acquire():
            raise RuntimeError("another reader instance is already running")
        self.notify_loop = asyncio.get_running_loop()
        retry_wait = self.heartbeat
        lyrics_started = False
        try:
            await self.lyrics.start()
            lyrics_started = True
            await self.adapter.initialize()
            self.adapter.attach_change_listener(self._on_changed)
            current = await self.adapter.current_session()
            self.lyrics.observe(current)
            self._log_selected_session(current)
            initial_success = await self._send_once(current)
            if not initial_success:
                retry_wait = min(DEFAULT_RETRY_DELAY_SECONDS, self.heartbeat * 2)
            while self._running:
                try:
                    await asyncio.wait_for(self.notify.wait(), timeout=retry_wait)
                except asyncio.TimeoutError:
                    pass
                self.notify.clear()
                session = await self.adapter.current_session()
                self.lyrics.observe(session)
                self._log_selected_session(session)
                success = await self._publish(session, sequence=self._sequence)
                if success:
                    self._sequence += 1
                retry_wait = self.heartbeat if success else min(
                    DEFAULT_RETRY_DELAY_SECONDS,
                    retry_wait * 2,
                )
        finally:
            try:
                if lyrics_started:
                    await self.lyrics.close()
            finally:
                self.lock.release()

    def stop(self) -> None:
        self._running = False
        self.notify.set()


def parse_preference(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in NON_SECRET_ENV_KEYS:
            result[key] = value.strip().strip("\"'")
    return result


def _configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows GSMTC reader for eryu presence")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--heartbeat", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--player-preference", default="")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def _resolve_token(env: dict[str, str]) -> str:
    return (
        env.get("ERYU_PRESENCE_TOKEN", "")
        or env.get("ERYU_AUTH_TOKEN", "")
    )


async def _list_sessions(args: argparse.Namespace, env: dict[str, str]) -> int:
    if platform.system() != "Windows":
        print("session listing requires Windows")
        return 1
    try:
        adapter = _WindowsGSMTCAdapter(
            parse_preference(
                args.player_preference or env.get("ERYU_GSMTC_PLAYER_PREFERENCES")
            ),
            parse_preference(env.get("ERYU_GSMTC_IGNORED_SOURCES")),
        )
        await adapter.initialize()
        sessions = await adapter.list_sessions()
    except Exception as exc:
        print(f"list sessions failed: {exc}")
        return 1

    if not sessions:
        print("No GSMTC sessions detected.")
        return 0
    print("GSMTC sessions:")
    for index, session in enumerate(sessions, start=1):
        source = session.source_app_user_model_id or "unknown"
        title = session.title or "unknown"
        artist = session.artist or "unknown"
        print(
            f"{index}. SourceAppUserModelId={source} title={title} "
            f"artist={artist} status={session.status} playing={session.playing}"
        )
    return 0


async def _amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_level)
    env = _read_env(Path(args.env_file))
    env.update(os.environ)

    # Authentication is process-only. The optional .env file is intentionally
    # limited to non-secret Reader configuration.
    token = _resolve_token(dict(os.environ))
    try:
        heartbeat = float(env.get("ERYU_PRESENCE_HEARTBEAT_SECONDS", args.heartbeat))
    except (TypeError, ValueError):
        print("ERYU_PRESENCE_HEARTBEAT_SECONDS must be a number")
        return 1
    preferred = parse_preference(
        args.player_preference or env.get("ERYU_GSMTC_PLAYER_PREFERENCES")
    )
    ignored = parse_preference(env.get("ERYU_GSMTC_IGNORED_SOURCES"))
    if args.list_sessions:
        return await _list_sessions(args, env)
    if not token:
        print("Missing token: set ERYU_PRESENCE_TOKEN or ERYU_AUTH_TOKEN")
        return 1

    try:
        endpoint = _normalize_endpoint(env.get("ERYU_PRESENCE_ENDPOINT", args.endpoint))
        basic_auth_user = env.get("ERYU_PRESENCE_BASIC_AUTH_USER", "")
        basic_auth_password = os.environ.get("ERYU_PRESENCE_BASIC_AUTH_PASSWORD", "")
        reader = GSMTCReader(
            endpoint,
            token,
            basic_auth_user=basic_auth_user,
            basic_auth_password=basic_auth_password,
            heartbeat=heartbeat,
            source_preference=preferred,
            ignored_sources=ignored,
        )
    except ValueError as exc:
        print(str(exc))
        return 1

    def stop() -> None:
        reader.stop()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop)
        except (NotImplementedError, AttributeError):
            pass

    try:
        await reader.run()
        return 0
    except asyncio.CancelledError:
        reader.stop()
        return 0
    except KeyboardInterrupt:
        reader.stop()
        return 0
    except Exception as exc:
        LOGGER.error("reader stopped: %s", exc)
        return 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
