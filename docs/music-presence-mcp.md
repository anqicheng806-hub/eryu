# Phase 1: read-only music presence MCP

This phase lets an MCP client observe the web player's last heartbeat. It does
not add any remote pause, skip, previous, seek, queue, analysis-trigger, or
memory-write capability.

## What runs

There are two services and two MCP launch modes:

1. `server/eryu.py` serves the existing player and the authenticated
   `POST/GET /music/presence` API.
2. `mcp_server/eryu_music_mcp.py` exposes exactly four MCP tools over local
   `stdio`:
   `music_now_playing`, `music_lyrics_window`, `music_analysis`, and
   `music_memory`.
3. `mcp_server/eryu_music_http.py` exposes the same four tools over authenticated
   Streamable HTTP. It binds only to loopback; Caddy supplies public HTTPS at
   `https://eryu-mcp.95.169.17.214.sslip.io/mcp`.

The remote transport uses the SDK's stateless JSON response mode. Eryu has no
server-to-client sampling, elicitation, or progress callback requirement, so it
does not retain authenticated MCP sessions between requests.

The HTTP mode is an Auth0-protected OAuth resource server. Its canonical
resource and JWT audience are both
`https://eryu-mcp.95.169.17.214.sslip.io`. It validates RS256 signature,
issuer, audience, expiry/not-before, and the standard `music:read` scope. The
incoming ChatGPT Bearer token is never reused for the Eryu backend.

The MCP process uses the official
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)
v2 line and requires Python 3.10 or newer.

The browser schedules a report every two seconds and also reports important
song, playback, metadata, and lyric events immediately. Only one request is in
flight; a newer event is coalesced and sent as soon as the active request
settles. Browser background throttling or a slow request can delay a report.
The server stores only one in-memory snapshot, so a restart returns `absent`.
A snapshot becomes `stale` after ten seconds by default. Paused playback with
a fresh heartbeat is not stale.

The MCP process receives a separate read-only token. That token is accepted
only for these backend requests:

- `GET /music/presence`
- `GET /music/analyze/status?id=<current numeric song id>`
- `GET /music/analyze/spectrogram?id=<current numeric song id>`
- `GET /music/memory?id=<current numeric song id>`

The backend checks that analysis/memory IDs match the fresh current presence.
`music_analysis` reads an analysis that already exists. It never calls
`POST /music/analyze`, downloads audio, or starts `librosa`. If the existing
spectrogram PNG is present, the tool returns it as MCP `ImageContent`; no local
filesystem path is exposed. `music_memory` reads only the fresh current song
and never lists or changes the memory store.

## Environment variables

| Name | Process | Required | Purpose |
|---|---|---:|---|
| `MUSIC_U` | eryu | for NetEase playback | NetEase cookie; never logged |
| `ERYU_AUTH_TOKEN` | eryu/browser operator | yes | Full web-player API token |
| `ERYU_MCP_READ_TOKEN` | eryu and MCP | yes | Separate least-privilege read token |
| `ERYU_HOST` | eryu | no | Fixed to `127.0.0.1`; any other value fails startup |
| `ERYU_PORT` | eryu | no | Player port, default `9090`; legacy `PORT` remains supported |
| `ERYU_DATA_DIR` | eryu | no | Absolute persistent data directory; default `server/data` |
| `ERYU_ALLOWED_ORIGIN` | eryu | no | Exact production web origin; local default `*` |
| `MUSIC_PRESENCE_TTL_SECONDS` | eryu | no | Plain decimal in `(0, 3600]`, default `10` |
| `ERYU_BASE_URL` | MCP | no | Backend origin, default `http://127.0.0.1:9090` |
| `MCP_HTTP_HOST` | HTTP MCP | no | Must be numeric loopback, default `127.0.0.1` |
| `MCP_HTTP_PORT` | HTTP MCP | no | Internal port, default `9091` |
| `MCP_PUBLIC_URL` | HTTP MCP | yes | Public canonical origin, no path or trailing slash |
| `AUTH0_AUDIENCE` | HTTP MCP | yes | Must exactly equal `MCP_PUBLIC_URL` |
| `AUTH0_ISSUER_URL` | HTTP MCP | yes | Exact public Auth0 issuer from OIDC discovery, including trailing slash |
| `MCP_REQUIRED_SCOPE` | HTTP MCP | no | Fixed to `music:read`; any other value fails startup |

