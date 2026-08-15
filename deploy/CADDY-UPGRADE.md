# Caddy 安全升级与 Shared Diary 兼容/回滚计划（线上切换尚未执行）

## 1. 状态与硬边界

本文件只是一份后续变更方案。2026-08-15 已完成候选资产的官方签名、Rekor、
checksum、版本、标准模块与隔离 validation；候选只安装在版本化路径，线上
`/usr/bin/caddy`、systemd、Caddyfile 和运行进程均未切换，也没有 restart 或
reload。

本次批准的唯一候选合同是：

- 路径：`/opt/caddy-candidates/v2.11.4/caddy`
- 官方资产：`caddy_2.11.4_linux_amd64.tar.gz`，SHA-256
  `527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9`
- Sigstore certificate identity：
  `https://github.com/caddyserver/caddy/.github/workflows/release.yml@refs/tags/v2.11.4`
  ；OIDC issuer：`https://token.actions.githubusercontent.com`
- ELF SHA-256：
  `b7105518e3ed1c0761f232e44fc09345535533c9cb0abf0e12809416c7ac64d9`
- 版本：`v2.11.4`，标准模块构建，非标准模块为 0，ELF file capabilities 为空
- 当前 Shared Diary 与计划加入 Eryu 后的完整配置均由该候选隔离 validate
- 两份配置的 `forward_auth` 计数均为 0

`/usr/bin/caddy` 仍是旧 v2.6.2。它作为可执行程序的唯一用途是完整二进制回滚；
升级前只能读取其文件与 package 元数据作基线取证，不运行旧入口的版本、模块或
validation 子命令。候选流程不得覆盖、链接或复制到该路径。

此前只读阶段已经分类旧二进制、模块、systemd、file-backed 配置与内存配置；
这些证据会随线上状态漂移，不能直接授权未来切换。当前本地
`shared-diary-mcp/deploy/Caddyfile` 也不能替代线上现状。维护窗口必须在任何
写入前重新核对：

- 旧 `/usr/bin/caddy` 的路径、SHA-256、安装来源与 v2.6.2 回滚可用性；
- 全部标准/第三方模块；
- systemd 的真实 `FragmentPath`、drop-ins、`ExecStart`、`ExecReload`、用户和组；
- Caddy 二进制与 `/usr/bin/git`、`/usr/bin/awk` 是否为 root-owned、不可由非 root
  写入，并分别属于哪个已验证 package；
- 线上根 `Caddyfile`、全部 import、全局 options、Shared Diary 实际路由；
- 当前与计划完整配置的 `forward_auth` 计数仍为 0，并且没有新出现的下划线
  header、WebSocket 或模块依赖；
- 是否存在只经 Admin API 生效、尚未回写 file-backed Caddyfile 的动态配置；
  如果变更记录不能证明不存在，restart 前必须停止。

Caddy 2.6.2 不允许直接承载 Eryu。不得安装本仓库的 Eryu Caddy fragment 或
credential drop-in，直到本计划的升级门禁完成。

## 2. 候选版本策略

