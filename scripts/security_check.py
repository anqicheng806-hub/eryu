#!/usr/bin/env python3
"""Fail-closed local checks for accidentally committed credentials.

This is intentionally dependency-free. It complements, rather than replaces,
a dedicated scanner such as gitleaks in CI or before deployment.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
FORBIDDEN_NAMES = {
    ".env",
    ".secret",
    ".netease_cred",
    "id_rsa",
    "id_ed25519",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".ppk", ".p12", ".pfx", ".cred"}
SAFE_ENV_EXAMPLE_NAMES = {".env.example", ".env.sample", ".env.template"}
IGNORED_DEPENDENCY_ROOTS = {".git", ".venv", "venv", "node_modules"}
MAX_SCAN_BYTES = 2_000_000

# Split fixed markers so this scanner does not flag its own source.
SECRET_PATTERNS = {
    "private key": re.compile("-----BEGIN" + r"(?: [A-Z0-9]+)? PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "bcrypt password hash": re.compile(
        r"\$2" + r"[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}"
    ),
}

SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"""
    ^[ \t]*(?:-[ \t]*)?(?:\{[ \t]*)?
    (?:(?:export|set)[ \t]+)?(?P<powershell>\$env:)?["']?
    (?P<name>
        ERYU_AUTH_TOKEN
        |ERYU_MCP_READ_TOKEN
        |ERYU_BASIC_AUTH_(?:PASSWORD|HASH|ENTRY)
        |MCP_AUTH_TOKEN
        |MUSIC_U
        |AUTH0_(?:CLIENT_SECRET|MANAGEMENT_TOKEN|CLIENT_ASSERTION)
        |VPS_(?:PASSWORD|PASS|SECRET|TOKEN|API_KEY|PRIVATE_KEY|SSH_KEY|SSH_PRIVATE_KEY)
        |SSH_PRIVATE_KEY
        |DEPLOY_PRIVATE_KEY
    )
    ["']?[ \t]*(?:=|:)[ \t]*
    (?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s#,\r\n]+)
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

SYSTEMD_PLAINTEXT_SECRET_PATTERN = re.compile(
    r"""
    ^[ \t]*Environment[ \t]*=[ \t]*["']?
    (?P<name>
        ERYU_AUTH_TOKEN
        |ERYU_MCP_READ_TOKEN
        |ERYU_BASIC_AUTH_(?:PASSWORD|HASH|ENTRY)
        |MCP_AUTH_TOKEN
        |MUSIC_U
        |AUTH0_(?:CLIENT_SECRET|MANAGEMENT_TOKEN|CLIENT_ASSERTION)
    )
    [ \t]*=
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

PLACEHOLDER_VALUES = {
    "changeme",
    "change-me",
    "dummy",
    "example",
    "none",
    "null",
    "placeholder",
    "replace-me",
    "secret",
    "your-password",
    "your-secret",
    "your-token",
}


def _git_bytes(*args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"git {' '.join(args)} failed")
    return completed.stdout


def _git_path_items(*args: str) -> list[str]:
    raw = _git_bytes(*args, "-z")
    return [
        item
        for item in raw.decode("utf-8", errors="surrogateescape").split("\0")
        if item
    ]


def _is_forbidden_credential_path(relative: Path) -> bool:
    lowered_name = relative.name.lower()
    if lowered_name in FORBIDDEN_NAMES or relative.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    return (
        lowered_name.startswith(".env.")
        and lowered_name not in SAFE_ENV_EXAMPLE_NAMES
    )


def _is_ignored_dependency_path(relative: Path) -> bool:
    return bool(relative.parts) and relative.parts[0].lower() in IGNORED_DEPENDENCY_ROOTS


def _candidate_files() -> list[Path]:
    items = _git_path_items("ls-files", "--cached", "--others", "--exclude-standard")
    ignored_items = _git_path_items(
        "ls-files", "--others", "--ignored", "--exclude-standard"
    )
    for item in ignored_items:
        relative = Path(*PurePosixPath(item).parts)
        if (
            not _is_ignored_dependency_path(relative)
            and _is_forbidden_credential_path(relative)
        ):
            items.append(item)

    paths: list[Path] = []
    seen: set[str] = set()
    for item in items:
        posix_path = PurePosixPath(item)
        if posix_path.is_absolute() or ".." in posix_path.parts:
            continue
        normalized = posix_path.as_posix()
        if normalized in seen:
            continue
        seen.add(normalized)
        paths.append(ROOT.joinpath(*posix_path.parts))
    return paths


def _decode_text(raw: bytes) -> str | None:
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding = "utf-32"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        even_nuls = raw[0::2].count(0)
        odd_nuls = raw[1::2].count(0)
        pair_count = max(1, len(raw) // 2)
        if odd_nuls / pair_count > 0.2 and even_nuls / pair_count < 0.05:
            encoding = "utf-16-le"
        elif even_nuls / pair_count > 0.2 and odd_nuls / pair_count < 0.05:
            encoding = "utf-16-be"
        else:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return _decode_text(path.read_bytes())
    except OSError:
        return None


def _assignment_value_is_secret(name: str, raw_value: str) -> bool:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    if not value:
        return False
    lowered = value.lower()
    if (
        value.startswith(("<", "${", "{{", "%"))
        or lowered in PLACEHOLDER_VALUES
        or lowered.startswith(("example-", "placeholder-", "replace-", "your-", "your_"))
    ):
        return False
    upper_name = name.upper()
    if upper_name.startswith("VPS_") or upper_name in {
        "SSH_PRIVATE_KEY",
        "DEPLOY_PRIVATE_KEY",
    }:
        minimum_length = 1
    elif upper_name == "MUSIC_U":
        minimum_length = 16
    else:
        minimum_length = 16
    return len(value) >= minimum_length


def _scan_text(label: str, text: str) -> list[str]:
    findings = []
    for description, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append(f"{label}: possible {description}")
    for match in SENSITIVE_ASSIGNMENT_PATTERN.finditer(text):
        raw_name = match.group("name")
        if match.group("powershell") is None and raw_name != raw_name.upper():
            continue
        name = raw_name.upper()
        if _assignment_value_is_secret(name, match.group("value")):
            findings.append(f"{label}: possible credential assigned to {name}")
    normalized_label = label.replace("\\", "/").lower()
    if normalized_label.endswith(".service"):
        for match in SYSTEMD_PLAINTEXT_SECRET_PATTERN.finditer(text):
            findings.append(
                f"{label}: plaintext systemd Environment assignment for "
                f"{match.group('name').upper()}"
            )
    return findings


def main() -> int:
    findings: list[str] = []
    checked = 0
    root_resolved = ROOT.resolve()

    for path in _candidate_files():
        if path == SELF or not path.is_file():
            continue
        try:
            relative = path.relative_to(ROOT)
            path.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            findings.append(f"{path}: path resolves outside the repository")
            continue
        if _is_forbidden_credential_path(relative):
            findings.append(f"{relative}: credential-bearing filename must not be tracked")
            continue
        if path.stat().st_size > MAX_SCAN_BYTES:
            continue
        text = _read_text(path)
        if text is None:
            continue
        checked += 1
        findings.extend(_scan_text(str(relative), text))

    # Also scan every committed patch so deleting a leaked value later does not
    # make the repository history appear clean.
    history = _git_bytes("log", "--all", "--format=%B", "--patch", "--no-ext-diff").decode(
        "utf-8", errors="replace"
    )
    findings.extend(_scan_text("git history", history))

    if findings:
        print("SECURITY CHECK FAILED", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(f"- {finding}", file=sys.stderr)
        return 1

    print(f"SECURITY CHECK OK: scanned {checked} text files and Git history; no credential pattern matched.")
    print("Note: run gitleaks as an additional check before any deployment if it is available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
