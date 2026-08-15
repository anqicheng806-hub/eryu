# Eryu 私人测试部署草案（尚未执行）

这套模板把网页播放器和远程 MCP 分开：

- `https://eryu.95.169.17.214.sslip.io` -> Caddy -> `127.0.0.1:9090`
- ChatGPT 连接端点：`https://eryu-mcp.95.169.17.214.sslip.io/mcp` -> Caddy -> `127.0.0.1:9091/mcp`
- OAuth canonical resource / Audience：`https://eryu-mcp.95.169.17.214.sslip.io`

Python 服务不直接监听公网，VPS 防火墙也不需要开放 9090/9091。Caddy 升级会
影响现有 Shared Diary，因此必须先执行独立的
[`CADDY-UPGRADE.md`](CADDY-UPGRADE.md) 兼容/回滚门禁；Eryu 路由本身仍不得
改变 Shared Diary 的域名、站点或上游。

“私人测试”表示只供本人使用，不表示网络隔离：两个 `sslip.io` 域名都可在
公网解析。MCP `/mcp` 由 Auth0 的 `music:read` 保护。网页域名除精确的
`/health` 外，静态页面、全部 API、JS/CSS 和所有音频缓存路径都先经过 Caddy
Basic Auth；API 在通过 Basic Auth 后仍需原有完整 token。公开 `/health` 由
Caddy 直接返回纯文本 `ok`，不转发后端，也不返回版本、路径或运行状态。
历史检查中的 Caddy 2.6.2 不允许直接承载本项目。升级后的 Eryu 片段使用
`basic_auth`；实际目标版本、二进制 SHA、模块与 Shared Diary 兼容性必须在
升级当天重新核验。

默认部署会创建全新的 `/var/lib/eryu`。本地 `server/data`、音乐缓存、cookie、token 和日志都不会上传；如果以后需要迁移本地音乐数据，必须作为单独阶段再次确认。

## 文件用途

- `systemd/eryu-web.service`：网页与后端服务，只能写 `/var/lib/eryu`。
- `systemd/eryu-mcp.service`：只读 MCP 服务，不接收完整控制 token 或 `MUSIC_U`。
- `systemd/caddy-eryu-credentials.conf`：升级后 Caddy 的最小 drop-in，只加载
  加密 Basic Auth 条目；不再包含 2.6.2 专用 autosave workaround。
- `systemd/auth0-public.conf.example`：保存已通过 OIDC discovery 验证的公开
  Auth0 issuer URL，不允许放 secret。
- `run-with-credentials.sh`：从 systemd 的临时凭据目录读取秘密并在进程内导出；不会打印值。
- `create-caddy-basic-auth-credential.sh`：遮罩读取用户名/密码，生成 bcrypt
  hash 后直接加密成 systemd credential；不创建明文临时文件，也不输出值。
- `caddy/eryu.caddy`：两个独立 HTTPS 测试域名；网页域名只公开通用健康
  检查，其他路径全部 Basic Auth；MCP 域名只放行 `/mcp` 和 OAuth
  protected-resource metadata 路径。

## 密钥边界

四项秘密只进入 `/etc/credstore.encrypted/eryu/*.cred`：

- `MUSIC_U`
- `ERYU_AUTH_TOKEN`
- `ERYU_MCP_READ_TOKEN`
- `ERYU_BASIC_AUTH_ENTRY`（用户名和 bcrypt hash 组成的一行 Caddy 账户条目）

网页服务接收三项；MCP 服务只接收 `ERYU_MCP_READ_TOKEN`。Auth0 的 issuer、audience、resource URL 和 `music:read` scope 都是公开配置，不需要也不应配置 Auth0 client secret。

Basic Auth 的明文密码只在创建凭据脚本的进程内短暂存在，不进入参数、环境、
stdout 或普通文件。升级后用 systemd 只读运行时 credential 注入账户条目；
共享根 Caddyfile 的唯一 global options block 必须原生启用
`persist_config off`，禁止把展开后的用户名/hash 保存为 `autosave.json`。
同一 global block 还必须把 Admin API 固定到权限为 `0600` 的
`unix//run/caddy/admin.sock`；systemd `ExecReload` 只通过该 socket reload。
这样 Eryu/Shared Diary 等其他低权限服务不能读取内存配置中的账户 verifier。
本模板不会把四项秘密写入普通持久文件；持久凭据副本只使用加密 `.cred`，
运行时只有 systemd 在 `/run/credentials/...` 提供的受限副本和 Caddy 进程
内存；只有 Caddy 用户和 root 能访问 Admin socket。不得运行 `caddy adapt` 或
读取 Admin API `/config`，它们会输出展开后的配置。Caddy 版本选择、staging、
Shared Diary 回归和回滚细节见
[`CADDY-UPGRADE.md`](CADDY-UPGRADE.md)。

## 待确认后才执行的命令顺序

以下命令只是部署方案，目前没有运行。每一步都应先检查上一条结果，再继续下一条。

