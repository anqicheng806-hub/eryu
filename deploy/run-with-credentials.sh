#!/usr/bin/env bash
# Export systemd credentials only inside the service process, then replace this
# wrapper with the requested Eryu process. Never enable shell tracing here.
set -Eeuo pipefail

readonly APP_ROOT="/opt/eryu/current"
readonly VENV_ROOT="/opt/eryu/venv"

fail() {
    printf 'eryu credential bootstrap failed: %s\n' "$1" >&2
    exit 1
}

load_credential() {
    local credential_name="$1"
    local credential_path
    local credential_value
    local -a credential_lines=()

    [[ -n "${CREDENTIALS_DIRECTORY:-}" ]] || fail "systemd credential directory is unavailable"
    credential_path="${CREDENTIALS_DIRECTORY}/${credential_name}"
    [[ -r "$credential_path" && -f "$credential_path" ]] || fail "required credential ${credential_name} is unavailable"

    mapfile -t credential_lines < "$credential_path"
    [[ ${#credential_lines[@]} -eq 1 ]] || fail "credential ${credential_name} must contain exactly one line"
    credential_value="${credential_lines[0]}"
    [[ -n "$credential_value" ]] || fail "credential ${credential_name} is empty"
    [[ "$credential_value" != *[[:space:]]* ]] || fail "credential ${credential_name} contains whitespace"

    printf -v "$credential_name" '%s' "$credential_value"
    export "$credential_name"
    unset credential_value credential_lines
}

case "${1:-}" in
    web)
        load_credential MUSIC_U
        load_credential ERYU_AUTH_TOKEN
        load_credential ERYU_MCP_READ_TOKEN
        exec "${VENV_ROOT}/bin/python" "${APP_ROOT}/server/eryu.py"
        ;;
    mcp)
        load_credential ERYU_MCP_READ_TOKEN
        exec "${VENV_ROOT}/bin/eryu-music-mcp-http"
        ;;
    *)
        fail "expected service mode web or mcp"
        ;;
esac
