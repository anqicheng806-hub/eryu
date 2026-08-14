# Auth0 与 ChatGPT 远程 MCP 配置清单

状态：这是只读核准后的配置说明。尚未访问或修改 Auth0 租户，也尚未在
ChatGPT 创建连接。不要在本文件、截图、聊天、命令参数或普通配置文件中
填写任何秘密。

## 固定地址

三个地址用途不同，不能互换：

| 用途 | 精确值 |
|---|---|
| 网页播放器 | `https://eryu.95.169.17.214.sslip.io` |
| ChatGPT 的 Streamable HTTP MCP URL | `https://eryu-mcp.95.169.17.214.sslip.io/mcp` |
| OAuth Resource、Auth0 API Identifier/Audience | `https://eryu-mcp.95.169.17.214.sslip.io` |
| Protected Resource Metadata | `https://eryu-mcp.95.169.17.214.sslip.io/.well-known/oauth-protected-resource` |

这里使用独立 MCP 域名，因此 canonical resource 采用无尾斜杠的 HTTPS
origin；ChatGPT 会把 metadata 中这个精确值作为 OAuth `resource` 参数，
服务端也会逐字符校验 JWT `aud`。

## 1. Auth0 Tenant Settings

在美国区租户的 **Settings > Advanced > Settings** 中人工核对：

- 打开 **Resource Parameter Compatibility Profile**。
- 打开 **Include Issuer in Authorization Responses**。
- 打开 **Client ID Metadata Document Registration**。
- 保持 **Dynamic Client Registration (DCR)** 关闭。

还要只读检查租户是否仍有旧 Rules，以及 Actions 是否会改写 `scope`。
若存在，不要先改；把名称和行为记录下来再单独评估。

ChatGPT 导入后属于 third-party application。只把实际登录要用的一条
Database、Social 或 Enterprise connection 提升为 domain-level connection；
不要一次开放所有 connection。

## 2. 创建独立 Auth0 API

在 **Applications > APIs > Create API** 中使用：

| 项目 | 值 |
|---|---|
| Name | `Eryu Music Presence MCP API` |
| Identifier | `https://eryu-mcp.95.169.17.214.sslip.io` |
| Signing Algorithm | `RS256` |
| RBAC | 开启 |
| Token Dialect | `rfc9068_profile_authz` |
| API permission/scope | `music:read` |
| Scope description | `Read current playback presence, nearby lyrics, existing analysis, and existing music memory.` |

只创建这一项 Eryu 权限，不复用日记 API、Application、Role 或 scope。
建议创建专属 Role `Eryu Music Reader`，只包含 `music:read`，并只分配给
本次私人测试账号。

在 API 的 **Application Access Policy** 中：

- User-Delegated Access 选择 **Per-app authorization**。
- 不设置 default third-party grant。
- ChatGPT CIMD Application 导入后，只给它 User-Delegated `music:read`。
- 不授予 Client/M2M access，也不创建 `client_credentials` grant。

服务端强制检查本次 token 的标准 `scope` claim 中包含 `music:read`。
即使 `permissions` claim 中有该权限，也不能替代本次客户端实际请求的 scope。

## 3. 创建 ChatGPT Draft app，再导入 CIMD

远程 HTTPS MCP 部署并通过未认证 `401`/metadata 检查后：

1. 在 ChatGPT 的 app management 页面创建 Draft app，MCP URL 填
   `https://eryu-mcp.95.169.17.214.sslip.io/mcp`。
2. 选择 OAuth 和 CIMD。ChatGPT 会为这个 MCP 生成专用的公开 CIMD URL，
   其 client id 形如 `https://chatgpt.com/oauth/.../client.json`。
3. 在 Auth0 **Applications > Applications > Create Application > Import from URL**
   中粘贴该 CIMD URL，先点 **Preview**。
4. Preview 必须显示 third-party public client、Authorization Code、PKCE S256，
   且 token endpoint authentication 可以使用 `none`。