0. 先完成 Caddy 的独立只读盘点、安全升级和 Shared Diary 回归。任何输出与
   预期不一致都必须停止，不能继续部署 Eryu。

   ```bash
   # 在 VPS 的 SSH 终端运行；这些只是下一轮只读入口，当前没有执行。
   readonly_gate_failed() {
     printf '%s\n' 'eryu_readonly_gate=failed' >&2
     exit 1
   }
   test "$(command -v caddy)" = /usr/bin/caddy || readonly_gate_failed
   test "$(command -v git)" = /usr/bin/git || readonly_gate_failed
   test "$(command -v awk)" = /usr/bin/awk || readonly_gate_failed
   test "$(command -v systemd-creds)" = /usr/bin/systemd-creds || readonly_gate_failed
   test "$(command -v systemd-run)" = /usr/bin/systemd-run || readonly_gate_failed
   test "$(command -v systemd-analyze)" = /usr/bin/systemd-analyze || readonly_gate_failed
   test "$(command -v systemctl)" = /usr/bin/systemctl || readonly_gate_failed
   test "$(command -v systemd-ask-password)" = /usr/bin/systemd-ask-password || readonly_gate_failed
   test "$(command -v systemd)" = /usr/bin/systemd || readonly_gate_failed
   test "$(command -v busctl)" = /usr/bin/busctl || readonly_gate_failed
   test "$(command -v sudo)" = /usr/bin/sudo || readonly_gate_failed
   test "$(command -v openssl)" = /usr/bin/openssl || readonly_gate_failed
   test "$(command -v grep)" = /usr/bin/grep || readonly_gate_failed
   test "$(command -v ss)" = /usr/bin/ss || readonly_gate_failed
   test "$(command -v stat)" = /usr/bin/stat || readonly_gate_failed
   test "$(command -v readlink)" = /usr/bin/readlink || readonly_gate_failed
   test "$(command -v sha256sum)" = /usr/bin/sha256sum || readonly_gate_failed
   test "$(command -v dpkg-query)" = /usr/bin/dpkg-query || readonly_gate_failed
   test "$(command -v dpkg)" = /usr/bin/dpkg || readonly_gate_failed
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
     package_status="$(/usr/bin/dpkg-query -W -f='${db:Status-Status}' "$package_name")" || readonly_gate_failed
     test "$package_status" = installed || readonly_gate_failed
     trusted_binary_sha256_record="$(/usr/bin/sha256sum "$resolved_trusted_binary")" || readonly_gate_failed
     trusted_binary_sha256=${trusted_binary_sha256_record%% *}
     [[ "$trusted_binary_sha256" =~ ^[0-9a-f]{64}$ ]] || readonly_gate_failed
     printf 'trusted_binary=%s package=%s sha256=%s\n' "$trusted_binary" "$package_name" "$trusted_binary_sha256"
     unset resolved_trusted_binary trusted_binary_mode package_record package_name
     unset package_status trusted_binary_sha256_record trusted_binary_sha256
   done
   systemd_version_record="$(/usr/bin/systemd --version)" || readonly_gate_failed
   [[ "$systemd_version_record" =~ ^systemd[[:space:]]+([0-9]+) ]] || readonly_gate_failed
   systemd_major=${BASH_REMATCH[1]}
   test "$systemd_major" -ge 254 || readonly_gate_failed
   /usr/bin/caddy version || readonly_gate_failed
   /usr/bin/caddy build-info || readonly_gate_failed
   /usr/bin/caddy list-modules --packages --versions || readonly_gate_failed
   /usr/bin/dpkg-query -W -f='${Package}\t${Version}\t${Status}\n' caddy || readonly_gate_failed
   /usr/bin/apt-cache policy caddy || readonly_gate_failed
   caddy_unit_user="$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=User --value 2> /dev/null)" || readonly_gate_failed
   caddy_unit_group="$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=Group --value 2> /dev/null)" || readonly_gate_failed
   test "$caddy_unit_user" = caddy || readonly_gate_failed
   test "$caddy_unit_group" = caddy || readonly_gate_failed
   printf '%s\n' 'caddy_unit_identity=caddy:caddy'
   caddy_need_daemon_reload="$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=NeedDaemonReload --value 2> /dev/null)" || readonly_gate_failed
   test "$caddy_need_daemon_reload" = no || readonly_gate_failed
   printf '%s\n' 'caddy_need_daemon_reload=no'
   unset caddy_unit_user caddy_unit_group caddy_need_daemon_reload
   unset systemd_version_record systemd_major
   unset -f readonly_gate_failed
   ```

   安装来源、模块、真实 ExecStart 和线上 Shared Diary 配置本轮均未现场核验。
   上述四项 `trusted_binary` 指纹也必须先进入维护审批记录，后续写入前重新计算
   并逐字符匹配；任一变化都停止。
   unit fragment、drop-ins、父目录、RuntimeDirectory 与 SHA 的完整固定分类必须
   按 `CADDY-UPGRADE.md` 同步通过；这里的简表不能替代它。整包
   `dpkg --verify caddy` 也不能替代从精确签名 `.deb` 解包后的二进制 SHA 对比。
   完整命令、候选版本条件、官方安全公告、维护窗口和回滚顺序都在
   [`CADDY-UPGRADE.md`](CADDY-UPGRADE.md)。升级必须先单独完成；不得把 Caddy
   upgrade、Eryu 路由和 Eryu 服务启动合并成一次变更。

1. 安装运行依赖并建立两个低权限系统用户。

   ```bash
   # 在 VPS 的 SSH 终端运行；成功时 apt 与 useradd 均返回 0。
   eryu_prerequisite_failed() {
     printf '%s\n' 'eryu_prerequisite_install=failed' >&2
     exit 1
   }
   /usr/bin/sudo /usr/bin/apt-get update || eryu_prerequisite_failed
   /usr/bin/sudo /usr/bin/apt-get install --no-install-recommends python3-venv ffmpeg || eryu_prerequisite_failed
   /usr/bin/sudo /usr/sbin/useradd --system --home /nonexistent --shell /usr/sbin/nologin eryu-web || eryu_prerequisite_failed
   /usr/bin/sudo /usr/sbin/useradd --system --home /nonexistent --shell /usr/sbin/nologin eryu-mcp || eryu_prerequisite_failed
   unset -f eryu_prerequisite_failed
   ```