截至 2026-08-14，官方最新正式版是
[v2.11.4](https://github.com/caddyserver/caddy/releases/tag/v2.11.4)。但是官方
[GHSA-6365-7ppr-5r92](https://github.com/caddyserver/caddy/security/advisories/GHSA-6365-7ppr-5r92)
把 `< v2.11.5` 标为受 `forward_auth + reverse_proxy` 连接复用问题影响；本次
检查时 v2.11.5 尚未正式发布。

本次 v2.11.4 例外必须遵守：

1. v2.11.4 仍是受 GHSA 影响的版本，不得描述为已修复；本次仅因配置无
   `forward_auth` 而接受窄范围例外。
2. 切换当天重新读取官方安全公告，并重新计算上述固定候选路径的 SHA-256；
   禁止使用浮动 `latest`、PATH 或 symlink。
3. 如果线上任一配置含 `forward_auth`，禁止使用 v2.11.4。
4. 已完成的 staging validation 不能替代切换窗口的 Shared Diary 基线复检；
   若配置/import 或候选摘要发生任何变化，例外立即失效。
5. 发现第三方模块时，候选二进制必须包含完全相同的必要模块集合；标准 apt 包
   不能直接覆盖自定义构建。

官方资料：

- [Debian/Ubuntu 官方安装来源](https://caddyserver.com/docs/install)
- [systemd 运行方式](https://caddyserver.com/docs/running)
- [`caddy list-modules`](https://caddyserver.com/docs/command-line#caddy-list-modules)
- [`caddy validate`](https://caddyserver.com/docs/command-line#caddy-validate)
- [`basic_auth`](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
- [Admin API Unix socket](https://caddyserver.com/docs/caddyfile/options#admin)
- [`persist_config off`](https://caddyserver.com/docs/caddyfile/options#persist-config)

`caddy upgrade` 仍被官方标为 Experimental，并会取得“最新”版本，不满足固定
版本要求；本项目禁止使用它完成生产切换。

## 3. 下一次 VPS 只读盘点（必须先单独批准）

在 VPS SSH 终端逐条运行。预期只输出公开的版本、包、模块名和 unit 结构；
不要输出完整 Caddyfile、环境变量值、Basic Auth 条目、TLS 私钥或 Admin API
`/config`。以下命令只允许在 Bash 中运行，且不得启用 `set -x`。

```bash
# 先关闭可能从调用者继承的 xtrace；后续不得在本代码块中重新开启。
set +x
readonly_gate_failed() {
  printf '%s\n' 'caddy_readonly_gate=failed' >&2
  exit 1
}
test -z "${DBUS_SYSTEM_BUS_ADDRESS+x}" || readonly_gate_failed
test "$(command -v caddy)" = /usr/bin/caddy || readonly_gate_failed
test "$(command -v git)" = /usr/bin/git || readonly_gate_failed
test "$(command -v awk)" = /usr/bin/awk || readonly_gate_failed
test "$(command -v systemd-creds)" = /usr/bin/systemd-creds || readonly_gate_failed
test "$(command -v systemd-run)" = /usr/bin/systemd-run || readonly_gate_failed
test "$(command -v systemctl)" = /usr/bin/systemctl || readonly_gate_failed
test "$(command -v systemd)" = /usr/bin/systemd || readonly_gate_failed
test "$(command -v busctl)" = /usr/bin/busctl || readonly_gate_failed
test "$(command -v sudo)" = /usr/bin/sudo || readonly_gate_failed
test "$(command -v journalctl)" = /usr/bin/journalctl || readonly_gate_failed
test "$(command -v grep)" = /usr/bin/grep || readonly_gate_failed
test "$(command -v readlink)" = /usr/bin/readlink || readonly_gate_failed
test "$(command -v stat)" = /usr/bin/stat || readonly_gate_failed
test "$(command -v sha256sum)" = /usr/bin/sha256sum || readonly_gate_failed
test "$(command -v dpkg-query)" = /usr/bin/dpkg-query || readonly_gate_failed
test "$(command -v dpkg)" = /usr/bin/dpkg || readonly_gate_failed
test "$(command -v apt-cache)" = /usr/bin/apt-cache || readonly_gate_failed
test "$(command -v dirname)" = /usr/bin/dirname || readonly_gate_failed

for trusted_binary in /usr/bin/caddy /usr/bin/git /usr/bin/awk /usr/bin/busctl /usr/bin/sudo; do
  resolved_trusted_binary="$(/usr/bin/readlink -f "$trusted_binary")" || readonly_gate_failed
  test -n "$resolved_trusted_binary" || readonly_gate_failed
  test -f "$resolved_trusted_binary" || readonly_gate_failed
  test "$(/usr/bin/stat -c '%U:%G' "$resolved_trusted_binary")" = root:root || readonly_gate_failed
  trusted_binary_mode="$(/usr/bin/stat -c '%a' "$resolved_trusted_binary")" || readonly_gate_failed
  [[ "$trusted_binary_mode" =~ ^[0-7]{3,4}$ ]] || readonly_gate_failed
  test "$((8#$trusted_binary_mode & 0022))" -eq 0 || readonly_gate_failed
  package_record="$(/usr/bin/dpkg-query -S "$resolved_trusted_binary")" || readonly_gate_failed
  test -n "$package_record" || readonly_gate_failed
  package_name=${package_record%%:*}
  test -n "$package_name" || readonly_gate_failed
  package_status="$(
    /usr/bin/dpkg-query -W -f='${db:Status-Status}' "$package_name"
  )" || readonly_gate_failed
  test "$package_status" = installed || readonly_gate_failed
  trusted_binary_sha256_record="$(
    /usr/bin/sha256sum "$resolved_trusted_binary"
  )" || readonly_gate_failed
  trusted_binary_sha256=${trusted_binary_sha256_record%% *}
  [[ "$trusted_binary_sha256" =~ ^[0-9a-f]{64}$ ]] || readonly_gate_failed
  printf 'trusted_binary=%s package=%s sha256=%s\n' \
    "$trusted_binary" "$package_name" "$trusted_binary_sha256"
  unset resolved_trusted_binary trusted_binary_mode package_record package_name
  unset package_status trusted_binary_sha256_record trusted_binary_sha256
done

# 不执行旧入口做版本探测；所有候选版本/模块检查均使用固定 /opt 路径。
/usr/bin/dpkg-query -W -f='${Package}\t${Version}\t${Status}\n' caddy || readonly_gate_failed
/usr/bin/apt-cache policy caddy || readonly_gate_failed

systemd_version_record="$(/usr/bin/systemd --version)" || readonly_gate_failed
[[ "$systemd_version_record" =~ ^systemd[[:space:]]+([0-9]+) ]] || readonly_gate_failed
systemd_major=${BASH_REMATCH[1]}
test "$systemd_major" -ge 254 || readonly_gate_failed

if ! caddy_unit_user="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=User --value 2> /dev/null
)"; then
  readonly_gate_failed
fi
if ! caddy_unit_group="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=Group --value 2> /dev/null
)"; then
  readonly_gate_failed
fi
if test "$caddy_unit_user" != caddy || test "$caddy_unit_group" != caddy; then
  printf '%s\n' 'caddy_unit_identity=unapproved' >&2
  exit 1
fi
printf '%s\n' 'caddy_unit_identity=caddy:caddy'
unset caddy_unit_user caddy_unit_group

if ! caddy_need_daemon_reload="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service \
    --property=NeedDaemonReload --value 2> /dev/null
)"; then
  readonly_gate_failed
fi
test "$caddy_need_daemon_reload" = no || readonly_gate_failed
printf '%s\n' 'caddy_need_daemon_reload=no'
unset caddy_need_daemon_reload

if ! caddy_fragment_path="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service \
    --property=FragmentPath --value 2> /dev/null
)"; then
  readonly_gate_failed
fi
case "$caddy_fragment_path" in
  /lib/systemd/system/caddy.service|/usr/lib/systemd/system/caddy.service) ;;
  *)
    printf '%s\n' 'caddy_fragment_path=unapproved' >&2
    exit 1
    ;;
esac
resolved_caddy_fragment="$(/usr/bin/readlink -f "$caddy_fragment_path")" || readonly_gate_failed
test -f "$resolved_caddy_fragment" || readonly_gate_failed
test ! -L "$caddy_fragment_path" || readonly_gate_failed
test ! -L "$resolved_caddy_fragment" || readonly_gate_failed
test "$(/usr/bin/stat -c '%U:%G' "$resolved_caddy_fragment")" = root:root || readonly_gate_failed
caddy_fragment_mode="$(/usr/bin/stat -c '%a' "$resolved_caddy_fragment")" || readonly_gate_failed
[[ "$caddy_fragment_mode" =~ ^[0-7]{3,4}$ ]] || readonly_gate_failed
test "$((8#$caddy_fragment_mode & 0022))" -eq 0 || readonly_gate_failed
caddy_fragment_parent="$(/usr/bin/dirname "$resolved_caddy_fragment")" || readonly_gate_failed
test "$(/usr/bin/stat -c '%U:%G' "$caddy_fragment_parent")" = root:root || readonly_gate_failed
caddy_fragment_parent_mode="$(/usr/bin/stat -c '%a' "$caddy_fragment_parent")" || readonly_gate_failed
[[ "$caddy_fragment_parent_mode" =~ ^[0-7]{3,4}$ ]] || readonly_gate_failed
test "$((8#$caddy_fragment_parent_mode & 0022))" -eq 0 || readonly_gate_failed
caddy_fragment_package_record="$(
  /usr/bin/dpkg-query -S "$caddy_fragment_path"
)" || readonly_gate_failed
test "${caddy_fragment_package_record%%:*}" = caddy || readonly_gate_failed
caddy_fragment_sha_record="$(/usr/bin/sha256sum "$resolved_caddy_fragment")" || readonly_gate_failed
caddy_fragment_sha256=${caddy_fragment_sha_record%% *}
[[ "$caddy_fragment_sha256" =~ ^[0-9a-f]{64}$ ]] || readonly_gate_failed
printf 'caddy_unit_fragment=approved sha256=%s\n' "$caddy_fragment_sha256"
unset caddy_fragment_path resolved_caddy_fragment caddy_fragment_mode
unset caddy_fragment_parent caddy_fragment_parent_mode
unset caddy_fragment_package_record caddy_fragment_sha_record caddy_fragment_sha256

if ! caddy_drop_in_paths="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service \
    --property=DropInPaths --value 2> /dev/null
)"; then
  readonly_gate_failed
fi
if test -n "$caddy_drop_in_paths"; then
  printf '%s\n' 'caddy_existing_drop_ins=present' >&2
  exit 1
fi
printf '%s\n' 'caddy_existing_drop_ins=absent'
unset caddy_drop_in_paths

if ! caddy_runtime_directory="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service \
    --property=RuntimeDirectory --value 2> /dev/null
)"; then
  readonly_gate_failed
fi
if ! caddy_runtime_directory_mode="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service \
    --property=RuntimeDirectoryMode --value 2> /dev/null
)"; then
  readonly_gate_failed
fi
if test -n "$caddy_runtime_directory"; then
  printf '%s\n' 'caddy_existing_runtime_directory=present' >&2
  exit 1
fi
case "$caddy_runtime_directory_mode" in
  ''|0755) ;;
  *)
    printf '%s\n' 'caddy_runtime_directory_mode=unapproved' >&2
    exit 1
    ;;
esac
printf '%s\n' 'caddy_existing_runtime_directory=absent'
unset caddy_runtime_directory caddy_runtime_directory_mode

/usr/bin/sudo /usr/bin/systemctl is-active caddy.service || readonly_gate_failed
/usr/bin/sudo /usr/bin/systemctl is-enabled caddy.service || readonly_gate_failed

# 原始记录只短暂存在当前 shell 内存；只输出固定分类，不显示任何 argv。
if ! caddy_exec_start_record="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecStart --value 2> /dev/null
)"; then
  printf '%s\n' 'caddy_exec_start=classification_failed' >&2
  exit 1
fi
exec_start_remainder=${caddy_exec_start_record#* path=}
if test "$exec_start_remainder" = "$caddy_exec_start_record" ||
  [[ "$exec_start_remainder" == *" path="* ]]; then
  printf '%s\n' 'caddy_exec_start=not_one_direct_record' >&2
  exit 1
fi
case "$caddy_exec_start_record" in
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile ; ignore_errors=no ; "*)
    printf '%s\n' 'caddy_exec_start=approved_file_backed_form'
    printf '%s\n' 'caddy_exec_resume=absent'
    printf '%s\n' 'caddy_exec_environ=present'
    ;;
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy run --config /etc/caddy/Caddyfile ; ignore_errors=no ; "*)
    printf '%s\n' 'caddy_exec_start=approved_file_backed_form'
    printf '%s\n' 'caddy_exec_resume=absent'
    printf '%s\n' 'caddy_exec_environ=absent'
    ;;
  *)
    printf '%s\n' 'caddy_exec_start=unapproved_form' >&2
    exit 1
    ;;
esac
unset caddy_exec_start_record exec_start_remainder

if ! caddy_exec_start_ex_record="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecStartEx --value 2> /dev/null
)"; then
  printf '%s\n' 'caddy_exec_start_ex=classification_failed' >&2
  exit 1
fi
exec_start_ex_remainder=${caddy_exec_start_ex_record#* path=}
if test "$exec_start_ex_remainder" = "$caddy_exec_start_ex_record" ||
  [[ "$exec_start_ex_remainder" == *" path="* ]]; then
  printf '%s\n' 'caddy_exec_start_ex=not_one_direct_record' >&2
  exit 1
fi
case "$caddy_exec_start_ex_record" in
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile ; flags= ; "*|\
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy run --config /etc/caddy/Caddyfile ; flags= ; "*)
    printf '%s\n' 'caddy_exec_start_ex=empty_flags'
    ;;
  *)
    printf '%s\n' 'caddy_exec_start_ex=unapproved_flags_or_form' >&2
    exit 1
    ;;
esac
unset caddy_exec_start_ex_record exec_start_ex_remainder

if ! caddy_exec_reload_record="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecReload --value 2> /dev/null
)"; then
  printf '%s\n' 'caddy_exec_reload=classification_failed' >&2
  exit 1
fi
exec_reload_remainder=${caddy_exec_reload_record#* path=}
if test "$exec_reload_remainder" = "$caddy_exec_reload_record" ||
  [[ "$exec_reload_remainder" == *" path="* ]]; then
  printf '%s\n' 'caddy_exec_reload=not_one_direct_record' >&2
  exit 1
fi
case "$caddy_exec_reload_record" in
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force --address unix//run/caddy/admin.sock ; ignore_errors=no ; "*)
    printf '%s\n' 'caddy_exec_reload=approved_unix_socket_form'
    ;;
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force ; ignore_errors=no ; "*)
    printf '%s\n' 'caddy_exec_reload=approved_pre_upgrade_form'
    ;;
  *)
    printf '%s\n' 'caddy_exec_reload=unapproved_form' >&2
    exit 1
    ;;
esac
unset caddy_exec_reload_record exec_reload_remainder

if ! caddy_exec_reload_ex_record="$(
  /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecReloadEx --value 2> /dev/null
)"; then
  printf '%s\n' 'caddy_exec_reload_ex=classification_failed' >&2
  exit 1
fi
exec_reload_ex_remainder=${caddy_exec_reload_ex_record#* path=}
if test "$exec_reload_ex_remainder" = "$caddy_exec_reload_ex_record" ||
  [[ "$exec_reload_ex_remainder" == *" path="* ]]; then
  printf '%s\n' 'caddy_exec_reload_ex=not_one_direct_record' >&2
  exit 1
fi
case "$caddy_exec_reload_ex_record" in
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force --address unix//run/caddy/admin.sock ; flags= ; "*|\
  "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force ; flags= ; "*)
    printf '%s\n' 'caddy_exec_reload_ex=empty_flags'
    ;;
  *)
    printf '%s\n' 'caddy_exec_reload_ex=unapproved_flags_or_form' >&2
    exit 1
    ;;
esac
unset caddy_exec_reload_ex_record exec_reload_ex_remainder

# 现有 service 若依赖任何环境/credential 输入，通用 staging 无权猜测如何复现。
# busctl 保留 D-Bus 类型与数组项数；这里必须逐字匹配空数组，不能把
# systemctl 的不可打印占位符或属性读取失败误判成“为空”。
caddy_expect_empty_bus_property() {
  local property_name=$1
  local expected_record=$2
  local effective_record
  effective_record="$(
    /usr/bin/busctl --system get-property \
      org.freedesktop.systemd1 \
      /org/freedesktop/systemd1/unit/caddy_2eservice \
      org.freedesktop.systemd1.Service \
      "$property_name" 2> /dev/null
  )" || {
    printf '%s\n' 'caddy_runtime_inputs=classification_failed' >&2
    exit 1
  }
  test "$effective_record" = "$expected_record" || {
    printf '%s\n' 'caddy_runtime_inputs=present' >&2
    exit 1
  }
  unset effective_record
}
caddy_expect_empty_bus_property Environment 'as 0'
caddy_expect_empty_bus_property EnvironmentFiles 'a(sb) 0'
caddy_expect_empty_bus_property PassEnvironment 'as 0'
caddy_expect_empty_bus_property LoadCredential 'a(ss) 0'
caddy_expect_empty_bus_property LoadCredentialEncrypted 'a(ss) 0'
caddy_expect_empty_bus_property ImportCredential 'as 0'
caddy_expect_empty_bus_property SetCredential 'a(say) 0'
caddy_expect_empty_bus_property SetCredentialEncrypted 'a(say) 0'
printf '%s\n' 'caddy_runtime_inputs=absent'
unset -f caddy_expect_empty_bus_property

# 通用 staging 不复现额外生命周期命令；任一钩子存在都必须另行审查。
caddy_lifecycle_hooks=absent
for hook_property in \
  ExecCondition ExecConditionEx ExecStartPre ExecStartPreEx \
  ExecStartPost ExecStartPostEx ExecStop ExecStopEx \
  ExecStopPost ExecStopPostEx; do
  if ! hook_property_value="$(
    /usr/bin/sudo /usr/bin/systemctl show caddy.service \
      --property="$hook_property" --value 2> /dev/null
  )"; then
    printf '%s\n' 'caddy_lifecycle_hooks=classification_failed' >&2
    exit 1
  fi
  if test -n "$hook_property_value"; then
    caddy_lifecycle_hooks=present
  fi
  unset hook_property_value
done
printf 'caddy_lifecycle_hooks=%s\n' "$caddy_lifecycle_hooks"
if test "$caddy_lifecycle_hooks" != absent; then
  exit 1
fi

# --environ 的历史泄露审计也只输出固定结果；匹配行和值永远不显示。
secret_name_pattern='(^|[^A-Za-z0-9_])(MUSIC_U|ERYU_AUTH_TOKEN|ERYU_MCP_READ_TOKEN|ERYU_BASIC_AUTH_ENTRY)=|(^|[^A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|PRIVATE_KEY|KEY|AUTHORIZATION|CREDENTIAL|BEARER)[A-Za-z0-9_]*='
/usr/bin/sudo /usr/bin/systemctl show-environment |
  /usr/bin/grep -Ei "$secret_name_pattern" > /dev/null
manager_pipeline_status=("${PIPESTATUS[@]}")
manager_source_status=${manager_pipeline_status[0]}
manager_match_status=${manager_pipeline_status[1]}
test "$manager_source_status" -eq 0 || {
  printf '%s\n' 'caddy_manager_environment_scan=failed' >&2
  exit 1
}
if test "$manager_match_status" -eq 0; then
  printf '%s\n' 'caddy_manager_environment_secret_name=present' >&2
  exit 1
elif test "$manager_match_status" -eq 1; then
  printf '%s\n' 'caddy_manager_environment_secret_name=absent'
else
  printf '%s\n' 'caddy_manager_environment_scan=failed' >&2
  exit 1
fi

/usr/bin/sudo /usr/bin/journalctl --unit=caddy.service --no-pager --output=cat |
  /usr/bin/grep -Ei "$secret_name_pattern" > /dev/null
journal_pipeline_status=("${PIPESTATUS[@]}")
journal_source_status=${journal_pipeline_status[0]}
journal_match_status=${journal_pipeline_status[1]}
test "$journal_source_status" -eq 0 || {
  printf '%s\n' 'caddy_journal_secret_name_scan=failed' >&2
  exit 1
}
if test "$journal_match_status" -eq 0; then
  printf '%s\n' 'caddy_journal_secret_name_hit=present' >&2
  exit 1
elif test "$journal_match_status" -eq 1; then
  printf '%s\n' 'caddy_journal_secret_name_hit=absent'
else
  printf '%s\n' 'caddy_journal_secret_name_scan=failed' >&2
  exit 1
fi
unset systemd_version_record systemd_major
unset -f readonly_gate_failed
```

结果判定：

- `dpkg-query -S` 失败：按静态/自定义二进制处理，停止 apt 升级方案。
- 模块列表出现非标准模块：先逐项确认 Shared Diary 是否依赖；候选缺任一必要
  模块就停止。
- `ExecStart` 不是 file-backed `caddy run --config /etc/caddy/Caddyfile`，或含
  `--resume`：停止。本方案不适用于 `caddy-api.service`。
- `ExecStart` 含 `--environ`：升级候选 unit/drop-in 必须只移除该参数，避免
  restart 时把 Caddy service 环境写入 journal；在移除并验证前不 restart。
- `caddy_runtime_inputs=present`：停止通用方案。必须另行审查这些输入的来源、
  权限与必要性，并设计一个不显示值、但精确复现必要安全输入的 transient
  validator；不得把环境文件或 credential 路径/内容复制进聊天或普通日志。
- `caddy_lifecycle_hooks=present`：停止通用方案。必须逐条审查启动条件、启动
  前后与停止前后的命令、执行 flags 和运行输入，并把必要行为纳入候选验证；
  不得假设 transient `caddy validate` 会自动复现它们。
- manager environment 或 journal 出现潜在秘密名称：按潜在泄露停止。不得打印
  匹配行、不得自动 vacuum/delete journal；先在单独批准的事件处置阶段确认影响
  并轮换受影响秘密，再重新拟定升级窗口。
- 解析后的 Caddy 路径不是 root-owned、可被非 root 写入，或不能归属到已核验
  package/固定自定义资产：停止；任何后续特权命令不得执行 PATH 中的替代程序。

`trusted_binary=... package=... sha256=...` 四行和 unit fragment SHA 只是公开的
只读盘点结果，不是
自动批准。它们必须写入本次维护审批记录；任何后续 staging/切换前都要再次计算
并与该记录逐字符一致。SHA、package 或 owner/mode 任一变化都停止，重新盘点，
不能沿用旧批准。

这里不把 `dpkg --verify caddy` 的整包结果当作二进制完整性结论，因为合法修改
的 `/etc/caddy/Caddyfile` 可能作为 conffile 报差异。dpkg metadata 只盘点旧
`/usr/bin/caddy` 的 v2.6.2 来源；新候选的完整性只来自已验证 Sigstore/Rekor 和
固定 checksum 的官方 Release archive，再核对解包 ELF SHA。其他 package 差异
只做固定分类并另审，不能混成“候选二进制已验证”。

`caddy validate` 会加载并 provision 模块，不能归类为严格只读。因此本阶段不
执行它；旧/新二进制的 validation 都放到下一节获得写入批准后的隔离 staging。

线上配置只允许在 VPS 内做受控审计，不复制到聊天或普通日志。至少要给出固定
PASS/FAIL 结果：

- 是否出现 `forward_auth`；
- 是否出现 `basicauth` / `basic_auth`；
- 是否已有唯一的全局 options block；
- 是否出现 `persist_config`、`admin` 或第三方指令；
- 是否有 `header_up` / `header_down` 依赖带下划线的 header；
- 全部 import 文件是否为 root/caddy 可控且权限合理；
- Shared Diary 是否使用 WebSocket、长连接或自定义 TLS/storage。

任何一项无法分类就停止，不猜。

禁止读取 Admin API `/config` 来证明活动配置与文件一致，因为它可能暴露已有
认证材料。必须依靠可审计的变更记录、运维交接和 file-backed 发布记录证明自
上次启动后没有 Admin-API-only 修改；如果不能证明，就把活动配置状态记为
`unknown` 并停止升级。外部路由基线只能辅助验收，不能替代这项 provenance。
同样，若不能证明 journal 覆盖了所有仍可保留的 Caddy 启动记录，或不能证明
历史 manager/service environment 的来源，就把历史 `--environ` 暴露状态记为
`unknown` 并停止；“扫描没有命中”不能替代这项 provenance。

## 4. 获批后的 staging（写入但不切换、不启动）

此阶段已按单独批准完成候选安装与隔离 validation；下列内容保留为证据合同和
未来重做时的 fail-closed 步骤。固定候选是
`/opt/caddy-candidates/v2.11.4/caddy`，ELF SHA-256 必须等于
`b7105518e3ed1c0761f232e44fc09345535533c9cb0abf0e12809416c7ac64d9`。
它不得覆盖 `/usr/bin/caddy`，也不得触发 package post-install 的服务动作。

1. 创建 root-only 备份目录，保存：
   - 旧二进制的精确副本和 SHA-256；
   - 当前 package 名称/版本/来源；
   - 模块清单；
   - systemd vendor unit 与全部 drop-ins；
   - 根 Caddyfile 及全部 import 的原权限副本。
   根文件另保存为 root-only 目录中的
   `/var/backups/caddy-v2114-cutover/Caddyfile.post-v2114-approved`（普通文件、
   root:root、mode `0644`、nlink 1），并把它的 SHA 固定进维护 transaction。
   Eryu route 阶段只能从该批准副本构造 pre/expected 文件，不能把届时 live 根
   Caddyfile 直接吸收为新基线。
   另生成 root-only 的
   `/var/backups/caddy-v2114-cutover/shared-diary-imports.sha256`，只列根
   Caddyfile 之外的全部 Shared Diary import 及其 SHA。未来只允许以
   `sha256sum --check --status` 无输出核对；根 Caddyfile 则与批准的 pre/expected
   副本用 `cmp --silent` 分别核对。
   只有当根快照与全部 imports 已经完成无输出的完整配置扫描且
   `forward_auth=0` 时，才允许生成并批准这两个 manifest。所有实际 import 必须
   位于非 symlink 的 `/etc/caddy` 配置树内；无法完整枚举或读取就停止。
   systemd vendor unit、候选 drop-in、既有全部 drop-ins 与之后获批的 Eryu
   credential drop-in 则写入 root-only
   `/var/backups/caddy-v2114-cutover/systemd-files-post-eryu.sha256`。该文件必须是
   root:root mode `0600`、nlink 1，内容来自维护审批中的已知 SHA，不能从未经
   核对的 live 文件反向生成；activation 与每次 reload 前都用
   `sha256sum --check --status` 复检。候选 ELF 的 `security.capability=absent` 也要
   写入同一审批记录，并在 activation、公开 reload 与 route-only rollback 前用
   固定 `/usr/sbin/getcap` 无输出复检；SHA 本身不覆盖扩展属性。
   另建 root:caddy 的临时 validation view：目录 mode `0750`，配置文件 mode
   `0640`，复制进 validation view 的旧/候选 Caddy 二进制 mode `0750`；最终
   版本化候选仍为 root:root mode `0755`。所有文件均不得 group
   write。只把 Caddy 用户本来就必须读取/执行的候选配置与已核验二进制复制
   进去。root-only 备份目录本身绝不放宽，也不能直接让 User=caddy 的
   transient unit 读取。
2. 保持 `/var/lib/caddy` 原位。这里含 TLS/ACME 私钥；不打印、不提交、不复制
   到普通目录。是否另做加密备份需要单独批准。
3. 本次只接受 Caddy 官方 v2.11.4 Release 的 Linux amd64 standalone 资产；先
   验证 Sigstore certificate identity、OIDC issuer、Rekor、已签 checksums 与
   固定 archive checksum，再解包并核对上述 ELF SHA。不得改用 `.deb` 安装、
   `apt install` 或 `caddy upgrade`。
4. 资产、签名、checksum、版本、平台或标准模块集合任一不符都停止；不得现场
   重建或补装模块来绕过差异。
5. 比较旧/新模块清单。候选只能是必要模块的相同集合或经逐项批准的安全超集。
6. 用候选二进制分别验证：
   - 原始 Shared Diary 配置；
   - 仅加入升级后全局选项的 Shared Diary 候选；
   - Shared Diary + Eryu 完整候选。
   validation 必须以现场 unit 的 Caddy 用户/组运行。只有上一步明确得到
   `caddy_runtime_inputs=absent` 时，才可使用本计划的通用 transient validator；
   若结果为 `present` 或 `classification_failed`，必须停下并另行评审如何精确
   复现必要输入，不能省略、猜测或把值写入命令、普通文件、stdout/journal。
   root 成功不能替代 Caddy 用户的权限验证。
7. 所有以 root、systemd transient unit 或 credential 运行的 validation，只能
   调用已经核验 owner/mode/package/SHA-256 的固定绝对路径；禁止把
   `command -v` 或当前 `PATH` 的结果直接提升权限。
8. 为候选二进制、vendor unit、全部 candidate drop-ins 及其父目录建立 root-only
   manifest：记录精确绝对路径、文件类型、owner/group、mode 与 SHA-256，不记录
   环境值或 credential 内容。候选 drop-in 使用本仓库
   `systemd/caddy-v2114-candidate.conf`，必须把 `ExecStart`/`ExecReload` 先清空再
   设为第 5 节的唯一精确 argv，并明确设置 `RuntimeDirectory=caddy`、
   `RuntimeDirectoryMode=0750`、`RuntimeDirectoryPreserve=no`。在任何
   `daemon-reload`/restart 前重新计算 manifest 并逐字符比较；不同即停止。

禁止运行会把展开配置输出到终端的 `caddy adapt`，也禁止读取 Admin API
`/config`；只运行 `caddy validate`。

## 5. 升级后配置合同

候选根 Caddyfile 必须：

1. 只有一个、且位于最前面的 global options block；
2. 在该 block 中合并 `persist_config off`，不能盲目新增第二个 global block；
3. 在同一 block 中把 Admin API 固定为
   `admin unix//run/caddy/admin.sock|0600`；默认 `localhost:2019` 不再保留；
4. systemd 必须提供 root/caddy 控制的 `/run/caddy`，且 `ExecReload` 使用
   精确 argv `/opt/caddy-candidates/v2.11.4/caddy reload --config /etc/caddy/Caddyfile
   --adapter caddyfile --force --address unix//run/caddy/admin.sock`。现场已有
   RuntimeDirectory 或 drop-in
   不明确时停止，不能盲目覆盖；
5. file-backed `ExecStart` 必须删除 `--environ`，其他已核验参数保持不变；
   最终精确 argv 为 `/opt/caddy-candidates/v2.11.4/caddy run --config
   /etc/caddy/Caddyfile --adapter caddyfile`；否则 Caddy
   会在启动时把完整环境写入 journal；
6. 完整保留 Shared Diary 的站点、TLS、storage 和其他已核验设置；
7. Eryu 片段使用 `basic_auth`，不再使用 v2.6.2 的 `basicauth` 名称；
8. Eryu Basic Auth 条目仍只从 systemd encrypted credential 的运行时目录
   import；
9. 删除旧的 `XDG_CONFIG_HOME` 与 `autosave.json -> /dev/null` workaround。

`persist_config off` 禁止把运行中展开后的配置写为 `autosave.json`。权限为
`0600` 的 Unix Admin socket 则把内存配置读取/reload 能力限制在 Caddy 用户与
root；其他低权限服务不能通过默认 TCP Admin API 读取 Basic Auth verifier。
旧 v2.6.2 无法解析这些升级版全局设置，因此回滚时必须同时恢复旧 Caddyfile
及 imports、旧 unit/drop-ins，并让 systemd 重新指向仍未被覆盖的
`/usr/bin/caddy`；不得把候选复制到旧入口。

## 6. Shared Diary 切换前基线与验收

升级前后使用相同、只读的测试矩阵。不得在测试中创建或修改日记内容：

| 项目 | 预期 |
|---|---|
| Caddy service | active，80/443 监听者未变化 |
| TLS/SNI | Shared Diary 现有域名证书有效、链和域名正确 |
| 公共根路径 | 保持原有 404/隐藏行为 |
| `/actions/*` | 未授权仍被拒绝；已授权只做最小只读身份检查 |
| `/diary/*`、`/api/*`、`/mcp` | 只按线上真实路由基线验证，不以本地未跟踪模板为真相 |
| WebSocket/长连接 | 如果现场使用，握手和持续连接均正常 |
| Header | 不依赖被新版本丢弃的下划线请求头 |
| 日志 | 不出现 Authorization、access key、cookie、hash 或其他秘密 |

任何状态码、TLS、认证、header 或连接行为与基线不同，都视为失败。升级窗口只
升级 Caddy，不同时加入 Eryu 路由；Shared Diary 完整通过后才进入 Eryu 阶段。

## 7. 切换门禁（必须另行批准维护窗口）

1. 重新核对候选普通文件、非 symlink、root-owned、不可由 group/other 写、没有
   file capabilities，且
   `/opt/caddy-candidates/v2.11.4/caddy` 的 SHA-256 精确为
   `b7105518e3ed1c0761f232e44fc09345535533c9cb0abf0e12809416c7ac64d9`；版本、
   标准模块清单、候选 validation、`forward_auth=0`、systemd >= 254 与
   “无 Admin-API-only 动态配置”provenance 也必须全部通过。
   标准构建门使用
   `/opt/caddy-candidates/v2.11.4/caddy list-modules --skip-standard --packages --versions`
   并要求输出为空；不得运行旧 `/usr/bin/caddy` 完成这项检查。
2. `/usr/bin/caddy` 必须仍与升级前旧 v2.6.2 manifest 完全一致。切换不得覆盖、
   删除、改写或 symlink 该文件；它只作为完整回滚入口保留。
3. 把 `systemd/caddy-v2114-candidate.conf` 作为独立候选 drop-in 安装到固定路径
   `/etc/systemd/system/caddy.service.d/caddy-v2114-candidate.conf`。该 drop-in 的
   `ExecStart` / `ExecReload` 只能使用版本化候选绝对
   路径；不得使用 `PATH`、`/usr/bin/caddy` 或 `current` symlink。
4. 写入候选 drop-in 后先重算第 4 节 manifest，执行 `daemon-reload`，再
   固定分类 `NeedDaemonReload=no`。随后重跑 User/Group、ExecStart/Reload
   legacy+Ex、lifecycle hooks、runtime inputs 与 drop-in 路径清单；任一多出、
   缺失或 flags 变化都停止，不能 restart。
5. 切换后确认 `/run/caddy` 为非 symlink 目录、owner/group 为 `caddy:caddy`，且
   group/other 不可写；Admin socket 是 `/run/caddy/admin.sock`、owner/group 为
   `caddy:caddy`、mode 为 `0600`；默认 `localhost:2019` 不监听，reload 只通过
   Unix socket成功。
6. 切换后立即用候选绝对路径核对版本、二进制/unit/drop-in SHA、模块，并确认
   `/proc/$MainPID/exe` 精确解析为该版本化候选，再核对 80/443、TLS 与
   Shared Diary 矩阵。
7. 成功后停下报告；Eryu credential drop-in、fragment、服务启动和 Caddy
   reload 仍是后续独立门。

进入 Eryu 阶段后，安装 `LoadCredentialEncrypted` drop-in 并执行
`daemon-reload` 还不足以改变已经运行的 Caddy activation。必须在 Eryu route
尚未加入时，另行批准一次 Caddy restart 来激活该 credential；随后再次完成
Shared Diary 回归并停下。只有这一步通过，才允许加入 Eryu fragment 并在更后
面的独立批准中 reload。

## 8. 一次性条件式回滚合同（未来可与切换一并预授权，尚未执行）

回滚不是默认动作。未来维护审批可以把**一次**条件式回滚与候选切换放在同一
授权中，但必须固定 maintenance transaction ID、授权过期时间、候选/脚本/
manifest SHA、完整旧状态备份闭包、只读 Shared Diary 矩阵，以及最多一次
`daemon-reload` 和一次 restart。该授权不能跨窗口复用，也不授权 stop、reload、
第二次 restart、package 安装/降级、Auth0 修改、Eryu 部署、删除候选或恢复
`/var/lib/caddy`。

### 8.1 唯一允许的两个 trigger

只有下面两类证据可以触发；CLI 参数或人工描述本身不算证据：

- `trigger=caddy_inactive`
- `trigger=shared_diary_baseline_failed_3x`

1. `caddy_inactive`：切换前 manifest 证明服务原为 active；切换后两次独立、成功
   的 systemd 读取均得到 `LoadState=loaded`、`ActiveState=inactive|failed`、
   `MainPID=0`，两次间隔固定且结果一致。`activating`、`deactivating`、`reloading`、
   查询失败、空输出或无法分类均为 `unknown`，不得触发。
2. `shared_diary_baseline_failed_3x`：Caddy 仍 active，进程、候选 SHA 和 effective
   unit 精确匹配本次 transaction；同一套 loopback、只读 Shared Diary 完整矩阵
   连续三轮得到确定的 baseline mismatch。首轮失败后必须再做两轮固定间隔复检；
   任一轮通过就清零并取消回滚，工具/网络/解析/权限错误一律为 `unknown`，不能
   计作失败。

除这两个 trigger 外，validation 失败、模块不一致、摘要 drift、额外 drop-in、
日志风险或任何未知状态都只能停止并报告，不能自动回滚。首次 live mutation 前
还须再次证明同一个 trigger；PID、InvocationID、unit、drop-ins、Caddyfile/imports
或候选 SHA 在两次检查间发生变化也必须停止。

### 8.2 切换前必须准备的恢复闭包

root-only、不可变且非 symlink 的 manifest/备份必须覆盖：根 Caddyfile 与全部
imports、vendor unit、升级前全部 drop-ins 的精确集合（包括原本 absence）、
每个文件的路径类型/owner/group/mode/SHA、旧 `/usr/bin/caddy` 的 SHA/package/
模块清单及 byte-identical 独立验证副本、候选状态 manifest，以及 Shared Diary
切换前基线。未知 import、额外
drop-in、hardlink/symlink、group/other 可写路径或任一 SHA 不符都停止。

候选切换从不覆盖 `/usr/bin/caddy`。回滚前必须重新证明它仍是与旧 manifest
一致的普通文件；符合时只恢复 systemd 对它的引用，不复制、不改写该二进制。
旧入口缺失或变化时，本条件式回滚必须停止，不能用备份副本自动修复。

### 8.3 单次恢复顺序

1. 在不改 live 文件的隔离 view 中，用 SHA 完全匹配的旧 `/usr/bin/caddy` 副本、
   以 Caddy 用户验证完整旧 Caddyfile/imports；同时完成 `systemd-analyze verify`。
2. 在各目标文件同一文件系统准备原子候选；再次核对 trigger、transaction、当前
   candidate manifest 与旧备份闭包。任一差异都在首次写入前无操作退出。
3. 只恢复本次切换实际改变的 imports、根 Caddyfile、vendor unit（若改变）和
   drop-ins 精确集合。只删除本次创建且 SHA 匹配的候选 drop-in；不得递归删除
   整个 drop-in 目录。
4. 确认 `/usr/bin/caddy` 未变化，执行一次 `daemon-reload`，并核对 effective
   `ExecStart`/`ExecReload`、User/Group、hooks、runtime inputs 与 drop-ins 已恢复
   旧 manifest；此时 systemd 必须重新指向 `/usr/bin/caddy`。
5. 最多执行一次已预授权的 restart；验证运行 executable、版本/SHA、listeners
   与 unit 均为旧基线，再执行完整 Shared Diary 只读矩阵。

回滚过程中不调用 Caddy Admin API、不 reload、不启动 Eryu。首次 live mutation
后任一步失败时，不重试、不重新应用候选、不扩大删除/修复范围，只报告精确完成
状态并等待新批准。只有旧 manifest 全部恢复、一次 restart 成功、运行入口确为
`/usr/bin/caddy` 且 Shared Diary 完整基线通过，才能报告
`rollback=verified_complete`。

默认不回滚 `/var/lib/caddy`，避免覆盖新的 TLS/ACME 状态。若 Caddy 升级和
Shared Diary 均正常、只是 Eryu 路由失败，只使用 README 的 Eryu route-only
回退；它仍由候选路径 validate/reload，不降级 Caddy。

## 9. 下次维护审批前必须实例化的现场锚点

本地模板已经锁定候选路径、ELF SHA、命令形态、恢复闭包和唯一两个 trigger，
但下面两项依赖切换当天的真实文件集合；在它们进入同一个 maintenance
transaction 前，任何 VPS 写入、restart 或 reload 都不得执行：

1. 为 `Caddyfile.post-v2114-approved`、`shared-diary-imports.sha256` 和
   `systemd-files-post-eryu.sha256` 各记录其**自身**的审批 SHA-256；每个操作块
   既要校验 manifest 内列出的目标，也要先校验 manifest/snapshot 本身等于该
   transaction 的固定 SHA。不得从当下 live 文件反向接受新摘要。
2. 为 `/etc/caddy`、根 Caddyfile、全部 imports 和 Eryu fragment 生成不含正文的
   type/owner/group/mode/nlink 批准记录；public reload 与 route-only rollback
   紧邻变更前都要无输出复核。任一权限、类型、链接数或文件集合 drift 都只能
   停止并报告，不能把它计入回滚 trigger。

这两项是未来执行门，不是当前已验证事实，也不授权读取或输出配置正文。
