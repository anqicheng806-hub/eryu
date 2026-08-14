"""Validation and in-memory storage for the read-only music presence feed."""

from __future__ import annotations

import copy
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any


DEFAULT_PRESENCE_TTL_SECONDS = 10.0
MAX_PRESENCE_TTL_SECONDS = 3600.0
MAX_TEXT_LENGTH = 512
MAX_URL_LENGTH = 2048
MAX_CLIENT_SESSION_ID_LENGTH = 128
MAX_LYRIC_LINES_PER_SIDE = 5
MAX_LYRIC_TEXT_LENGTH = 2000

_TTL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]+\Z")
_SONG_ID_PATTERN = re.compile(r"[0-9]{1,20}\Z")
_PLAYBACK_STATUSES = frozenset(
    {"idle", "loading", "playing", "paused", "ended", "error"}
)
_LYRICS_STATUSES = frozenset({"idle", "loading", "ready", "none", "error"})


class PresenceValidationError(ValueError):
    """Raised when a presence payload does not match the public schema."""


class PresenceSequenceError(PresenceValidationError):
    """Raised when a client replays an old or duplicate presence sequence."""


def parse_presence_ttl(raw_value: str | None) -> float:
    """Parse the TTL without accepting whitespace, signs, or exponent notation."""
    if raw_value is None:
        return DEFAULT_PRESENCE_TTL_SECONDS
    if not isinstance(raw_value, str) or not _TTL_PATTERN.fullmatch(raw_value):
        raise ValueError("MUSIC_PRESENCE_TTL_SECONDS must be a plain positive number")
    value = float(raw_value)
    if not math.isfinite(value) or value <= 0 or value > MAX_PRESENCE_TTL_SECONDS:
        raise ValueError(
            "MUSIC_PRESENCE_TTL_SECONDS must be greater than 0 and at most 3600"
        )
    return value


def is_valid_song_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value <= 10**20 - 1
    return isinstance(value, str) and bool(_SONG_ID_PATTERN.fullmatch(value)) and int(value) > 0


def _expect_exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PresenceValidationError(f"{field} has invalid fields ({'; '.join(details)})")


def _expect_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PresenceValidationError(f"{field} must be an object")
    return value