2. 在分支已经推送并再次获批后，把代码安装到 `/opt/eryu/current`，创建独立
   虚拟环境。部署人员必须输入已批准的 40 位完整 SHA；远端分支 tip、clone
   后的 remote-tracking ref、本地 HEAD 和 detached 状态必须全部匹配。

   ```bash
   # 在 VPS 的 Bash SSH 终端运行；成功时所有 test 都无输出并返回 0。
   release_gate_failed() {
     printf '%s\n' 'eryu_release_gate=failed' >&2
     exit 1
   }
   readonly ERYU_REPOSITORY='https://github.com/anqicheng806-hub/eryu'
   readonly ERYU_BRANCH='feature/music-presence-mcp'
   test "$(command -v git)" = /usr/bin/git || release_gate_failed
   test -x /usr/bin/git || release_gate_failed
   read -r -p 'Approved 40-hex release SHA: ' ERYU_RELEASE_SHA
   [[ "$ERYU_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || release_gate_failed
   readonly ERYU_RELEASE_SHA
   remote_release_record="$(
     /usr/bin/git ls-remote --exit-code --heads \
       "$ERYU_REPOSITORY" "refs/heads/$ERYU_BRANCH"
   )" || release_gate_failed
   [[ "$remote_release_record" != *$'\n'* ]] || release_gate_failed
   IFS=$'\t' read -r remote_release_sha remote_release_ref <<< "$remote_release_record"
   test "$remote_release_sha" = "$ERYU_RELEASE_SHA" || release_gate_failed
   test "$remote_release_ref" = "refs/heads/$ERYU_BRANCH" || release_gate_failed
   /usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 /opt/eryu || release_gate_failed
   /usr/bin/sudo /usr/bin/git clone --branch "$ERYU_BRANCH" --single-branch --no-checkout "$ERYU_REPOSITORY" /opt/eryu/current || release_gate_failed
   test "$(/usr/bin/sudo /usr/bin/git -C /opt/eryu/current rev-parse "refs/remotes/origin/$ERYU_BRANCH")" = "$ERYU_RELEASE_SHA" || release_gate_failed
   /usr/bin/sudo /usr/bin/git -C /opt/eryu/current checkout --detach "$ERYU_RELEASE_SHA" || release_gate_failed
   test "$(/usr/bin/sudo /usr/bin/git -C /opt/eryu/current rev-parse HEAD)" = "$ERYU_RELEASE_SHA" || release_gate_failed
   test "$(/usr/bin/sudo /usr/bin/git -C /opt/eryu/current rev-parse --abbrev-ref HEAD)" = HEAD || release_gate_failed
   /usr/bin/sudo /usr/bin/python3 -m venv /opt/eryu/venv || release_gate_failed
   /usr/bin/sudo /opt/eryu/venv/bin/python -m pip install /opt/eryu/current/mcp_server || release_gate_failed
   /usr/bin/sudo /opt/eryu/venv/bin/python -m pip install --only-binary=:all: --progress-bar off -r /opt/eryu/current/server/requirements-analysis.txt || release_gate_failed
   /usr/bin/sudo /opt/eryu/venv/bin/python -m pip check || release_gate_failed
   unset remote_release_record remote_release_sha remote_release_ref
   unset -f release_gate_failed
   ```

   分析依赖的直接版本已固定为 `librosa 0.11.0`、`numpy 2.2.6`、
   `matplotlib 3.10.8`，三者都保留 Python 3.10+ 兼容范围。本地已用这组版本
   对 12 秒合成音频真实生成 BPM、六段能量和频谱 PNG；VPS/Ubuntu wheel、
   `ffmpeg` 与真实网易云音频仍必须在部署预检中验证。如果安装找不到二进制
   wheel，或 `pip check` 不通过，应立即停止，不能改成现场源码编译。

3. 用 systemd 加密凭据创建四个秘密文件。先在你自己的密码管理器中
   生成并保存一个至少 32 字符、无空白的 `ERYU_AUTH_TOKEN`；不要把它发到
   聊天、写入命令历史或普通文件。浏览器播放器稍后也要输入这个相同值。
   下列遮罩提示会把它直接送入加密流程，不在终端回显。内部
   `ERYU_MCP_READ_TOKEN` 不需要人工复用，可以直接随机生成后加密。另在密码
   管理器中保存一组独立的 Basic Auth 用户名和 32-64 位 ASCII 随机密码
   （脚本接受 20-72 bytes）；创建脚本会遮罩读取密码并要求确认，不会显示
   生成的 hash。

   ```bash
   # 在 VPS 的 SSH 终端运行；成功后只应看到四个 .cred 文件名，不能查看其内容。
   eryu_credential_creation_failed() {
     printf '%s\n' 'eryu_credential_creation=failed' >&2
     exit 1
   }
   /usr/bin/sudo /usr/bin/test -d /etc || eryu_credential_creation_failed
   /usr/bin/sudo /usr/bin/test ! -L /etc || eryu_credential_creation_failed
   test "$(/usr/bin/sudo /usr/bin/stat -c '%U:%G' /etc)" = root:root || eryu_credential_creation_failed
   credential_etc_mode="$(/usr/bin/sudo /usr/bin/stat -c '%a' /etc)" || eryu_credential_creation_failed
   [[ "$credential_etc_mode" =~ ^[0-7]{3,4}$ ]] || eryu_credential_creation_failed
   test "$((8#$credential_etc_mode & 0022))" -eq 0 || eryu_credential_creation_failed
   unset credential_etc_mode
   for credential_directory in /etc/credstore.encrypted /etc/credstore.encrypted/eryu; do
     if /usr/bin/sudo /usr/bin/test ! -e "$credential_directory" &&
       /usr/bin/sudo /usr/bin/test ! -L "$credential_directory"; then
       /usr/bin/sudo /usr/bin/install -d -o root -g root -m 0700 "$credential_directory" || eryu_credential_creation_failed
     fi
     /usr/bin/sudo /usr/bin/test -d "$credential_directory" || eryu_credential_creation_failed
     /usr/bin/sudo /usr/bin/test ! -L "$credential_directory" || eryu_credential_creation_failed
     test "$(/usr/bin/sudo /usr/bin/stat -c '%U:%G:%a' "$credential_directory")" = root:root:700 || eryu_credential_creation_failed
   done
   unset credential_directory
   for credential_target in \
     /etc/credstore.encrypted/eryu/MUSIC_U.cred \
     /etc/credstore.encrypted/eryu/ERYU_AUTH_TOKEN.cred \
     /etc/credstore.encrypted/eryu/ERYU_MCP_READ_TOKEN.cred \
     /etc/credstore.encrypted/eryu/ERYU_BASIC_AUTH_ENTRY.cred; do
     /usr/bin/sudo /usr/bin/test ! -e "$credential_target" || eryu_credential_creation_failed
     /usr/bin/sudo /usr/bin/test ! -L "$credential_target" || eryu_credential_creation_failed
   done
   unset credential_target
   /usr/bin/sudo /usr/bin/systemd-ask-password 'MUSIC_U' | /usr/bin/sudo /usr/bin/systemd-creds encrypt --name=MUSIC_U - /etc/credstore.encrypted/eryu/MUSIC_U.cred
   credential_pipeline_status=("${PIPESTATUS[@]}")
   test "${credential_pipeline_status[0]}" -eq 0 && test "${credential_pipeline_status[1]}" -eq 0 || eryu_credential_creation_failed
   /usr/bin/sudo /usr/bin/systemd-ask-password 'ERYU_AUTH_TOKEN' | /usr/bin/sudo /usr/bin/systemd-creds encrypt --name=ERYU_AUTH_TOKEN - /etc/credstore.encrypted/eryu/ERYU_AUTH_TOKEN.cred
   credential_pipeline_status=("${PIPESTATUS[@]}")
   test "${credential_pipeline_status[0]}" -eq 0 && test "${credential_pipeline_status[1]}" -eq 0 || eryu_credential_creation_failed
   /usr/bin/openssl rand -base64 48 | /usr/bin/sudo /usr/bin/systemd-creds encrypt --name=ERYU_MCP_READ_TOKEN - /etc/credstore.encrypted/eryu/ERYU_MCP_READ_TOKEN.cred
   credential_pipeline_status=("${PIPESTATUS[@]}")
   test "${credential_pipeline_status[0]}" -eq 0 && test "${credential_pipeline_status[1]}" -eq 0 || eryu_credential_creation_failed
   unset credential_pipeline_status
   /usr/bin/sudo /opt/eryu/current/deploy/create-caddy-basic-auth-credential.sh || eryu_credential_creation_failed
   /usr/bin/sudo /usr/bin/find /etc/credstore.encrypted/eryu -maxdepth 1 -type f -printf '%f\n' || eryu_credential_creation_failed
   unset -f eryu_credential_creation_failed
   ```

   `ERYU_AUTH_TOKEN` 必须与只读 token 不同。部署后只从密码管理器把它输入
   网页的密码框；网页不会把它保存到 `localStorage`，刷新页面后需要重新输入。

