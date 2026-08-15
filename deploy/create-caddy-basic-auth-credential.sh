#!/usr/bin/env bash
# Create one encrypted Caddy account-line credential without writing the
# username, plaintext password, or password hash to a regular file or stdout.
set -Eeuo pipefail
set +x
umask 0077
export LC_ALL=C

readonly CREDENTIAL_NAME="ERYU_BASIC_AUTH_ENTRY"
readonly CREDENTIAL_PARENT_DIR="/etc/credstore.encrypted"
readonly CREDENTIAL_DIR="/etc/credstore.encrypted/eryu"
readonly CREDENTIAL_PATH="${CREDENTIAL_DIR}/${CREDENTIAL_NAME}.cred"
readonly CADDY_CANDIDATE="/opt/caddy-candidates/v2.11.4/caddy"
readonly CADDY_CANDIDATE_SHA256="b7105518e3ed1c0761f232e44fc09345535533c9cb0abf0e12809416c7ac64d9"

fail() {
    printf 'Eryu Basic Auth credential creation failed: %s\n' "$1" >&2
    exit 1
}

cleanup() {
    unset basic_auth_username basic_auth_password basic_auth_confirmation basic_auth_hash
}
trap cleanup EXIT

[[ ${EUID} -eq 0 ]] || fail "run this helper with sudo"
for required_command in \
    "$CADDY_CANDIDATE" \
    /usr/bin/systemd-ask-password \
    /usr/bin/systemd-creds \
    /usr/bin/install \
    /usr/bin/chmod \
    /usr/bin/stat \
    /usr/bin/sha256sum \
    /usr/sbin/getcap; do
    [[ -x "$required_command" ]] \
        || fail "required fixed command ${required_command} is unavailable"
done

for caddy_candidate_directory in \
    /opt \
    /opt/caddy-candidates \
    /opt/caddy-candidates/v2.11.4; do
    [[ -d "$caddy_candidate_directory" && ! -L "$caddy_candidate_directory" ]] \
        || fail "the approved Caddy candidate directory is not a real directory"
    [[ "$(/usr/bin/stat -c '%U:%G:%a' "$caddy_candidate_directory")" == root:root:755 ]] \
        || fail "the approved Caddy candidate directory is not root:root mode 0755"
done
unset caddy_candidate_directory
[[ -f "$CADDY_CANDIDATE" && ! -L "$CADDY_CANDIDATE" ]] \
    || fail "the approved Caddy candidate is not a regular file"
[[ "$(/usr/bin/stat -c '%U:%G:%a:%h' "$CADDY_CANDIDATE")" == root:root:755:1 ]] \
    || fail "the approved Caddy candidate must be root:root mode 0755 with one link"
caddy_candidate_sha256="$(/usr/bin/sha256sum "$CADDY_CANDIDATE" 2>/dev/null)"
caddy_candidate_sha256=${caddy_candidate_sha256%% *}
[[ "$caddy_candidate_sha256" == "$CADDY_CANDIDATE_SHA256" ]] \
    || fail "the approved Caddy candidate digest does not match"
unset caddy_candidate_sha256
caddy_candidate_capabilities="$(/usr/sbin/getcap "$CADDY_CANDIDATE" 2>/dev/null)" \
    || fail "the approved Caddy candidate capabilities could not be checked"
[[ -z "$caddy_candidate_capabilities" ]] \
    || fail "the approved Caddy candidate has file capabilities"
unset caddy_candidate_capabilities

[[ -d /etc && ! -L /etc ]] || fail "/etc is not a real directory"
[[ "$(/usr/bin/stat -c '%U:%G' /etc)" == root:root ]] \
    || fail "/etc is not root controlled"
etc_mode="$(/usr/bin/stat -c '%a' /etc)"
[[ "$etc_mode" =~ ^[0-7]{3,4}$ ]] || fail "/etc has an unexpected mode"
(( (8#$etc_mode & 0022) == 0 )) || fail "/etc is group/other writable"
unset etc_mode

for private_directory in "$CREDENTIAL_PARENT_DIR" "$CREDENTIAL_DIR"; do
    if [[ ! -e "$private_directory" && ! -L "$private_directory" ]]; then
        /usr/bin/install -d -o root -g root -m 0700 "$private_directory"
    fi
    [[ -d "$private_directory" && ! -L "$private_directory" ]] \
        || fail "credential directory is not a real directory"
    [[ "$(/usr/bin/stat -c '%U:%G:%a' "$private_directory")" == root:root:700 ]] \
        || fail "credential directory must be root:root mode 0700"
done
unset private_directory
[[ ! -e "$CREDENTIAL_PATH" ]] || fail "encrypted credential already exists"
[[ ! -L "$CREDENTIAL_PATH" ]] || fail "encrypted credential path is a symlink"

basic_auth_username="$(/usr/bin/systemd-ask-password 'Eryu Basic Auth username')"
[[ "$basic_auth_username" =~ ^[A-Za-z0-9._-]{1,64}$ ]] \
    || fail "username must be 1-64 ASCII letters, digits, dot, underscore, or hyphen"

basic_auth_password="$(/usr/bin/systemd-ask-password 'Eryu Basic Auth password (20-72 bytes)')"
basic_auth_confirmation="$(/usr/bin/systemd-ask-password 'Confirm Eryu Basic Auth password')"
[[ "$basic_auth_password" == "$basic_auth_confirmation" ]] \
    || fail "password confirmation does not match"
[[ ${#basic_auth_password} -ge 20 && ${#basic_auth_password} -le 72 ]] \
    || fail "password must contain 20-72 bytes"

basic_auth_hash="$(
    printf '%s\n' "$basic_auth_password" \
        | "$CADDY_CANDIDATE" hash-password --algorithm bcrypt
)"
unset basic_auth_password basic_auth_confirmation
[[ "$basic_auth_hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] \
    || fail "Caddy returned an unexpected bcrypt hash format"

printf '%s %s\n' "$basic_auth_username" "$basic_auth_hash" \
    | /usr/bin/systemd-creds encrypt --name="$CREDENTIAL_NAME" - "$CREDENTIAL_PATH" \
        >/dev/null
unset basic_auth_username basic_auth_hash
/usr/bin/chmod 0600 "$CREDENTIAL_PATH"

printf 'Encrypted Caddy Basic Auth credential created.\n'
