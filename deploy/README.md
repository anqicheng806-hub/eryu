# Eryu 私人测试部署草案（尚未执行）

这套模板把网页播放器和远程 MCP 分开：

- `https://eryu.95.169.17.214.sslip.io` -> Caddy -> `127.0.0.1:9090`
- ChatGPT 连接端点：`https://eryu-mcp.95.169.17.214.sslip.io/mcp` -> Caddy -> `127.0.0.1:9091/mcp`
- OAuth canonical resource / Audience：`https://eryu-mcp.95.169.17.214.sslip.io`

Python 服务不直接监听公网，VPS 防火墙也不需要开放 9090/9091。现有 Shared Diary 的域名、Caddy 路由和服务不在这些模板的修改范围内。

“私人测试”表示只供本人使用，不表示网络隔离：两个 `sslip.io` 域名都可在
公网解析。MCP `/mcp` 由 Auth0 的 `music:read` 保护。网页域名除精确的
`/health` 外，静态页面、全部 API、JS/CSS 和所有音频缓存路径都先经过 Caddy
Basic Auth；API 在通过 Basic Auth 后仍需原有完整 token。公开 `/health` 由
Caddy 直接返回纯文本 `ok`，不转发后端，也不返回版本、路径或运行状态。
现场 Caddy 是 2.6.2，因此片段使用该版本的指令名 `basicauth`；2.8 以后才
更名为 `basic_auth`，部署时不能自行替换。

默认部署会创建全新的 `/var/lib/eryu`。本地 `server/data`、音乐缓存、cookie、token 和日志都不会上传；如果以后需要迁移本地音乐数据，必须作为单独阶段再次确认。

## 文件用途

- `systemd/eryu-web.service`：网页与后端服务，只能写 `/var/lib/eryu`。
- `systemd/eryu-mcp.service`：只读 MCP 服务，不接收完整控制 token 或 `MUSIC_U`。
- `systemd/caddy-eryu-credentials.conf`：Caddy drop-in，只加载加密 Basic Auth
  条目，并把 Caddy 2.6.2 的配置 autosave 明确丢弃到 `/dev/null`。
- `systemd/auth0-public.conf.example`：只保存公开的 Auth0 issuer URL 示例，不允许放 secret。
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
stdout、普通文件或 Caddy 配置。Caddy 2.6.2 会在内存配置中展开用户名和 hash；
模板用 systemd 只读运行时 credential 注入账户条目，并在每次 Caddy 启动前把
该版本固定的 autosave 路径建成指向 `/dev/null` 的符号链接，因此不会生成含
用户名/hash 的 autosave JSON。本模板不会把这四项秘密写入普通持久文件；
持久凭据副本只使用加密 `.cred`。运行时只有
systemd 在 `/run/credentials/...` 提供的受限凭据副本和 Caddy 进程内存。
这是针对已审计 2.6.2 `os.WriteFile` 行为的版本固定措施；Caddy 升级前必须重新
审计，不能直接沿用。`XDG_DATA_HOME` 不变，所以现有 TLS 证书和 ACME 数据不会
搬迁。不得运行 `caddy adapt` 或读取 Admin API `/config`，它们会输出展开后的
用户名/hash。

## 待确认后才执行的命令顺序

以下命令只是部署方案，目前没有运行。每一步都应先检查上一条结果，再继续下一条。

0. 先只读确认现场前提。任何输出与预期不一致都必须停止并重新拟定方案。

   ```bash
   # 在 VPS 的 SSH 终端运行；这些命令只读，不显示 Caddyfile 或任何凭据内容。
   /usr/bin/caddy version
   systemd --version | head -n 1
   findmnt -no TARGET,FSTYPE /run
   command -v caddy install ln
   sudo systemctl show caddy.service --property=User --property=Group --property=ExecStart --property=ExecStartPre --property=RuntimeDirectory --property=DropInPaths --no-pager
   ```

   当前方案只适用于已审计的 Caddy `v2.6.2`、systemd 247+、`/run` 为 tmpfs、
   `caddy`、`install` 与 `ln` 分别位于 `/usr/bin/caddy`、`/usr/bin/install`、
   `/usr/bin/ln`，
   Caddy 用户/组均为 `caddy`，且 `ExecStart` 从 `/etc/caddy/Caddyfile` 启动并且
   不含 `--resume`。还必须人工确认当前活动配置没有只存在于 Admin API、却未
   写回 Caddyfile 的动态改动；否则 restart 会丢失它们。不要输出 Admin API
   配置来做这项确认。