4. 只有 Caddy 升级与 Shared Diary 回归已经单独通过后，才安装 Eryu 的无秘密
   unit/helper 模板和最小 Caddy credential drop-in。公开 issuer 已通过 OIDC
   discovery 验证并固定在模板中。

   ```bash
   # 在 VPS 的 SSH 终端运行；install 成功时无输出。
   eryu_template_install_failed() {
     printf '%s\n' 'eryu_template_install=failed' >&2
     exit 1
   }
   for install_target in \
     /usr/local/libexec/eryu-run-with-credentials \
     /etc/systemd/system/eryu-web.service \
     /etc/systemd/system/eryu-mcp.service \
     /etc/systemd/system/caddy.service.d/eryu-credentials.conf \
     /etc/eryu/auth0-public.conf; do
     /usr/bin/sudo /usr/bin/test ! -e "$install_target" || eryu_template_install_failed
     /usr/bin/sudo /usr/bin/test ! -L "$install_target" || eryu_template_install_failed
   done
   unset install_target
   /usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/install -o root -g root -m 0755 /opt/eryu/current/deploy/run-with-credentials.sh /usr/local/libexec/eryu-run-with-credentials || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/eryu-web.service /etc/systemd/system/eryu-web.service || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/eryu-mcp.service /etc/systemd/system/eryu-mcp.service || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 /etc/systemd/system/caddy.service.d || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/caddy-eryu-credentials.conf /etc/systemd/system/caddy.service.d/eryu-credentials.conf || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 /etc/eryu || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/auth0-public.conf.example /etc/eryu/auth0-public.conf || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/grep -Fxq 'AUTH0_ISSUER_URL=https://dev-k1463twcjjecqewp.us.auth0.com/' /etc/eryu/auth0-public.conf || eryu_template_install_failed
   /usr/bin/sudo /usr/bin/systemd-analyze verify /etc/systemd/system/eryu-web.service /etc/systemd/system/eryu-mcp.service caddy.service || eryu_template_install_failed
   unset -f eryu_template_install_failed
   ```

   `grep -Fxq` 成功时没有输出；非 0 就停止。该公开文件只能包含已验证的
   `AUTH0_ISSUER_URL`，不要加入 token、`MUSIC_U` 或 client secret。