Each auth token must contain at least 32 characters and no whitespace; the two
values must differ. `ERYU_BASE_URL` must be an origin only: no path, query,
credentials, or fragment. Plain HTTP is allowed only for loopback; use HTTPS
for a remote origin. Do not put real values in this repository, a command
argument, a URL, a screenshot, or a log. The legacy `server/.secret` and
`server/.netease_cred` files are not read.

### Safely set process-only variables in Windows PowerShell

Use the helper below in a PowerShell window. Input is masked, values exist only
in that PowerShell process and its child processes, and nothing is saved to a
file. Prepare strong values first; the helper deliberately does not print,
generate, or persist them.

```powershell
function Set-ProcessSecret([string]$Name) {
  $secureValue = Read-Host "Enter $Name" -AsSecureString
  $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
  try {
    $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    [Environment]::SetEnvironmentVariable($Name, $plainValue, "Process")
  }
  finally {
    if ($secretPointer -ne [IntPtr]::Zero) {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
    Remove-Variable plainValue,secureValue,secretPointer -ErrorAction SilentlyContinue
  }
}

```

In the first PowerShell, which starts the player backend, run:

```powershell
Set-ProcessSecret MUSIC_U
Set-ProcessSecret ERYU_AUTH_TOKEN
Set-ProcessSecret ERYU_MCP_READ_TOKEN

if ($env:ERYU_AUTH_TOKEN.Length -lt 32 -or $env:ERYU_AUTH_TOKEN -match '\s') {
  throw 'ERYU_AUTH_TOKEN must be at least 32 characters with no whitespace'
}
if ($env:ERYU_MCP_READ_TOKEN.Length -lt 32 -or $env:ERYU_MCP_READ_TOKEN -match '\s') {
  throw 'ERYU_MCP_READ_TOKEN must be at least 32 characters with no whitespace'
}
if ($env:ERYU_AUTH_TOKEN -ceq $env:ERYU_MCP_READ_TOKEN) {
  throw 'The full and read-only tokens must differ'
}
```

In the separate MCP PowerShell or MCP host, define the helper again and set
only the least-privilege value:

```powershell
Set-ProcessSecret ERYU_MCP_READ_TOKEN
# Set this only when the backend is not the default loopback origin:
# $env:ERYU_BASE_URL = 'https://music.example.com'
```

Expected result: PowerShell returns to the prompt and prints none of the
values. Do not give the MCP process `MUSIC_U` or `ERYU_AUTH_TOKEN`. To remove
the backend values from its current PowerShell later:

```powershell
Remove-Item Env:MUSIC_U,Env:ERYU_AUTH_TOKEN,Env:ERYU_MCP_READ_TOKEN -ErrorAction SilentlyContinue
```

The browser's masked login needs the full token once per page session. It keeps
only a transient in-memory copy; refresh or close the page to clear it. It is
never saved to `localStorage` or `sessionStorage`.

## Local installation and validation

Run every command below from the eryu repository root: the directory containing
`README.md` and the `docs` subdirectory.

1. Confirm Python is new enough:

   ```powershell
   python --version
   node --version
   ```

   Expected result: Python `3.10` or newer and a Node.js version. This checkout
   was validated with Python `3.12.10` and Node.js `v24.16.0`. Node.js runs the
   browser-reporter race tests; missing/skipped frontend tests are not a full
   validation pass.

2. Create an isolated Python environment:

   ```powershell
   python -m venv .venv
   ```

   Expected result: no error and a new ignored `.venv` directory.

