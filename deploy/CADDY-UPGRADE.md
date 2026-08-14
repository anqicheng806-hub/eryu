# Caddy 安全升级与 Shared Diary 兼容/回滚计划（尚未执行）

## 1. 状态与硬边界

本文件只是一份后续变更方案。2026-08-14 本轮没有连接 VPS，没有读取线上
`Caddyfile`，也没有安装、启动、停止、restart 或 reload Caddy。

已知的历史证据只有：较早的只读检查曾看到 Caddy `v2.6.2`，Shared Diary 由
systemd 管理的 Caddy 代理。当时的 handoff 只记录公开 `/actions/*`。当前本地
`shared-diary-mcp/deploy/Caddyfile` 使用标准的 `path`、`handle`、
`reverse_proxy`、`respond` 和 `header`，同时还出现 `/mcp`、`/diary`、`/api`；
整个本地 `deploy/` 目录未被该仓库跟踪。因此它既与历史 handoff 不同，也不能
替代线上现状。以下信息仍是 **未核验**，下一次必须先只读取得：

- 当前二进制的真实路径、SHA-256 和安装来源；
- 是 Ubuntu 包、Caddy 官方 Cloudsmith 包、静态二进制还是自定义构建；
- 全部标准/第三方模块；
- systemd 的真实 `FragmentPath`、drop-ins、`ExecStart`、`ExecReload`、用户和组；
- Caddy 二进制与 `/usr/bin/git`、`/usr/bin/awk` 是否为 root-owned、不可由非 root
  写入，并分别属于哪个已验证 package；
- 线上根 `Caddyfile`、全部 import、全局 options、Shared Diary 实际路由；
- 是否使用 `forward_auth`、下划线请求头、WebSocket 或非标准模块。
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

版本选择必须遵守：

1. 默认优先等待并选择正式签名的 `v2.11.5` 或更高稳定版。
2. 切换当天重新读取官方 `releases/latest` 和安全公告，固定精确版本及二进制
   SHA-256；禁止使用浮动 `latest`。
3. 如果线上任一配置含 `forward_auth`，禁止使用 v2.11.4。
4. 只有线上确认没有 `forward_auth`、不依赖带下划线的请求头，且 staging
   validation 与 Shared Diary 回归都通过时，v2.11.4 才可作为临时候选。
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

/usr/bin/caddy version || readonly_gate_failed
/usr/bin/caddy build-info || readonly_gate_failed
/usr/bin/caddy list-modules --packages --versions || readonly_gate_failed
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
的 `/etc/caddy/Caddyfile` 可能作为 conffile 报差异。staging 必须从已验证签名、
精确版本的 `.deb` 解包，比较其中 Caddy 二进制与候选 SHA；其他 package 差异只
做固定分类并另审，不能混成“二进制已验证”。

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

此阶段仍需单独批准。目标是准备可回滚的候选，不覆盖 `/usr/bin/caddy`，也不
触发 package post-install 的服务动作。

1. 创建 root-only 备份目录，保存：
   - 旧二进制的精确副本和 SHA-256；
   - 当前 package 名称/版本/来源；
   - 模块清单；
   - systemd vendor unit 与全部 drop-ins；
   - 根 Caddyfile 及全部 import 的原权限副本。
   另建 root:caddy 的临时 validation view：目录 mode `0750`，配置文件 mode
   `0640`，已核验的旧/候选 Caddy 二进制 mode `0750`；所有文件均不得 group
   write。只把 Caddy 用户本来就必须读取/执行的候选配置与已核验二进制复制
   进去。root-only 备份目录本身绝不放宽，也不能直接让 User=caddy 的
   transient unit 读取。
2. 保持 `/var/lib/caddy` 原位。这里含 TLS/ACME 私钥；不打印、不提交、不复制
   到普通目录。是否另做加密备份需要单独批准。
3. 如果现场是官方 apt 包，先用 `apt-get download caddy=<精确版本>` 下载到
   root-only staging，再用 `dpkg-deb --extract` 取得候选二进制；此时不执行
   `apt install`。
4. 如果是静态/自定义构建，按官方 checksum/Sigstore 流程验证固定资产，并
   重建完全相同的必要模块集合。
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
   环境值或 credential 内容。候选 unit 必须把 `ExecStart`/`ExecReload` 先清空再
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
   精确 argv `/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
   --address unix//run/caddy/admin.sock`。现场已有 RuntimeDirectory 或 drop-in
   不明确时停止，不能盲目覆盖；