5. 在加入 Eryu 路由前再次确认 Caddy 升级门已完成：当前精确版本和二进制
   SHA 等于维护窗口批准值；Shared Diary 回归通过；根 Caddyfile 已有且只有
   一个最前面的 global options block，其中原生启用 `persist_config off`；
   当前配置由升级后的二进制验证成功。然后执行 `daemon-reload`，并在**单独
   批准的维护门**中 restart Caddy 一次，使第 4 步新增的 encrypted credential
   真正进入这次 service activation。此时仍未加入 Eryu route；restart 后必须
   再完成 Shared Diary 回归并停下报告。只做 reload 不能替代这次 activation。
   目标值来自 `CADDY-UPGRADE.md` 的现场定案，不能在这里猜。
   代码块开头要求输入的两条 approved record 必须来自第 4 节 root-only manifest
   与维护审批记录，不能从本次 `daemon-reload` 后的 live 输出反填。其中
   `LoadCredentialEncrypted` 记录只能包含这一条 Eryu Basic Auth credential；
   `DropInPaths` 必须是升级后已批准的完整集合再加这一条 Eryu drop-in。
   restart 前还必须完成该文档的历史 `--environ`/journal 门：任何潜在秘密名称
   命中或 provenance 为 `unknown` 都要停止，并在另行批准的事件处置中轮换
   受影响秘密；不得打印匹配行、自动清理 journal 或继续 restart。

   ```bash
   # 在 VPS SSH 终端运行；检查只返回退出码/固定分类，不显示配置内容或 argv。
   # 先关闭可能从调用者继承的 xtrace；后续不得在本代码块中重新开启。
   set +x
   caddy_activation_failed() {
     printf '%s\n' 'caddy_credential_activation=failed' >&2
     exit 1
   }
   test -z "${DBUS_SYSTEM_BUS_ADDRESS+x}" || caddy_activation_failed
   read -r -p 'Approved exact post-Eryu DropInPaths record: ' caddy_approved_drop_in_paths || caddy_activation_failed
   test -n "$caddy_approved_drop_in_paths" || caddy_activation_failed
   case " $caddy_approved_drop_in_paths " in
     *" /etc/systemd/system/caddy.service.d/eryu-credentials.conf "*) ;;
     *) caddy_activation_failed ;;
   esac
   test "$(command -v caddy)" = /usr/bin/caddy || caddy_activation_failed
   test "$(command -v busctl)" = /usr/bin/busctl || caddy_activation_failed
   test -x /usr/bin/caddy || caddy_activation_failed
   test -x /usr/bin/busctl || caddy_activation_failed
   systemd_version_record="$(/usr/bin/systemd --version)" || caddy_activation_failed
   [[ "$systemd_version_record" =~ ^systemd[[:space:]]+([0-9]+) ]] || caddy_activation_failed
   systemd_major=${BASH_REMATCH[1]}
   test "$systemd_major" -ge 254 || caddy_activation_failed
   unset systemd_version_record systemd_major
   test "$(/usr/bin/sudo /usr/bin/grep -Ec '^[[:space:]]*persist_config[[:space:]]+off[[:space:]]*$' /etc/caddy/Caddyfile)" = 1 || caddy_activation_failed
   test "$(/usr/bin/sudo /usr/bin/grep -Ec '^[[:space:]]*admin[[:space:]]+unix//run/caddy/admin\.sock\|0600[[:space:]]*$' /etc/caddy/Caddyfile)" = 1 || caddy_activation_failed
   if ! caddy_exec_start_record="$(
     /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecStart --value 2> /dev/null
   )"; then
     printf '%s\n' 'Caddy ExecStart classification failed' >&2
     exit 1
   fi
   exec_start_remainder=${caddy_exec_start_record#* path=}
   if test "$exec_start_remainder" = "$caddy_exec_start_record" ||
     [[ "$exec_start_remainder" == *" path="* ]]; then
     printf '%s\n' 'Caddy ExecStart is not one direct record' >&2
     exit 1
   fi
   case "$caddy_exec_start_record" in
     "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy run --config /etc/caddy/Caddyfile ; ignore_errors=no ; "*)
       printf '%s\n' 'caddy_exec_start=approved_post_upgrade_form'
       ;;
     *)
       printf '%s\n' 'Caddy ExecStart is not the approved post-upgrade form' >&2
       exit 1
       ;;
   esac
   caddy_pre_reload_exec_start_record=$caddy_exec_start_record
   unset caddy_exec_start_record exec_start_remainder
   if ! caddy_exec_start_ex_record="$(
     /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecStartEx --value 2> /dev/null
   )"; then
     printf '%s\n' 'Caddy ExecStartEx classification failed' >&2
     exit 1
   fi
   exec_start_ex_remainder=${caddy_exec_start_ex_record#* path=}
   if test "$exec_start_ex_remainder" = "$caddy_exec_start_ex_record" ||
     [[ "$exec_start_ex_remainder" == *" path="* ]]; then
     printf '%s\n' 'Caddy ExecStartEx is not one direct record' >&2
     exit 1
   fi
   case "$caddy_exec_start_ex_record" in
     "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy run --config /etc/caddy/Caddyfile ; flags= ; "*)
       printf '%s\n' 'caddy_exec_start_ex=empty_flags'
       ;;
     *)
       printf '%s\n' 'Caddy ExecStartEx has unapproved flags or form' >&2
       exit 1
       ;;
   esac
   caddy_pre_reload_exec_start_ex_record=$caddy_exec_start_ex_record
   unset caddy_exec_start_ex_record exec_start_ex_remainder
   if ! caddy_exec_reload_record="$(
     /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecReload --value 2> /dev/null
   )"; then
     printf '%s\n' 'Caddy ExecReload classification failed' >&2
     exit 1
   fi
   exec_reload_remainder=${caddy_exec_reload_record#* path=}
   if test "$exec_reload_remainder" = "$caddy_exec_reload_record" ||
     [[ "$exec_reload_remainder" == *" path="* ]]; then
     printf '%s\n' 'Caddy ExecReload is not one direct record' >&2
     exit 1
   fi
   case "$caddy_exec_reload_record" in
     "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force --address unix//run/caddy/admin.sock ; ignore_errors=no ; "*)
       printf '%s\n' 'caddy_exec_reload=approved_post_upgrade_form'
       ;;
     *)
       printf '%s\n' 'Caddy ExecReload is not the approved post-upgrade form' >&2
       exit 1
       ;;
   esac
   caddy_pre_reload_exec_reload_record=$caddy_exec_reload_record
   unset caddy_exec_reload_record exec_reload_remainder
   if ! caddy_exec_reload_ex_record="$(
     /usr/bin/sudo /usr/bin/systemctl show caddy.service --property=ExecReloadEx --value 2> /dev/null
   )"; then
     printf '%s\n' 'Caddy ExecReloadEx classification failed' >&2
     exit 1
   fi
   exec_reload_ex_remainder=${caddy_exec_reload_ex_record#* path=}
   if test "$exec_reload_ex_remainder" = "$caddy_exec_reload_ex_record" ||
     [[ "$exec_reload_ex_remainder" == *" path="* ]]; then
     printf '%s\n' 'Caddy ExecReloadEx is not one direct record' >&2
     exit 1
   fi
   case "$caddy_exec_reload_ex_record" in
     "{ path=/usr/bin/caddy ; argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force --address unix//run/caddy/admin.sock ; flags= ; "*)
       printf '%s\n' 'caddy_exec_reload_ex=empty_flags'
       ;;
     *)
       printf '%s\n' 'Caddy ExecReloadEx has unapproved flags or form' >&2
       exit 1
       ;;
   esac
   caddy_pre_reload_exec_reload_ex_record=$caddy_exec_reload_ex_record
   unset caddy_exec_reload_ex_record exec_reload_ex_remainder
   caddy_lifecycle_hooks=absent
   for hook_property in \
     ExecCondition ExecConditionEx ExecStartPre ExecStartPreEx \
     ExecStartPost ExecStartPostEx ExecStop ExecStopEx \
     ExecStopPost ExecStopPostEx; do
     if ! hook_property_value="$(
       /usr/bin/sudo /usr/bin/systemctl show caddy.service \
         --property="$hook_property" --value 2> /dev/null
     )"; then
       printf '%s\n' 'Caddy lifecycle hook classification failed' >&2
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
   /usr/bin/sudo /usr/bin/systemd-run --quiet --wait --pipe --collect \
     --unit=eryu-caddy-base-validate \
     --property=Type=oneshot \
     --property=User=caddy \
     --property=Group=caddy \
     /usr/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile || caddy_activation_failed
   /usr/bin/sudo /usr/bin/systemctl daemon-reload || caddy_activation_failed
   caddy_effective_user="$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=User --value 2> /dev/null)" || caddy_activation_failed
   caddy_effective_group="$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=Group --value 2> /dev/null)" || caddy_activation_failed
   caddy_effective_need_daemon_reload="$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=NeedDaemonReload --value 2> /dev/null)" || caddy_activation_failed
   caddy_effective_drop_in_paths="$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=DropInPaths --value 2> /dev/null)" || caddy_activation_failed
   test "$caddy_effective_user" = caddy || caddy_activation_failed
   test "$caddy_effective_group" = caddy || caddy_activation_failed
   test "$caddy_effective_need_daemon_reload" = no || caddy_activation_failed
   test "$caddy_effective_drop_in_paths" = "$caddy_approved_drop_in_paths" || caddy_activation_failed
   unset caddy_effective_user caddy_effective_group caddy_effective_need_daemon_reload
   unset caddy_effective_drop_in_paths
   caddy_expect_bus_property() {
     local property_name=$1
     local expected_record=$2
     local effective_record
     effective_record="$(
       /usr/bin/busctl --system get-property \
         org.freedesktop.systemd1 \
         /org/freedesktop/systemd1/unit/caddy_2eservice \
         org.freedesktop.systemd1.Service \
         "$property_name" 2> /dev/null
     )" || caddy_activation_failed
     test "$effective_record" = "$expected_record" || caddy_activation_failed
     unset effective_record
   }
   caddy_expect_bus_property Environment 'as 0'
   caddy_expect_bus_property EnvironmentFiles 'a(sb) 0'
   caddy_expect_bus_property PassEnvironment 'as 0'
   caddy_expect_bus_property LoadCredential 'a(ss) 0'
   caddy_expect_bus_property ImportCredential 'as 0'
   caddy_expect_bus_property SetCredential 'a(say) 0'
   caddy_expect_bus_property SetCredentialEncrypted 'a(say) 0'
   caddy_expect_bus_property LoadCredentialEncrypted \
     'a(ss) 1 "ERYU_BASIC_AUTH_ENTRY" "/etc/credstore.encrypted/eryu/ERYU_BASIC_AUTH_ENTRY.cred"'
   printf '%s\n' 'caddy_runtime_inputs=one_approved_encrypted_credential'
   for effective_exec_property in ExecStart ExecStartEx ExecReload ExecReloadEx; do
     effective_exec_record="$(
       /usr/bin/sudo /usr/bin/systemctl show caddy.service \
         --property="$effective_exec_property" --value 2> /dev/null
     )" || caddy_activation_failed
     case "$effective_exec_property" in
       ExecStart) expected_exec_record=$caddy_pre_reload_exec_start_record ;;
       ExecStartEx) expected_exec_record=$caddy_pre_reload_exec_start_ex_record ;;
       ExecReload) expected_exec_record=$caddy_pre_reload_exec_reload_record ;;
       ExecReloadEx) expected_exec_record=$caddy_pre_reload_exec_reload_ex_record ;;
       *) caddy_activation_failed ;;
     esac
     test "$effective_exec_record" = "$expected_exec_record" || caddy_activation_failed
     unset effective_exec_record expected_exec_record
   done
   unset effective_exec_property
   for hook_property in \
     ExecCondition ExecConditionEx ExecStartPre ExecStartPreEx \
     ExecStartPost ExecStartPostEx ExecStop ExecStopEx \
     ExecStopPost ExecStopPostEx; do
     post_reload_hook_value="$(
       /usr/bin/sudo /usr/bin/systemctl show caddy.service \
         --property="$hook_property" --value 2> /dev/null
     )" || caddy_activation_failed
     test -z "$post_reload_hook_value" || caddy_activation_failed
     unset post_reload_hook_value
   done
   unset hook_property
   /usr/bin/sudo /usr/bin/systemd-analyze verify caddy.service || caddy_activation_failed
   # 下面的 restart 必须在取得该维护门的单独批准后才运行。
   /usr/bin/sudo /usr/bin/systemctl restart caddy.service || caddy_activation_failed
   /usr/bin/sudo /usr/bin/systemctl is-active --quiet caddy.service || caddy_activation_failed
   test "$(/usr/bin/sudo /usr/bin/systemctl show caddy.service --property=MainPID --value)" -gt 0 || caddy_activation_failed
   /usr/bin/sudo /usr/bin/test -S /run/caddy/admin.sock || caddy_activation_failed
   /usr/bin/sudo /usr/bin/test -d /run/caddy || caddy_activation_failed
   /usr/bin/sudo /usr/bin/test ! -L /run/caddy || caddy_activation_failed
   test "$(/usr/bin/sudo /usr/bin/stat -c '%U:%G' /run/caddy)" = caddy:caddy || caddy_activation_failed
   caddy_runtime_mode="$(/usr/bin/sudo /usr/bin/stat -c '%a' /run/caddy)" || caddy_activation_failed
   [[ "$caddy_runtime_mode" =~ ^[0-7]{3,4}$ ]] || caddy_activation_failed
   test "$((8#$caddy_runtime_mode & 0022))" -eq 0 || caddy_activation_failed
   unset caddy_runtime_mode
   test "$(/usr/bin/sudo /usr/bin/stat -c '%U:%G:%a' /run/caddy/admin.sock)" = caddy:caddy:600 || caddy_activation_failed
   /usr/bin/sudo /usr/bin/ss -ltnH 'sport = :2019' | /usr/bin/grep . > /dev/null
   admin_port_pipeline_status=("${PIPESTATUS[@]}")
   admin_port_source_status=${admin_port_pipeline_status[0]}
   admin_port_match_status=${admin_port_pipeline_status[1]}
   test "$admin_port_source_status" -eq 0 || {
     printf '%s\n' 'Caddy Admin TCP listener check failed' >&2
     exit 1
   }
   if test "$admin_port_match_status" -eq 0; then
     printf '%s\n' 'unsafe Caddy Admin TCP listener on port 2019' >&2
     exit 1
   elif test "$admin_port_match_status" -eq 1; then
     printf '%s\n' 'caddy_admin_tcp_2019=absent'
   else
     printf '%s\n' 'Caddy Admin TCP listener check failed' >&2
     exit 1
   fi
   unset caddy_approved_drop_in_paths
   unset caddy_pre_reload_exec_start_record caddy_pre_reload_exec_start_ex_record
   unset caddy_pre_reload_exec_reload_record caddy_pre_reload_exec_reload_ex_record
   unset -f caddy_expect_bus_property
   unset -f caddy_activation_failed
   ```

   这些命令通过也不能代替版本、SHA、模块和 restart 前后相同的 Shared Diary
   回归记录。不得读取 credential 内容，不得运行 `caddy adapt`、`caddy environ`
   或访问 Admin API `/config`。失败就停止，不得继续添加 Eryu 路由。