3. Install the pinned official MCP SDK, the local server package, and the
   pinned optional audio-analysis dependencies:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install --only-binary=:all: --progress-bar off mcp==2.0.0 "PyJWT[crypto]==2.13.0" cryptography==50.0.0
   .\.venv\Scripts\python.exe -m pip install --no-deps -e .\mcp_server
   .\.venv\Scripts\python.exe -m pip install --only-binary=:all: --progress-bar off -r .\server\requirements-analysis.txt
   ```

   Expected result: all three commands end without an error. The first run needs
   an internet connection and may take three to five minutes on Windows. The
   analysis file pins direct dependencies that retain Python 3.10+ support.

4. Verify the installed dependency set:

   ```powershell
   .\.venv\Scripts\python.exe -m pip show mcp
   .\.venv\Scripts\python.exe -m pip check
   ```

   Expected result: `pip show` includes `Version: 2.0.0`; `pip check` prints
   `No broken requirements found.`

5. Run all local tests:

   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   ```

   Expected result: every test is `ok` and the final line is `OK`. The suite
   includes real stdio and authenticated Streamable HTTP MCP handshakes, JWT
   rejection cases, browser-reporter race regressions, and deployment-template
   checks.

6. Run the repository credential check:

   ```powershell
   .\.venv\Scripts\python.exe .\scripts\security_check.py
   ```

   Expected result: the first line begins `SECURITY CHECK OK`. This bounded
   check does not replace a maintained scanner such as gitleaks.

7. In the backend PowerShell, after setting its three variables, start the
   player:

   ```powershell
   .\.venv\Scripts\python.exe .\server\eryu.py
   ```

   Expected result: logs say both auth tokens and the NetEase cookie are
   configured without showing any value or token prefix. Open
   `http://127.0.0.1:9090`, enter the full web token, and play or pause a song.
   The server is fixed to `127.0.0.1`; any other `ERYU_HOST` value fails startup.
   Production HTTPS must go through the reviewed Caddy proxy.

8. In the MCP-only PowerShell, inspect presence:

   ```powershell
   $headers = @{ "X-Auth-Token" = $env:ERYU_MCP_READ_TOKEN }
   Invoke-RestMethod -Uri "http://127.0.0.1:9090/music/presence" -Headers $headers
   ```

   Expected result while the page is open: `freshness.state` is `fresh`, and the
   snapshot has `song`, `playback`, and `lyrics`. While playing a real song,
   check the status, position, duration, current lyric, and previous/next lines.
   Close the page and wait longer than the configured TTL; it becomes `stale`.

9. The MCP host launches this command over `stdio`:

   ```powershell
   .\.venv\Scripts\python.exe .\mcp_server\eryu_music_mcp.py
   ```

   Expected result when run by hand: no normal output; the process waits for MCP
   messages on standard input. Press `Ctrl+C` to stop it. Configure
   `ERYU_MCP_READ_TOKEN` in the host's process environment, never inside a
   checked-in MCP configuration. The unit suite performs a real stdio handshake
   and confirms the four-tool list.

10. The remote entry is a separate command and intentionally fails startup until
    all public Auth0 settings are supplied:

    ```powershell
    .\.venv\Scripts\eryu-music-mcp-http.exe
    ```

    Do not invent an issuer or use a client secret. The unit suite tests this
    entry with locally generated RSA keys and fake discovery/JWKS data; no real
    Auth0 tenant is contacted. Follow
    [the Auth0 and ChatGPT checklist](auth0-chatgpt.md) before a live connection.

## Presence contract

The browser sends a versioned, bounded snapshot. It includes whitelisted song
metadata, playback status and times, and only the current lyric plus two lines
before and after. It never sends a token, cookie, full lyric file, audio, or
control command.

The server owns freshness. Client `reportedAt` is diagnostic only; freshness
uses the server's monotonic receive time. A successful GET or POST returns:

```json
{
  "ok": true,
  "presence": null,
  "freshness": {
    "state": "absent",
    "stale": true,
    "ageSeconds": null,
    "staleAfterSeconds": 10,
    "receivedAt": null
  }
}
```

For a stale snapshot, `presence` remains available as explicitly last-known
data, but all four MCP tools fail closed and do not describe it as current.

## Security checks and known boundaries

- Protected API auth is header-only and uses constant-time comparison.
- Missing, weak, whitespace-containing, or identical full/read tokens prevent
  server startup.
- The read token can fetch analysis, spectrogram, or memory only when its
  numeric ID matches a fresh current presence; stale, absent, and wrong-song
  requests fail closed.
- JSON request bodies are bounded; presence rejects unknown/control fields,
  invalid numeric song IDs, non-finite numbers, oversized text, and oversized
  lyric windows.
