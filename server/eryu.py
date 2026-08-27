#!/usr/bin/env python3
"""eryu — standalone music server for Netease Cloud Music.

Zero external dependencies (Python stdlib only). Handles:
  - Song search, audio URL resolution with CDN fallback, audio streaming
  - Lyrics with translation caching (.lrc + .tlyric)
  - Playlist CRUD (single default + multi-playlist system)
  - Recent play history
  - Music profile (avatar, signature, background)
  - Daily recommendations (based on liked songs)
  - Song memory / notes system
  - Listening stats
  - Roam mode (random genre discovery)
  - Similar song discovery
  - Remote play (push a song to another client)
  - Background audio analysis (via analyze_song.py subprocess)
  - Listen-complete tracking (together count)
  - Static file serving for cached mp3s and frontend

Usage:
    python3 server/eryu.py                     # port 9090
    PORT=8080 python3 server/eryu.py           # custom port

Data layout:
    ./data/music_cache/    — cached mp3, lrc, tlyric, analysis files
    ./data/music_data.json — playlists, recent, profile
    ./data/music_memory.json — per-song memory (notes, listen counts)
    ./data/music_playlist.json — legacy flat playlist (synced with liked)
    ./data/music_remote.json — ephemeral remote-play payload
Environment:
    ERYU_HOST                  - IPv4 listen address (default: 127.0.0.1)
    ERYU_PORT                  - listen port (default: 9090; PORT is legacy)
    ERYU_DATA_DIR              - absolute persistent data directory
    ERYU_ALLOWED_ORIGIN        - exact web origin (default: * for local use)
    ERYU_AUTH_TOKEN            — full-access API token (required)
    ERYU_MCP_READ_TOKEN         — read-only MCP token (required, must differ)
    MUSIC_U                    — NetEase cookie value (optional)
    MUSIC_PRESENCE_TTL_SECONDS — presence freshness TTL (default: 10)
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import mimetypes
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.request
import urllib.error

try:
    from .presence import (
        PresenceStore,
        PresenceSequenceError,
        PresenceValidationError,
        is_valid_song_id,
        parse_presence_ttl,
    )
except ImportError:  # Running as ``python server/eryu.py``.
    from presence import (
        PresenceStore,
        PresenceSequenceError,
        PresenceValidationError,
        is_valid_song_id,
        parse_presence_ttl,
    )

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("eryu")


# ── Secret management ────────────────────────────────────────────────────────

MAX_JSON_BODY_BYTES = 64 * 1024
MAX_JSON_NESTING_DEPTH = 64
MAX_SPECTROGRAM_BYTES = 8 * 1024 * 1024
MAX_MEMORY_TEXT_LENGTH = 512
MAX_MEMORY_EVENT_IDS = 64
MAX_TRACK_DURATION_SECONDS = 24 * 60 * 60
LISTEN_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
LISTEN_EVENT_KEYS = frozenset(
    {
        "eventId",
        "songId",
        "name",
        "artist",
        "album",
        "durationSeconds",
        "catalog",
    }
)
ANALYZER_ENV_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)
MCP_READ_PATHS = frozenset(
    {
        "/music/presence",
        "/music/analyze/status",
        "/music/analyze/spectrogram",
        "/music/memory",
    }
)


def _analyzer_environment(cache_dir: Path) -> dict[str, str]:
    """Build a minimal child environment without any API token or cookie."""

    runtime_cache = cache_dir / ".analysis_runtime"
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in ANALYZER_ENV_ALLOWLIST and value
    }
    environment.update(
        {
            "HOME": str(runtime_cache / "home"),
            "MPLCONFIGDIR": str(cache_dir / ".matplotlib"),
            "NUMBA_CACHE_DIR": str(cache_dir / ".numba"),
            "PYTHONNOUSERSITE": "1",
            "XDG_CACHE_HOME": str(runtime_cache / "cache"),
            "XDG_CONFIG_HOME": str(runtime_cache / "config"),
        }
    )
    if os.name == "nt":
        environment.update(
            {
                "APPDATA": str(runtime_cache / "appdata"),
                "LOCALAPPDATA": str(runtime_cache / "localappdata"),
                "PROGRAMDATA": str(runtime_cache / "programdata"),
            }
        )
    return environment


class RequestBodyError(ValueError):
    def __init__(self, status: int, message: str, *, close_connection: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.close_connection = close_connection


class SongMemoryStoreError(RuntimeError):
    """A sanitized persistent-memory failure safe to report without details."""


def _json_nesting_exceeds_limit(value: Any) -> bool:
    """Check parsed container depth without relying on Python recursion limits."""

    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_NESTING_DEPTH:
            return True
        if isinstance(current, dict):
            children = current.values()
        elif isinstance(current, list):
            children = current
        else:
            continue
        child_depth = depth + 1
        stack.extend(
            (child, child_depth)
            for child in children
            if isinstance(child, (dict, list))
        )
    return False


def _parse_server_host(value: str | None) -> str:
    """Keep the player backend pinned to the exact loopback address."""
    if value is None:
        return "127.0.0.1"
    if value != "127.0.0.1":
        raise ValueError("ERYU_HOST must be exactly 127.0.0.1")
    return value


def _parse_server_port(value: str | None) -> int:
    """Parse a decimal TCP port without accepting whitespace or sign prefixes."""
    if value is None:
        return 9090
    if not re.fullmatch(r"[1-9][0-9]{0,4}", value):
        raise ValueError("ERYU_PORT (or legacy PORT) must be an integer from 1 to 65535")
    port = int(value)
    if port > 65535:
        raise ValueError("ERYU_PORT (or legacy PORT) must be an integer from 1 to 65535")
    return port


def _load_server_port() -> int:
    """Prefer ERYU_PORT while retaining the project's legacy PORT setting."""
    value = os.environ.get("ERYU_PORT")
    if value is None:
        value = os.environ.get("PORT")
    return _parse_server_port(value)


