from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import security_check


class SecurityCheckTest(unittest.TestCase):
    def test_common_assignment_formats_are_detected_without_echoing_values(self) -> None:
        cases = [
            (
                "ERYU_AUTH_TOKEN",
                "export ERYU_AUTH_" + "TOKEN=" + "A" * 40,
                "A" * 40,
            ),
            (
                "ERYU_MCP_READ_TOKEN",
                "$env:ERYU_MCP_" + "READ_TOKEN = '" + "B" * 40 + "'",
                "B" * 40,
            ),
            (
                "MUSIC_U",
                "MUSIC_" + 'U: "' + "C" * 40 + '"',
                "C" * 40,
            ),
            (
                "VPS_PASSWORD",
                "VPS_" + "PASSWORD: " + "D" * 20,
                "D" * 20,
            ),
            (
                "VPS_PASSWORD",
                "VPS_" + "PASSWORD=" + "Q",
                "Q",
            ),
            (
                "VPS_PRIVATE_KEY",
                '{"VPS_' + 'PRIVATE_KEY": "' + "E" * 48 + '"}',
                "E" * 48,
            ),
            (
                "AUTH0_CLIENT_SECRET",
                "AUTH0_" + "CLIENT_SECRET=" + "G" * 40,
                "G" * 40,
            ),
            (
                "ERYU_BASIC_AUTH_ENTRY",
                "ERYU_BASIC_" + "AUTH_ENTRY=" + "J" * 40,
                "J" * 40,
            ),
        ]

        for expected_name, text, secret_value in cases:
            with self.subTest(expected_name=expected_name):
                findings = security_check._scan_text("sample", text)
                rendered = "\n".join(findings)
                self.assertIn(expected_name, rendered)
                self.assertNotIn(secret_value, rendered)

    def test_systemd_units_cannot_embed_secret_environment_assignments(self) -> None:
        secret_value = "H" * 40
        text = (
            "[Service]\n"
            'Environment="ERYU_AUTH_TOKEN=' + secret_value + '"\n'
            'Environment="ERYU_BASIC_AUTH_HASH=' + secret_value + '"\n'
            "LoadCredentialEncrypted=ERYU_MCP_READ_TOKEN:/safe/read-token.cred\n"
        )

        findings = security_check._scan_text(
            "deploy/systemd/example.service",
            text,
        )
        rendered = "\n".join(findings)
        self.assertIn("plaintext systemd Environment assignment", rendered)
        self.assertIn("ERYU_AUTH_TOKEN", rendered)
        self.assertIn("ERYU_BASIC_AUTH_HASH", rendered)
        self.assertNotIn(secret_value, rendered)
        self.assertNotIn("ERYU_MCP_READ_TOKEN", rendered)

    def test_placeholders_are_not_reported_as_credentials(self) -> None:
        placeholders = [
            "ERYU_AUTH_" + "TOKEN=<generate-a-random-token>",
            "$env:ERYU_MCP_" + "READ_TOKEN = '${READ_TOKEN}'",
            "MUSIC_" + "U={{ music_cookie }}",
            "VPS_" + "PASSWORD: your-password",
            "ERYU_BASIC_" + "AUTH_ENTRY=${CREDENTIALS_DIRECTORY}/entry",
        ]
        for text in placeholders:
            with self.subTest(text=text):
                self.assertEqual(security_check._scan_text("sample", text), [])

    def test_utf16_with_and_without_bom_is_scanned(self) -> None:
        secret_value = "F" * 24
        text = "VPS_" + "PASSWORD=" + secret_value
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                decoded = security_check._decode_text(text.encode(encoding))
                self.assertIsNotNone(decoded)
                findings = security_check._scan_text("utf16", decoded or "")
                self.assertTrue(findings)
                self.assertNotIn(secret_value, "\n".join(findings))

    def test_ignored_credential_filenames_are_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (root / ".gitignore").write_text(
                "\n".join(
                    [
                        ".env",
                        ".env.*",
                        "!.env.example",
                        "*.pem",
                        "*.key",
                        "*.ppk",
                        "*.cred",
                        "server/.secret",
                        "server/.netease_cred",
                        ".venv/",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "server").mkdir()
            (root / ".venv").mkdir()
            for relative in (
                ".env",
                ".env.local",
                "deploy.pem",
                "server/deploy.key",
                "deploy.ppk",
                "basic-auth.cred",
                "server/.secret",
                "server/.netease_cred",
                ".venv/dependency.key",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("local-only", encoding="utf-8")
            (root / ".env.example").write_text(
                "ERYU_AUTH_TOKEN=<generate>", encoding="utf-8"
            )

            with mock.patch.object(security_check, "ROOT", root):
                candidates = {
                    path.relative_to(root).as_posix()
                    for path in security_check._candidate_files()
                }

            expected = {
                ".env",
                ".env.local",
                "deploy.pem",
                "server/deploy.key",
                "deploy.ppk",
                "basic-auth.cred",
                "server/.secret",
                "server/.netease_cred",
            }
            self.assertTrue(expected.issubset(candidates))
            self.assertNotIn(".venv/dependency.key", candidates)
            self.assertIn(".env.example", candidates)
            self.assertFalse(
                security_check._is_forbidden_credential_path(Path(".env.example"))
            )
            for relative in expected:
                self.assertTrue(
                    security_check._is_forbidden_credential_path(Path(relative))
                )

    def test_bcrypt_hash_is_detected_without_echoing_it(self) -> None:
        password_hash = "$2" + "a$14$" + "A" * 53
        findings = security_check._scan_text("sample", "user " + password_hash)
        self.assertIn("bcrypt password hash", "\n".join(findings))
        self.assertNotIn(password_hash, "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