6. 备份共享 Caddyfile，再把 Eryu 片段装到独立文件。下列 `test` 命令要求
   Caddyfile 是普通文件而不是 symlink，且备份名、预期副本、同目录原子候选和
   Eryu 片段都尚不存在；任一失败就停止。先构造并验证“原文件 + 唯一 Eryu
   import 行”的 root-only 预期副本；验证通过后才复制到 `/etc/caddy` 同一文件
   系统并以 `mv -T` 原子替换 live Caddyfile。不会显示共享配置内容。

   ```bash
   # 在 VPS 的同一个 SSH 终端运行；每条成功后再运行下一条。
   caddy_route_install_failed() {
     printf '%s\n' 'eryu_caddy_route_install=failed' >&2
     exit 1
   }
   /usr/bin/sudo /usr/bin/test -d /etc/caddy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -L /etc/caddy || caddy_route_install_failed
   test "$(/usr/bin/sudo /usr/bin/stat -c '%U:%G' /etc/caddy)" = root:root || caddy_route_install_failed
   caddy_dir_mode="$(/usr/bin/sudo /usr/bin/stat -c '%a' /etc/caddy)" || caddy_route_install_failed
   [[ "$caddy_dir_mode" =~ ^[0-7]{3,4}$ ]] || caddy_route_install_failed
   test "$((8#$caddy_dir_mode & 0022))" -eq 0 || caddy_route_install_failed
   unset caddy_dir_mode
   /usr/bin/sudo /usr/bin/test ! -L /var/backups/eryu-deploy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/install -d -o root -g root -m 0700 /var/backups/eryu-deploy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test -d /var/backups/eryu-deploy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -L /var/backups/eryu-deploy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test -f /etc/caddy/Caddyfile || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -L /etc/caddy/Caddyfile || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -e /var/backups/eryu-deploy/Caddyfile.pre-eryu || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -L /var/backups/eryu-deploy/Caddyfile.pre-eryu || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -e /var/backups/eryu-deploy/Caddyfile.expected-eryu || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -L /var/backups/eryu-deploy/Caddyfile.expected-eryu || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -e /etc/caddy/.Caddyfile.eryu-candidate || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -L /etc/caddy/.Caddyfile.eryu-candidate || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/cp --preserve=mode,ownership,timestamps /etc/caddy/Caddyfile /var/backups/eryu-deploy/Caddyfile.pre-eryu || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/cp --preserve=mode,ownership,timestamps /var/backups/eryu-deploy/Caddyfile.pre-eryu /var/backups/eryu-deploy/Caddyfile.expected-eryu || caddy_route_install_failed
   printf '\nimport /etc/caddy/eryu.caddy\n' | /usr/bin/sudo /usr/bin/tee -a /var/backups/eryu-deploy/Caddyfile.expected-eryu > /dev/null || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -e /etc/caddy/eryu.caddy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/test ! -L /etc/caddy/eryu.caddy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/install -o root -g root -m 0644 /opt/eryu/current/deploy/caddy/eryu.caddy /etc/caddy/eryu.caddy || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/cp --preserve=mode,ownership,timestamps /var/backups/eryu-deploy/Caddyfile.expected-eryu /etc/caddy/.Caddyfile.eryu-candidate || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/cmp --silent /var/backups/eryu-deploy/Caddyfile.expected-eryu /etc/caddy/.Caddyfile.eryu-candidate || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/systemd-run --quiet --wait --pipe --collect \
     --unit=eryu-caddy-validate \
     --property=Type=oneshot \
     --property=User=caddy \
     --property=Group=caddy \
     --property=LoadCredentialEncrypted=ERYU_BASIC_AUTH_ENTRY:/etc/credstore.encrypted/eryu/ERYU_BASIC_AUTH_ENTRY.cred \
     /usr/bin/caddy validate --config /etc/caddy/.Caddyfile.eryu-candidate --adapter caddyfile || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/cmp --silent /var/backups/eryu-deploy/Caddyfile.pre-eryu /etc/caddy/Caddyfile || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/mv -T /etc/caddy/.Caddyfile.eryu-candidate /etc/caddy/Caddyfile || caddy_route_install_failed
   /usr/bin/sudo /usr/bin/cmp --silent /var/backups/eryu-deploy/Caddyfile.expected-eryu /etc/caddy/Caddyfile || caddy_route_install_failed
   unset -f caddy_route_install_failed
   ```

   `cmp --silent` 成功时没有输出，且只在 Caddyfile 与预期副本逐字节一致时
   返回 0；任何其他改动都会失败并停止。现有 Shared Diary 内容不会打印，
   也必须完全不变。必须使用上面的临时 systemd 验证进程，让 Caddyfile 只从
   临时凭据目录解析账户条目；不能改回普通 `caddy adapt`。验证必须返回 0，
   才允许进入下一步。

