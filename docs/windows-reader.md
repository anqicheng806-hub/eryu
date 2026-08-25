# Windows GSMTC Reader

The Reader bridges Windows media sessions to the existing Eryu presence API:

```text
Windows player -> GSMTC -> Windows Reader -> POST /music/presence -> Eryu MCP
```

It never controls playback. It reads media properties, playback state, and the
timeline from Windows, then sends presence schema version 2. A non-null v2 song
always includes `catalog`, which is either `null` or one strictly resolved
NetEase reference. GSMTC does not expose lyrics, so the Reader enriches them
independently through the existing authenticated Eryu search and lyric
endpoints. Presence reporting never waits for that enrichment.

## Supported players

Any application that publishes a Windows Global System Media Transport Controls
session can work. This commonly includes Spotify and some versions/settings of
NetEase Cloud Music, but support must be confirmed on the actual machine with
the session-list command below. The Reader prefers a playing session, then uses
the optional source preference list to break ties.

The current server schema has no `source`/`player` field and rejects unknown
keys. The Reader therefore keeps `SourceAppUserModelId` in its local selection
and logs, and uses it when deriving a stable numeric `songId`. That public
synthetic ID never changes when catalog metadata or lyrics arrive.

NetEase fallback boundary: the repository's NetEase HTTP API can search and
fetch metadata, lyrics, and audio, but it cannot observe the playback state of
the Windows desktop client. A window title alone does not reliably prove
playing/paused state, position, or duration, so the Reader does not fabricate a
fallback presence from it. `GSMTCReader` accepts an injected adapter so a future
independent NetEase adapter can be added when a trustworthy local state source
is identified.

## Lyrics enrichment

For each newly selected GSMTC song that is not already known in the current
Reader run, the Reader immediately continues presence reporting with
`lyrics.status = loading` while one background worker searches through the
configured Eryu Web endpoint. A cached result can instead restore `ready`,
`none`, or `error` directly without passing through `loading`. The lookup
accepts only one exact normalized title-and-artist match; ambiguous or unmatched
results become `none` rather than guessing. The provider song ID remains
separate from lyric matching. The public presence `song.songId` and
`lyrics.songId` continue using the Reader's stable synthetic ID, so a song does
not change identity when lyrics arrive.

The same search response is also checked by a stricter catalog resolver without
making another request. It accepts a NetEase reference only when normalized
title, artist, and album are all exact, both the GSMTC and candidate durations
are valid and differ by no more than two seconds, and exactly one provider ID
matches. When proven, `song.catalog` is
`{"provider":"netease","songId":"<positive numeric id>"}`; otherwise it is
`null`. The provider ID is canonical decimal text with no leading zeroes. This
stricter check does not narrow the existing lyric rule: lyrics can still be
ready when catalog is unresolved, and catalog can be resolved when lyrics are
ambiguous or unavailable.

Ready lyrics include the current line, the previous two lines, the next two
lines, and timestamp-matched translation where available. A timeout, malformed
response, or other lookup failure becomes `error`. Each song is queried at most
once per Reader run. Up to 32 ready lyric bodies use a bounded LRU cache;
`none` and `error` use only the compact attempt record, so they cannot evict a
reusable ready body. The same attempt record prevents an evicted song from
being fetched again. Returning to an evicted ready song fails closed to `none`
until the Reader restarts. A proven catalog reference is retained in both the
ready cache and compact attempt record, including `none`/`error` results and
evicted ready lyrics, so returning to the song does not lose or re-query its
strict identity. Lyrics completion is picked up by the next normal heartbeat;
it does not wake or shorten presence failure backoff. None of these states alter
the presence heartbeat, sequence, or presence retry policy.

The latest queued song replaces any lookup that has not started. A lookup
already running in a worker thread is allowed to finish and can delay the next
song's lyrics by at most its bounded HTTP work, but generation and exact-song
checks discard its result from the current song. Presence reporting continues
independently throughout that wait.

The background worker reuses the same process-only full Eryu Web token and the
same endpoint/tunnel as presence. It does not read a NetEase cookie locally,
persist a new secret, or grant the MCP read token additional access. The
existing Eryu `GET /music/lyric` handler may populate its server-side lyric
cache on a first successful lookup; the Reader does not write any local lyric
cache. Catalog resolution itself only reuses search metadata: the Reader never
calls `/music/url`, `/music/stream`, or `POST /music/analyze`, never downloads
audio, and never starts analysis.

## Install the Windows-only dependency

Run in Windows PowerShell from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r .\reader\requirements-windows.txt
```

Expected: the pinned `3.2.1` Control, Foundation, Foundation.Collections,
Media, Storage.Streams, and runtime wheels are added (existing
`typing_extensions` may be reused). These are the narrow API namespaces used by
GSMTC; the much broader `all` extra is intentionally not installed.

## Inspect sessions without a token

Start music playback, then run:

```powershell
.\scripts\start_windows_reader.ps1 -ListSessions
```

The output lists `SourceAppUserModelId`, title, artist, and playback state. If a
player is absent, that player is not currently exposing a usable GSMTC session.

## Start reporting

### Recommended for the current VPS: private SSH tunnel

Eryu Web intentionally listens only on VPS loopback port `9090`. The public
`eryu-mcp` hostname exposes MCP only and must not be used as the presence write
endpoint. With the existing `vps` entry in the Windows SSH config, start the
Reader and its temporary private tunnel together:

```powershell
.\scripts\start_windows_reader.ps1 -SshTunnel
```

The launcher opens only local `127.0.0.1:19090`, forwards it to VPS
`127.0.0.1:9090`, verifies `/health`, and then asks for the full Eryu Web token
with masked input. It does not change the VPS, Caddy, firewall, or public routes.
Ctrl+C stops the Reader and closes only the SSH process created by that run.

### Direct HTTPS endpoint (other deployments)

Copy `.env.example` to `.env` and set the non-secret Eryu Web base URL in
`ERYU_PRESENCE_ENDPOINT`. Do not append `/music/presence`. If the Web host is
behind Caddy Basic Auth, also set only its non-secret username in
`ERYU_PRESENCE_BASIC_AUTH_USER`.

Then run:

```powershell
.\scripts\start_windows_reader.ps1
```

The launcher asks for the full Eryu Web token with masked input and, when a Basic
Auth username is configured, asks for that password the same way. It does not
put either secret in command history, arguments, logs, or `.env`, and removes
the temporary process variables when the Reader exits. The MCP read token is
not accepted for presence writes. Remote endpoints must use HTTPS; plain HTTP
is accepted only for loopback development. Press Ctrl+C to stop cleanly.

The Reader reports every two seconds and immediately after GSMTC media,
playback, timeline, current-session, or session-list changes. It holds a
per-user lock and uses a unique client session ID plus strictly increasing
sequence values, preventing two local Reader instances from racing.

Automatic login startup is intentionally not installed: an unattended task
would require a separate audited Windows secret-storage design. The launcher is
the safe, lightweight manual start path.