- Presence lives only in memory and is never written every two seconds.
- Duplicate or decreasing sequence numbers from one browser session return 409
  and cannot refresh or roll back the stored snapshot.
- API JSON responses are marked `Cache-Control: no-store`.
- Analysis results exposed to MCP omit local filesystem paths and raw analyzer
  error text. The authenticated PNG response is bounded to 8 MiB and returned
  with `no-store` and `nosniff` headers.
- The backend cache route rejects JSON, lyrics, markers, errors, and analysis
  PNGs; it serves only positive numeric `<songId>.mp3` files required by the
  current browser audio flow. Production keeps that backend on loopback and
  puts the entire HTTPS player origin (static files, APIs, and MP3s) behind
  Caddy Basic Auth. Only exact `/health` is public, with a plain `ok` body.
- The included scanner checks tracked/non-ignored candidate text, dangerous
  ignored credential filenames, UTF-8/UTF-16 text, and reachable Git history
  for common credential patterns. It skips dependency trees and oversized
  files; run a maintained scanner such as gitleaks before deployment too.
- Both production service templates bind to loopback. The web service sets one
  exact CORS origin, and the HTTP MCP enforces Host/Origin allowlists plus a
  64 KiB request-body limit. Do not expose ports `9090` or `9091` directly.
- HTTP MCP JWT verification uses three-second metadata timeouts, bounded JSON,
  cached signing keys, and global backoff for unknown signing keys/fetch
  failures. It never logs tokens or raw claims.
- Auth0 issuer, audience, and JWKS are public metadata. No Auth0 client secret
  or Management API token is used by this resource server.

## VPS deployment gate (local templates ready; not executed)

Historical VPS evidence has been reviewed, but the currently installed Caddy
source, binary, modules, systemd command line, and live Shared Diary Caddyfile
have not been re-inspected in this no-VPS phase. No upload, install, service
restart, proxy change, certificate request, secret creation, Auth0 write, or
deployment has been performed. The approved design uses separate web/MCP
sslip.io hosts, loopback ports `9090`/`9091`, Caddy, two systemd services, and
encrypted systemd credentials. Every VPS write still needs separate approval.

The historically observed Caddy 2.6.2 is blocked from hosting Eryu. Caddy must
first pass the separate source/module/systemd inventory, exact-version upgrade,
Shared Diary regression, and rollback gates in
[`deploy/CADDY-UPGRADE.md`](../deploy/CADDY-UPGRADE.md). The post-upgrade Eryu
fragment uses `basic_auth`; the shared root Caddyfile must natively set
`persist_config off` and move the Admin API to a permissioned `0600` Unix
socket before the encrypted account credential is loaded. The old
XDG/autosave symlink workaround and default localhost Admin API are no longer
part of the deployment. No Caddy upgrade, restart, or reload has been executed.

After approval, deployment should proceed in these separately verified stages:

1. Read-only inventory the actual Caddy package source, exact binary and hash,
   modules, systemd commands/drop-ins, and file-backed Shared Diary routes.
2. Upgrade Caddy in its own approved maintenance window and complete the Shared
   Diary regression; do not add Eryu during that window.
3. Present the exact Eryu target paths and commands for confirmation; do not
   transmit or print any secret during this step.
4. Pull only the approved 40-hex feature-branch commit, verify the remote tip,
   and check it out detached before installing in an isolated environment.
5. Inject `MUSIC_U`, the two internal tokens, and the Caddy Basic Auth account
   only through encrypted systemd credentials. Public Auth0 metadata remains
   separate from secrets.
6. Start the backend/MCP topology only after separate approval, then verify
   health, auth denial, fresh/stale presence, OAuth discovery, and all four
   read-only tools before a separately approved Caddy reload.
7. Keep both the pre-upgrade Caddy artifacts and previous application release
   intact until verification succeeds; no rollback or restart is automatic.

The final target is a ChatGPT-connectable HTTPS Streamable HTTP MCP, not merely
local stdio. `stdio` remains only as a local regression/debug mode. See
[`deploy/README.md`](../deploy/README.md) for the proposed files and commands;
they are documentation, not evidence that deployment ran. Never place a VPS
password, private key, token, cookie, or client secret in this repository, a
systemd unit, a command argument, or a log.