7. 只有在第 5 步的 credential activation restart 与 Shared Diary 回归通过，
   且服务、Caddy 和 Auth0 配置再次确认后，才启动两个 Eryu 服务、验证
   loopback，最后 reload Caddy。这里的服务启动和 reload 都需要分别批准；
   它们不能与 Caddy 二进制升级窗口或 credential activation restart 合并。

   2026-08-14 已只读确认 Auth0 的 OIDC 与 OAuth metadata：issuer 精确匹配，
   PKCE 包含 `S256`、token endpoint auth method 包含 `none`，且两份 metadata
   都明确 `client_id_metadata_document_supported: true`。CIMD discovery 门已
   通过；真实连接仍等待 ChatGPT CIMD app、精确 callback 和该 app 的 per-app
   User-Delegated `music:read`。

   ```bash
   # 这些是最后写入阶段的命令，目前禁止执行。
   eryu_public_cutover_failed() {
     printf '%s\n' 'eryu_public_cutover=failed' >&2
     exit 1
   }
   test "$(command -v curl)" = /usr/bin/curl || eryu_public_cutover_failed
   /usr/bin/sudo /usr/bin/systemctl enable --now eryu-web.service || eryu_public_cutover_failed
   test "$(/usr/bin/curl --fail --silent --show-error http://127.0.0.1:9090/health)" = "ok" || eryu_public_cutover_failed
   /usr/bin/sudo /usr/bin/systemctl enable --now eryu-mcp.service || eryu_public_cutover_failed
   /usr/bin/curl --fail --silent --show-error --output /dev/null http://127.0.0.1:9091/.well-known/oauth-protected-resource || eryu_public_cutover_failed
   /usr/bin/sudo /usr/bin/systemctl reload caddy.service || eryu_public_cutover_failed
   test "$(/usr/bin/sudo /usr/bin/grep -Ec '^[[:space:]]*persist_config[[:space:]]+off[[:space:]]*$' /etc/caddy/Caddyfile)" = 1 || eryu_public_cutover_failed
   test "$(/usr/bin/curl --fail --silent --show-error https://eryu.95.169.17.214.sslip.io/health)" = "ok" || eryu_public_cutover_failed
   test "$(/usr/bin/curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://eryu.95.169.17.214.sslip.io/)" = "401" || eryu_public_cutover_failed
   test "$(/usr/bin/curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://eryu.95.169.17.214.sslip.io/music/presence)" = "401" || eryu_public_cutover_failed
   test "$(/usr/bin/curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://eryu.95.169.17.214.sslip.io/music/file/1.mp3)" = "401" || eryu_public_cutover_failed
   unset -f eryu_public_cutover_failed
   ```

