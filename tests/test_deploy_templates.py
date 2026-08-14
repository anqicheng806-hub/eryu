from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
ANALYSIS_REQUIREMENTS = ROOT / "server" / "requirements-analysis.txt"


class DeploymentTemplateTest(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (DEPLOY / relative_path).read_text(encoding="utf-8")

    def test_web_unit_uses_loopback_persistent_state_and_encrypted_credentials(self) -> None:
        unit = self.read("systemd/eryu-web.service")
        self.assertIn("Environment=ERYU_HOST=127.0.0.1", unit)
        self.assertIn("Environment=ERYU_PORT=9090", unit)
        self.assertIn("Environment=ERYU_DATA_DIR=/var/lib/eryu", unit)
        self.assertIn(
            "Environment=ERYU_ALLOWED_ORIGIN=https://eryu.95.169.17.214.sslip.io",
            unit,
        )
        self.assertIn("ReadWritePaths=/var/lib/eryu", unit)
        self.assertIn("User=eryu-web", unit)
        self.assertIn("ProtectSystem=strict", unit)
        for name in ("MUSIC_U", "ERYU_AUTH_TOKEN", "ERYU_MCP_READ_TOKEN"):
            self.assertRegex(unit, rf"(?m)^LoadCredentialEncrypted={name}:")
            self.assertNotRegex(unit, rf"(?m)^Environment={name}=")

    def test_mcp_unit_receives_only_read_token_and_uses_canonical_https_resource(self) -> None:
        unit = self.read("systemd/eryu-mcp.service")
        canonical_url = "https://eryu-mcp.95.169.17.214.sslip.io"
        self.assertIn("User=eryu-mcp", unit)
        self.assertIn("Environment=ERYU_BASE_URL=http://127.0.0.1:9090", unit)
        self.assertIn("Environment=MCP_HTTP_HOST=127.0.0.1", unit)
        self.assertIn("Environment=MCP_HTTP_PORT=9091", unit)
        self.assertIn(f"Environment=MCP_PUBLIC_URL={canonical_url}", unit)
        self.assertIn(f"Environment=AUTH0_AUDIENCE={canonical_url}", unit)
        self.assertIn("Environment=MCP_REQUIRED_SCOPE=music:read", unit)
        self.assertRegex(unit, r"(?m)^LoadCredentialEncrypted=ERYU_MCP_READ_TOKEN:")
        self.assertNotIn("LoadCredentialEncrypted=MUSIC_U:", unit)
        self.assertNotIn("LoadCredentialEncrypted=ERYU_AUTH_TOKEN:", unit)
        self.assertNotRegex(unit, r"(?m)^Environment=.*(?:TOKEN|MUSIC_U)=")

    def test_public_auth0_file_cannot_be_mistaken_for_a_secret_file(self) -> None:
        config = self.read("systemd/auth0-public.conf.example")
        assignments = [
            line for line in config.splitlines() if line and not line.startswith("#")
        ]
        self.assertEqual(
            assignments,
            [
                "AUTH0_ISSUER_URL="
                "https://dev-k1463twcjjecqewp.us.auth0.com/"
            ],
        )
        self.assertNotRegex(config, r"(?im)^(?:.*TOKEN|MUSIC_U|CLIENT_SECRET)=")

    def test_credential_wrapper_never_passes_secrets_as_arguments_or_prints_values(self) -> None:
        wrapper = self.read("run-with-credentials.sh")
        self.assertIn("${CREDENTIALS_DIRECTORY}/${credential_name}", wrapper)
        self.assertIn("export \"$credential_name\"", wrapper)
        self.assertNotIn("set -x", wrapper)
        self.assertNotRegex(wrapper, r"(?m)^\s*echo.*credential_value")
        self.assertNotRegex(wrapper, r"(?m)^\s*printf(?!\s+-v).*credential_value")
        self.assertIn('exec "${VENV_ROOT}/bin/python"', wrapper)
        self.assertIn('exec "${VENV_ROOT}/bin/eryu-music-mcp-http"', wrapper)

    def test_caddy_exposes_two_hosts_and_only_proxies_loopback_upstreams(self) -> None:
        caddy = self.read("caddy/eryu.caddy")
        player = caddy.split("# ChatGPT connects", 1)[0]
        mcp = caddy.split("# ChatGPT connects", 1)[1]
        self.assertIn("eryu.95.169.17.214.sslip.io {", caddy)
        self.assertIn("eryu-mcp.95.169.17.214.sslip.io {", caddy)
        self.assertIn("reverse_proxy 127.0.0.1:9090", caddy)
        self.assertIn("reverse_proxy 127.0.0.1:9091", caddy)
        self.assertRegex(player, r"(?m)^\s*handle /health \{")
        self.assertRegex(player, r'(?m)^\s*respond "ok" 200$')
        self.assertIn('Cache-Control "no-store"', player)
        self.assertRegex(player, r"(?m)^\s*basicauth \{")
        self.assertNotRegex(player, r"(?m)^\s*basic_auth\b")
        self.assertIn(
            "import {$CREDENTIALS_DIRECTORY}/ERYU_BASIC_AUTH_ENTRY",
            player,
        )
        self.assertIn("header_up -Authorization", player)
        basicauth_offset = re.search(r"(?m)^\s*basicauth \{", player)
        self.assertIsNotNone(basicauth_offset)
        self.assertLess(player.index("handle /health"), basicauth_offset.start())
        self.assertLess(
            basicauth_offset.start(),
            player.index("reverse_proxy 127.0.0.1:9090"),
        )
        self.assertNotRegex(mcp, r"(?m)^\s*basic(?:_)?auth\b")
        self.assertIn("/.well-known/oauth-protected-resource", caddy)
        self.assertIn(
            "@mcp_endpoints path /mcp /.well-known/oauth-protected-resource",
            caddy,
        )
        self.assertNotIn("/.well-known/oauth-protected-resource/mcp", caddy)
        self.assertNotIn("/mcp/*", caddy)
        self.assertNotIn("0.0.0.0", caddy)
        self.assertNotIn("diary", caddy.lower())

    def test_caddy_basic_auth_is_loaded_only_from_an_encrypted_systemd_credential(self) -> None:
        drop_in = self.read("systemd/caddy-eryu-credentials.conf")
        assignments = [
            line for line in drop_in.splitlines() if line and not line.startswith("[")
        ]
        self.assertEqual(
            assignments,
            [
                "RuntimeDirectory=eryu-caddy-config",
                "RuntimeDirectoryMode=0700",
                "RuntimeDirectoryPreserve=no",
                "Environment=XDG_CONFIG_HOME=/run/eryu-caddy-config",
                "ExecStartPre=/usr/bin/install -d -m 0700 "
                "/run/eryu-caddy-config/caddy",
                "ExecStartPre=/usr/bin/ln -sfnT /dev/null "
                "/run/eryu-caddy-config/caddy/autosave.json",
                "LoadCredentialEncrypted=ERYU_BASIC_AUTH_ENTRY:"
                "/etc/credstore.encrypted/eryu/ERYU_BASIC_AUTH_ENTRY.cred",
            ],
        )
        self.assertIn("autosave.json", drop_in)
        self.assertIn("/dev/null", drop_in)
        self.assertNotIn("XDG_DATA_HOME", drop_in)
        self.assertNotRegex(drop_in, r"\$2[aby]\$")
        self.assertNotRegex(drop_in, r"(?i)password\s*=")

        plan = self.read("README.md")
        self.assertIn(
            'test "$(/usr/bin/caddy version | awk \'{print $1}\')" = "v2.6.2"',
            plan,
        )
        self.assertIn(
            'grep -Fxq "XDG_CONFIG_HOME=/run/eryu-caddy-config"',
            plan,
        )
        self.assertGreaterEqual(
            plan.count(
                "sudo test -L /run/eryu-caddy-config/caddy/autosave.json"
            ),
            2,
        )

        helper = self.read("create-caddy-basic-auth-credential.sh")
        self.assertIn("systemd-ask-password", helper)
        self.assertIn("caddy hash-password --algorithm bcrypt", helper)
        self.assertIn('systemd-creds encrypt --name="$CREDENTIAL_NAME"', helper)
        self.assertNotIn("--plaintext", helper)
        self.assertNotIn("set -x", helper)
        self.assertIn("set +x", helper)
        self.assertIn("printf '%s\\n'", helper)
        self.assertNotIn("tee", helper)
        self.assertIn('[[ ! -e "$CREDENTIAL_PATH" ]]', helper)
        self.assertIn("20-72 bytes", helper)

    def test_units_invoke_the_non_secret_wrapper_and_not_a_shell_command_string(self) -> None:
        expected = "/usr/local/libexec/eryu-run-with-credentials"
        for relative_path, mode in (
            ("systemd/eryu-web.service", "web"),
            ("systemd/eryu-mcp.service", "mcp"),
        ):
            with self.subTest(unit=relative_path):
                unit = self.read(relative_path)
                self.assertIn(f"ExecStart={expected} {mode}", unit)
                self.assertNotRegex(unit, r"(?m)^ExecStart=.*/(?:ba)?sh\s+-c")
                self.assertNotRegex(unit, re.compile(r"(?m)^Environment=.*secret", re.I))

    def test_analysis_dependencies_are_pinned_and_used_by_the_deploy_plan(self) -> None:
        requirements = {
            line
            for line in ANALYSIS_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }
        self.assertEqual(
            requirements,
            {"librosa==0.11.0", "numpy==2.2.6", "matplotlib==3.10.8"},
        )
        plan = self.read("README.md")
        self.assertIn(
            "--only-binary=:all: --progress-bar off -r "
            "/opt/eryu/current/server/requirements-analysis.txt",
            plan,
        )
        self.assertIn("/opt/eryu/venv/bin/python -m pip check", plan)
        self.assertIn("Caddyfile.pre-eryu", plan)
        self.assertIn("Caddyfile.expected-eryu", plan)
        self.assertIn("sudo cmp --silent", plan)
        self.assertNotIn("sudo diff", plan)
        self.assertNotIn("sudoedit /etc/caddy/Caddyfile", plan)
        self.assertIn("不表示网络隔离", plan)
        self.assertIn("ERYU_BASIC_AUTH_ENTRY", plan)
        self.assertIn("/health", plan)
        self.assertIn("basicauth", plan)
        self.assertNotIn("数字 MP3 边界", plan)


if __name__ == "__main__":
    unittest.main()