5. 如果 Preview 只允许 `private_key_jwt`，先停下确认 Auth0 套餐与 Preview
   结果。它也不使用 shared client secret，但 Auth0 对该模式有套餐条件，
   不能自行切换或猜测。
6. 导入后，到上一节的 API Application Access 页面，只授予该 CIMD app
   User-Delegated `music:read`。

首选 CIMD 是因为 ChatGPT 和 Auth0 都支持它，而且不需要共享 client secret。
不要为本次测试打开 DCR，也不要另建 `client_secret_post` 或
`client_secret_basic` Application。

## 4. Callback URL

精确 Callback 只有创建 ChatGPT Draft app 后才会显示，格式是：

```text
https://chatgpt.com/connector/oauth/{callback_id}
```

`callback_id` 不能预先推测。Auth0 导入 CIMD 时会读取 `redirect_uris`；在
Preview 和最终 Application 中核对它与 ChatGPT app management 页面逐字符
一致。不要使用 wildcard，不要填播放器地址或 MCP `/mcp` 地址。旧的
`https://chatgpt.com/connector_platform_oauth_redirect` 只用于已发布的旧 app，
本项目不添加。

## 5. Issuer 与运行时公开配置

用户稍后只需提供公开 issuer URL。应以 Auth0
`/.well-known/openid-configuration` 返回的 `issuer` 精确值为准，通常带
尾斜杠，例如：

```text
https://YOUR_TENANT.us.auth0.com/
```

不要根据“美国区”猜租户名，也不要把自定义域 issuer 与
`*.us.auth0.com` 的 issuer/JWKS 混用。运行时只需要：

- 公开的 `AUTH0_ISSUER_URL`；
- 公开且相同的 `MCP_PUBLIC_URL` 与 `AUTH0_AUDIENCE`；
- 固定 `music:read`；
- Auth0 discovery 公布的公开 JWKS。

Eryu MCP 是 resource server，不需要 Auth0 client secret、Management API
token 或 OBO client。ChatGPT 的 Bearer token 也绝不转发给 Eryu 后端。

## 6. 本阶段不需要的 Auth0 设置

- Allowed Web Origins / CORS：ChatGPT 后端完成 token exchange，不填 wildcard。
- Allowed Logout URLs：本阶段不需要。
- M2M / Client Credentials：ChatGPT 不使用，本阶段不创建。
- OBO / Token Vault：MCP 只读本机 Eryu 后端，不向其他 Auth0 API 换票。
- Refresh Token：先以 ChatGPT 生成的 CIMD Preview 为准。只有其中包含
  `refresh_token` 且实际请求 `offline_access` 时，才单独评估 rotation 与
  expiration；私人首测不预先开启。

Auth0/OpenAI 可能同时请求标准 OIDC 身份 scope（例如 `openid`、`email`、
`profile`）。它们不是 Eryu API 权限；本项目自定义 API scope 仍只有
`music:read`。

## 7. 真实验收门槛

本地 fake-JWKS 测试不能代替真实 Auth0。部署后仍需逐项确认：

1. 无 Bearer 请求 `/mcp` 返回 `401`，并指向上面的 metadata URL。
2. metadata 的 resource、issuer 和 `music:read` 正确。
3. 错 issuer、错 audience、过期 token 或缺 scope 都被拒绝。
4. Auth0 登录与 consent 完成后，ChatGPT 能连接 Streamable HTTP MCP。
5. `tools/list` 恰好只有四个只读工具，实际调用成功。
6. 日记项目的 Application、API、Role、scope 和服务完全未改变。

官方依据：

- [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)
- [OpenAI Developer mode](https://developers.openai.com/api/docs/guides/developer-mode)
- [Auth0 MCP authorization quickstart](https://auth0.com/ai/docs/mcp/get-started/authorization-for-your-mcp-server)
- [Auth0 manual CIMD registration](https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application/manual-cimd-registration)
- [Auth0 Resource Parameter Compatibility Profile](https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile)
