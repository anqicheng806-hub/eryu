from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
ANALYSIS_REQUIREMENTS = ROOT / "server" / "requirements-analysis.txt"
CADDY_CANDIDATE = "/opt/caddy-candidates/v2.11.4/caddy"
CADDY_CANDIDATE_SHA256 = (
    "b7105518e3ed1c0761f232e44fc09345535533c9cb0abf0e12809416c7ac64d9"
)
CADDY_ASSET_SHA256 = (
    "527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9"
)
ERYU_CADDY_SHA256 = (
    "23c29b8ec7b777f8858281e832ca257ff9200f2cb3d51e5640024a7eb1b3fed3"
)


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
        self.assertRegex(player, r"(?m)^\s*basic_auth \{")
        self.assertNotRegex(player, r"(?m)^\s*basicauth\b")
        self.assertIn(
            "import {$CREDENTIALS_DIRECTORY}/ERYU_BASIC_AUTH_ENTRY",
            player,
        )
        self.assertIn("header_up -Authorization", player)
        basic_auth_offset = re.search(r"(?m)^\s*basic_auth \{", player)
        self.assertIsNotNone(basic_auth_offset)
        self.assertLess(player.index("handle /health"), basic_auth_offset.start())
        self.assertLess(
            basic_auth_offset.start(),
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
                "LoadCredentialEncrypted=ERYU_BASIC_AUTH_ENTRY:"
                "/etc/credstore.encrypted/eryu/ERYU_BASIC_AUTH_ENTRY.cred",
            ],
        )
        self.assertNotIn("RuntimeDirectory", drop_in)
        self.assertNotIn("XDG_CONFIG_HOME", drop_in)
        self.assertNotIn("autosave.json", drop_in)
        self.assertNotIn("ExecStartPre", drop_in)
        self.assertNotRegex(drop_in, r"\$2[aby]\$")
        self.assertNotRegex(drop_in, r"(?i)password\s*=")

        candidate_drop_in = self.read("systemd/caddy-v2114-candidate.conf")
        candidate_directives = [
            line
            for line in candidate_drop_in.splitlines()
            if line and not line.startswith("[") and not line.startswith("#")
        ]
        self.assertEqual(
            candidate_directives,
            [
                "ExecStart=",
                f"ExecStart={CADDY_CANDIDATE} run --config /etc/caddy/Caddyfile "
                "--adapter caddyfile",
                "ExecReload=",
                f"ExecReload={CADDY_CANDIDATE} reload --config /etc/caddy/Caddyfile "
                "--adapter caddyfile --force --address unix//run/caddy/admin.sock",
                "RuntimeDirectory=caddy",
                "RuntimeDirectoryMode=0750",
                "RuntimeDirectoryPreserve=no",
            ],
        )
        self.assertIn(
            f"ExecStart={CADDY_CANDIDATE} run --config /etc/caddy/Caddyfile "
            "--adapter caddyfile",
            candidate_drop_in,
        )
        self.assertIn(
            f"ExecReload={CADDY_CANDIDATE} reload --config /etc/caddy/Caddyfile "
            "--adapter caddyfile --force --address unix//run/caddy/admin.sock",
            candidate_drop_in,
        )
        self.assertIn("RuntimeDirectory=caddy", candidate_drop_in)
        self.assertIn("RuntimeDirectoryMode=0750", candidate_drop_in)
        self.assertIn("RuntimeDirectoryPreserve=no", candidate_drop_in)
        self.assertNotIn("/usr/bin/caddy", candidate_drop_in)
        self.assertNotIn("ExecStartPre", candidate_drop_in)
        self.assertNotIn("ExecCondition", candidate_drop_in)

        plan = self.read("README.md")
        upgrade_plan = self.read("CADDY-UPGRADE.md")
        self.assertIn("Caddy 2.6.2 不允许直接承载 Eryu", upgrade_plan)
        self.assertIn("persist_config off", upgrade_plan)
        self.assertIn("admin unix//run/caddy/admin.sock|0600", upgrade_plan)
        self.assertIn("--address unix//run/caddy/admin.sock", upgrade_plan)
        self.assertIn("basic_auth", upgrade_plan)
        self.assertIn("GHSA-6365-7ppr-5r92", upgrade_plan)
        self.assertIn("v2.11.5", upgrade_plan)
        self.assertIn(
            f"{CADDY_CANDIDATE} list-modules --skip-standard --packages --versions",
            upgrade_plan,
        )
        self.assertIn('test "$systemd_major" -ge 254', upgrade_plan)
        self.assertIn("Admin-API-only", upgrade_plan)
        self.assertIn("--environ", upgrade_plan)
        self.assertNotIn("ln -sfnT /dev/null", plan)
        self.assertNotIn("XDG_CONFIG_HOME=/run/eryu-caddy-config", plan)
        readonly_section = upgrade_plan.split(
            "## 3. 下一次 VPS 只读盘点（必须先单独批准）", 1
        )[1].split("## 4.", 1)[0]
        self.assertNotIn("validate --config", readonly_section)
        visible_unit_inventory = readonly_section.split(
            "# 只输出固定分类", 1
        )[0]
        self.assertNotIn("--property=ExecStart \\", visible_unit_inventory)
        self.assertNotIn("--property=ExecReload \\", visible_unit_inventory)
        self.assertIn(
            "caddy_exec_start=approved_file_backed_form", readonly_section
        )
        self.assertIn("caddy_exec_resume=absent", readonly_section)
        self.assertIn("caddy_exec_start=unapproved_form", readonly_section)
        self.assertIn("caddy_exec_reload=unapproved_form", readonly_section)
        self.assertIn("ignore_errors=no", readonly_section)
        self.assertIn("--property=ExecStartEx", readonly_section)
        self.assertIn("--property=ExecReloadEx", readonly_section)
        self.assertIn("caddy_exec_start_ex=empty_flags", readonly_section)
        self.assertIn("caddy_exec_reload_ex=empty_flags", readonly_section)
        self.assertIn("unapproved_flags_or_form", readonly_section)
        self.assertIn("not_one_direct_record", readonly_section)
        self.assertIn("unset caddy_exec_start_record", readonly_section)
        self.assertIn("unset caddy_exec_reload_record", readonly_section)
        self.assertIn("caddy_runtime_inputs=present", readonly_section)
        self.assertIn("caddy_expect_empty_bus_property", readonly_section)
        self.assertIn("/usr/bin/busctl --system get-property", readonly_section)
        self.assertIn('test -z "${DBUS_SYSTEM_BUS_ADDRESS+x}"', readonly_section)
        self.assertLess(
            readonly_section.index("set +x"),
            readonly_section.index("/usr/bin/busctl --system get-property"),
        )
        self.assertIn("EnvironmentFiles 'a(sb) 0'", readonly_section)
        self.assertIn("ImportCredential 'as 0'", readonly_section)
        self.assertIn("LoadCredentialEncrypted 'a(ss) 0'", readonly_section)
        self.assertIn("caddy_journal_secret_name_hit=present", readonly_section)
        self.assertIn("caddy_lifecycle_hooks=present", readonly_section)
        self.assertIn("ExecConditionEx", readonly_section)
        self.assertIn("ExecStartPreEx", readonly_section)
        self.assertIn("ExecStartPostEx", readonly_section)
        self.assertIn("ExecStopEx", readonly_section)
        self.assertIn("ExecStopPostEx", readonly_section)
        self.assertIn(
            'manager_pipeline_status=("${PIPESTATUS[@]}")', readonly_section
        )
        self.assertIn(
            'journal_pipeline_status=("${PIPESTATUS[@]}")', readonly_section
        )
        self.assertIn("匹配行和值永远不显示", readonly_section)
        self.assertIn("不得自动 vacuum/delete journal", upgrade_plan)
        self.assertIn("历史 `--environ` 暴露状态记为", upgrade_plan)
        self.assertIn("`unknown` 并停止", upgrade_plan)
        self.assertIn("caddy_runtime_inputs=absent", upgrade_plan)
        self.assertIn(
            "for trusted_binary in /usr/bin/caddy /usr/bin/git "
            "/usr/bin/awk /usr/bin/busctl /usr/bin/sudo",
            upgrade_plan,
        )
        self.assertIn("/usr/bin/stat -c '%U:%G'", upgrade_plan)
        self.assertIn("8#$trusted_binary_mode & 0022", upgrade_plan)
        self.assertIn("/usr/bin/dpkg-query -S", upgrade_plan)
        self.assertNotIn("/usr/bin/dpkg --verify", upgrade_plan)
        self.assertIn("/usr/bin/sha256sum", upgrade_plan)
        self.assertIn("sha256=", upgrade_plan)
        self.assertIn("维护审批记录", upgrade_plan)
        self.assertIn("caddy_readonly_gate=failed", upgrade_plan)
        self.assertIn("|| readonly_gate_failed", upgrade_plan)
        self.assertIn("KEY|AUTHORIZATION|CREDENTIAL|BEARER", upgrade_plan)
        self.assertIn("caddy_unit_identity=caddy:caddy", upgrade_plan)
        self.assertIn("caddy_need_daemon_reload=no", upgrade_plan)
        self.assertIn("caddy_existing_drop_ins=absent", upgrade_plan)
        self.assertIn("caddy_existing_runtime_directory=absent", upgrade_plan)
        self.assertIn("caddy_unit_fragment=approved sha256=", upgrade_plan)
        self.assertIn("官方 Release archive", upgrade_plan)
        self.assertIn("dpkg metadata 只盘点旧", upgrade_plan)
        self.assertIn("RuntimeDirectory=caddy", upgrade_plan)
        self.assertIn("RuntimeDirectoryMode=0750", upgrade_plan)
        self.assertIn("RuntimeDirectoryPreserve=no", upgrade_plan)
        self.assertIn("配置文件 mode\n   `0640`", upgrade_plan)
        self.assertIn("Caddy 二进制 mode `0750`", upgrade_plan)
        self.assertIn("root-only\n   manifest", upgrade_plan)
        self.assertIn("NeedDaemonReload=no", upgrade_plan)
        self.assertIn(CADDY_CANDIDATE, plan)
        self.assertIn(CADDY_CANDIDATE, upgrade_plan)
        self.assertIn(CADDY_CANDIDATE_SHA256, plan)
        self.assertIn(CADDY_CANDIDATE_SHA256, upgrade_plan)
        self.assertIn(CADDY_ASSET_SHA256, upgrade_plan)
        self.assertIn(
            "https://github.com/caddyserver/caddy/.github/workflows/release.yml@"
            "refs/tags/v2.11.4",
            upgrade_plan,
        )
        self.assertIn("https://token.actions.githubusercontent.com", upgrade_plan)

        helper = self.read("create-caddy-basic-auth-credential.sh")
        self.assertIn("/usr/bin/systemd-ask-password", helper)
        self.assertIn('"$CADDY_CANDIDATE" hash-password --algorithm bcrypt', helper)
        self.assertIn(CADDY_CANDIDATE, helper)
        self.assertIn(CADDY_CANDIDATE_SHA256, helper)
        self.assertIn("/usr/bin/sha256sum", helper)
        self.assertIn("/usr/sbin/getcap", helper)
        self.assertIn('[[ -f "$CADDY_CANDIDATE" && ! -L "$CADDY_CANDIDATE" ]]', helper)
        self.assertLess(
            helper.index("caddy_candidate_sha256="),
            helper.index("Eryu Basic Auth username"),
        )
        self.assertLess(
            helper.index("caddy_candidate_capabilities="),
            helper.index("Eryu Basic Auth username"),
        )
        self.assertIn('/usr/bin/systemd-creds encrypt --name="$CREDENTIAL_NAME"', helper)
        self.assertNotIn("--plaintext", helper)
        self.assertNotIn("set -x", helper)
        self.assertIn("set +x", helper)
        self.assertIn("printf '%s\\n'", helper)
        self.assertNotIn("tee", helper)
        self.assertIn('[[ ! -e "$CREDENTIAL_PATH" ]]', helper)
        self.assertIn('[[ ! -L "$CREDENTIAL_PATH" ]]', helper)
        self.assertIn('[[ -d /etc && ! -L /etc ]]', helper)
        self.assertIn('"$CREDENTIAL_PARENT_DIR" "$CREDENTIAL_DIR"', helper)
        self.assertIn("root:root:700", helper)
        self.assertIn("20-72 bytes", helper)
        self.assertNotIn('command -v "$required_command"', helper)

    def test_release_is_pinned_to_the_verified_remote_tip_and_checked_out_detached(self) -> None:
        plan = self.read("README.md")
        upgrade_plan = self.read("CADDY-UPGRADE.md")
        self.assertIn("Approved 40-hex release SHA", plan)
        self.assertIn("^[0-9a-f]{40}$", plan)
        self.assertIn('test "$(command -v git)" = /usr/bin/git', plan)
        self.assertIn('test "$(command -v awk)" = /usr/bin/awk', plan)
        self.assertIn("ls-remote --exit-code --heads", plan)
        self.assertIn("eryu_release_gate=failed", plan)
        self.assertIn("remote_release_ref", plan)
        self.assertIn('test "$remote_release_sha" = "$ERYU_RELEASE_SHA"', plan)
        self.assertIn("--single-branch --no-checkout", plan)
        self.assertIn('checkout --detach "$ERYU_RELEASE_SHA"', plan)
        self.assertIn("refs/remotes/origin/$ERYU_BRANCH", plan)
        self.assertIn("rev-parse HEAD", plan)
        self.assertIn("rev-parse --abbrev-ref HEAD", plan)
        self.assertNotIn('/usr/bin/sudo "$(readlink -f', plan)
        self.assertIn('test "$(command -v sudo)" = /usr/bin/sudo', plan)
        self.assertNotRegex(plan, r"(?m)(?<!/usr/bin/)\bsudo\s")
        self.assertNotRegex(upgrade_plan, r"(?m)(?<!/usr/bin/)\bsudo\s")

    def test_caddy_credential_activation_precedes_eryu_route_installation(self) -> None:
        plan = self.read("README.md")
        daemon_reload = plan.index("/usr/bin/systemctl daemon-reload")
        credential_restart = plan.index("/usr/bin/systemctl restart caddy.service")
        route_install = plan.index(
            "/usr/bin/sudo /usr/bin/install -o root -g root -m 0644 "
            "/opt/eryu/current/deploy/caddy/eryu.caddy"
        )
        final_reload = plan.index("/usr/bin/systemctl reload caddy.service")
        self.assertLess(daemon_reload, credential_restart)
        self.assertLess(credential_restart, route_install)
        self.assertLess(route_install, final_reload)
        self.assertIn("只做 reload 不能替代这次 activation", plan)
        self.assertIn("admin\\.sock\\|0600", plan)
        self.assertIn("--address unix//run/caddy/admin.sock", plan)
        self.assertIn("caddy_exec_start=approved_post_upgrade_form", plan)
        self.assertIn("caddy_exec_reload=approved_post_upgrade_form", plan)
        self.assertIn("Caddy ExecStart classification failed", plan)
        self.assertIn("Caddy ExecReload classification failed", plan)
        self.assertIn("Caddy ExecStartEx classification failed", plan)
        self.assertIn("Caddy ExecReloadEx classification failed", plan)
        self.assertIn("caddy_exec_start_ex=empty_flags", plan)
        self.assertIn("caddy_exec_reload_ex=empty_flags", plan)
        self.assertIn("Caddy lifecycle hook classification failed", plan)
        self.assertIn('if test "$caddy_lifecycle_hooks" != absent; then', plan)
        self.assertNotIn(
            "--property=ExecReload --value | grep -Fq", plan
        )
        self.assertIn("--unit=eryu-caddy-base-validate", plan)
        self.assertIn("caddy_credential_activation=failed", plan)
        self.assertIn("Approved exact post-Eryu DropInPaths record", plan)
        self.assertNotIn("Approved exact LoadCredentialEncrypted record", plan)
        self.assertIn("caddy_expect_bus_property", plan)
        self.assertIn("/usr/bin/busctl --system get-property", plan)
        activation_start = plan.index("caddy_activation_failed() {")
        activation_end = plan.index("```", activation_start)
        activation_section = plan[activation_start:activation_end]
        self.assertIn(
            'test -z "${DBUS_SYSTEM_BUS_ADDRESS+x}"', activation_section
        )
        activation_xtrace_guard = plan.rfind("set +x", 0, activation_start)
        self.assertGreaterEqual(activation_xtrace_guard, 0)
        self.assertLess(
            activation_xtrace_guard,
            plan.index("/usr/bin/busctl --system get-property", activation_start),
        )
        self.assertIn("Environment 'as 0'", plan)
        self.assertIn("EnvironmentFiles 'a(sb) 0'", plan)
        self.assertIn("PassEnvironment 'as 0'", plan)
        self.assertIn("LoadCredential 'a(ss) 0'", plan)
        self.assertIn("ImportCredential 'as 0'", plan)
        self.assertIn("SetCredential 'a(say) 0'", plan)
        self.assertIn("SetCredentialEncrypted 'a(say) 0'", plan)
        self.assertIn(
            'a(ss) 1 "ERYU_BASIC_AUTH_ENTRY" '
            '"/etc/credstore.encrypted/eryu/ERYU_BASIC_AUTH_ENTRY.cred"',
            plan,
        )
        self.assertIn(
            '"$CADDY_CANDIDATE" validate --config /etc/caddy/Caddyfile '
            "--adapter caddyfile || caddy_activation_failed",
            plan,
        )
        self.assertIn(
            "/usr/bin/systemctl restart caddy.service || "
            "caddy_activation_failed",
            plan,
        )
        post_reload_unit_check = plan.index(
            "caddy_effective_need_daemon_reload="
        )
        self.assertLess(daemon_reload, post_reload_unit_check)
        self.assertLess(post_reload_unit_check, credential_restart)
        self.assertIn("caddy_effective_drop_in_paths", plan)
        self.assertIn(
            "caddy_runtime_inputs=one_approved_encrypted_credential", plan
        )
        self.assertIn(
            "for effective_exec_property in ExecStart ExecStartEx "
            "ExecReload ExecReloadEx",
            plan,
        )
        self.assertGreaterEqual(plan.count("ExecStopPost ExecStopPostEx"), 2)
        self.assertIn("caddy:caddy:600", plan)
        self.assertIn(
            'test "$(/usr/bin/sudo /usr/bin/stat -c \'%U:%G\' '
            '/run/caddy)" = caddy:caddy',
            plan,
        )
        self.assertIn("8#$caddy_runtime_mode & 0022", plan)
        self.assertIn("sport = :2019", plan)
        self.assertIn('test "$(command -v ss)" = /usr/bin/ss', plan)
        self.assertIn(
            'admin_port_pipeline_status=("${PIPESTATUS[@]}")', plan
        )
        self.assertIn("admin_port_source_status", plan)
        self.assertIn("caddy_admin_tcp_2019=absent", plan)
        self.assertIn("eryu_template_install=failed", plan)
        self.assertIn('test ! -L "$install_target"', plan)
        self.assertIn(
            "/etc/systemd/system/caddy.service.d/eryu-credentials.conf "
            "|| eryu_template_install_failed",
            plan,
        )
        self.assertIn("eryu_public_cutover=failed", plan)
        self.assertIn("--property=PrivateNetwork=yes", plan)
        self.assertIn("--property=ProtectSystem=strict", plan)
        self.assertIn("--property=StandardOutput=null", plan)
        self.assertIn("--property=StandardError=null", plan)
        self.assertNotIn("systemd-run --quiet --wait --pipe --collect", plan)
        web_health = plan.index(
            "/usr/bin/curl --fail --silent --show-error "
            "http://127.0.0.1:9090/health"
        )
        mcp_start = plan.index(
            "/usr/bin/systemctl enable --now eryu-mcp.service"
        )
        mcp_health = plan.index(
            "/usr/bin/curl --fail --silent --show-error --output /dev/null "
            "http://127.0.0.1:9091/.well-known/oauth-protected-resource"
        )
        self.assertLess(web_health, mcp_start)
        self.assertLess(mcp_start, mcp_health)
        self.assertLess(mcp_health, final_reload)
        self.assertIn(
            "/usr/bin/systemctl reload caddy.service || "
            "eryu_public_cutover_failed",
            plan,
        )
        self.assertIn(
            "/etc/credstore.encrypted/eryu/MUSIC_U.cred \\",
            plan,
        )
        self.assertIn('test ! -e "$credential_target"', plan)
        self.assertIn('test ! -L "$credential_target"', plan)
        self.assertIn(
            "for credential_directory in /etc/credstore.encrypted "
            "/etc/credstore.encrypted/eryu",
            plan,
        )
        self.assertIn(
            "stat -c '%U:%G:%a' \"$credential_directory\")\" = "
            "root:root:700",
            plan,
        )

    def test_caddy_candidate_is_validated_before_atomic_live_replacement(self) -> None:
        plan = self.read("README.md")
        validate_candidate = plan.index(
            f"{CADDY_CANDIDATE} validate --config "
            "/etc/caddy/.Caddyfile.eryu-candidate"
        )
        copy_candidate = plan.index(
            "/usr/bin/sudo /usr/bin/cp --preserve=mode,ownership,timestamps "
            "/var/backups/eryu-deploy/Caddyfile.expected-eryu "
            "/etc/caddy/.Caddyfile.eryu-candidate"
        )
        atomic_replace = plan.index(
            "/usr/bin/sudo /usr/bin/mv -T /etc/caddy/.Caddyfile.eryu-candidate "
            "/etc/caddy/Caddyfile"
        )
        self.assertLess(copy_candidate, validate_candidate)
        self.assertLess(validate_candidate, atomic_replace)
        approved_root_check = plan.index(
            "/var/backups/caddy-v2114-cutover/Caddyfile.post-v2114-approved "
            "/etc/caddy/Caddyfile || caddy_route_install_failed"
        )
        approved_root_copy = plan.index(
            "/var/backups/caddy-v2114-cutover/Caddyfile.post-v2114-approved "
            "/var/backups/eryu-deploy/Caddyfile.pre-eryu"
        )
        self.assertLess(approved_root_check, approved_root_copy)
        self.assertLess(approved_root_copy, copy_candidate)
        self.assertNotIn(
            "/usr/bin/cp --preserve=mode,ownership,timestamps "
            "/etc/caddy/Caddyfile /var/backups/eryu-deploy/Caddyfile.pre-eryu",
            plan,
        )
        self.assertIn("eryu_caddy_route_install=failed", plan)
        self.assertIn(
            f"{CADDY_CANDIDATE} validate --config "
            "/etc/caddy/.Caddyfile.eryu-candidate --adapter caddyfile "
            "|| caddy_route_install_failed",
            plan,
        )
        live_unchanged = plan.index(
            "/usr/bin/sudo /usr/bin/cmp --silent "
            "/var/backups/eryu-deploy/Caddyfile.pre-eryu "
            "/etc/caddy/Caddyfile || caddy_route_install_failed"
        )
        self.assertLess(validate_candidate, live_unchanged)
        self.assertLess(live_unchanged, atomic_replace)
        self.assertIn(
            "/usr/bin/sudo /usr/bin/test ! -L /etc/caddy/Caddyfile", plan
        )
        self.assertIn(
            "/usr/bin/sudo /usr/bin/test ! -L "
            "/etc/caddy/.Caddyfile.eryu-candidate",
            plan,
        )
        self.assertIn(
            "/usr/bin/sudo /usr/bin/test ! -L /etc/caddy/eryu.caddy", plan
        )
        self.assertIn(
            "test \"$((8#$caddy_dir_mode & 0022))\" -eq 0", plan
        )
        self.assertNotIn("tee -a /etc/caddy/Caddyfile", plan)

    def test_caddy_route_rollback_is_validated_before_atomic_replacement(self) -> None:
        plan = self.read("README.md")
        rollback_section = plan.split("caddy_route_rollback_failed() {", 1)[1].split(
            "```", 1
        )[0]
        rollback_validate = plan.index("--unit=eryu-caddy-rollback-validate")
        rollback_replace = plan.index(
            "/usr/bin/sudo /usr/bin/mv -T /etc/caddy/.Caddyfile.eryu-rollback "
            "/etc/caddy/Caddyfile"
        )
        rollback_delete_fragment = plan.index(
            "/usr/bin/sudo /usr/bin/rm -f /etc/caddy/eryu.caddy"
        )
        self.assertLess(rollback_validate, rollback_replace)
        self.assertLess(rollback_replace, rollback_delete_fragment)
        self.assertIn("eryu_caddy_route_rollback=failed", plan)
        self.assertIn(
            f"{CADDY_CANDIDATE} validate --config "
            "/etc/caddy/.Caddyfile.eryu-rollback --adapter caddyfile "
            "|| caddy_route_rollback_failed",
            plan,
        )
        rollback_live_approved = plan.index(
            "/usr/bin/sudo /usr/bin/cmp --silent "
            "/var/backups/eryu-deploy/Caddyfile.expected-eryu "
            "/etc/caddy/Caddyfile || caddy_route_rollback_failed"
        )
        self.assertLess(rollback_validate, rollback_live_approved)
        self.assertLess(rollback_live_approved, rollback_replace)
        approved_root_binding = (
            "/var/backups/caddy-v2114-cutover/Caddyfile.post-v2114-approved "
            "/var/backups/eryu-deploy/Caddyfile.pre-eryu"
        )
        self.assertEqual(rollback_section.count(approved_root_binding), 2)
        self.assertLess(
            rollback_section.index(approved_root_binding),
            rollback_section.index(
                "/usr/bin/cp --preserve=mode,ownership,timestamps "
                "/var/backups/eryu-deploy/Caddyfile.pre-eryu"
            ),
        )
        self.assertLess(
            rollback_section.rindex(approved_root_binding),
            rollback_section.index(
                "/usr/bin/mv -T /etc/caddy/.Caddyfile.eryu-rollback"
            ),
        )
        self.assertIn("root:root:700", rollback_section)
        self.assertIn("root:root:644:1", rollback_section)
        self.assertIn(
            "/usr/bin/sudo /usr/bin/test ! -L "
            "/etc/caddy/.Caddyfile.eryu-rollback",
            plan,
        )
        self.assertGreaterEqual(
            plan.count(
                'test "$(/usr/bin/sudo /usr/bin/stat -c \'%U:%G\' '
                '/etc/caddy)" '
                "= root:root"
            ),
            2,
        )

    def test_caddy_candidate_and_conditional_rollback_contract_are_pinned(self) -> None:
        plan = self.read("README.md")
        upgrade_plan = self.read("CADDY-UPGRADE.md")
        helper = self.read("create-caddy-basic-auth-credential.sh")
        candidate_drop_in = self.read("systemd/caddy-v2114-candidate.conf")

        for document in (plan, upgrade_plan, helper, candidate_drop_in):
            self.assertIn(CADDY_CANDIDATE, document)
        for document in (plan, upgrade_plan, helper):
            self.assertIn(CADDY_CANDIDATE_SHA256, document)
        for document in (plan, upgrade_plan):
            self.assertNotIn("/usr/bin/caddy version", document)
            self.assertNotIn("/usr/bin/caddy build-info", document)
            self.assertNotIn("/usr/bin/caddy list-modules", document)
        self.assertEqual(
            hashlib.sha256((DEPLOY / "caddy/eryu.caddy").read_bytes()).hexdigest(),
            ERYU_CADDY_SHA256,
        )

        activation_section = plan.split("caddy_activation_failed() {", 1)[1].split(
            "```", 1
        )[0]
        route_install_section = plan.split("caddy_route_install_failed() {", 1)[1].split(
            "```", 1
        )[0]
        route_rollback_section = plan.split("caddy_route_rollback_failed() {", 1)[1].split(
            "```", 1
        )[0]
        self.assertNotIn("/usr/bin/caddy", activation_section)
        self.assertNotIn("/usr/bin/caddy", route_install_section)
        self.assertNotIn("/usr/bin/caddy", route_rollback_section)

        rollback_contract = upgrade_plan.split(
            "## 8. 一次性条件式回滚合同", 1
        )[1]
        self.assertIn("caddy_inactive", rollback_contract)
        self.assertIn("shared_diary_baseline_failed_3x", rollback_contract)
        self.assertIn("连续三轮", rollback_contract)
        self.assertIn("任一轮通过就清零并取消回滚", rollback_contract)
        self.assertIn("重新指向 `/usr/bin/caddy`", rollback_contract)
        self.assertIn("最多一次", rollback_contract)
        self.assertIn("unknown", rollback_contract)
        self.assertIn("不得覆盖", upgrade_plan)
        self.assertIn("## 9. 下次维护审批前必须实例化的现场锚点", upgrade_plan)
        self.assertIn("manifest/snapshot 本身等于", upgrade_plan)
        self.assertIn("type/owner/group/mode/nlink 批准记录", upgrade_plan)
        self.assertIn("第 9 节的两个现场锚点", plan)
        self.assertEqual(
            re.findall(r"`trigger=([a-z0-9_]+)`", rollback_contract),
            ["caddy_inactive", "shared_diary_baseline_failed_3x"],
        )

        public_section = plan.split("eryu_public_cutover_failed() {", 1)[1].split(
            "```", 1
        )[0]
        public_reload = public_section.index(
            "/usr/bin/systemctl reload caddy.service"
        )
        self.assertLess(public_section.index(CADDY_CANDIDATE_SHA256), public_reload)
        self.assertLess(public_section.index("caddy_public_reload_property"), public_reload)
        self.assertLess(
            public_section.rindex("sha256sum --check --status"), public_reload
        )
        self.assertLess(
            public_section.rindex("Caddyfile.expected-eryu"), public_reload
        )
        self.assertIn("caddy-v2114-candidate.conf", public_section)
        self.assertIn("eryu-credentials.conf", public_section)
        self.assertIn("systemd-files-post-eryu.sha256", public_section)
        self.assertIn("caddy_public_expect_bus_property Environment 'as 0'", public_section)
        self.assertIn(
            "caddy_public_expect_bus_property LoadCredentialEncrypted 'a(ss) 1",
            public_section,
        )
        self.assertIn("ExecStart ExecStartEx ExecReload ExecReloadEx", public_section)
        self.assertIn("/usr/sbin/getcap", public_section)
        self.assertIn('/proc/$caddy_public_main_pid/exe', public_section)
        self.assertLess(public_section.index("systemd-files-post-eryu.sha256"), public_reload)
        self.assertLess(public_section.index("caddy_public_expect_bus_property"), public_reload)
        self.assertLess(public_section.rindex("forward_auth"), public_reload)

        self.assertLess(
            route_rollback_section.index("caddy_rollback_reload_property"),
            route_rollback_section.index("/usr/bin/systemctl reload caddy.service"),
        )
        self.assertLess(
            route_rollback_section.index("sha256sum --check --status"),
            route_rollback_section.index("/usr/bin/systemctl reload caddy.service"),
        )
        self.assertIn("caddy-v2114-candidate.conf", route_rollback_section)
        self.assertIn("eryu-credentials.conf", route_rollback_section)
        self.assertIn("set +x\ncaddy_route_rollback_failed", plan)
        self.assertIn("DBUS_SYSTEM_BUS_ADDRESS", route_rollback_section)
        self.assertIn("caddy_rollback_expect_bus_property Environment 'as 0'", route_rollback_section)
        self.assertIn(
            "caddy_rollback_expect_bus_property LoadCredentialEncrypted 'a(ss) 1",
            route_rollback_section,
        )
        self.assertIn("ExecStart ExecStartEx ExecReload ExecReloadEx", route_rollback_section)
        self.assertIn("/usr/sbin/getcap", route_rollback_section)
        self.assertIn('/proc/$caddy_route_rollback_main_pid/exe', route_rollback_section)
        self.assertLess(route_rollback_section.rindex("forward_auth"), route_rollback_section.index("/usr/bin/systemctl reload caddy.service"))
        self.assertIn('/proc/$caddy_main_pid/exe', activation_section)
        self.assertIn("/usr/sbin/getcap", activation_section)
        self.assertIn(ERYU_CADDY_SHA256, plan)
        self.assertNotRegex(
            self.read("caddy/eryu.caddy"), r"(?m)^\s*forward_auth\b"
        )

    def test_bash_pipeline_status_is_snapshotted_before_indexing(self) -> None:
        if os.name == "nt":
            program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            bash = program_files / "Git/bin/bash.exe"
            bash_path = str(bash) if bash.is_file() else None
        else:
            bash_path = shutil.which("bash")
        if not bash_path:
            self.skipTest("Bash is unavailable")

        script = r'''
printf '%s\n' safe | grep -Ei 'TOKEN=' > /dev/null
pipeline_status=("${PIPESTATUS[@]}")
source_status=${pipeline_status[0]}
match_status=${pipeline_status[1]}
test "$source_status" -eq 0
test "$match_status" -eq 1

printf '%s\n' 'TOKEN=sentinel' | grep -Ei 'TOKEN=' > /dev/null
pipeline_status=("${PIPESTATUS[@]}")
source_status=${pipeline_status[0]}
match_status=${pipeline_status[1]}
test "$source_status" -eq 0
test "$match_status" -eq 0
'''
        completed = subprocess.run(
            [bash_path, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_documented_bash_blocks_are_syntactically_valid(self) -> None:
        if os.name == "nt":
            program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            bash = program_files / "Git/bin/bash.exe"
            bash_path = str(bash) if bash.is_file() else None
        else:
            bash_path = shutil.which("bash")
        if not bash_path:
            self.skipTest("Bash is unavailable")

        fence = re.compile(
            r"(?ms)^[ \t]*```bash[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*$"
        )
        for relative_path in ("README.md", "CADDY-UPGRADE.md"):
            blocks = fence.findall(self.read(relative_path))
            self.assertTrue(blocks, relative_path)
            for index, block in enumerate(blocks, 1):
                with self.subTest(path=relative_path, block=index):
                    completed = subprocess.run(
                        [bash_path, "-n"],
                        input=block,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        f"{relative_path} block {index}: {completed.stderr}",
                    )

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
        self.assertIn("/usr/bin/sudo /usr/bin/cmp --silent", plan)
        self.assertNotIn("/usr/bin/sudo /usr/bin/diff", plan)
        self.assertNotIn("sudoedit /etc/caddy/Caddyfile", plan)
        self.assertIn("不表示网络隔离", plan)
        self.assertIn("ERYU_BASIC_AUTH_ENTRY", plan)
        self.assertIn("/health", plan)
        self.assertIn("basic_auth", plan)
        self.assertIn("CADDY-UPGRADE.md", plan)
        self.assertNotIn("数字 MP3 边界", plan)


if __name__ == "__main__":
    unittest.main()