1. 安装运行依赖并建立两个低权限系统用户。

   ```bash
   # 在 VPS 的 SSH 终端运行；成功时 apt 与 useradd 均返回 0。
   sudo apt-get update
   sudo apt-get install --no-install-recommends python3-venv ffmpeg
   sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin eryu-web
   sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin eryu-mcp
   ```

2. 在分支已经推送并再次获批后，把代码安装到 `/opt/eryu/current`，创建独立虚拟环境。实际部署时会固定到已测试的 commit，不使用浮动的 `main`。

   ```bash
   # 在 VPS 的 SSH 终端运行；最后应显示预先确认的 commit SHA。
   sudo install -d -o root -g root -m 0755 /opt/eryu
   sudo git clone --branch feature/music-presence-mcp --single-branch https://github.com/sebastianevan200-stack/eryu /opt/eryu/current
   sudo python3 -m venv /opt/eryu/venv
   sudo /opt/eryu/venv/bin/python -m pip install /opt/eryu/current/mcp_server
   sudo /opt/eryu/venv/bin/python -m pip install --only-binary=:all: --progress-bar off -r /opt/eryu/current/server/requirements-analysis.txt
   sudo /opt/eryu/venv/bin/python -m pip check
   git -C /opt/eryu/current rev-parse HEAD
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
   sudo install -d -o root -g root -m 0700 /etc/credstore.encrypted/eryu
   sudo systemd-ask-password 'MUSIC_U' | sudo systemd-creds encrypt --name=MUSIC_U - /etc/credstore.encrypted/eryu/MUSIC_U.cred
   sudo systemd-ask-password 'ERYU_AUTH_TOKEN' | sudo systemd-creds encrypt --name=ERYU_AUTH_TOKEN - /etc/credstore.encrypted/eryu/ERYU_AUTH_TOKEN.cred
   openssl rand -base64 48 | sudo systemd-creds encrypt --name=ERYU_MCP_READ_TOKEN - /etc/credstore.encrypted/eryu/ERYU_MCP_READ_TOKEN.cred
   sudo /opt/eryu/current/deploy/create-caddy-basic-auth-credential.sh
   sudo find /etc/credstore.encrypted/eryu -maxdepth 1 -type f -printf '%f\n'
   ```

   `ERYU_AUTH_TOKEN` 必须与只读 token 不同。部署后只从密码管理器把它输入
   网页的密码框；网页不会把它保存到 `localStorage`，刷新页面后需要重新输入。

4. 安装无秘密模板，并只在公开配置文件中填写用户稍后提供的 issuer URL。

   ```bash
   # 在 VPS 的 SSH 终端运行；install 成功时无输出。
   sudo install -d -o root -g root -m 0755 /usr/local/libexec
   sudo install -o root -g root -m 0755 /opt/eryu/current/deploy/run-with-credentials.sh /usr/local/libexec/eryu-run-with-credentials
   sudo install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/eryu-web.service /etc/systemd/system/eryu-web.service
   sudo install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/eryu-mcp.service /etc/systemd/system/eryu-mcp.service
   sudo test ! -e /etc/systemd/system/caddy.service.d/eryu-credentials.conf
   sudo install -d -o root -g root -m 0755 /etc/systemd/system/caddy.service.d
   sudo install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/caddy-eryu-credentials.conf /etc/systemd/system/caddy.service.d/eryu-credentials.conf
   sudo install -d -o root -g root -m 0755 /etc/eryu
   sudo install -o root -g root -m 0644 /opt/eryu/current/deploy/systemd/auth0-public.conf.example /etc/eryu/auth0-public.conf
   sudoedit /etc/eryu/auth0-public.conf
   sudo systemd-analyze verify /etc/systemd/system/eryu-web.service /etc/systemd/system/eryu-mcp.service caddy.service
   ```

   `sudoedit` 后应只出现一项公开值：`AUTH0_ISSUER_URL=https://.../`。不要加入 token、`MUSIC_U` 或 client secret。

