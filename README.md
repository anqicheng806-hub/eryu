# eryu

A self-hosted music player for listening together. Powered by NetEase Cloud Music.

## Features

- **Search & Play** — Full NetEase Cloud Music catalog with VIP-quality streams
- **Synced Lyrics** — Real-time scrolling lyrics with tap-to-seek and draggable progress bar
- **Translation** — Foreign songs automatically show Chinese translation
- **Playlists** — Create and manage multiple playlists
- **Roam Mode** — Auto-discover similar songs when the queue is empty
- **Song Notes** — Save feelings, favorite lines, and tags for each song
- **Spectrum Analysis** — BPM, key, and energy curve analysis (optional, requires librosa)
- **Read-only Music Presence MCP** — Share current playback, a nearby lyric window, existing analysis, and existing song memories with an AI companion
- **Remote Play** — Push songs to the player from any device via API
- **Daily Recommendations** — Personalized song suggestions
- **CDN Fallback** — Automatic node switching for overseas servers
- **Zero Core Dependencies** — Pure Python stdlib player server and vanilla JS frontend; the separate MCP process uses the official SDK

## Quick Start

```powershell
git clone https://github.com/sebastianevan200-stack/eryu.git
cd eryu

# First set MUSIC_U, ERYU_AUTH_TOKEN, and ERYU_MCP_READ_TOKEN in this
# process using the masked procedure in docs/music-presence-mcp.md.

# Run
python server/eryu.py
```

Open `http://localhost:9090` in your browser and enter `ERYU_AUTH_TOKEN`. The
masked input keeps the token only in this page's memory; refreshing or closing
the page clears it. The server never creates credential files and fails closed
when either auth token is missing.

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `ERYU_HOST` | `127.0.0.1` | Fixed loopback address; any other value fails startup |
| `ERYU_PORT` | `9090` | Server port; legacy `PORT` is still accepted |
| `ERYU_DATA_DIR` | `server/data` | Persistent data directory; production uses `/var/lib/eryu` |
| `ERYU_ALLOWED_ORIGIN` | `*` | Exact production web origin; deployment template sets the approved HTTPS player origin |
| `MUSIC_U` | none | NetEase cookie; local environment only |
| `ERYU_AUTH_TOKEN` | none | Full-access web-player token; required, at least 32 characters, no whitespace |
| `ERYU_MCP_READ_TOKEN` | none | Distinct read-only token for the four current-song MCP backend paths; same length/whitespace rule |
| `MUSIC_PRESENCE_TTL_SECONDS` | `10` | Seconds after the last heartbeat before presence is stale |

## API

All protected endpoints require the `X-Auth-Token` header. Query-string tokens are intentionally rejected because URLs can be logged.

### Presence

- `POST /music/presence` — Web player heartbeat (full token only)
- `GET /music/presence` — Last snapshot with explicit `absent`, `fresh`, or `stale` freshness

Windows desktop players that expose GSMTC can use the independent
[Windows Reader](docs/windows-reader.md) without changing this API.

### Playback
- `GET /music/search?q=keyword` — Search songs
- `GET /music/url?id=songId` — Get audio URL (auto-caches)
- `GET /music/lyric?id=songId` — Get lyrics + translation
- `GET /music/similar?id=songId` — Get similar songs
- `GET /music/roam` — Discover songs from random genres

### Playlists
- `GET /music/playlist` — Default playlist
- `GET /music/playlists` — List all playlists
- `POST /music/playlists/create` — Create playlist
- `POST /music/playlists/add-song` — Add song to playlist
- `POST /music/playlists/remove-song` — Remove song from playlist

### Memory
- `GET /music/memory?id=songId` — Get song notes
- `POST /music/memory` — Save notes, feelings, tags
- `POST /music/listen` — Record one validated, idempotent 30-second Reader listen event (full token only)

### Existing analysis
- `GET /music/analyze/status?id=songId` — Sanitized BPM/key/energy status
- `GET /music/analyze/spectrogram?id=songId` — Authenticated existing PNG
- `POST /music/analyze` — Trigger analysis (full token only; never used by MCP)

### Remote
- `POST /music/remote` — Push a song to the player
- `GET /music/remote` — Poll for pushed song

## For AI Companions

eryu includes a spectrum analysis feature designed for AI companions to "listen" to music:

```powershell
# Install the pinned analysis dependencies (Python 3.10+ binary wheels)
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --progress-bar off -r .\server\requirements-analysis.txt
```

These optional analysis packages are not needed to run the read-only presence
MCP and may take several minutes to install. The pinned direct versions were
locally verified with a real synthetic-audio analysis; Linux/VPS installation
and a real NetEase track remain deployment checks.

`POST /music/analyze` triggers background analysis. Results include BPM, key,
energy curve, and a spectrogram image. The read-only MCP never calls that POST;
when an existing image is available, `music_analysis` returns it as MCP
`ImageContent` without exposing the server's local file path.

The phase-one MCP server is deliberately read-only. It exposes exactly four tools:

- `music_now_playing`
- `music_lyrics_window`
- `music_analysis`
- `music_memory`

It never pauses, skips, seeks, triggers analysis, or writes song memory. See [local setup, tests, security checks, and the deployment gate](docs/music-presence-mcp.md).

The repository now keeps both MCP transports:

- local regression/debug: `stdio` through `eryu-music-mcp`;
- remote ChatGPT target: Auth0-protected Streamable HTTP through
  `eryu-music-mcp-http`, published by Caddy at
  `https://eryu-mcp.95.169.17.214.sslip.io/mcp`.

The remote endpoint is implemented and locally tested, but it has not been
deployed or connected to a real Auth0 tenant yet. See the
[Auth0/ChatGPT Dashboard checklist](docs/auth0-chatgpt.md) and the
[unexecuted VPS deployment plan](deploy/README.md). No Auth0 client secret is
used by the MCP resource server.

The proposed HTTPS player host keeps the Python backend on `127.0.0.1` and
protects every static, API, and audio-cache path with Caddy Basic Auth. Its only
public exception is `/health`, which returns plain `ok`. This boundary is also
only a reviewed local template; it has not been applied to the VPS.

## License

MIT
