# mspbots-fleet-mcp

MCP server for the **MSPbots Fleet Platform API** — a multi-tenant console over
[fleetdm](https://fleetdm.com/): host inventory, on-demand scripts, and saved
osquery queries, exposed to MCP clients.

It follows the same design as the sibling `mspbots-agent-mcp` service:
stateless, no stored credentials, per-request header authentication over the
[Model Context Protocol](https://modelcontextprotocol.io/) (Streamable HTTP/SSE
transport).

## When would you use this

This MCP wraps an internal MSPbots product (device fleet management), not a
third-party integration. Typical uses:

- "What hosts does this tenant have, and which are offline?" →
  `mspbotsfleet_list_hosts`
- "Tag this laptop as Production" → `mspbotsfleet_set_host_labels`
- "This machine looks stale, force a refresh" → `mspbotsfleet_refetch_host`
- "Run our log-collection script on that server" → `mspbotsfleet_run_script`
- "What's the disk usage across all Windows hosts right now?" → create a
  saved query with `mspbotsfleet_create_query`, then
  `mspbotsfleet_run_query`
- "A batch of new laptops just enrolled, claim the ones we declared" →
  `mspbotsfleet_claim_hosts`

## Tools

所有工具的凭证均来自请求头（`X-MSP-Token` / `X-MSP-Tenant-Id` / `X-MSP-Host`），
工具参数里不需要传 token。

### Hosts

主机无法通过接口创建：机器装上 fleetd 并注册后自动出现，再按本租户声明过的标识符
（主机名/序列号/UUID）认领归属，见 `mspbotsfleet_claim_hosts`。

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsfleet_list_hosts` | 列出当前租户的主机（模糊搜索+状态过滤+分页） | `search`、`status`(`online`/`offline`/`new`/`missing`)、`page`(默认0)、`per_page`(默认25，上限200) |
| `mspbotsfleet_get_host` | 获取主机详情+手动标签 | `host_id`(必填) |
| `mspbotsfleet_list_host_scripts` | 列出该主机可执行的脚本（已按租户可见范围过滤） | `host_id`(必填) |
| `mspbotsfleet_set_host_labels` | 全量替换主机的手动标签 | `host_id`(必填)、`labels`(必填，全量替换) |
| `mspbotsfleet_refetch_host` | 请求主机下次签入时刷新属性（异步） | `host_id`(必填) |
| `mspbotsfleet_delete_host` | 从 Fleet 移除主机并清除归属（fleetd 未卸载会再次注册） | `host_id`(必填) |
| `mspbotsfleet_claim_hosts` | 将本租户已声明的标识符与无主主机匹配并接管（幂等） | 无 |

> 主机列表为全量拉取后在内存过滤/排序/分页，结果缓存 15 秒——认领/删除后立即失效。
> 修改主机租户归属（`tenantId`）没有对应工具，只能由平台管理员通过 REST 执行。

### Scripts

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsfleet_list_scripts` | 列出本租户脚本 + 共享库脚本 | `page`、`per_page` |
| `mspbotsfleet_get_script` | 获取脚本详情（含正文） | `script_id`(必填) |
| `mspbotsfleet_create_script` | 新建脚本（文件名须以 `.sh`/`.ps1` 结尾） | `name`(必填)、`contents`(必填，≤500KB)、`shared`(默认false，仅superAdmin可发共享库) |
| `mspbotsfleet_update_script` | 替换脚本正文（文件名不可改） | `script_id`(必填)、`contents`(必填) |
| `mspbotsfleet_delete_script` | 删除脚本（对所有主机生效） | `script_id`(必填) |
| `mspbotsfleet_run_script` | 在主机上执行脚本 | `script_id`(必填)、`host_id`(必填)、`sync`(默认false，true则等待≤60s并带回结果) |
| `mspbotsfleet_get_script_result` | 查询执行结果（`exit_code`为null表示未回传） | `execution_id`(必填) |

> 共享库脚本仅 `superAdmin` 可改/删；其他租户尝试修改/删除会被当作"不存在"处理（404）。

### Saved queries

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsfleet_list_queries` | 列出本租户查询 + 共享库查询 | `page`、`per_page` |
| `mspbotsfleet_get_query` | 获取查询详情（含 SQL） | `query_id`(必填) |
| `mspbotsfleet_create_query` | 新建保存查询（仅允许单条 SELECT / WITH…SELECT） | `name`(必填)、`sql`(必填)、`description`、`platform`(`darwin`/`windows`/`linux`)、`shared`(默认false，仅superAdmin) |
| `mspbotsfleet_update_query` | 更新查询（partial，只传要改的字段） | `query_id`(必填)、`name`、`sql`、`description`、`platform` |
| `mspbotsfleet_delete_query` | 删除查询及其报表数据 | `query_id`(必填) |
| `mspbotsfleet_run_query` | 对主机实时执行查询并等待结果（≤45s） | `query_id`(必填)、`host_ids`(可选，≤50台，省略默认取本租户前50台) |

> SQL 校验规则：只允许单条 `SELECT` 或 `WITH … SELECT`，末尾分号自动去除，正文中出现 `;`
> 直接拒绝。这些校验由后端执行，本服务不重复校验。
> 共享库查询仅 `superAdmin` 可改/删。

## Quick Start

### Docker (recommended)

```bash
docker compose up --build
```

The server starts on `http://localhost:8080`.

### Local (uv)

```bash
uv sync
python -m mspbots_fleet_mcp
```

## Health Check

```bash
curl http://localhost:8080/health
# {"status": "ok"}
```

No credentials are required for the health endpoint.

## 授权参数说明 (Authentication)

Every request to `/mcp` must include the following HTTP headers (provided by the
MCP caller — kept consistent with `mspbots-agent-mcp` / `ticketqa-mcp`):

| Header | 类型 | 是否必填 | 字段描述 | Example |
|---|---|---|---|---|
| `X-MSP-Token` | string | 必填 | Fleet Platform 已签发的访问凭证 (JWT bearer token)。本服务原样转发为下游请求的 `Authorization: Bearer <token>`。 | `X-MSP-Token: <jwt-bearer-token>` |
| `X-MSP-Tenant-Id` | string | 必填 | 租户标识。转发给下游 API 时改名为 `X_Tenant_ID` header(租户也已内嵌在 JWT 中)。 | `X-MSP-Tenant-Id: <tenant-id>` |
| `X-MSP-Host` | string | 必填 | Fleet API 所在的 host。 | `X-MSP-Host: https://agent.mspbots.ai` |

Missing any of the three headers returns `401 Unauthorized`.

> 下游 Fleet API 本身对鉴权失败（缺 token/签名错误/角色不足）统一返回 **403**，
> 不是 401——401 只发生在本服务这一层（缺 gateway header）。

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MCP_HTTP_PORT` | `8080` | Listening port |
| `MCP_HTTP_HOST` | `0.0.0.0` | Listening host |

## MCP Endpoint

```
POST http://localhost:8080/mcp
```

Connect your MCP client with:
- Transport: `http` (Streamable HTTP / SSE)
- Headers: `X-MSP-Token`, `X-MSP-Tenant-Id`, `X-MSP-Host` (all required)

## 测试示例 (Test Example)

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-MSP-Token: <token>" \
  -H "X-MSP-Tenant-Id: <tenant-id>" \
  -H "X-MSP-Host: https://agent.mspbots.ai" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": { "name": "mspbotsfleet_list_hosts", "arguments": { "status": "online", "per_page": 10 } }
  }'
```

> ⚠️ 本仓库为公开仓库，请勿在任何提交的文件中写入真实的 token / tenant id 等敏感信息，
> 上面的 `<token>` / `<tenant-id>` 仅为占位符。