def _expect_string(value: Any, field: str, max_length: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise PresenceValidationError(f"{field} must be a string")
    if not allow_empty and not value:
        raise PresenceValidationError(f"{field} must not be empty")
    if len(value) > max_length:
        raise PresenceValidationError(f"{field} is too long")
    return value


def _expect_integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PresenceValidationError(f"{field} must be an integer >= {minimum}")
    if value > 2**53 - 1:
        raise PresenceValidationError(f"{field} is too large")
    return value


def _expect_number(
    value: Any,
    field: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresenceValidationError(f"{field} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise PresenceValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum:
        raise PresenceValidationError(f"{field} must be a finite number >= {minimum:g}")
    if maximum is not None and number > maximum:
        raise PresenceValidationError(f"{field} must be <= {maximum:g}")
    return value


def _validate_reported_at(value: Any) -> str:
    reported_at = _expect_string(value, "reportedAt", 64, allow_empty=False)
    candidate = reported_at[:-1] + "+00:00" if reported_at.endswith("Z") else reported_at
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise PresenceValidationError("reportedAt must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PresenceValidationError("reportedAt must include a timezone")
    return reported_at


def _validate_song(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    song = _expect_mapping(value, "song")
    _expect_exact_keys(song, {"songId", "name", "artist", "album", "cover"}, "song")
    if not is_valid_song_id(song["songId"]):
        raise PresenceValidationError("song.songId must be a positive numeric id")
    return {
        "songId": str(song["songId"]),
        "name": _expect_string(song["name"], "song.name", MAX_TEXT_LENGTH),
        "artist": _expect_string(song["artist"], "song.artist", MAX_TEXT_LENGTH),
        "album": _expect_string(song["album"], "song.album", MAX_TEXT_LENGTH),
        "cover": _expect_string(song["cover"], "song.cover", MAX_URL_LENGTH),
    }


def _validate_playback(value: Any) -> dict[str, Any]:
    playback = _expect_mapping(value, "playback")
    _expect_exact_keys(
        playback,
        {"status", "playing", "positionSeconds", "durationSeconds", "progressRatio"},
        "playback",
    )
    status = _expect_string(playback["status"], "playback.status", 16, allow_empty=False)
    if status not in _PLAYBACK_STATUSES:
        raise PresenceValidationError("playback.status is invalid")
    playing = playback["playing"]
    if not isinstance(playing, bool):
        raise PresenceValidationError("playback.playing must be a boolean")
    if playing != (status == "playing"):
        raise PresenceValidationError("playback.playing conflicts with playback.status")
    return {
        "status": status,
        "playing": playing,
        "positionSeconds": _expect_number(
            playback["positionSeconds"], "playback.positionSeconds"
        ),
        "durationSeconds": _expect_number(
            playback["durationSeconds"], "playback.durationSeconds"
        ),
        "progressRatio": _expect_number(
            playback["progressRatio"], "playback.progressRatio", maximum=1.0
        ),
    }


def _validate_lyric_line(value: Any, field: str) -> dict[str, Any]:
    line = _expect_mapping(value, field)
    _expect_exact_keys(line, {"index", "timeSeconds", "text", "translation"}, field)
    return {
        "index": _expect_integer(line["index"], f"{field}.index"),
        "timeSeconds": _expect_number(line["timeSeconds"], f"{field}.timeSeconds"),
        "text": _expect_string(line["text"], f"{field}.text", MAX_LYRIC_TEXT_LENGTH),
        "translation": _expect_string(
            line["translation"], f"{field}.translation", MAX_LYRIC_TEXT_LENGTH
        ),
    }


def _validate_line_list(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PresenceValidationError(f"{field} must be an array")
    if len(value) > MAX_LYRIC_LINES_PER_SIDE:
        raise PresenceValidationError(f"{field} has too many lyric lines")
    return [_validate_lyric_line(line, f"{field}[{index}]") for index, line in enumerate(value)]


def _validate_lyrics(value: Any, song: dict[str, Any] | None) -> dict[str, Any]:
    lyrics = _expect_mapping(value, "lyrics")
    _expect_exact_keys(
        lyrics,
        {"songId", "status", "currentIndex", "current", "previous", "next"},
        "lyrics",
    )
    lyrics_song_id = lyrics["songId"]
    if song is None:
        if lyrics_song_id is not None:
            raise PresenceValidationError("lyrics.songId must be null when song is null")
    else:
        if not is_valid_song_id(lyrics_song_id) or str(lyrics_song_id) != song["songId"]:
            raise PresenceValidationError("lyrics.songId must match song.songId")

    status = _expect_string(lyrics["status"], "lyrics.status", 16, allow_empty=False)
    if status not in _LYRICS_STATUSES:
        raise PresenceValidationError("lyrics.status is invalid")
    current = lyrics["current"]
    if current is not None:
        current = _validate_lyric_line(current, "lyrics.current")
    current_index = _expect_integer(
        lyrics["currentIndex"], "lyrics.currentIndex", minimum=-1
    )
    if current is None and current_index != -1:
        raise PresenceValidationError("lyrics.currentIndex must be -1 when current is null")
    if current is not None and current_index != current["index"]:
        raise PresenceValidationError("lyrics.currentIndex must match lyrics.current.index")
    return {
        "songId": None if lyrics_song_id is None else str(lyrics_song_id),
        "status": status,
        "currentIndex": current_index,
        "current": current,
        "previous": _validate_line_list(lyrics["previous"], "lyrics.previous"),
        "next": _validate_line_list(lyrics["next"], "lyrics.next"),
    }


def validate_presence_payload(value: Any) -> dict[str, Any]:
    payload = _expect_mapping(value, "presence")
    _expect_exact_keys(
        payload,
        {
            "schemaVersion",
            "clientSessionId",
            "sequence",
            "reportedAt",
            "song",
            "playback",
            "lyrics",
        },
        "presence",
    )
    schema_version = payload["schemaVersion"]
    if isinstance(schema_version, bool) or schema_version != 1:
        raise PresenceValidationError("schemaVersion must be 1")
    client_session_id = _expect_string(
        payload["clientSessionId"],
        "clientSessionId",
        MAX_CLIENT_SESSION_ID_LENGTH,
        allow_empty=False,
    )
    if not _SESSION_ID_PATTERN.fullmatch(client_session_id):
        raise PresenceValidationError("clientSessionId contains unsupported characters")
    song = _validate_song(payload["song"])
    return {
        "schemaVersion": 1,
        "clientSessionId": client_session_id,
        "sequence": _expect_integer(payload["sequence"], "sequence"),
        "reportedAt": _validate_reported_at(payload["reportedAt"]),
        "song": song,
        "playback": _validate_playback(payload["playback"]),
        "lyrics": _validate_lyrics(payload["lyrics"], song),
    }


class PresenceStore:
    """Thread-safe, process-local latest-presence store."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._received_monotonic: float | None = None
        self._received_at: str | None = None
        self._last_sequence_by_session: dict[str, int] = {}

    def update(self, payload: Any) -> dict[str, Any]:
        snapshot = validate_presence_payload(payload)
        with self._lock:
            session_id = snapshot["clientSessionId"]
            sequence = snapshot["sequence"]
            previous_sequence = self._last_sequence_by_session.get(session_id)
            if previous_sequence is not None and sequence <= previous_sequence:
                raise PresenceSequenceError(
                    "presence sequence must strictly increase for a client session"
                )
            received_monotonic = self._monotonic()
            received_at = (
                self._utcnow()
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            self._last_sequence_by_session[session_id] = sequence
            self._snapshot = snapshot
            self._received_monotonic = received_monotonic
            self._received_at = received_at
        return self.read()

    def read(self) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
            received_monotonic = self._received_monotonic
            received_at = self._received_at

        if snapshot is None or received_monotonic is None or received_at is None:
            state = "absent"
            stale = True
            age_seconds = None
        else:
            age = max(0.0, now - received_monotonic)
            stale = age >= self.ttl_seconds
            state = "stale" if stale else "fresh"
            age_seconds = round(age, 3)

        return {
            "ok": True,
            "presence": snapshot,
            "freshness": {
                "state": state,
                "stale": stale,
                "ageSeconds": age_seconds,
                "staleAfterSeconds": self.ttl_seconds,
                "receivedAt": received_at,
            },
        }