5. 在尚未加入 Eryu 路由时，先让现有 Caddy 采用“不保存 autosave”的边界。
   Caddy 2.6.2 在进程启动时固定 autosave 路径，`daemon-reload` 后只做 reload
   仍会把新 hash 写到旧持久路径，因此这里不可避免地需要一次 Caddy restart。
   drop-in 会在新进程启动前建立 `/run/eryu-caddy-config/caddy/autosave.json`
   并使其只指向 `/dev/null`；Caddy 的写入被丢弃，不会留下展开后的 JSON 文件。
   这会短暂影响 Shared Diary，必须作为单独变更再次批准；当前没有批准，也没有执行。

   ```bash
   # 仅在单独批准 Caddy restart 后于 VPS SSH 终端逐条运行。
   # 此时 /etc/caddy/Caddyfile 仍必须是部署前原文件，尚无 Eryu import。
   sudo systemctl daemon-reload
   test "$(/usr/bin/caddy version | awk '{print $1}')" = "v2.6.2"
   sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   sudo systemctl restart caddy.service
   sudo systemctl is-active --quiet caddy.service
   sudo test "$(sudo stat -c '%U:%G:%a' /run/eryu-caddy-config)" = "caddy:caddy:700"
   sudo test -L /run/eryu-caddy-config/caddy/autosave.json
   sudo test "$(sudo readlink /run/eryu-caddy-config/caddy/autosave.json)" = "/dev/null"
   sudo sh -c 'pid=$(systemctl show --property=MainPID --value caddy.service); tr "\0" "\n" < "/proc/$pid/environ" | grep -Fxq "XDG_CONFIG_HOME=/run/eryu-caddy-config"'
   ```

   restart 前后还要用只显示路径、mtime、size 的 `stat` 对比现场原持久
   `autosave.json`，确认它没有变化；路径必须来自步骤 0 的现场只读核对，不能
   猜。不得读取文件内容、运行 `caddy adapt` 或访问 Admin API `/config`，因为
   它们会展开并输出 Basic Auth 用户名/hash。restart 后必须先人工确认现有
   Shared Diary 完全正常；失败就停止，不得继续添加 Eryu 路由。

6. 备份共享 Caddyfile，再把 Eryu 片段装到独立文件。下列 `test` 命令
   要求备份名、预期副本和 Eryu 片段都尚不存在；任一失败就停止，不能覆盖
   旧备份或未知文件。命令会构造“原文件 + 唯一 Eryu import 行”的 root-only
   预期副本，再把同一公开行追加到 Caddyfile；不会显示共享配置内容。

   ```bash
   # 在 VPS 的同一个 SSH 终端运行；每条成功后再运行下一条。
   sudo install -d -o root -g root -m 0700 /var/backups/eryu-deploy
   sudo test ! -e /var/backups/eryu-deploy/Caddyfile.pre-eryu
   sudo test ! -e /var/backups/eryu-deploy/Caddyfile.expected-eryu
   sudo cp --preserve=mode,ownership,timestamps /etc/caddy/Caddyfile /var/backups/eryu-deploy/Caddyfile.pre-eryu
   sudo cp --preserve=mode,ownership,timestamps /var/backups/eryu-deploy/Caddyfile.pre-eryu /var/backups/eryu-deploy/Caddyfile.expected-eryu
   printf '\nimport /etc/caddy/eryu.caddy\n' | sudo tee -a /var/backups/eryu-deploy/Caddyfile.expected-eryu > /dev/null
   sudo test ! -e /etc/caddy/eryu.caddy
   sudo install -o root -g root -m 0644 /opt/eryu/current/deploy/caddy/eryu.caddy /etc/caddy/eryu.caddy
   printf '\nimport /etc/caddy/eryu.caddy\n' | sudo tee -a /etc/caddy/Caddyfile > /dev/null
   sudo cmp --silent /var/backups/eryu-deploy/Caddyfile.expected-eryu /etc/caddy/Caddyfile
   sudo systemd-run --quiet --wait --pipe --collect \
     --unit=eryu-caddy-validate \
     --property=Type=oneshot \
     --property=User=caddy \
     --property=Group=caddy \
     --property=RuntimeDirectory=eryu-caddy-validate \
     --property=RuntimeDirectoryMode=0700 \
     --property=Environment=XDG_CONFIG_HOME=/run/eryu-caddy-validate \
     --property=LoadCredentialEncrypted=ERYU_BASIC_AUTH_ENTRY:/etc/credstore.encrypted/eryu/ERYU_BASIC_AUTH_ENTRY.cred \
     /usr/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   ```

   `cmp --silent` 成功时没有输出，且只在 Caddyfile 与预期副本逐字节一致时
   返回 0；任何其他改动都会失败并停止。现有 Shared Diary 内容不会打印，
   也必须完全不变。必须使用上面的临时 systemd 验证进程，让 Caddyfile 只从
   临时凭据目录解析账户条目；不能改回普通 `caddy adapt`。验证必须返回 0，
   才允许进入下一步。

