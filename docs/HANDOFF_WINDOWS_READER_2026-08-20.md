# Eryu Windows Reader handoff — 2026-08-20

## 1. Read this first

New task order:

1. Read the workspace `AGENTS.md` instructions.
2. Read this handoff completely.
3. Read `docs/windows-reader.md` and inspect the current dirty worktree.
4. Preserve the currently running Reader and SSH tunnel until the user chooses
   the next test. Do not restart services or rotate credentials during review.

This handoff contains no credential values, hashes of plaintext credentials, or
Authorization headers.

## 2. Outcome already achieved

The complete core path is live and user-verified:

```text
Spotify desktop
  -> Windows GSMTC
  -> local Windows Reader
  -> private SSH tunnel
  -> VPS Eryu Web POST /music/presence
  -> Eryu MCP
  -> public OAuth-protected MCP
  -> ChatGPT Eryu connector
```

ChatGPT successfully returned the song currently playing on Windows. The Reader
terminal also proved automatic song changes, playing status, increasing
sequence numbers, and repeated successful presence posts.

## 3. Repository state

- Repository: `C:\Users\qac\Documents\给顾安\eryu`
- Branch: `feature/music-presence-mcp`
- Local HEAD: `bc0deb21051188f0fe3a8216fd392eb7102b8027`
- Remote branch SHA: `bc0deb21051188f0fe3a8216fd392eb7102b8027`
- Reader work is **not committed or pushed yet**.

Current dirty set at handoff:

```text
 M README.md
?? .env.example
?? docs/HANDOFF_WINDOWS_READER_2026-08-20.md
?? docs/windows-reader.md
?? reader/
?? scripts/start_windows_reader.ps1
?? tests/test_windows_gsmtc_reader.py
?? tmp_check.txt
```

`tmp_check.txt` is unrelated/unclassified user workspace content. Preserve it;
do not delete, stage, or modify it without a separate decision.

## 4. Implemented Reader files

- `reader/windows_gsmtc_reader.py`
  - Uses `Windows.Media.Control` / GSMTC.
  - Requests the session manager, reads current/all sessions, media properties,
    playback information, and timeline information.
  - Watches manager and active-session change events.
  - Prefers a genuinely Playing session; configured source preferences break
    ties and ignored-source substrings filter obvious non-player sessions.
  - Adapts sessions to the existing strict `/music/presence` schema without
    changing the server protocol.
  - Sends about every two seconds and immediately on relevant events.
  - Uses a unique client session, strictly increasing sequence values, retry of
    uncertain requests with the same sequence, and a per-user single-instance
    lock.
  - Rejects redirects, embedded URL credentials, weak/whitespace tokens, and
    non-loopback plain HTTP endpoints.
- `reader/requirements-windows.txt`
  - Pins the narrow PyWinRT 3.2.1 namespaces used by GSMTC.
- `scripts/start_windows_reader.ps1`
  - Masked, process-only token input.
  - `-SshTunnel` mode starts only local `127.0.0.1:19090`, forwarding to VPS
    `127.0.0.1:9090`, checks `/health`, runs the Reader, and closes its own SSH
    child in `finally`.
- `.env.example`
  - Contains non-secret Reader settings only.
- `docs/windows-reader.md`
  - Setup, supported-player boundary, tunnel usage, and safe manual startup.
- `tests/test_windows_gsmtc_reader.py`
  - Payload/schema, status, position, song change, session selection, sequence,
    retry/recovery, endpoint security, and single-instance tests.
- `README.md`
  - Links the Windows Reader documentation.

No Reader implementation changed Auth0, MCP OAuth/RBAC, Caddy routes, Shared
Diary, or the server presence schema.

## 5. Verification already completed

Local checks after the tunnel-launcher change:

- Reader tests: 14/14 passed.
- Complete suite: 101/101 passed, zero failures/errors/skips.
- `pip check`: clean.
- Existing security scan: `SECURITY CHECK OK`, 40 text files plus Git history,
  no credential pattern matched.
- PowerShell parser: `start_windows_reader.ps1` clean.
- `git diff --check`: passed (only the existing Windows LF/CRLF warning).
- Real SSH forward test: local `127.0.0.1:19090` to VPS
  `127.0.0.1:9090`, `/health` returned 200/ok, then the test forward was closed.

Live presence evidence in the final run:

- Local Reader process count: 1.
- Local SSH listener: `127.0.0.1:19090`, owned by `ssh`.
- Spotify session source:
  `SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify`.
- Reader logs showed status `playing`, song switching, and sequences increasing
  from 1 onward.
- VPS journal sample: 48 `POST /music/presence 200` entries, zero non-200
  presence posts, zero selected secret-name matches.
- ChatGPT Eryu tool returned the live song and playback progress.

## 6. Current live runtime state

Keep the visible Reader PowerShell window open. `Ctrl+C` is the intended clean
stop and should also close the tunnel. Closing or rebooting Windows stops the
current Reader; there is no unattended startup or persisted Reader token.

Fresh VPS snapshot at handoff:

```text
eryu-web.service  active/running  PID 916036  NRestarts 0
eryu-mcp.service  active/running  PID 916038  NRestarts 0
caddy.service     active/running  PID 640929  NRestarts 0
9090              127.0.0.1 only
9091              127.0.0.1 only
Shared Diary      local /health = 200
public MCP        unauthenticated /mcp = 401
```

The Web credential rotation restarted `eryu-web`. Because the MCP unit requires
the Web unit, MCP started at the same time with a new PID. It is active, its
public authentication boundary remains 401 without a bearer token, and its new
invocation log had zero selected error/secret-name matches.

All rotation staging artifacts were removed after live presence validation:

```text
/run/eryu-auth-token-pre-rotation.cred               absent
/run/eryu-auth-token-rotation.lock                   absent
/run/eryu-rotate-auth-token                          absent
/etc/credstore.encrypted/eryu/.ERYU_AUTH_TOKEN.cred.new absent
```

## 7. Credential and routing boundary

The Reader does **not** use an Auth0 access token:

- Auth0 MCP token: audience is the public Eryu MCP resource and scope is
  `music:read`; it cannot write presence.
- Reader token: `ERYU_AUTH_TOKEN`, no OAuth audience or scope, sent only as
  `X-Auth-Token` to the Eryu Web presence route.

The current full Reader token:

- was newly generated as 64 lowercase hexadecimal characters;
- is saved by the user in their password manager;
- is installed only as the encrypted VPS `ERYU_AUTH_TOKEN` credential;
- is passed to the local Reader only through masked process input;
- is not in Git, `.env`, command arguments, logs, this handoff, or clipboard.

One earlier generated token was accidentally pasted at a normal PowerShell
prompt and appeared in a screenshot. It was never installed on the VPS, was
permanently abandoned, and its exact final PSReadLine history entry was removed
without changing other history bytes. Do not reuse it. The current token is a
separate later value.

Eryu Web intentionally remains private on VPS loopback. There is no Caddy site
or certificate for `eryu.95.169.17.214.sslip.io`; TLS to that nonexistent host
returns an internal alert before HTTP. This is expected, not a Reader failure.
The working public MCP URL remains:

```text
https://eryu-mcp.95.169.17.214.sslip.io/mcp
```

The Reader therefore uses the private SSH tunnel rather than exposing 9090 or
adding a new public Caddy route.

## 8. Player observations

- Spotify: confirmed GSMTC media properties, playing/paused state, duration,
  continuously advancing position, song changes, and live Eryu presence.
- 汽水音乐: confirmed to expose a GSMTC session; it was paused during the
  earlier enumeration.
- NetEase Cloud Music desktop: the `cloudmusic` process and playing window were
  present, but no GSMTC session appeared in the actual enumeration.
- The repository's existing NetEase HTTP API can search/fetch metadata, lyrics,
  and audio, but cannot truthfully infer the desktop player's live playing
  state/position. No window-title fabrication was added.

## 9. Known limitations / next work

Suggested next order, with a fresh review before edits:

1. **Lyrics enrichment:** GSMTC does not expose lyrics, so current Reader
   presence reports `lyrics.status = none`. Audit the existing server NetEase
   lookup and add lyrics as a non-blocking independent enrichment path; presence
   freshness must remain independent of lyrics failures.
2. **Real analysis validation:** exercise BPM/key/energy/spectrogram on actual
   available song data and document unsupported-source behavior. Do not assume
   Spotify media is downloadable.
3. **Memory/tool validation:** exercise `music_memory` and decide the intended
   persistence semantics before adding writes or statistics.
4. **Resilience:** test network interruption, SSH loss, VPN switching, pause,
   player exit, Spotify-to-other-player switching, and Windows reboot.
5. **Startup UX:** design an audited Windows secret-storage/startup method.
   Current safe launcher intentionally asks for the token each run; do not put
   it in `.env`, Task Scheduler arguments, command history, or a plaintext file.
6. **NetEase adapter decision:** only add a fallback if a trustworthy local
   state source can supply playback status, position, and duration. Keep it as
   an adapter; do not replace the general GSMTC path.
7. **Delivery:** review the dirty set, decide what `tmp_check.txt` is, rerun the
   complete checks, then obtain approval before commit/push. No Reader commit or
   remote deployment has occurred yet.

## 10. Safety boundaries for the next task

- Do not ask the user to paste any token/Cookie into chat.
- Do not read, print, hash, or decrypt existing credential values.
- Do not rotate credentials again unless explicitly requested.
- Do not modify Auth0, OAuth/RBAC, Caddy, Shared Diary, or MCP permissions while
  working on Reader enhancements unless a separate, proven issue and approval
  exist.
- Preserve 9090 and 9091 loopback-only listeners.
- Preserve the existing Web Player presence path and strict server schema.
- Distinguish code/test evidence from live evidence.
- Inspect and preserve unrelated dirty files.

## 11. Exact next-task opening

Recommended first message in the new task:

```text
继续 Eryu Windows Reader。请先完整读取 AGENTS.md 和
docs/HANDOFF_WINDOWS_READER_2026-08-20.md，复核当前工作树与运行状态；
先不要停止正在运行的 Reader，也不要修改 Auth0/Caddy/MCP/Shared Diary。
从歌词非阻塞增强的只读审计开始，审计完先汇报再决定实现范围。
```
