# 仓管作业面 API 契约（内部页集 `supply-chain.wh` · 不进客户端载荷）

> 本文件声明**内部页集 `supply-chain.wh`** 的服务端面：仓管（收货 / 发货 / 调拨 / 盘点）
> 不属于四个对外角色中的任何一个——UE 是客户、SE 是业务员、SP 是供应商、CP 是承运商，
> 把「按仓收发货与盘点」套上其中任何一个都是错的人。
>
> 声明在这里而不是 `templates/supply-chain/pagesets/*/api-contract.md`，理由与
> `INTERNAL-API.md` 同源：页面集契约那一层被覆盖矩阵度量（功能点对账表、域计数、
> 已提交快照），塞进内部面会改变一个封闭空间的度量；而且这一面**没有面向客户的页面**，
> 没有 `pages[].apis` 可言（覆盖矩阵里它登记为内部面，不是页面）。
>
> **与 `INTERNAL-API.md` 分文件而不是同一份**：那份的字段承诺闸按 `fi` 前缀对齐
> OpenAPI 路由（`test_internal_api_field_promise.py`），混进 `wh` 行会被判成「文档里有、
> 代码里没有」的幽灵行。两面的闸人口各按自己的前缀划，别让一份文件承载两个页集。

## 0. 通用约定

### 0.1 三道闸，顺序不能换

| 顺序 | 判什么 | 失败码 | 为什么这个顺序 |
|---|---|---|---|
| 1 | 租户头 `X-ERP-Tenant` | `TENANT_DENIED` | 一进程一租户，最外层（中间件，全域生效） |
| 2 | 页集 `supply-chain.wh` 已授权 | `PAGESET_FORBIDDEN` | 未授权时连「这一面做了什么」都不该泄露 |
| 3 | 该端点 grant 在册 | `GRANT_REQUIRED` | 部署层面的事实先答 |
| 4 | 仓管身份头 `X-ERP-Warehouse` 非空且不是程序身份 | `AUTH_REQUIRED` / `INVALID_ARGUMENT` | 见 0.2 |

**与 `fi` 的差异是有意的**：`fi` 在第 4 道之后还有一层「财务任职名单」
（`STAFF_NOT_AUTHORIZED`），因为财务没有数据范围、能看到全公司的账。仓管**有**数据范围
（仓库来自请求体/查询串），越权由仓库维度兜住，所以不再叠人员名单——但身份头仍然必填。

### 0.2 身份头用 `X-ERP-Warehouse`，不共用 `X-ERP-Staff`

它要进单据的 `requestedBy` 留痕。共用财务那个头会让留痕指向错误的席位（同一个字符串在
两个面上代表两种岗位），事后查「这批货是谁收的」会查到财务。程序身份（`system`/`agent`/
各引擎）在 REST 层一律拒 400：将来要接自动化，得先有服务账号机制（独立凭据 + 独立审计），
而不是把引擎名写进人的头。

### 0.3 三个 grant 档，读与写都不覆盖拍板

`inventory.read`（看单据与余额）/ `inventory.write`（提交单据）/ `inventory.confirm`
（拍板 + 投影 + 过账）。**「能开一张单」与「能让这批货成为账面事实」是两件事**，
后者动的是对象余额，所以是独立档；读权限 MUST NOT 覆盖写与拍板。

### 0.4 提交只落拟稿事实

四个 `POST` 走内核拟稿车道写 `pending` 事实，**不投影、不过账**。投影与过账只发生在
`POST /wh/documents/{ref}/confirm`。所以 `pendingLines` 是「还没成为账面事实」的那部分，
而**单据状态为 `pending` 不等于货已经动了**。

### 0.5 数量面的唯一出处是本板块

余额一律读 `query_object_balance`（`sku` 维度 + 仓库/批次子维度），**不是外部 WMS 的实时
库存**。本面只记录我方收下的作业事实，不回写外部 WMS——外部库存台账仍归 WMS，边界写在
`erp-vertical-supply-chain` 的「仓配与交付编排（执行在外部）」那条需求里。

