# mspbots-rmm-mcp

MCP server for the **MSPbots RMM Control API** (`mb-platform-rmm`) — a
vendor-agnostic remote monitoring and management console: device
inventory, script management, and remote execution, proxied through a
vendor adapter so the same endpoints work whichever RMM a tenant has
connected.

It follows the same design as the sibling `mspbots-agent-mcp` service:
stateless, no stored credentials, per-request header authentication over the
[Model Context Protocol](https://modelcontextprotocol.io/) (Streamable HTTP/SSE
transport).

> **Renamed from `mspbots-fleet-mcp`.** The backend product was renamed
> `mb-platform-fleet` → `mb-platform-rmm` to reflect that it now proxies
> multiple RMM vendors, not just fleetdm. This is a breaking change, not a
> relabeling — see **Migration notes** below.

## When would you use this

This MCP wraps an internal MSPbots product (device/RMM management), not a
third-party integration. Typical uses:

- "What devices does this tenant have, and which are offline?" →
  `mspbotsrmm_list_devices`
- "This machine looks stale, force a refresh" → `mspbotsrmm_refetch_device`
- "Run our log-collection script on that server" →
  `mspbotsrmm_run_script` (dry-run first, then confirm)
- "Did that script run finish? What was the output?" →
  `mspbotsrmm_list_script_runs`

## Tools

所有工具的凭证均来自请求头（`X-MSP-Token` / `X-MSP-Tenant-Id` / `X-MSP-Host`），
工具参数里不需要传 token。除以下列出的参数外，**每个工具都额外接受一个可选的
`connection`**（RMM connection UUID）——租户只连了一个 RMM 时可省略，连了多个才
需要显式传。

### Devices（只读）

设备无法通过接口创建/删除：机器装上连接的 RMM 客户端并注册后自动出现，被上游移除
后自动消失。

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsrmm_list_devices` | 列出当前租户的设备（模糊搜索+状态/平台过滤+分页） | `search`、`status`(`online`/`offline`/`missing`)、`platform`、`page`(默认0)、`per_page`(默认25，上限200) |
| `mspbotsrmm_get_device` | 获取设备详情：硬件标识符(`identifiers`)、厂商专属属性(`attributes`)、该连接支持的能力列表(`capabilities`) | `id`(必填) |
| `mspbotsrmm_refetch_device` | 请求设备下次签入时刷新属性（异步，非全部厂商支持） | `id`(必填) |

### Scripts

脚本存于所连接的 RMM 里，不在本应用本地——每次写操作都是直通，没有本地副本，改动
对直接使用该 RMM 的人立即可见。**部分厂商的脚本库是只读的**，create/update/delete
在不支持时会返回永久性错误（见下方"能力网关"）。

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsrmm_list_scripts` | 列出本租户脚本库，附带每个脚本最近一次执行结果 | `page`、`per_page` |
| `mspbotsrmm_get_script` | 获取脚本详情（含正文） | `id`(必填) |
| `mspbotsrmm_create_script` | 新建脚本。文件名的扩展名决定解释器并按厂商规则校验（如 Fleet：`.sh`可选shell shebang、`.py`必须有`#!/usr/bin/env python3`、`.ps1`不能有shebang）。**非幂等**——重复调用会建出多个脚本 | `name`(必填)、`contents`(必填) |
| `mspbotsrmm_update_script` | 整篇替换脚本名称+正文（非局部patch，必须传完整内容） | `id`(必填)、`name`(必填)、`contents`(必填) |
| `mspbotsrmm_delete_script` | 永久删除脚本（对所有设备生效，不可撤销） | `id`(必填) |

### Execution & history

执行是异步、拉取式的：RMM 把任务交给客户端在下次签入时执行，结果不会在派发的响应
里，要通过 `list_script_runs` 轮询。

| Tool | 功能 | 参数 |
|---|---|---|
| `mspbotsrmm_run_script` | 在一台或多台设备上执行脚本。**`confirm` 默认 `false`=只做 dry-run 预览，不会真正派发**；`confirm=true` 才真正执行。派发后立即返回每台设备一个 commandId，不含输出/退出码 | `id`(必填)、`device_ids`(必填，至少1个)、`confirm`(默认false)、`parameters`(可选) |
| `mspbotsrmm_list_script_runs` | 读取某脚本的执行历史（最新在前），含退出码、输出、`matchConfidence`(`exact`\|`heuristic`) | `id`(必填)、`page`(默认0)、`per_page`(默认20，上限200) |

> `run_script` 设备是逐个派发的：如果某台设备中途被拒绝，前面的设备已经在跑了，
> 调用仍会失败——用返回的 commandIds 数量和 `device_ids` 数量做对比来判断是否
> 部分派发成功。
>
> 运行状态：`queued`/`dispatched`/`running`(非终态) → `succeeded`/`failed`/
> `completed_unknown`/`expired`(终态)。`completed_unknown` 是 RMM 确认完成但没
> 返回退出码；`expired` 是 15 分钟内没收到结果（脚本可能其实跑了，只是回执丢了）。

### 能力网关 (Capability gating)

不是所有 RMM 厂商都支持所有操作。`mspbotsrmm_get_device` 返回的 `capabilities`
数组声明了该连接实际支持哪些能力。调用一个当前连接不支持的操作会返回**永久性、
不可重试**的错误（HTTP 501，错误信封里 `code` 为 `not_supported`），并在消息里
点名具体厂商，而不是静默失败或返回空结果。