7. 只有在服务、Caddy 和 Auth0 配置再次确认后，才启动两个 Eryu 服务、验证
   loopback，最后 reload Caddy。这里的 reload 也需要再次批准；它不会重启
   Shared Diary，但前提是步骤 5 的受控 restart 已经成功完成。

   ```bash
   # 这些是最后写入阶段的命令，目前禁止执行。
   sudo systemctl enable --now eryu-web.service
   sudo systemctl enable --now eryu-mcp.service
   test "$(curl --fail --silent --show-error http://127.0.0.1:9090/health)" = "ok"
   curl --fail --silent --show-error http://127.0.0.1:9091/.well-known/oauth-protected-resource
   sudo systemctl reload caddy.service
   sudo test -L /run/eryu-caddy-config/caddy/autosave.json
   sudo test "$(sudo readlink /run/eryu-caddy-config/caddy/autosave.json)" = "/dev/null"
   test "$(curl --fail --silent --show-error https://eryu.95.169.17.214.sslip.io/health)" = "ok"
   test "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://eryu.95.169.17.214.sslip.io/)" = "401"
   test "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://eryu.95.169.17.214.sslip.io/music/presence)" = "401"
   test "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://eryu.95.169.17.214.sslip.io/music/file/1.mp3)" = "401"
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
7. `/run/eryu-caddy-config` 是 tmpfs 上的 `0700` 目录，autosave 路径是只指向
   `/dev/null` 的符号链接，旧持久 autosave 的 mtime/size 不变。
8. 不存在远程暂停、切歌或拖动进度工具。

如果任何一步失败，先保留日志和当前状态，不自动重试、回滚、重启或修改 Shared Diary。

## Caddy 回退方案（只列出，需另行批准）

如果后续验证证明新 Caddy 路由有问题，先报告失败证据并取得单独回退批准，
再运行以下命令。第一段会恢复精确备份、删除本次新增片段、重新验证并 reload；
不得在部署命令失败后自动执行。

```bash
# 仅在另行批准后于 VPS SSH 终端运行。
sudo cp --preserve=mode,ownership,timestamps /var/backups/eryu-deploy/Caddyfile.pre-eryu /etc/caddy/Caddyfile
sudo rm -f /etc/caddy/eryu.caddy
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy.service
```

上面的路由回退会保留“不保存 autosave”的 drop-in 与加密凭据。若还要恢复
Caddy 原先的持久 autosave 行为，必须再单独批准：删除
`/etc/systemd/system/caddy.service.d/eryu-credentials.conf`、执行
`daemon-reload`，并再次 restart Caddy。不能只删 drop-in 后 reload；加密
credential 本体是否删除也必须另行确认。