## 1. 单据提交与查询

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 | 响应字段 |
|---|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/wh/receipts` | JSON：`warehouse`（必填）、`lines`（必填，逐行 `sku`/`quantity`，可选 `unit`/`lot`/`cost`/`currency`/`po_ref`/`serial`）、可选 `ref`/`occurredAt`/`note` | 单据摘要；逐行落 `goods_received` **拟稿**事实（`pending`，不投影）。同 `ref` 重放幂等 | `inventory.write` | ref, kind, kindTitle, status, warehouse, occurredAt, requestedBy, lineCount, errorCount, pendingLines, duplicate, note |
| GET | `/erp/supply-chain/v1/wh/receipts` | `offset`/`limit`、`status`（可选） | 入库单列表 + 分页窗口 + 数量面出处说明 | `inventory.read` | ok, rows_returned, data, total, offset, limit, truncated, kind, caliber_note |
| POST | `/erp/supply-chain/v1/wh/issues` | JSON：同入库单（`cost` 系字段不适用） | 单据摘要；**先整单核可用量**，不足则整单拒绝并点名行号与缺口，一行都不写 | `inventory.write` | ref, kind, kindTitle, status, warehouse, occurredAt, requestedBy, lineCount, errorCount, pendingLines, duplicate, note |
| GET | `/erp/supply-chain/v1/wh/issues` | `offset`/`limit`、`status`（可选） | 出库单列表 + 分页窗口 + 数量面出处说明 | `inventory.read` | ok, rows_returned, data, total, offset, limit, truncated, kind, caliber_note |
| POST | `/erp/supply-chain/v1/wh/transfers` | JSON：`fromWarehouse`、`toWarehouse`（必填且**不得相同**）、`lines` | 单据摘要；同单落**两笔**事实（源仓 `goods_issued` + 目的仓 `goods_received`，数量守恒），故一条行对应 `pendingLines: 2` | `inventory.write` | ref, kind, kindTitle, status, warehouse, toWarehouse, occurredAt, requestedBy, lineCount, errorCount, pendingLines, duplicate, note |
| GET | `/erp/supply-chain/v1/wh/transfers` | `offset`/`limit`、`status`（可选） | 调拨单列表 + 分页窗口 + 数量面出处说明 | `inventory.read` | ok, rows_returned, data, total, offset, limit, truncated, kind, caliber_note |
| POST | `/erp/supply-chain/v1/wh/stocktakes` | JSON：`warehouse`、`counts`（逐行 `sku`/`counted_qty`，可选 `warehouse`/`lot`/`unit`） | 单据摘要；**只对有差异的行**出 `goods_adjusted`（盘盈正、盘亏负），无差异不出事实；账面数读本板块余额，**不接受调用方递来的账面数** | `inventory.write` | ref, kind, kindTitle, status, warehouse, occurredAt, requestedBy, lineCount, errorCount, pendingLines, duplicate, note |
| GET | `/erp/supply-chain/v1/wh/stocktakes` | `offset`/`limit`、`status`（可选） | 盘点单列表 + 分页窗口 + 数量面出处说明 | `inventory.read` | ok, rows_returned, data, total, offset, limit, truncated, kind, caliber_note |
| GET | `/erp/supply-chain/v1/wh/documents/{ref}` | — | 单张单据：逐行**当前**状态 + 按侧汇总。`event_status` 是内核 draft 车道此刻的状态，`event_status_submitted` 才是提交时的快照；`sides` 逐侧 `lines`/`pending`/`accepted`/`rejected`/`unknown`/`statuses`/`settled`；`halfConfirmed` = 至少一侧定案且另一侧仍待确认（调拨半边确认**不是**整单成功）。不存在 → `NOT_FOUND`；内核读不到 → 逐行 `unknown` 并在 `status_source` 点名 | `inventory.read` | ref, kind, kindTitle, status, warehouse, toWarehouse, occurredAt, requestedBy, lineCount, errorCount, pendingLines, duplicate, note, lines, errors, caliber_note, sides, halfConfirmed, status_source |

## 2. 拍板与驳回（确认门）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 | 响应字段 |
|---|---|---|---|---|---|
| POST | `/erp/supply-chain/v1/wh/documents/{ref}/confirm` | 可选 `note`、`side`（`out`/`in`）、`line`（整数） | 逐行过内核确认门，再统一投影与过账。**逐行报错**：某行被拒其余行照常推进（`decided[]` 那一行 `ok: false` + 内核码）。可只拍一侧或一行：筛过时 `finalize: false`，**整单状态不推进**。已驳回的单据 → `WORKFLOW_STATE` | `inventory.confirm` | ref, operator, decided, decidedOk, decidedFailed, scope, project, note |
| POST | `/erp/supply-chain/v1/wh/documents/{ref}/reject` | JSON：`reason`（**必填**） | 内核侧逐行 `rejected` + 本地单据状态转 `rejected`；两者是两件事（本地拒绝不改事件）。缺原因 → `INVALID_ARGUMENT`；已裁决 → `WORKFLOW_STATE` | `inventory.confirm` | ref, operator, rejected, rejectedOk, rejectedFailed, document, note |

**确认门只接受人工拍板**：内核 `decide_event` 的签名里没有自报身份（工具面硬编码
`lane=draft, actor=human`），本层的 `actor` 固定 `human`，经办人身份留在响应里的 `operator`
与访问日志——**不写进内核事件，也不假装内核收下了自报身份**。往审计台账里写一个看起来
精确、实际是自报的经办人，比不写更坏。

**缺过账规则不是失败**：`project.lines[].posted=false` + `posting_reason` 明说没命中规则，
不当成已入账（同 `fi` 的提案收件箱：不猜科目）。

## 3. 明确不做的

- **不回写外部 WMS**（§0.5）：本面只记录我方作业事实，外部库存台账归 WMS。
- **不限「只能从某仓出库」之类业务策略**：仓库维度是数据范围，不是策略引擎；要做策略请落在
  口径册，别在这里散着判。
- **不做批次/序列号的自动分配**：`lot`/`serial` 由调用方给，缺省记 `__unknown__`
  （内核「确实不可知」哨兵），不猜一个值填进去。
- **不做盘点任务的派发与提醒**：这是一张单据的提交面，不是作业排期器。