5. file-backed `ExecStart` 必须删除 `--environ`，其他已核验参数保持不变；
   最终精确 argv 为 `/usr/bin/caddy run --config /etc/caddy/Caddyfile`；否则 Caddy
   会在启动时把完整环境写入 journal；
6. 完整保留 Shared Diary 的站点、TLS、storage 和其他已核验设置；
7. Eryu 片段使用 `basic_auth`，不再使用 v2.6.2 的 `basicauth` 名称；
8. Eryu Basic Auth 条目仍只从 systemd encrypted credential 的运行时目录
   import；
9. 删除旧的 `XDG_CONFIG_HOME` 与 `autosave.json -> /dev/null` workaround。

`persist_config off` 禁止把运行中展开后的配置写为 `autosave.json`。权限为
`0600` 的 Unix Admin socket 则把内存配置读取/reload 能力限制在 Caddy 用户与
root；其他低权限服务不能通过默认 TCP Admin API 读取 Basic Auth verifier。
旧 v2.6.2 无法解析这些升级版全局设置，因此回滚时必须同时恢复旧 Caddyfile、
旧 unit/drop-ins 和旧二进制。

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

1. 再次确认目标精确版本、SHA-256、模块清单、systemd >= 254、候选 validation
   和“无 Admin-API-only 动态配置”provenance 全部通过。
2. 明确 package 安装是否会自动 restart；把实际安装和一次 Caddy restart
   作为同一项高影响批准，不能隐藏在普通 apt 命令中。
3. 保持 systemd `ExecStart` / `ExecReload` 与已核验 file-backed unit 一致。
4. 写入候选 unit/drop-ins 后先重算第 4 节 manifest，执行 `daemon-reload`，再
   固定分类 `NeedDaemonReload=no`。随后重跑 User/Group、ExecStart/Reload
   legacy+Ex、lifecycle hooks、runtime inputs 与 drop-in 路径清单；任一多出、
   缺失或 flags 变化都停止，不能 restart。
5. 切换后确认 `/run/caddy` 为非 symlink 目录、owner/group 为 `caddy:caddy`，且
   group/other 不可写；Admin socket 是 `/run/caddy/admin.sock`、owner/group 为
   `caddy:caddy`、mode 为 `0600`；默认 `localhost:2019` 不监听，reload 只通过
   Unix socket成功。
6. 切换后立即核对版本、二进制/unit/drop-in SHA、模块、PID、80/443、TLS 与
   Shared Diary 矩阵。
7. 成功后停下报告；Eryu credential drop-in、fragment、服务启动和 Caddy
   reload 仍是后续独立门。

进入 Eryu 阶段后，安装 `LoadCredentialEncrypted` drop-in 并执行
`daemon-reload` 还不足以改变已经运行的 Caddy activation。必须在 Eryu route
尚未加入时，另行批准一次 Caddy restart 来激活该 credential；随后再次完成
Shared Diary 回归并停下。只有这一步通过，才允许加入 Eryu fragment 并在更后
面的独立批准中 reload。

## 8. 回滚计划（准备但不自动执行）

触发条件包括：

- 新 unit 无法启动或模块不一致；
- Caddyfile validation/startup 失败；
- Shared Diary TLS、认证、路由、WebSocket、header 或只读流程异常；
- 发现 autosave/日志可能落入凭据或 hash。

除非维护批准明确预授权一次失败回滚，否则失败后先停下报告。获批后的顺序是：

1. 在不改 live 文件的 staging 中，从 root-only 备份复制一个仅 root:caddy
   可达的 validation view；用已保存且 SHA-256 完全匹配的旧二进制、以 Caddy
   unit 用户先验证升级前 Caddyfile/imports，并核对 vendor unit/drop-ins；
   未通过就停止，不能先安装旧 package 碰运气；
2. 预先检查旧 package maintainer scripts。必须用现场批准的发行版机制抑制
   package 自动 service action，或把不可抑制的一次 restart 明确写进回滚批准；
3. 只有前两项通过后，才恢复升级前根 Caddyfile、imports、vendor unit 和
   drop-ins，并恢复旧 v2.6.2 package/二进制；再次逐字符核对旧 SHA-256；
4. 如 unit 文件有变化，执行一次 `daemon-reload`；
5. 只进行一次已批准的 Caddy restart；
6. 重新执行完整 Shared Diary 只读矩阵。

默认不回滚 `/var/lib/caddy`，避免覆盖新的 TLS/ACME 状态。只有证明确需恢复并
再次获批，才使用加密备份。若 Caddy 升级和 Shared Diary 均正常、只是 Eryu
路由失败，只回退 Eryu import 并在另行批准后 reload，不降级 Caddy。