def _parse_data_dir(value: str | None) -> Path:
    """Resolve the configured persistent directory without relying on the CWD."""
    if value is None:
        return (HERE / "data").resolve()
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError("ERYU_DATA_DIR must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("ERYU_DATA_DIR must be an absolute path")
    return path.resolve()


def _parse_allowed_origin(value: str | None) -> str:
    """Accept one exact HTTP(S) origin; retain ``*`` only as the local default."""
    if value is None or value == "*":
        return "*"
    if not value or value != value.strip() or any(char.isspace() for char in value):
        raise ValueError("ERYU_ALLOWED_ORIGIN must be one exact HTTP(S) origin")
    parsed = urlparse(value)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("ERYU_ALLOWED_ORIGIN has an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or (parsed_port is not None and not (1 <= parsed_port <= 65535))
    ):
        raise ValueError("ERYU_ALLOWED_ORIGIN must be one exact HTTP(S) origin")
    return value


def _load_auth_tokens() -> tuple[str, str]:
    full_token = os.environ.get("ERYU_AUTH_TOKEN", "")
    read_token = os.environ.get("ERYU_MCP_READ_TOKEN", "")
    if (
        not full_token
        or full_token != full_token.strip()
        or len(full_token) < 32
        or any(char.isspace() for char in full_token)
    ):
        raise RuntimeError(
            "ERYU_AUTH_TOKEN must be a non-whitespace value of at least 32 characters"
        )
    if (
        not read_token
        or read_token != read_token.strip()
        or len(read_token) < 32
        or any(char.isspace() for char in read_token)
    ):
        raise RuntimeError(
            "ERYU_MCP_READ_TOKEN must be a non-whitespace value of at least 32 characters"
        )
    if hmac.compare_digest(full_token, read_token):
        raise RuntimeError("ERYU_AUTH_TOKEN and ERYU_MCP_READ_TOKEN must differ")
    return full_token, read_token


def _safe_analysis_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return value


def _duration_seconds_from_milliseconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        milliseconds = float(value)
    except (OverflowError, ValueError):
        return 0.0
    if not math.isfinite(milliseconds) or milliseconds < 0:
        return 0.0
    return round(milliseconds / 1000.0, 3)


def _sanitize_analysis_result(
    result: Any, song_id: str, *, spectrogram_available: bool
) -> dict[str, Any] | None:
    """Return only stable analysis fields; never expose cache filesystem paths."""
    if not isinstance(result, dict):
        return None
    sanitized: dict[str, Any] = {
        "songId": song_id,
        "name": result.get("name", "") if isinstance(result.get("name", ""), str) else "",
        "artist": (
            result.get("artist", "") if isinstance(result.get("artist", ""), str) else ""
        ),
        "spectrogramAvailable": spectrogram_available,
    }
    sanitized["name"] = sanitized["name"][:512]
    sanitized["artist"] = sanitized["artist"][:512]
    for key in ("duration", "bpm"):
        number = _safe_analysis_number(result.get(key))
        if number is not None and number >= 0:
            sanitized[key] = number
    key_name = result.get("key")
    if isinstance(key_name, str):
        sanitized["key"] = key_name[:32]

    segments: list[dict[str, float | int]] = []
    raw_segments = result.get("segments")
    if isinstance(raw_segments, list):
        for raw_segment in raw_segments[:64]:
            if not isinstance(raw_segment, dict):
                continue
            segment: dict[str, float | int] = {}
            valid = True
            for field in ("start", "end", "avgEnergy", "maxEnergy"):
                number = _safe_analysis_number(raw_segment.get(field))
                if number is None or number < 0:
                    valid = False
                    break
                segment[field] = number
            if valid:
                segments.append(segment)
    sanitized["segments"] = segments
    return sanitized


# ── Request handler ──────────────────────────────────────────────────────────

class EryuHandler(BaseHTTPRequestHandler):
    state: "ServerState"

    server_version = "Eryu/1.0"

    def log_message(self, fmt, *args):
        # Never log query strings: legacy ``?token=`` requests must not leak
        # credentials even though query-parameter authentication is rejected.
        status = str(args[1]) if len(args) > 1 else "-"
        logger.info(
            "%s %s %s %s",
            self.address_string(),
            getattr(self, "command", "-"),
            urlparse(getattr(self, "path", "")).path,
            status,
        )

    # ── Helpers ──

    def _read_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise RequestBodyError(400, "invalid Content-Length") from exc
        if length < 0:
            raise RequestBodyError(400, "invalid Content-Length")
        if length > MAX_JSON_BODY_BYTES:
            raise RequestBodyError(413, "request body too large", close_connection=True)
        if not length:
            return {}
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise RequestBodyError(415, "Content-Type must be application/json")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RequestBodyError(400, "invalid JSON body") from exc
        if not isinstance(body, dict):
            raise RequestBodyError(400, "JSON body must be an object")
        if _json_nesting_exceeds_limit(body):
            raise RequestBodyError(400, "invalid JSON body")
        return body

    def _mcp_read_route_allowed(self, path: str) -> bool:
        if path not in MCP_READ_PATHS:
            return False
        parsed = urlparse(self.path)
        if path == "/music/presence":
            return not parsed.query
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"id"}:
            return False
        song_ids = query.get("id", [])
        if len(song_ids) != 1 or not is_valid_song_id(song_ids[0]):
            return False
        current = self.state.presence.read()
        if current.get("freshness", {}).get("state") != "fresh":
            return False
        presence = current.get("presence")
        song = presence.get("song") if isinstance(presence, dict) else None
        return isinstance(song, dict) and hmac.compare_digest(
            str(song.get("songId", "")), str(song_ids[0])
        )

    def _resolve_mcp_analysis_cache_song_id(
        self, public_song_id: str
    ) -> tuple[bool, str | None]:
        """Recheck current presence and resolve its private analysis cache key."""

        current = self.state.presence.read()
        if current.get("freshness", {}).get("state") != "fresh":
            return False, None
        presence = current.get("presence")
        if not isinstance(presence, dict):
            return False, None
        song = presence.get("song")
        if not isinstance(song, dict) or not hmac.compare_digest(
            str(song.get("songId", "")), public_song_id
        ):
            return False, None
        if presence.get("schemaVersion") != 2:
            return True, public_song_id
        catalog = song.get("catalog")
        if catalog is None:
            return True, None
        if (
            not isinstance(catalog, dict)
            or catalog.get("provider") != "netease"
            or not isinstance(catalog.get("songId"), str)
            or not is_valid_song_id(catalog["songId"])
            or str(int(catalog["songId"])) != catalog["songId"]
        ):
            return False, None
        return True, catalog["songId"]

    def _check_auth(self, path: str, method: str) -> str | None:
        token = self.headers.get("X-Auth-Token", "")
        if not token:
            return None
        if hmac.compare_digest(token, self.state.full_auth_token):
            return "full"
        if (
            hmac.compare_digest(token, self.state.mcp_read_token)
            and method == "GET"
            and self._mcp_read_route_allowed(path)
        ):
            return "mcp_read"
        return None

    def _require_auth(self, path: str, method: str) -> bool:
        auth_role = self._check_auth(path, method)
        if auth_role:
            self.auth_role = auth_role
            return True
        self._send_json(403, {"error": "auth required"})
        return False

    def _send_json(self, status: int, body: dict[str, Any]):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", self.state.allowed_origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()
        self.wfile.write(data)

    def _send_health_ok(self):
        """Return the smallest useful health response without server metadata."""
        data = b"ok"
        self.send_response_only(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(
        self,
        file_path: Path,
        content_type: str | None = None,
        *,
        cache_control: str | None = None,
    ):
        """Serve a file with proper headers and Range support."""
        if not file_path.exists() or not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        size = file_path.stat().st_size
        if content_type is None:
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

        # Range request support (needed for audio seeking)
        range_header = self.headers.get("Range")
        if range_header:
            try:
                range_spec = range_header.replace("bytes=", "")
                start_str, end_str = range_spec.split("-", 1)
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Type", content_type)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", self.state.allowed_origin)
                if cache_control is not None:
                    self.send_header("Cache-Control", cache_control)
                self.end_headers()
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass  # Fall through to full response

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", self.state.allowed_origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        if cache_control is not None:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.state.allowed_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, Range")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # ── Netease helpers ──

    def _netease_cookie(self) -> str:
        music_u = os.environ.get("MUSIC_U", "")
        return f"MUSIC_U={music_u}" if music_u else ""

    def _netease_request(self, url: str, data: bytes | None = None,
                         extra_headers: dict[str, str] | None = None,
                         timeout: int = 10) -> Any:
        """Make an authenticated request to Netease API and return parsed JSON."""
        headers = {
            "Cookie": self._netease_cookie(),
            "Referer": "https://music.163.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if extra_headers:
            headers.update(extra_headers)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _ensure_cover(self, song_id, cover: str = "") -> str:
        if cover:
            return cover
        try:
            url = f"https://music.163.com/api/song/detail?ids=[{song_id}]"
            d = self._netease_request(url)
            return d.get("songs", [{}])[0].get("album", {}).get("picUrl", "")
        except Exception:
            return ""

    # ── Data helpers ──

    def _playlist_path(self) -> Path:
        return self.state.data_dir / "music_playlist.json"

    def _load_playlist(self) -> list:
        p = self._playlist_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return []

    def _save_playlist(self, songs: list):
        p = self._playlist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(songs, ensure_ascii=False))

    def _music_data_path(self) -> Path:
        return self.state.data_dir / "music_data.json"

    def _load_music_data(self) -> dict:
        p = self._music_data_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        # Bootstrap from legacy playlist
        old = self._load_playlist()
        data = {
            "playlists": [{"id": "liked", "name": "Liked", "songs": old}],
            "recent": [],
            "profile": {"avatar": "", "signature": "", "bg": ""},
        }
        self._save_music_data(data)
        return data

    def _save_music_data(self, data: dict):
        p = self._music_data_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1))

    def _song_memory_path(self) -> Path:
        return self.state.data_dir / "music_memory.json"

    def _load_song_memory(self) -> dict:
        p = self._song_memory_path()
        if not p.exists():
            return {}
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SongMemoryStoreError("song memory is unavailable") from exc
        if not isinstance(value, dict):
            raise SongMemoryStoreError("song memory is unavailable")
        return value

    def _save_song_memory(self, mem: dict):
        p = self._song_memory_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        temp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(mem, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, p)
        except (OSError, TypeError, ValueError) as exc:
            raise SongMemoryStoreError("song memory is unavailable") from exc
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    # ── Audio download with CDN fallback ──

    def _download_audio(self, audio_url: str, cache_file: Path):
        """Download audio to cache_file with CDN fallback for overseas servers."""
        def _dl(dl_url: str):
            areq = urllib.request.Request(dl_url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://music.163.com",
                "Cookie": self._netease_cookie(),
            })
            tmp = cache_file.with_suffix(".tmp")
            with urllib.request.urlopen(areq, timeout=120) as aresp:
                with open(tmp, "wb") as f:
                    while True:
                        chunk = aresp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
            tmp.rename(cache_file)

        try:
            _dl(audio_url)
        except urllib.error.HTTPError:
            # CDN fallback: m*.music.126.net -> m701.music.126.net
            fallback = re.sub(r'm\d+\.music\.126\.net', 'm701.music.126.net', audio_url)
            _dl(fallback)

    def _fetch_music_url(self, song_id) -> bool:
        """Ensure audio is cached, return True if available."""
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            return True
        try:
            url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=128000"
            raw = self._netease_request(url)
            audio_url = (raw.get("data") or [{}])[0].get("url")
            if not audio_url:
                return False
            self._download_audio(audio_url, cache_file)
            return cache_file.exists() and cache_file.stat().st_size > 1000
        except Exception:
            return False

    # ── GET routes ────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Health check (no auth)
        if path == "/health":
            self._send_health_ok()
            return

        # Browser audio cannot attach the API header; expose numeric MP3s only.
        if path.startswith("/music/file/"):
            self._serve_music_file(path)
            return

        # Static: frontend files from ../client/
        if path == "/" or not path.startswith("/music") and not path.startswith("/health"):
            # Serve frontend static files
            self._serve_static(path)
            return

        # All /music/* endpoints below require auth
        if not self._require_auth(path, "GET"):
            return

        if path == "/music/search":
            self._handle_music_search()
        elif path == "/music/url":
            self._handle_music_url()
        elif path == "/music/stream":
            self._handle_music_stream()
        elif path == "/music/lyric":
            self._handle_music_lyric()
        elif path == "/music/playlist":
            self._handle_music_playlist_get()
        elif path == "/music/playlists":
            self._handle_music_playlists_list()
        elif path == "/music/playlists/songs":
            self._handle_music_playlists_songs()
        elif path == "/music/recent":
            self._handle_music_recent_get()
        elif path == "/music/profile":
            self._handle_music_profile_get()
        elif path == "/music/daily":
            self._handle_music_daily()
        elif path == "/music/memory":
            self._handle_music_memory_get()
        elif path == "/music/stats":
            self._handle_music_stats()
        elif path == "/music/roam":
            self._handle_music_roam()
        elif path == "/music/similar":
            self._handle_music_similar()
        elif path == "/music/remote":
            self._handle_music_remote_get()
        elif path == "/music/presence":
            self._handle_music_presence_get()
        elif path == "/music/analyze/status":
            self._handle_analyze_status()
        elif path == "/music/analyze/spectrogram":
            self._handle_analyze_spectrogram()
        else:
            self._send_json(404, {"error": "not found"})

    # ── POST routes ───────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._require_auth(path, "POST"):
            return
        try:
            body = self._read_body()
        except RequestBodyError as exc:
            if exc.close_connection:
                self.close_connection = True
            self._send_json(exc.status, {"error": exc.message})
            return

        if path == "/music/playlist/add":
            self._handle_music_playlist_add(body)
        elif path == "/music/playlist/remove":
            self._handle_music_playlist_remove(body)
        elif path == "/music/playlists/create":
            self._handle_music_playlists_create(body)
        elif path == "/music/playlists/rename":
            self._handle_music_playlists_rename(body)
        elif path == "/music/playlists/delete":
            self._handle_music_playlists_delete(body)
        elif path == "/music/playlists/add-song":
            self._handle_music_playlists_add_song(body)
        elif path == "/music/playlists/remove-song":
            self._handle_music_playlists_remove_song(body)
        elif path == "/music/recent/add":
            self._handle_music_recent_add(body)
        elif path == "/music/memory":
            self._handle_music_memory_save(body)
        elif path == "/music/listen":
            self._handle_music_listen(body)
        elif path == "/music/analyze":
            self._handle_analyze_trigger(body)
        elif path == "/music/listen-together":
            self._handle_listen_together(body)
        elif path == "/music/listen-complete":
            self._handle_music_listen_complete(body)
        elif path == "/music/profile":
            self._handle_music_profile_update(body)
        elif path == "/music/remote":
            self._handle_music_remote_post(body)
        elif path == "/music/presence":
            self._handle_music_presence_post(body)
        else:
            self._send_json(404, {"error": "not found"})

    # ── Static file serving ───────────────────────────────────────────────────

    def _serve_music_file(self, path: str):
        """Serve only cached numeric-song-id MP3 files required by the player."""
        filename = path[len("/music/file/"):]
        match = re.fullmatch(r"([0-9]{1,20})\.mp3", filename)
        if match is None or not is_valid_song_id(match.group(1)):
            self._send_json(404, {"error": "not found"})
            return
        song_id = match.group(1)
        cache_dir = self.state.data_dir / "music_cache"
        target = (cache_dir / f"{song_id}.mp3").resolve()
        # Path traversal guard
        try:
            target.relative_to(cache_dir.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        self._send_file(target, "audio/mpeg", cache_control="no-store")

    def _serve_static(self, path: str):
        """Serve frontend static files from ../client/ directory."""
        client_dir = HERE.parent / "client"
        if not client_dir.is_dir():
            self._send_json(404, {"error": "frontend not found — place files in ../client/"})
            return
        rel = path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel:
            self._send_json(403, {"error": "forbidden"})
            return
        target = (client_dir / rel).resolve()
        try:
            target.relative_to(client_dir.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        # SPA fallback: if file not found, serve index.html
        if not target.exists() or not target.is_file():
            target = client_dir / "index.html"
            if not target.exists():
                self._send_json(404, {"error": "not found"})
                return
        self._send_file(target)

    # ── Music endpoint handlers ───────────────────────────────────────────────

    def _handle_music_search(self):
        qs = parse_qs(urlparse(self.path).query)
        keyword = qs.get("q", [""])[0]
        if not keyword:
            self._send_json(400, {"error": "missing q"})
            return
        try:
            url = "https://music.163.com/api/search/get"
            post_data = urlencode({
                "s": keyword, "type": "1", "limit": "6", "offset": "0"
            }).encode()
            raw = self._netease_request(url, data=post_data)
            songs = []
            result = raw.get("result", {})
            if not isinstance(result, dict):
                self._send_json(200, {"ok": True, "songs": []})
                return
            raw_songs = result.get("songs", [])[:6]
            # Batch-fetch covers
            ids = [s.get("id") for s in raw_songs if s.get("id")]
            covers: dict[int, str] = {}
            if ids:
                try:
                    detail_url = f"https://music.163.com/api/song/detail?ids=[{','.join(str(i) for i in ids)}]"
                    detail = self._netease_request(detail_url)
                    for ds in detail.get("songs", []):
                        al = ds.get("album", {}) or {}
                        if al.get("picUrl"):
                            covers[ds.get("id")] = al["picUrl"]
                except Exception:
                    pass
            for s in raw_songs:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                album = s.get("album", {}) or {}
                cover = covers.get(s.get("id"), album.get("picUrl", "") or "")
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s.get("id"),
                    "name": s.get("name", ""),
                    "artist": artists,
                    "album": album.get("name", ""),
                    "cover": cover,
                    "durationSeconds": _duration_seconds_from_milliseconds(
                        s.get("duration")
                    ),
                })
            self._send_json(200, {"ok": True, "songs": songs})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_music_url(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            self._send_json(200, {"ok": True, "url": f"/music/file/{song_id}.mp3", "cached": True})
            return
        try:
            url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=128000"
            raw = self._netease_request(url)
            data_list = raw.get("data", [])
            audio_url = data_list[0].get("url") if data_list else None
            if not audio_url:
                self._send_json(200, {"ok": False, "error": "no url, may need VIP or song unavailable"})
                return
            self._download_audio(audio_url, cache_file)
            self._send_json(200, {"ok": True, "url": f"/music/file/{song_id}.mp3", "cached": True})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_music_stream(self):
        """Stream audio directly — resolve URL, cache, and redirect to file."""
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if not (cache_file.exists() and cache_file.stat().st_size > 0):
            # Try to fetch and cache the file
            if not self._fetch_music_url(song_id):
                self._send_json(404, {"ok": False, "error": "audio unavailable"})
                return
        self._send_file(cache_file, "audio/mpeg")

    def _handle_music_lyric(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.lrc"
        cache_trans = cache_dir / f"{song_id}.tlyric"
        # Serve from cache if available
        if cache_file.exists():
            tlyric = cache_trans.read_text() if cache_trans.exists() else ""
            self._send_json(200, {"ok": True, "lrc": cache_file.read_text(), "tlyric": tlyric})
            return
        try:
            url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&tv=-1"
            raw = self._netease_request(url)
            lrc = raw.get("lrc", {}).get("lyric", "")
            tlyric = raw.get("tlyric", {}).get("lyric", "")
            # Cache BOTH .lrc AND .tlyric (critical: both must be saved)
            if lrc:
                cache_file.write_text(lrc)
            if tlyric:
                cache_trans.write_text(tlyric)
            self._send_json(200, {"ok": True, "lrc": lrc, "tlyric": tlyric})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    # ── Playlist (legacy flat) ──

    def _handle_music_playlist_get(self):
        self._send_json(200, {"ok": True, "songs": self._load_playlist()})

    def _handle_music_playlist_add(self, body: dict):
        song = body.get("song")
        if not song or not song.get("songId"):
            self._send_json(400, {"error": "missing song"})
            return
        song["cover"] = self._ensure_cover(song["songId"], song.get("cover", ""))
        song["addedBy"] = body.get("by", "unknown")
        playlist = self._load_playlist()
        if any(s.get("songId") == song["songId"] for s in playlist):
            self._send_json(200, {"ok": True, "duplicate": True, "songs": playlist})
            return
        playlist.append(song)
        self._save_playlist(playlist)
        # Also add to "liked" in multi-playlist system
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == "liked":
                if not any(s.get("songId") == song["songId"] for s in pl["songs"]):
                    pl["songs"].append(song)
                self._save_music_data(data)
                break
        self._send_json(200, {"ok": True, "songs": playlist})

    def _handle_music_playlist_remove(self, body: dict):
        song_id = body.get("songId")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        playlist = self._load_playlist()
        playlist = [s for s in playlist if s.get("songId") != song_id]
        self._save_playlist(playlist)
        self._send_json(200, {"ok": True, "songs": playlist})

    # ── Multi-playlist system ──

    def _handle_music_playlists_list(self):
        data = self._load_music_data()
        out = []
        for pl in data["playlists"]:
            cover = ""
            if pl["songs"]:
                cover = pl["songs"][0].get("cover", "")
            out.append({"id": pl["id"], "name": pl["name"], "count": len(pl["songs"]), "cover": cover})
        self._send_json(200, {"ok": True, "playlists": out})

    def _handle_music_playlists_songs(self):
        qs = parse_qs(urlparse(self.path).query)
        pid = qs.get("id", [""])[0]
        if not pid:
            self._send_json(400, {"error": "missing id"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                self._send_json(200, {"ok": True, "songs": pl["songs"]})
                return
        self._send_json(404, {"error": "not found"})

    def _handle_music_playlists_create(self, body: dict):
        name = body.get("name", "").strip()
        if not name:
            self._send_json(400, {"error": "missing name"})
            return
        data = self._load_music_data()
        pl = {"id": uuid.uuid4().hex[:8], "name": name, "songs": []}
        data["playlists"].append(pl)
        self._save_music_data(data)
        self._send_json(200, {"ok": True, "playlist": {"id": pl["id"], "name": pl["name"], "count": 0, "cover": ""}})

    def _handle_music_playlists_rename(self, body: dict):
        pid = body.get("id", "")
        name = body.get("name", "").strip()
        if not pid or not name:
            self._send_json(400, {"error": "missing id or name"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                pl["name"] = name
                self._save_music_data(data)
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "not found"})

    def _handle_music_playlists_delete(self, body: dict):
        pid = body.get("id", "")
        if not pid or pid == "liked":
            self._send_json(400, {"error": "cannot delete"})
            return
        data = self._load_music_data()
        data["playlists"] = [p for p in data["playlists"] if p["id"] != pid]
        self._save_music_data(data)
        self._send_json(200, {"ok": True})

    def _handle_music_playlists_add_song(self, body: dict):
        pid = body.get("playlistId", "")
        song = body.get("song")
        if not pid or not song or not song.get("songId"):
            self._send_json(400, {"error": "missing playlistId or song"})
            return
        song["cover"] = self._ensure_cover(song["songId"], song.get("cover", ""))
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                if any(s.get("songId") == song["songId"] for s in pl["songs"]):
                    self._send_json(200, {"ok": True, "duplicate": True})
                    return
                song["addedBy"] = body.get("by", "unknown")
                pl["songs"].append(song)
                self._save_music_data(data)
                if pid == "liked":
                    self._save_playlist(pl["songs"])
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "playlist not found"})

    def _handle_music_playlists_remove_song(self, body: dict):
        pid = body.get("playlistId", "")
        song_id = body.get("songId")
        if not pid or not song_id:
            self._send_json(400, {"error": "missing playlistId or songId"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                pl["songs"] = [s for s in pl["songs"] if s.get("songId") != song_id]
                self._save_music_data(data)
                if pid == "liked":
                    self._save_playlist(pl["songs"])
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "playlist not found"})

    # ── Recent play history ──

    def _handle_music_recent_get(self):
        data = self._load_music_data()
        self._send_json(200, {"ok": True, "songs": data.get("recent", [])[:30]})

    def _handle_music_recent_add(self, body: dict):
        song = body.get("song")
        if not song or not song.get("songId"):
            self._send_json(200, {"ok": True})
            return
        data = self._load_music_data()
        recent = data.get("recent", [])
        recent = [s for s in recent if s.get("songId") != song["songId"]]
        song["playedAt"] = datetime.now(timezone.utc).isoformat()
        recent.insert(0, song)
        data["recent"] = recent[:50]
        self._save_music_data(data)
        # Auto-increment listen count in song memory
        try:
            with self.state.music_memory_lock:
                mem = self._load_song_memory()
                sid = str(song["songId"])
                entry = mem.get(sid, {
                    "songId": song["songId"],
                    "name": song.get("name", ""),
                    "artist": song.get("artist", ""),
                    "listenCount": 0,
                    "togetherCount": 0,
                    "firstListened": None,
                    "lastListened": None,
                    "analyzed": False,
                    "notes": "",
                    "feeling": "",
                    "favoriteLines": [],
                    "tags": [],
                })
                entry["listenCount"] = entry.get("listenCount", 0) + 1
                now = datetime.now(timezone.utc).isoformat()
                entry["lastListened"] = now
                if not entry.get("firstListened"):
                    entry["firstListened"] = now
                entry["name"] = song.get("name", entry.get("name", ""))
                entry["artist"] = song.get("artist", entry.get("artist", ""))
                mem[sid] = entry
                self._save_song_memory(mem)
        except SongMemoryStoreError:
            self._send_json(500, {"ok": False, "error": "song memory unavailable"})
            return
        self._send_json(200, {"ok": True})

    # ── Song memory system ──

    @staticmethod
    def _listen_text(body: dict[str, Any], field: str) -> str:
        value = body.get(field)
        if not isinstance(value, str) or len(value) > MAX_MEMORY_TEXT_LENGTH:
            raise RequestBodyError(400, f"{field} must be a bounded string")
        return value.strip()

    @staticmethod
    def _listen_catalog(body: dict[str, Any]) -> dict[str, str] | None:
        catalog = body.get("catalog")
        if catalog is None:
            return None
        if not isinstance(catalog, dict) or set(catalog) != {"provider", "songId"}:
            raise RequestBodyError(400, "catalog must be a valid provider reference")
        song_id = catalog.get("songId")
        if (
            catalog.get("provider") != "netease"
            or not isinstance(song_id, str)
            or not is_valid_song_id(song_id)
            or str(int(song_id)) != song_id
        ):
            raise RequestBodyError(400, "catalog must be a valid provider reference")
        return {"provider": "netease", "songId": song_id}

    def _handle_music_listen(self, body: dict[str, Any]):
        if set(body) != LISTEN_EVENT_KEYS - {"catalog"} and set(body) != LISTEN_EVENT_KEYS:
            self._send_json(400, {"error": "invalid listen event fields"})
            return
        event_id = body.get("eventId")
        song_id = body.get("songId")
        duration = body.get("durationSeconds")
        if not isinstance(event_id, str) or not LISTEN_EVENT_ID_RE.fullmatch(event_id):
            self._send_json(400, {"error": "invalid eventId"})
            return
        if not is_valid_song_id(song_id) or str(int(song_id)) != str(song_id):
            self._send_json(400, {"error": "invalid songId"})
            return
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0
            or float(duration) > MAX_TRACK_DURATION_SECONDS
        ):
            self._send_json(400, {"error": "invalid durationSeconds"})
            return
        try:
            name = self._listen_text(body, "name")
            artist = self._listen_text(body, "artist")
            album = self._listen_text(body, "album")
            catalog = self._listen_catalog(body)
        except RequestBodyError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return

        sid = str(int(song_id))
        try:
            with self.state.music_memory_lock:
                mem = self._load_song_memory()
                existing = mem.get(sid)
                if existing is not None and not isinstance(existing, dict):
                    raise SongMemoryStoreError("song memory is unavailable")
                entry = existing or {
                    "songId": int(sid),
                    "name": "",
                    "artist": "",
                    "album": "",
                    "listenCount": 0,
                    "togetherCount": 0,
                    "firstListened": None,
                    "lastListened": None,
                    "analyzed": False,
                    "notes": "",
                    "feeling": "",
                    "favoriteLines": [],
                    "tags": [],
                }
                recent_ids = entry.get("_listenEventIds", [])
                if not isinstance(recent_ids, list) or any(
                    not isinstance(value, str) for value in recent_ids
                ):
                    raise SongMemoryStoreError("song memory is unavailable")
                duplicate = event_id in recent_ids
                if not duplicate:
                    count = entry.get("listenCount", 0)
                    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                        raise SongMemoryStoreError("song memory is unavailable")
                    now = datetime.now(timezone.utc).isoformat()
                    entry["listenCount"] = count + 1
                    entry["lastListened"] = now
                    if not entry.get("firstListened"):
                        entry["firstListened"] = now
                    entry["name"] = name
                    entry["artist"] = artist
                    entry["album"] = album
                    entry["duration"] = float(duration)
                    if catalog is not None:
                        entry["catalog"] = catalog
                    entry["_listenEventIds"] = (
                        recent_ids + [event_id]
                    )[-MAX_MEMORY_EVENT_IDS:]
                    mem[sid] = entry
                    self._save_song_memory(mem)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "counted": not duplicate,
                        "duplicate": duplicate,
                        "listenCount": entry.get("listenCount", 0),
                        "firstListened": entry.get("firstListened"),
                        "lastListened": entry.get("lastListened"),
                    },
                )
        except SongMemoryStoreError:
            self._send_json(500, {"ok": False, "error": "song memory unavailable"})

    def _handle_music_memory_get(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if song_id and not is_valid_song_id(song_id):
            self._send_json(400, {"error": "id must be a positive numeric id"})
            return
        try:
            with self.state.music_memory_lock:
                mem = self._load_song_memory()
        except SongMemoryStoreError:
            self._send_json(500, {"ok": False, "error": "song memory unavailable"})
            return
        if song_id:
            entry = mem.get(str(song_id))
            self._send_json(200, {"ok": True, "memory": entry})
        else:
            self._send_json(200, {"ok": True, "memories": mem})

    def _handle_music_memory_save(self, body: dict):
        song_id = str(body.get("songId", ""))
        if not is_valid_song_id(song_id):
            self._send_json(400, {"error": "invalid songId"})
            return
        try:
            with self.state.music_memory_lock:
                mem = self._load_song_memory()
                entry = mem.get(song_id, {
                    "songId": int(song_id),
                    "name": "",
                    "artist": "",
                    "listenCount": 0,
                    "togetherCount": 0,
                    "firstListened": None,
                    "lastListened": None,
                    "analyzed": False,
                    "notes": "",
                    "feeling": "",
                    "favoriteLines": [],
                    "tags": [],
                })
                now = datetime.now(timezone.utc).isoformat()
                action = body.get("action", "listen")
                if action == "listen":
                    entry["listenCount"] = entry.get("listenCount", 0) + 1
                    entry["lastListened"] = now
                    if not entry.get("firstListened"):
                        entry["firstListened"] = now
                    entry["name"] = body.get("name", entry.get("name", ""))
                    entry["artist"] = body.get("artist", entry.get("artist", ""))
                elif action == "together":
                    entry["togetherCount"] = entry.get("togetherCount", 0) + 1
                    entry["lastListened"] = now
                elif action == "analyze":
                    entry["analyzed"] = True
                    if body.get("notes"):
                        entry["notes"] = body["notes"]
                    if body.get("feeling"):
                        entry["feeling"] = body["feeling"]
                    if body.get("favoriteLines"):
                        entry["favoriteLines"] = body["favoriteLines"]
                    if body.get("tags"):
                        entry["tags"] = body["tags"]
                    if body.get("bpm"):
                        entry["bpm"] = body["bpm"]
                    if body.get("duration"):
                        entry["duration"] = body["duration"]
                elif action == "like":
                    entry["liked"] = True
                    entry["name"] = body.get("name", entry.get("name", ""))
                    entry["artist"] = body.get("artist", entry.get("artist", ""))
                    cover = self._ensure_cover(song_id, body.get("cover", ""))
                    song_obj = {
                        "songId": int(song_id),
                        "name": entry["name"],
                        "artist": entry["artist"],
                        "cover": cover,
                        "addedBy": body.get("by", "user"),
                    }
                    data = self._load_music_data()
                    liked_pl = None
                    for playlist in data.get("playlists", []):
                        if playlist.get("id") == "user_liked":
                            liked_pl = playlist
                            break
                    if not liked_pl:
                        liked_pl = {
                            "id": "user_liked",
                            "name": "User Liked",
                            "songs": [],
                        }
                        data.setdefault("playlists", []).append(liked_pl)
                    if not any(
                        song.get("songId") == int(song_id)
                        for song in liked_pl["songs"]
                    ):
                        liked_pl["songs"].append(song_obj)
                        self._save_music_data(data)
                elif action == "note":
                    entry["notes"] = body.get("notes", entry.get("notes", ""))
                    if body.get("feeling"):
                        entry["feeling"] = body["feeling"]
                    if body.get("favoriteLines"):
                        entry["favoriteLines"] = body["favoriteLines"]
                else:
                    self._send_json(400, {"error": "invalid memory action"})
                    return
                mem[song_id] = entry
                self._save_song_memory(mem)
        except SongMemoryStoreError:
            self._send_json(500, {"ok": False, "error": "song memory unavailable"})
            return
        self._send_json(200, {"ok": True, "memory": entry})

    # ── Listen together ──

    def _handle_listen_together(self, body: dict):
        """Record a 'listen together' event. In standalone mode this just logs
        the event; in the full CcCompanion it also injects into tmux."""
        song_id = body.get("songId")
        name = body.get("name", "")
        artist = body.get("artist", "")
        cover = self._ensure_cover(song_id, body.get("cover", ""))
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        is_roam = body.get("roam", False)
        # Record in song memory
        try:
            with self.state.music_memory_lock:
                mem = self._load_song_memory()
                sid = str(song_id)
                entry = mem.get(sid, {
                    "songId": song_id,
                    "name": name,
                    "artist": artist,
                    "listenCount": 0,
                    "togetherCount": 0,
                    "firstListened": None,
                    "lastListened": None,
                    "analyzed": False,
                    "notes": "",
                    "feeling": "",
                    "favoriteLines": [],
                    "tags": [],
                })
                now = datetime.now(timezone.utc).isoformat()
                entry["listenCount"] = entry.get("listenCount", 0) + 1
                entry["lastListened"] = now
                if not entry.get("firstListened"):
                    entry["firstListened"] = now
                entry["name"] = name or entry.get("name", "")
                entry["artist"] = artist or entry.get("artist", "")
                mem[sid] = entry
                self._save_song_memory(mem)
        except SongMemoryStoreError:
            self._send_json(500, {"ok": False, "error": "song memory unavailable"})
            return
        logger.info("listen-together: %s — %s (roam=%s)", name, artist, is_roam)
        self._send_json(200, {"ok": True})

    def _handle_music_listen_complete(self, body: dict):
        """Called when a song finishes playing naturally (audio ended event)."""
        song_id = body.get("songId")
        source = body.get("source", "")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        if source != "together":
            self._send_json(200, {"ok": True, "counted": False})
            return
        sid = str(song_id)
        try:
            with self.state.music_memory_lock:
                mem = self._load_song_memory()
                entry = mem.get(sid)
                if not entry:
                    self._send_json(200, {"ok": True, "counted": False})
                    return
                now = datetime.now(timezone.utc).isoformat()
                entry["togetherCount"] = entry.get("togetherCount", 0) + 1
                entry["lastListened"] = now
                if not entry.get("firstListened"):
                    entry["firstListened"] = now
                mem[sid] = entry
                self._save_song_memory(mem)
        except SongMemoryStoreError:
            self._send_json(500, {"ok": False, "error": "song memory unavailable"})
            return
        self._send_json(200, {"ok": True, "counted": True})

    # ── Read-only companion presence ──

    def _handle_music_presence_get(self):
        self._send_json(200, self.state.presence.read())

    def _handle_music_presence_post(self, body: dict):
        try:
            response = self.state.presence.update(body)
        except PresenceSequenceError:
            self._send_json(
                409,
                {"ok": False, "error": "presence sequence must strictly increase"},
            )
            return
        except PresenceValidationError:
            self._send_json(400, {"ok": False, "error": "invalid presence payload"})
            return
        self._send_json(200, response)

    # ── Background pre-analysis ──

    def _handle_analyze_trigger(self, body: dict):
        song_id = body.get("songId")
        song_name = body.get("name", "")
        song_artist = body.get("artist", "")
        if not is_valid_song_id(song_id):
            self._send_json(400, {"error": "songId must be a positive numeric id"})
            return
        song_id = str(song_id)
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        result_file = cache_dir / f"{song_id}_preanalysis.json"
        marker_file = cache_dir / f"{song_id}.analyzing"
        if result_file.exists():
            self._send_json(200, {"ok": True, "status": "ready"})
            return
        if marker_file.exists():
            age = time.time() - marker_file.stat().st_mtime
            if age < 60:
                self._send_json(200, {"ok": True, "status": "running"})
                return
            marker_file.unlink(missing_ok=True)
        audio_file = cache_dir / f"{song_id}.mp3"
        if not audio_file.exists():
            if not self._fetch_music_url(song_id):
                self._send_json(400, {"error": "cannot fetch audio"})
                return
        marker_file.write_text(json.dumps({
            "songId": song_id, "name": song_name, "started": time.time()
        }))
        script = str(HERE / "analyze_song.py")
        subprocess.Popen(
            [sys.executable, script, str(song_id), song_name, song_artist, str(cache_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_analyzer_environment(cache_dir),
        )
        self._send_json(200, {"ok": True, "status": "started"})

    def _handle_analyze_status(self):
        qs = parse_qs(urlparse(self.path).query)
        song_ids = qs.get("id", [])
        if len(song_ids) != 1 or not is_valid_song_id(song_ids[0]):
            self._send_json(400, {"error": "id must be a positive numeric id"})
            return
        public_song_id = str(song_ids[0])
        cache_song_id = public_song_id
        if getattr(self, "auth_role", None) == "mcp_read":
            allowed, resolved_song_id = self._resolve_mcp_analysis_cache_song_id(
                public_song_id
            )
            if not allowed:
                self._send_json(403, {"error": "auth required"})
                return
            if resolved_song_id is None:
                self._send_json(200, {"ok": True, "status": "none"})
                return
            cache_song_id = resolved_song_id
        cache_dir = self.state.data_dir / "music_cache"
        result_file = cache_dir / f"{cache_song_id}_preanalysis.json"
        if result_file.exists():
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(200, {"ok": True, "status": "error"})
                return
            stored_song_id = result.get("songId") if isinstance(result, dict) else None
            if not isinstance(stored_song_id, str) or not hmac.compare_digest(
                stored_song_id, cache_song_id
            ):
                self._send_json(200, {"ok": True, "status": "error"})
                return
            analysis = _sanitize_analysis_result(
                result,
                public_song_id,
                spectrogram_available=(
                    cache_dir / f"{cache_song_id}_analysis.png"
                ).is_file(),
            )
            if analysis is None:
                self._send_json(200, {"ok": True, "status": "error"})
                return
            self._send_json(200, {"ok": True, "status": "ready", "analysis": analysis})
            return
        marker_file = cache_dir / f"{cache_song_id}.analyzing"
        if marker_file.exists():
            age = time.time() - marker_file.stat().st_mtime
            if age < 60:
                self._send_json(200, {"ok": True, "status": "running"})
                return
        err_file = cache_dir / f"{cache_song_id}_analyze_error.txt"
        if err_file.exists():
            self._send_json(200, {"ok": True, "status": "error"})
            return
        self._send_json(200, {"ok": True, "status": "none"})

    def _handle_analyze_spectrogram(self):
        qs = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        song_ids = qs.get("id", [])
        if set(qs) != {"id"} or len(song_ids) != 1 or not is_valid_song_id(song_ids[0]):
            self._send_json(400, {"error": "id must be a positive numeric id"})
            return
        public_song_id = str(song_ids[0])
        cache_song_id = public_song_id
        if getattr(self, "auth_role", None) == "mcp_read":
            allowed, resolved_song_id = self._resolve_mcp_analysis_cache_song_id(
                public_song_id
            )
            if not allowed:
                self._send_json(403, {"error": "auth required"})
                return
            if resolved_song_id is None:
                self._send_json(404, {"error": "spectrogram not found"})
                return
            cache_song_id = resolved_song_id
        cache_dir = self.state.data_dir / "music_cache"
        result_file = cache_dir / f"{cache_song_id}_preanalysis.json"
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(404, {"error": "spectrogram not found"})
            return
        stored_song_id = result.get("songId") if isinstance(result, dict) else None
        if not isinstance(stored_song_id, str) or not hmac.compare_digest(
            stored_song_id, cache_song_id
        ):
            self._send_json(404, {"error": "spectrogram not found"})
            return
        image_file = cache_dir / f"{cache_song_id}_analysis.png"
        try:
            size = image_file.stat().st_size
        except OSError:
            self._send_json(404, {"error": "spectrogram not found"})
            return
        if size <= 0:
            self._send_json(404, {"error": "spectrogram not found"})
            return
        if size > MAX_SPECTROGRAM_BYTES:
            self._send_json(413, {"error": "spectrogram too large"})
            return
        self._send_file(image_file, "image/png", cache_control="no-store")

    # ── Stats ──

    def _handle_music_stats(self):
        try:
            with self.state.music_memory_lock:
                mem = self._load_song_memory()
        except SongMemoryStoreError:
            self._send_json(500, {"ok": False, "error": "song memory unavailable"})
            return
        total_songs = len(mem)
        total_listens = sum(e.get("listenCount", 0) for e in mem.values())
        together_listens = sum(e.get("togetherCount", 0) for e in mem.values())
        analyzed = sum(1 for e in mem.values() if e.get("analyzed"))
        top = sorted(mem.values(), key=lambda e: e.get("listenCount", 0), reverse=True)[:10]
        top_list = [
            {"name": e.get("name", ""), "artist": e.get("artist", ""),
             "count": e.get("listenCount", 0), "songId": e.get("songId")}
            for e in top
        ]
        self._send_json(200, {"ok": True, "stats": {
            "totalSongs": total_songs,
            "totalListens": total_listens,
            "togetherListens": together_listens,
            "analyzedSongs": analyzed,
            "topSongs": top_list,
        }})

    # ── Profile ──

    def _handle_music_profile_get(self):
        data = self._load_music_data()
        self._send_json(200, {"ok": True, "profile": data.get("profile", {})})

    def _handle_music_profile_update(self, body: dict):
        data = self._load_music_data()
        profile = data.get("profile", {})
        for k in ("avatar", "signature", "bg", "name", "appBg"):
            if k in body:
                profile[k] = body[k]
        data["profile"] = profile
        self._save_music_data(data)
        self._send_json(200, {"ok": True, "profile": profile})

    # ── Daily recommendations ──

    def _handle_music_daily(self):
        data = self._load_music_data()
        liked = []
        for pl in data["playlists"]:
            if pl["id"] == "liked":
                liked = pl["songs"]
                break
        if not liked:
            self._send_json(200, {"ok": True, "songs": []})
            return
        seed_song = random.choice(liked)
        if not seed_song.get("songId"):
            self._send_json(200, {"ok": True, "songs": []})
            return
        try:
            url = f"https://music.163.com/api/discovery/simiSong?songid={seed_song['songId']}&offset=0&limit=6"
            raw = self._netease_request(url)
            songs = []
            for s in raw.get("songs", [])[:6]:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                al = s.get("album", {}) or {}
                cover = al.get("picUrl", "")
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s["id"], "name": s.get("name", ""), "artist": artists,
                    "album": al.get("name", ""), "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs, "seed": seed_song.get("name", "")})
        except Exception as e:
            self._send_json(200, {"ok": True, "songs": [], "error": str(e)})

    # ── Remote play ──

    def _handle_music_remote_get(self):
        f = self.state.data_dir / "music_remote.json"
        if f.exists():
            data = json.loads(f.read_text())
            f.unlink()
            self._send_json(200, {"ok": True, "song": data})
        else:
            self._send_json(200, {"ok": False})

    def _handle_music_remote_post(self, body: dict):
        song = body.get("song")
        if not song:
            self._send_json(400, {"error": "missing song"})
            return
        f = self.state.data_dir / "music_remote.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(song, ensure_ascii=False))
        self._send_json(200, {"ok": True})

    # ── Roam mode (random genre discovery) ──

    def _handle_music_roam(self):
        """Diverse random song discovery — rotates across genres/languages."""
        # Netease top song area IDs: 0=All, 7=Chinese, 96=Western, 8=Japanese, 16=Korean
        # Netease playlist IDs for genre diversity
        genre_playlists = [
            3779629,      # Chinese classics
            2884035,      # Western classics
            71384707,     # Japanese pop
            991319590,    # Korean pop
            60198,        # Hip-hop/Rap
            11640012,     # R&B
            5059642708,   # Electronic
            2529283982,   # Folk
            3136952023,   # Rock
        ]
        top_types = [0, 7, 96, 8, 16]
        strategy = random.choice(["top", "playlist"])
        try:
            songs = []
            if strategy == "top":
                t = random.choice(top_types)
                url = f"https://music.163.com/api/discovery/new/songs?areaId={t}&limit=50&total=true"
                raw = self._netease_request(url)
                for s in raw.get("data", []):
                    artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                    al = s.get("album", {}) or {}
                    cover = al.get("picUrl", "")
                    if cover and not cover.startswith("http"):
                        cover = "https:" + cover
                    songs.append({
                        "songId": s["id"], "name": s.get("name", ""), "artist": artists,
                        "album": al.get("name", ""), "cover": cover,
                    })
            else:
                pid = random.choice(genre_playlists)
                url = f"https://music.163.com/api/playlist/detail?id={pid}"
                raw = self._netease_request(url)
                result = raw.get("result", {})
                for s in result.get("tracks", []):
                    artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                    al = s.get("album", {}) or {}
                    cover = al.get("picUrl", "")
                    if cover and not cover.startswith("http"):
                        cover = "https:" + cover
                    songs.append({
                        "songId": s["id"], "name": s.get("name", ""), "artist": artists,
                        "album": al.get("name", ""), "cover": cover,
                    })
            if songs:
                pick = random.choice(songs)
                self._send_json(200, {"ok": True, "song": pick})
            else:
                self._send_json(200, {"ok": False, "error": "no songs found"})
        except Exception as e:
            self._send_json(200, {"ok": False, "error": str(e)})

    # ── Similar songs ──

    def _handle_music_similar(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        try:
            url = f"https://music.163.com/api/discovery/simiSong?songid={song_id}&offset=0&total=true&limit=6"
            raw = self._netease_request(url)
            raw_songs = raw.get("songs", [])[:6]
            songs = []
            for s in raw_songs:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                album = s.get("album", {}) or {}
                cover = album.get("picUrl", "") or ""
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s.get("id"),
                    "name": s.get("name", ""),
                    "artist": artists,
                    "album": album.get("name", ""),
                    "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


# ── Server state ─────────────────────────────────────────────────────────────

class ServerState:
    def __init__(
        self,
        port: int,
        *,
        data_dir: Path | None = None,
        presence_clock=None,
        presence_utcnow=None,
    ):
        self.full_auth_token, self.mcp_read_token = _load_auth_tokens()
        presence_ttl = parse_presence_ttl(os.environ.get("MUSIC_PRESENCE_TTL_SECONDS"))
        self.host = _parse_server_host(os.environ.get("ERYU_HOST"))
        self.allowed_origin = _parse_allowed_origin(os.environ.get("ERYU_ALLOWED_ORIGIN"))
        self.port = port
        self.data_dir = (
            Path(data_dir).resolve()
            if data_dir is not None
            else _parse_data_dir(os.environ.get("ERYU_DATA_DIR"))
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "music_cache").mkdir(parents=True, exist_ok=True)
        self.music_memory_lock = threading.RLock()
        presence_kwargs = {}
        if presence_clock is not None:
            presence_kwargs["monotonic"] = presence_clock
        if presence_utcnow is not None:
            presence_kwargs["utcnow"] = presence_utcnow
        self.presence = PresenceStore(presence_ttl, **presence_kwargs)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    port = _load_server_port()
    state = ServerState(port)
    EryuHandler.state = state

    server = ThreadingHTTPServer((state.host, state.port), EryuHandler)
    logger.info("eryu starting on %s:%d", state.host, state.port)
    logger.info("Data dir: %s", state.data_dir)
    logger.info("Authentication: full and MCP read-only tokens configured")
    logger.info("Music presence stale-after: %s seconds", state.presence.ttl_seconds)
    logger.info("Netease cookie: %s", "configured" if os.environ.get("MUSIC_U") else "not configured")
    logger.info("Frontend: %s", "found" if (HERE.parent / "client").is_dir() else "not found (place files in ../client/)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
