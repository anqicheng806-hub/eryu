#!/usr/bin/env bash
# Create one encrypted Caddy account-line credential without writing the
# username, plaintext password, or password hash to a regular file or stdout.
set -Eeuo pipefail
set +x
umask 0077
export LC_ALL=C

readonly CREDENTIAL_NAME="ERYU_BASIC_AUTH_ENTRY"
readonly CREDENTIAL_DIR="/etc/credstore.encrypted/eryu"
readonly CREDENTIAL_PATH="${CREDENTIAL_DIR}/${CREDENTIAL_NAME}.cred"

fail() {
    printf 'Eryu Basic Auth credential creation failed: %s\n' "$1" >&2
    exit 1
}

cleanup() {
    unset basic_auth_username basic_auth_password basic_auth_confirmation basic_auth_hash
}
trap cleanup EXIT

[[ ${EUID} -eq 0 ]] || fail "run this helper with sudo"
for required_command in caddy systemd-ask-password systemd-creds; do
    command -v "$required_command" >/dev/null 2>&1 \
        || fail "required command ${required_command} is unavailable"
done

install -d -o root -g root -m 0700 "$CREDENTIAL_DIR"
[[ ! -e "$CREDENTIAL_PATH" ]] || fail "encrypted credential already exists"

basic_auth_username="$(systemd-ask-password 'Eryu Basic Auth username')"
[[ "$basic_auth_username" =~ ^[A-Za-z0-9._-]{1,64}$ ]] \
    || fail "username must be 1-64 ASCII letters, digits, dot, underscore, or hyphen"

basic_auth_password="$(systemd-ask-password 'Eryu Basic Auth password (20-72 bytes)')"
basic_auth_confirmation="$(systemd-ask-password 'Confirm Eryu Basic Auth password')"
[[ "$basic_auth_password" == "$basic_auth_confirmation" ]] \
    || fail "password confirmation does not match"
[[ ${#basic_auth_password} -ge 20 && ${#basic_auth_password} -le 72 ]] \
    || fail "password must contain 20-72 bytes"

basic_auth_hash="$(
    printf '%s\n' "$basic_auth_password" \
        | caddy hash-password --algorithm bcrypt
)"
unset basic_auth_password basic_auth_confirmation
[[ "$basic_auth_hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] \
    || fail "Caddy returned an unexpected bcrypt hash format"

printf '%s %s\n' "$basic_auth_username" "$basic_auth_hash" \
    | systemd-creds encrypt --name="$CREDENTIAL_NAME" - "$CREDENTIAL_PATH" \
        >/dev/null
unset basic_auth_username basic_auth_hash
chmod 0600 "$CREDENTIAL_PATH"

printf 'Encrypted Caddy Basic Auth credential created.\n'