始终可用：`list_devices`、`get_device`、`list_scripts`、`get_script`、
`run_script`。可能因厂商而异（501）：`refetch_device`、`create_script`、
`update_script`、`delete_script`；`list_script_runs` 在缺少对应能力时仍可工作，
只是跳过与上游的实时核对。

> **`reboot_device` 未注册为工具**：后端路由存在，但截至目前没有任何厂商适配器
> 实现它，每次调用都会返回 501（Fleet 甚至没有重启 API）。等真正有适配器实现后
> 再补上。

## Migration notes（从 `mspbots-fleet-mcp` 迁移）

后端 `mb-platform-fleet` 已改名为 `mb-platform-rmm`，接口做了不兼容改动，本仓库
随之整体改名（仓库/包名/tool前缀 `mspbotsfleet_*` → `mspbotsrmm_*`）。**旧的
`mspbotsfleet_*` 工具会从网关消失**，引用它们的 agent 配置需要同步更新。主要变化：

- Base path：`/apps/mb-platform-fleet/api/fleet` → `/apps/mb-platform-rmm/api/rmm`
- **资源模型**：`hosts`(主机) → `devices`(设备)，新增 `connectionId`/`identifiers[]`/`attributes{}`/`capabilities[]` 字段
- **Saved queries（osquery）概念整体移除**——`mspbotsfleet_*_query`(6个工具)在新API里没有对应功能，vendor-agnostic的抽象层不再支持osquery
- **Host claim/release（认领机制）整体移除**——`mspbotsfleet_claim_hosts`/`release_host`/`set_host_labels`/`delete_host`/`list_host_scripts`(5个工具)及建连时的 `X-Claim-Hosts` header 逻辑都没有了，设备完全靠厂商自身的加入/离开机制管理
- `run_script` 语义改变：旧版单 `host_id`+`sync`标志(同步等待) → 新版 `device_ids[]`批量派发+`confirm`标志(dry-run预览/真实执行)，改为异步拉取式
- `get_script_result`(按execution_id单条查询) → `list_script_runs`(按脚本id查历史列表，`matchConfidence`标注结果匹配可信度)
- 新增 `refetch_device`；`reboot_device` 路由存在但未注册（见上方能力网关说明）
- 错误信封上游格式：`{message, code}` → `{message, detail, code}`（本服务对外暴露的 `{error:{code,message,retryable}}` 信封形状不变，只是内部解析多读了 `detail`），新增 501 状态码（能力网关）

## Quick Start

### Docker (recommended)

```bash
docker compose up --build
```

The server starts on `http://localhost:8080`.

### Local (uv)

```bash
uv sync
python -m mspbots_rmm_mcp
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
| `X-MSP-Token` | string | 必填 | RMM Control API 已签发的访问凭证 (JWT bearer token)。本服务原样转发为下游请求的 `Authorization: Bearer <token>`。 | `X-MSP-Token: <jwt-bearer-token>` |
| `X-MSP-Tenant-Id` | string | 必填 | 租户标识。转发给下游 API 时改名为 `X_Tenant_ID` header(租户也已内嵌在 JWT 中)。 | `X-MSP-Tenant-Id: <tenant-id>` |
| `X-MSP-Host` | string | 必填 | RMM Control API 所在的 host。 | `X-MSP-Host: https://agent.mspbots.ai` |

Missing any of the three required headers returns `401 Unauthorized`.

> 下游 RMM Control API 本身对鉴权失败（缺 token/签名错误/角色不足）统一返回
> **403**，不是 401——401 只发生在本服务这一层（缺 gateway header）。能力不支持
> （厂商没实现该功能）返回 **501**，是永久性错误，不要重试。

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
    "params": { "name": "mspbotsrmm_list_devices", "arguments": { "status": "online", "per_page": 10 } }
  }'
```

> ⚠️ 本仓库为公开仓库，请勿在任何提交的文件中写入真实的 token / tenant id 等敏感信息，
> 上面的 `<token>` / `<tenant-id>` 仅为占位符。

## Known Gaps

- **Not yet live-verified against the new `mb-platform-rmm` backend.** This
  rewrite was built entirely from the interface spec provided for the
  rename (route handlers `service/routes/scripts.ts` /
  `service/routes/devices.ts`), matching the same discipline the previous
  `mspbots-fleet-mcp` build followed for its own API version — schema/tool
  count confirmed via the real MCP protocol (`tools/list`, 10 tools) and 18
  unit tests passing, but no call has yet been exercised against a real
  tenant/token on the new backend.
- **Capability gating is enforced server-side only, not at tool-registration
  time.** The upstream doc suggests registering only the tools a
  connection's live capability list supports — structurally not possible
  here, since tool registration happens once at process startup (before
  any tenant's credentials exist), while capabilities are per-connection
  and only knowable per-request. All 10 tools are always registered; an
  unsupported call surfaces as the same 501 `not_supported` error envelope
  every other error goes through, naming the vendor.
- **`GET /vendors`** (per-vendor script-extension/naming rules referenced in
  `mspbotsrmm_create_script`'s description) **and `GET /connections`** (raw
  per-connection capability list) are reference endpoints only, not
  exposed as MCP tools — the doc does not tag either with a tool name.