## 部署后验证

部署后应逐项确认：

1. `ss -ltn` 显示 9090/9091 只绑定 `127.0.0.1`。
2. 未提供 Basic Auth 时，网页根路径、静态文件、API 和任意数字 MP3 均返回
   401；`/health` 与带查询参数的 `/health?...` 只返回 `ok`，而 `/health/`
   仍返回 401。
3. 只从密码管理器向浏览器的 HTTPS Basic Auth 对话框输入凭据后，播放器可
   正常打开；再输入完整 API token 后，每两秒上报 presence。不要把 Basic
   Auth 密码放进 curl 参数、shell 历史或自动化日志。
4. 未带 Bearer token 请求 MCP 工具会被拒绝，并返回正确的 protected-resource metadata。
5. Auth0 token 的 issuer、audience、有效期和 `music:read` 都正确时，ChatGPT 才可调用四个只读工具。
6. `journalctl` 中不出现 `MUSIC_U`、token、Authorization header、Basic Auth
   用户名/hash 或 client secret；后端也收不到被 Caddy 删除的
   `Authorization` header。
7. 运行中的 Caddy 精确版本、二进制 SHA-256 和模块清单等于维护窗口批准值；
   根 Caddyfile 只有一个最前面的 global options block，且其中原生启用
   `persist_config off`。升级前持久 autosave 的 mtime/size 不变，也没有因
   Eryu Basic Auth 产生新的明文用户名/hash 配置副本。
8. 不存在远程暂停、切歌或拖动进度工具。

如果任何一步失败，先保留日志和当前状态，不自动重试、回滚、重启或修改 Shared Diary。

## Caddy 回退方案（只列出，需另行批准）

如果后续验证证明新 Caddy 路由有问题，先报告失败证据并取得单独回退批准，
再运行以下命令。它会先把精确备份复制到同目录候选，以 Caddy 用户验证，成功
后才原子替换 live Caddyfile，随后删除已不再被引用的 Eryu 片段并 reload；
不得在部署命令失败后自动执行。

```bash
# 仅在另行批准后于 VPS SSH 终端运行。
caddy_route_rollback_failed() {
  printf '%s\n' 'eryu_caddy_route_rollback=failed' >&2
  exit 1
}
/usr/bin/sudo /usr/bin/test -d /etc/caddy || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/test ! -L /etc/caddy || caddy_route_rollback_failed
test "$(/usr/bin/sudo /usr/bin/stat -c '%U:%G' /etc/caddy)" = root:root || caddy_route_rollback_failed
caddy_dir_mode="$(/usr/bin/sudo /usr/bin/stat -c '%a' /etc/caddy)" || caddy_route_rollback_failed
[[ "$caddy_dir_mode" =~ ^[0-7]{3,4}$ ]] || caddy_route_rollback_failed
test "$((8#$caddy_dir_mode & 0022))" -eq 0 || caddy_route_rollback_failed
unset caddy_dir_mode
/usr/bin/sudo /usr/bin/test -f /var/backups/eryu-deploy/Caddyfile.pre-eryu || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/test ! -L /var/backups/eryu-deploy/Caddyfile.pre-eryu || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/test -f /var/backups/eryu-deploy/Caddyfile.expected-eryu || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/test ! -L /var/backups/eryu-deploy/Caddyfile.expected-eryu || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/test ! -e /etc/caddy/.Caddyfile.eryu-rollback || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/test ! -L /etc/caddy/.Caddyfile.eryu-rollback || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/cp --preserve=mode,ownership,timestamps /var/backups/eryu-deploy/Caddyfile.pre-eryu /etc/caddy/.Caddyfile.eryu-rollback || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/systemd-run --quiet --wait --pipe --collect \
  --unit=eryu-caddy-rollback-validate \
  --property=Type=oneshot \
  --property=User=caddy \
  --property=Group=caddy \
  /usr/bin/caddy validate --config /etc/caddy/.Caddyfile.eryu-rollback --adapter caddyfile || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/cmp --silent /var/backups/eryu-deploy/Caddyfile.expected-eryu /etc/caddy/Caddyfile || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/mv -T /etc/caddy/.Caddyfile.eryu-rollback /etc/caddy/Caddyfile || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/cmp --silent /var/backups/eryu-deploy/Caddyfile.pre-eryu /etc/caddy/Caddyfile || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/rm -f /etc/caddy/eryu.caddy || caddy_route_rollback_failed
/usr/bin/sudo /usr/bin/systemctl reload caddy.service || caddy_route_rollback_failed
unset -f caddy_route_rollback_failed
```

上面的路由回退只撤销 Eryu import，保留已经完成并验证过的 Caddy 升级、根
global block 中的 `persist_config off`、credential drop-in 和加密凭据。是否
移除 credential drop-in/加密凭据必须另行确认；不得顺带改变 Shared Diary。

如果问题来自 Caddy 新二进制或 Shared Diary 兼容性，而不是 Eryu 路由，则不
使用上面的路由回退命令。应按 [`CADDY-UPGRADE.md`](CADDY-UPGRADE.md) 中的
二进制回滚门，恢复升级前的精确 package/二进制、unit/drop-in 与 Caddyfile，
用旧二进制验证后再经单独批准 restart。两类回退都不得自动执行。
