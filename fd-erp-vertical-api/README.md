# fd-erp-vertical-api

> **Part of the [Base line (寻数·壹座)](https://www.finddatatech.cloud/products/base) of [FindData](https://www.finddatatech.cloud)** — ERP vertical API — per-industry record schemas and REST surface over the ledger core.

ERP 行业垂直 **REST 服务面**（OpenSpec change `add-supply-chain-vertical` 任务 6.5）：
UE 订货台 19 + SE 业务员台 15 + SP 供应商 13 + CP 承运商 12 契约端点 +
pageset 授权端点 + 2 条契约外对账支撑入口（共 63 路由）。契约真源：
`fd-erp-vertical-templates/templates/supply-chain/pagesets/{ue-commerce,se-sales,sp-supplier,cp-carrier}/api-contract.md`。

## 边界纪律（同 fd-erp-vertical-templates）

1. **零旁路**：全部业务读写经 `fd_erp_vertical_templates` 驱动
   （masterdata / pricing / orchestration / fulfillment / wms_sync / reports）
   + KernelClient 工具面 —— 无平行取数路径；
2. **包源码不 import 任何 DB 驱动、不写内核表名 SQL**（tests 目录豁免，与模板包
   conftest 同口径：真库约束层不可 mock）；
3. **落账仍走内核确认门**：REST 不直接落账。下单经内核 `reserve_credit` 记账；
   对账单确认只提交自平核对 proposal（pending）进确认门，绝不假报已确认；
4. **租户模型照内核**：每进程 `ERP_TENANT_ID` 绑定唯一租户；每请求带
   `X-ERP-Tenant` 头过租户闸，跨租户一律 403 `TENANT_DENIED`；
   身份头（壹座底座 PAT 在网关侧落地前的演示/测试形态）：UE=`X-ERP-Customer`
   （客户联系人所属客户档案）、SE=`X-ERP-Staff`（业务员工号，归属=客户主数据
   `sales_rep`）、SP=`X-ERP-Supplier`（供应商档案 id/编码）、CP=`X-ERP-Carrier`
   （承运商标识，运单 carrier 域）。未知/越域身份一律 404（不泄露存在性）；
   例外：SE 代客下单归属越权 403 `DATA_SCOPE_DENIED`（契约明示，可解释拒绝）；
5. **BYOK**：外部凭证只经环境变量注入，零落库零日志；DSN 已从 repr 隐去
   （`field(repr=False)`），任何日志路径不外泄口令。

## 运行

```bash
pip install -e .            # 或 uv pip install -e .
fd-erp-vertical-api serve --host 127.0.0.1 --port 8100
fd-erp-vertical-api check   # 自检：授权配置 + 错误码封闭集（不回显敏感值）
```

环境变量：`ERP_TENANT_ID`、`ERP_LEDGER_DATABASE_URL`、`ERP_BASE_CURRENCY`、
`ERP_API_DATA_DIR`（模板层 JSONL 事件档目录）、`ERP_KERNEL_TRANSPORT`
（`inprocess` 缺省 / `mcp`）、`ERP_KERNEL_URL`（mcp 形态）、
`ERP_PAGESET_AUTH`（内联 JSON）或 `ERP_PAGESET_AUTH_FILE`（文件）、
**`ERP_FINANCE_STAFF`（财务后台任职名单，内联 JSON）或 `ERP_FINANCE_STAFF_FILE`**、
`ERP_WMS_SNAPSHOT_FILE`（WMS 快照 JSON，缺省无快照源如实降级）、
`ERP_ACCESS_LOG`（访问日志档位，见下节）。

**财务后台要两处都配齐才通**（缺任一处一律 403，fail-closed）：

```
ERP_PAGESET_AUTH     += "supply-chain.fi": ["finance.read","finance.write","finance.confirm"]
ERP_FINANCE_STAFF     = {"read":["cfo@example.com"],"write":["cfo@example.com"],"confirm":["cfo@example.com"]}
```

只配第一处是**危险的**：页集授权判的是「这个部署允许哪些 grant」，它不认识调用者，
而 fi 面没有任何数据范围——那样等于把财务后台发给所有已登录用户。详见
`INTERNAL-API.md` §0（四道闸与单调闭包：`confirm` ⊇ `write` ⊇ `read`）。
程序身份（`system`/`agent`/各 `*-engine`）在 REST 面一律拒，**不靠名单放行**。

## 访问日志（被拒的请求可归因）

一行一个 JSON 写 stdout（容器日志即采集面，不新增落盘形态），信封
`ts`/`event`/`tenant_hash` 与内核同构。字段：`request_id` `method` `route`
`status` `dur_ms` `code` `hint` `detail_keys` `grant`。

| 档位 | 行为 |
|---|---|
| `errors`（缺省） | 只写 `status>=400` 或带业务码的响应；正常下单零日志行 |
| `all` | 逐请求全量（**含 `/health` 探针与 `/metrics` 抓取**，仅调试用，别常开） |
| `off` | 关 |

纪律：路径记**路由模板**（`/ue/orders/{id}`，不记原始单号）；租户只落 12 位
sha256（跨租户被拒时记的是请求方声称那个租户的哈希）；`detail` 只记键名
（值里可能有客户 id/金额）；响应头回传 `X-Request-Id`（调用方带来的合格值原样
保留），客户报障时可凭号定位到那一行。

为什么中间件注册在最外层：租户闸拒掉的 401/403 根本不进业务路由，日志挂在
内侧就永远记不到——而这恰恰是唯一值得逐条看的流量。业务码在错误发生处盖到
`request.state`，中间件不解析响应体。

生产形态 `ERP_KERNEL_TRANSPORT=mcp` 时经
`fd_erp_vertical_templates.kernel_client.McpKernelClient` 调内核 MCP 工具面；
缺省 InProcess 形态与模板包测试同构（服务进程内委托内核模块，同形签名）。

## pageset 授权端点（形状被 paas P0 客户端固定）

`GET /erp/supply-chain/v1/pagesets` →
`{"ok":true, "data":{"pagesets":[...]}, "pagesets":[{"key":"supply-chain.ue","grants":["catalog.read",...]},...], "rows_returned":1}`

- 与 `paas/miniapp/src/pagesets/authorize.ts` `parsePagesetAuth` 对齐：key 白名单
  逐字同（supply-chain.ue/se/sp/cp）、grants 全字符串、顶层 `pagesets` 可解析
  （客户端 `res.json()` 原始体直读，故顶层镜像字段在统一壳之外附加）；
- 授权配置源：`ERP_PAGESET_AUTH`（内联 JSON）优先于 `ERP_PAGESET_AUTH_FILE`
  （形如 `{"supply-chain.ue": ["catalog.read", ...]}`）；**fail-closed**：
  缺失/损坏/形态非法 → 空授权（无页面集可见、所有 /ue/* 端点 403）；
- grant 码封闭集（`pagesets.KNOWN_GRANTS`）= paas registry 四页面集
  requiredGrants 并集；集外码一律丢弃 —— 绝不输出会让客户端整体作废的形态；
- 端点闸序：租户闸 → 页面集授权（`PAGESET_FORBIDDEN`）→ grant
  （`GRANT_REQUIRED`）→ 客户身份域（404）；内部页集 `supply-chain.fi` 在同一条链上
  多一道**按人授权**（`STAFF_NOT_AUTHORIZED`，见 `INTERNAL-API.md` §0.2）。

## 端点覆盖（契约 19 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 商品 | GET /ue/products · /ue/product-categories · /ue/products/{id} | masterdata 商品档 + 商品池授权过滤（visible_product_codes）；详情带 pricing.resolve_price 取价（规则链 provenance）+ wms_sync 快照库存可见性 | 实现 |
| 下单 | GET/POST/DELETE /ue/cart(/lines) · POST /ue/cart/price-preview | 进货单=进程内暂存 UI 态（当次取价快照，非账务事实）；预览纯读不占额度 | 实现 |
| 下单 | POST /ue/orders | orchestration.accept_order：信用校验（fail-closed `CREDIT_EXCEEDED`+占用明细）→ 取价 → 内核 reserve_credit → 订单提案（确认车道） | 实现 |
| 查单 | GET /ue/orders · /ue/orders/{id} · /ue/orders/{id}/fulfillment | 编排层事件档 + fulfillment_view；未确认→404；已确认未编排→如实 `handoff_pending` | 实现 |
| 履约触发 | POST /ue/orders/{id}/fulfillment | `plan_fulfillment`：移交记录 → 履约计划（直发/多仓分配）。未确认→409 `WORKFLOW_STATE`（不伪造计划）；已有计划→幂等返回 `idempotent:true`；越域→404 | 实现 |
| 对账 | GET /ue/statements(/{id}) · POST /ue/statements/{id}/confirm | 订单按月派生 + 内核 proposal 面状态真源；确认→自平核对 proposal（pending 确认门，幂等回放）；异议→ThresholdEvaluator 处置车道 | 实现 |
| 开票 | GET/POST /ue/invoice-requests | 内核事件层**拟稿车道**（`submit_event` → `invoice_requested` pending）+ 确认门（`decide_event`）拍板；列表取内核投影的 `pending/accepted/rejected`，本服务不留第二份状态台账 | 实现 |
| 售后 | POST/GET /ue/aftersale-requests(/{id}) | 售后编排事件档（`record="aftersale"`，只追加流程留痕）：申请受理 → 进度查询；申请是**流程记录**不是入库事实，不进事件层 | 实现 |

计数：**实现 19/19；501 0/19**。开票只做到「申请受理 + 进度」：票据本体仍走客户既有
开票渠道（本端不代开票、不生成票据文件）；申请里的 `amount` 一律不采信，审核时按单据定。
说明，禁止假实现。AI 助手入口按契约 §7 无独立端点（复用底座 chat/WS 面）。
WS 事件面（订单状态推送/确认门回执）属底座通道接入，随 paas P1 逐页联调，挂账。

### 契约外：对账任务代理（optimize-erp-solution 6.6）

| 端点 | 形态 | 说明 |
|---|---|---|
| POST /ue/reconciliations | **202 + task_id** | 两源对账提交（源声明只放 `inline`）；内核后台线程执行，不在 HTTP 里等结果 |
| GET /ue/reconciliations/{task_id} | 状态/进度/四类差异 | 轮询报告；未知任务 404，内核不可达 501 + `planned_lane` |

这两条**不计入 19 条契约端点**（客户 App 不消费，对账工作台/运维调用），权限沿用
`statement.confirm`（契约里对账单确认门的上游就是内核自平核对，语义同源）。
源类型只允许 `inline`：内核的 `api`/`csv` 适配器会服务端拉任意 URL / 以 `file:`
读服务端文件，透传给客户面等于开 SSRF 与本地文件读取。

**通道结论（6.6）**：内核对账工具本身就是「提交即返回 task_id + `get_recon_report`
轮询」的长任务形态（`recon/engine.py`：写 queued 行 → daemon 线程 → 落库态可查），
所以走**任务化代理**，不做同步等待（design D13 已否掉同步代理：HTTP 同步必然超时）。
两种 transport 的错误语义由 `kernel_client.KernelProxy` 收口——MCP 形态下内核
`@safe_tool` 把失败包成 `{"ok": false, "error": …}` **返回**，不翻信封就会把 404
当 200 数据吐出去、把连接失败当 500；实测（本地起内核 MCP 面 + 本例 REST 面）：
内核在 → `202 task_id` → 轮询 `done`（四类差异、`external_writes=0`）；
内核停 → `501 NOT_IMPLEMENTED` + `planned_lane`；未知任务 → `404 NOT_FOUND`。

## 端点覆盖（SE 业务员台，契约 15 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 工作台 | GET /se/workspace | 名下客户（主数据 `sales_rep`）+ 订单/对账/处置任务聚合卡 | 实现 |
| CRM | GET /se/customers(/{id})(/orders) | 主数据客户档 + kernel query_credit（账本同源）+ 对账单派生摘要；未登记信用额度如实缺省 | 实现 |
| CRM | GET/POST /se/leads · POST/GET /se/visits | CRM 编排事件档（`record="lead"/"visit"`，只追加流程留痕）：线索池只本人、状态默认 `new`；拜访纪要必填、主体（客户/线索）至少一个；`location` 只存调用方给的引用，服务端不采集不补默认；越域与不存在同一条 404 | 实现 |
| 代客下单 | POST /se/orders/proxy · GET /se/orders(/{id}) | 与 POST /ue/orders 同一 accept_order 链路（取价+信用校验同源）；业务员-客户归属主数据校验（越权 403）；代理身份以 `requested_by=se:{工号}` 留痕订单事件档（请求/服务端日志/单据三处） | 实现 |
| 预警处置 | GET /se/alerts · POST /se/alerts/{id}/handle | reports 处置任务确认车道（与 owner 席位同一确认门实现）；accept→拍板生效，reject→拒绝留痕不落账 | 实现 |
| 对账协助 | GET /se/statements | StatementService 按名下客户聚合 | 实现 |
| 对账协助 | POST /se/statements/{id}/push | — | **501 NOT_IMPLEMENTED**（端内消息通道归底座通知面；推送须留痕；客户确认仍走 UE 端确认门，SE 端不可代拍） |

计数：**实现 14/15；501 如实 1/15**（对账单推送）。工作台的 `today_visit_plan`
与 `/se/visits` 同源（本人今天已登记的拜访条数），不再写死 0。

## 端点覆盖（SP 供应商，契约 13 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 工作台 | GET /sp/workspace | 推送给我方供应商档案的采购单/到货预告聚合计数 | 实现 |
| 接单 | GET /sp/purchase-orders(/{id}) · POST /{poId}/accept | 采购编排事件档（数据域=已推送给我方供应商档案的单据）；接单回执写入采购单**同一状态机**（accepted/rejected 事件；rejected 须附 note，终态拒绝留痕）；越域 404 | 实现 |
| 发货反馈 | POST /{poId}/delivery-feedback · GET/POST /sp/arrival-notices | 编排层 delivery_feedback 留痕 + **自动生成到货预告**（与入库任务联动）；凭证影像仅存引用（中台文件服务另行接入） | 实现 |
| 对账 | GET/POST /sp/statements(/{id})(/confirm) | — | **501 NOT_IMPLEMENTED**（契约数据源=金蝶 provider 往来面拉取+账本内核计算，任务 3.1 挂账；采购编排单据无金额无日期，无法如实派生对账单） |
| 发票 | GET/POST /sp/invoices | — | **501 NOT_IMPLEMENTED**（中台文件服务上传契约 + 发票查验/勾兑车道未接入；金蝶回写 contract-only） |
| 结算 | GET /sp/settlements | — | **501 NOT_IMPLEMENTED**（与金蝶往来面同源挂账；**零支付操作面红线 D4 不变**） |

计数：**实现 7/13；501 如实 6/13**。

**契约外：`GET /sp/inventory`** —— 到货落 `goods_received` 事件后的库存数量只读面
（按供应商档案数据域过滤，数量来自事件投影，不建第二份库存台账）。不计入 13 条
契约端点：契约未列该面，供应商看的是「我供的货在我方仓里的现存数量」。

## 端点覆盖（CP 承运商，契约 12 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 工作台 | GET /cp/workspace | 运单/月台/POD 回执聚合计数 | 实现 |
| 运单 | GET /cp/waybills(/{id}) · POST /{waybillId}/accept · GET /{waybillId}/events | 交付编排运单事件档（数据域=carrier 命中的运单）；承运回执写同一事件档（rejected 终态留痕，改派由租户侧重下运单）；事件流与客户侧全单物流视图同源；轨迹不可得如实置空 | 实现 |
| 月台 | POST /{waybillId}/dock-reservation | 预约/改约/取消回执留痕（slotId 为仓库侧不透明引用，与仓库作业编排联动） | 实现 |
| 月台 | GET /cp/dock-slots | — | **501 NOT_IMPLEMENTED**（月台资源主数据/可约时段计算归仓库作业编排，未建模） |
| 提货 | POST /{waybillId}/pickup-checkin | 提货打卡回执（驱动在途状态）；定位/影像为**可选采集**（隐私声明过审后启用） | 实现 |
| 签收 | POST /{waybillId}/pod | POD 回执（驱动结单/计费事件——计费规则引擎同源，本端零落账）；异常签收进处置车道（确认门派单留痕） | 实现 |
| 运费对账 | GET/POST /cp/freight-statements(/{id})(/confirm) | 内核费用行按（承运商,期间）归集：费用行 `refs` 带承运商才可归集（无承运商的费用行不进单，宁可空也不编）；明细按运单聚合带四层溯源；确认/异议落**内核自平核对 proposal**（借贷同额、净额为零，不动余额），由确认门拍板，已拍板再拍如实 409 | 实现 |

计数：**实现 11/12；501 如实 1/12**（仅月台可约时段：时段目录归仓库作业编排，未建模）。

**四面合计（契约 59 条）：实现 51/59；501 如实 8/59**（SE 推送 1 + SP 对账/发票/
结算 6 + CP 月台时段 1），全部带 `planned_lane`，禁止假实现。

## 端点覆盖（WH 仓管作业，**内部页集** `supply-chain.wh`，契约 11 条）

这是**内部页集**，不是第五个对外页面集：仓管（收货/发货/调拨/盘点）不属于四个对外角色中的
任何一个——UE 是客户、SE 是业务员、SP 是供应商、CP 是承运商，套上任何一个人都是错的人。
它不进 `GET /pagesets` 载荷（客户端 `parsePagesetAuth` 遇到不认识的 key 会整包作废，连带把
四个业务页集一起打掉），也没有面向客户的页面，所以**没有 `pages[].apis`**。契约声明在
`INTERNAL-WH-API.md`（与 fi 的 `INTERNAL-API.md` 分文件：那份的字段承诺闸按 `fi` 前缀对齐
OpenAPI 路由）。

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 入库 | POST/GET /wh/receipts | 内核事件层**拟稿车道**（`goods_received` pending，不投影不过账）；可选 `cost`/`currency`/`po_ref`/`serial` 随事实走 | 实现 |
| 出库 | POST/GET /wh/issues | 同上（`goods_issued`）；**先整单核可用量**，不足整单拒绝并点名行号与缺口，一行都不写 | 实现 |
| 调拨 | POST/GET /wh/transfers | 同单**两笔**事实（源仓 `goods_issued` + 目的仓 `goods_received`，数量守恒）；同仓调拨按参数错误拒 | 实现 |
| 盘点 | POST/GET /wh/stocktakes | 只对**有差异**的行出 `goods_adjusted`（盘盈正、盘亏负）；账面数读本板块余额，**不接受调用方递来的账面数** | 实现 |
| 单据读面 | GET /wh/documents/{ref} | 逐行**当前**状态（内核 draft 车道，不是提交时快照）+ 按侧汇总 `sides` + `halfConfirmed` | 实现 |
| 拍板 | POST /wh/documents/{ref}/confirm | 逐行过确认门 → `project`（投影+过账）。可只拍一侧/一行，此时 `finalize:false` **整单状态不推进** | 实现 |
| 驳回 | POST /wh/documents/{ref}/reject | 内核逐行 `rejected` + 本地单据状态；原因必填 | 实现 |

计数：**实现 11/11**（无 501）。

**三件与四面不同的口径**（改之前先读 `INTERNAL-WH-API.md` §0）：

1. **三档 grant 分开**：`inventory.read` / `inventory.write` / `inventory.confirm`。
   「能开一张单」与「能让这批货成为账面事实」是两件事，读权限不覆盖后两档；
2. **身份头是 `X-ERP-Warehouse`，不共用 `X-ERP-Staff`**：它要进 `requestedBy` 留痕，共用
   会让留痕指向错误的席位；程序身份一律 400（内核确认门硬编码 `actor="human"`，不接受自报身份）；
3. **数量面唯一出处是本板块**（`query_object_balance`），**不回写外部 WMS**——外部库存台账
   仍归 WMS，边界见 `erp-vertical-supply-chain` 的「仓配与交付编排（执行在外部）」。

> **与 `tasks.md` 3.1 的偏差（有意）**：那份写的是 `/ue/inventory/*`。落地改成
> `/erp/supply-chain/v1/wh/*` + 内部页集，因为 UE 面要求**客户身份头**（`ue_context`
> 校验 `CUSTOMER_HEADER` 且必须是 customer 实体），而仓管不是客户。走内部页集还顺带
> 解掉了那条需求自己写明的阻塞：对外页集 key/grant 是封闭集、镜像在 paas 的
> `registry.ts`，新增对外 key 要改 paas 那棵有数千未提交文件的树；内部页集只在服务端守门、
> 不进载荷，**零 paas 改动、零客户端契约变更**。

### 501 未接通面清单（机器可读，禁止手抄）

`planned-surfaces.json` 由 `scripts/planned_surfaces.py` **从源码 AST 派生**（路由装饰器
+ `NOT_IMPLEMENTED` raise + `planned_lane` 字面量），逐条含 method / path / capability /
domain / grant / planned_lane / source 行号。清单不要手改：

```bash
python3 scripts/planned_surfaces.py --check    # 与代码比对，零差异才退出 0（pytest 同跑）
python3 scripts/planned_surfaces.py --write    # 改了 501 面之后回填
```

当前 **8 条**（SE 推送 1 + SP 对账/发票/结算 6 + CP 月台时段 1；UE 0）。这个数字只认
`planned-surfaces.json` 与生成器的输出，**不认文档里的历史数**——本段此前写着「20 条
（UE 5 + SE 5 + SP 6 + CP 4）」，那是提交时的规划量，后来 12 条真做完了，而文档没人回来改
（同一次核对里 `check_blocked_endpoints.py` 也报 8 条）。判据不含 except 块里的降级 raise
（例如内核不可达时的 501 + planned_lane）：那说明端点本体有实现，不算未接通面。

## 统一响应壳与错误

- 成功 `{"ok":true,"data":...,"rows_returned":n}`（数组按行计，对象/写成功 0 ——
  内核 result.stamp 语义）；
- 失败 `{"ok":false,"error":{code,message,hint,detail},"rows_returned":0}`；
- 错误码封闭集（`errors.ERROR_CATALOG`）：复用内核语义码
  （NOT_FOUND/INVALID_ARGUMENT/TENANT_DENIED/CREDIT_EXCEEDED/NOT_WIRED/...）
  + 新码 AUTH_REQUIRED / PAGESET_FORBIDDEN / GRANT_REQUIRED / NOT_IMPLEMENTED
  / DATA_SCOPE_DENIED（SE 代客归属越权 403）
  （全部登记封闭集，`error_codes_selfcheck.assert_closed_catalog` 断言四类语义）；
- 分页 `offset/limit`（+`page` 便捷参数）沿内核 result.py 语义：上限
  `RESULT_MAX_ROWS=500` 不放开、truncated 标记 + `continuation:{method:"offset",
  next_offset}` 续读。

## 测试

```bash
ERP_LEDGER_DATABASE_URL=... .venv/bin/python -m pytest tests/ -q
# 缺省读 secrets/erp-demo.env 并把库名 erp_demo→erp_test_b（口令绝不打印）
```

用例（UE + SE/SP/CP 四面对齐）：pagesets 授权形状对齐 → 商品检索/详情（池过滤+库存
可见性）→ 进货单 → 取价试算（额度预估）→ 下单（信用占用真记账）→ 查单/越域
404 → 履约（handoff_pending → plan → view）→ 对账单确认门（pending→confirmed）→
异议处置车道 → 跨租户 403 → fail-closed 无授权 403 → grant 缺失 403 →
开票申请（拟稿车道 pending → 确认门拍板 → 状态随投影变）→ 分页/上限/壳/校验错误 400；
SE 面：工作台/名下客户/360/代客下单全链（归属越权 403 + 额度不足 422 + 代理
身份留痕）/预警处置拍板（accept/reject）/CRM 线索池与拜访（缺纪要 400、他人客户 404、location 可选且不补默认）；
SP 面：推送可见域 → 接单（重复裁决 409、拒单须 note）→ 发货反馈（自动到货
预告）→ 补录 → 入库任务联动 → 越权 404 → 对账/发票/结算 501；
CP 面：运单域 → 承运回执 → 月台（预留/改约/取消）→ 提货打卡 → 正常/异常 POD
（异常进处置车道）→ 事件流 → dock-slots/运费单 501；
分页/统一壳/租户闸对三面同壳复验。

## 演示数据（四页面集要看得见东西）

```bash
# 在服务容器内跑（ERP_TENANT_ID / ERP_API_DATA_DIR 已在 envFrom 里）
python3 scripts/seed_demo.py            # 幂等：主数据 + 演示单据（采购单/订单/计划/运单）
python3 scripts/seed_demo.py --check    # 只报现状，不写
python3 scripts/seed_demo.py --no-docs  # 只建客户/供应商/商品/商品池
```

脚本按 `code`/名称判存在，**重复跑零新增**（事件档 append-only，首灌当天实测
35 行 → 35 行）。建档走确认车道，不直接写档。三个易错点已固化进去：档闸认
**entity_id 不认 code**；商品必须挂在 `authorized` 商品池里客户才看得见；
SE 归属靠客户档 `sales_rep` = 会话邮箱。

信用额度不在脚本里（属内核库）：用 `fd-erp-ledger-mcp set-credit-limit
--customer <entity_id> --limit-amount 100000 --source-ref seed-demo`，在内核容器执行。
身份绑定表（会话 → 客户/供应商/承运商）在 `fd-infra-deploy` 的
`erp-identity-bindings` ConfigMap，subPath 挂载不热更：改完须重新滚动 Deployment。

## 如实挂账

- 对账单「与金蝶往来面同源」：金蝶 live 接通属任务 3.1 挂账（待客户凭证），
  现行行项目源 = 编排层订单事件 + 内核信用占用（响应内 `source_note` 如实标注）；
  SP 应付对账单因此 501（采购单无金额无日期，不伪造派生）；
- 下单收货信息/备注：受理回执回显（shipping_receipt/note_receipt），编排层
  accept_order 尚不持久化收货字段；
- SE 代理身份持久化：订单事件 `requested_by=se:{工号}`（编排 JSONL append-only，
  即服务端留痕）；独立审计流随底座 PAT 落地；
- SP 接单/发货反馈与 CP 承运/月台/提货/POD 回执车道：本波在模板编排层新增的
  最小回流车道（同一状态机事件档，additive，模板包 99 用例零破坏）；
- CP 运单结单态/计费事件、运费单 read 面（fee_details 按承运商归集）、
  月台时段主数据、SP 发票查验/勾兑车道、CRM 线索/拜访车道、端内消息通道：
  均如实 501（planned_lane 见端点覆盖表）；
- 运单轨迹/电子回单：DemoLogisticsAdapter（合成 read 面），真实承运商回流随
  连接器 onboarding；
- WS 事件面与 paas P1 UE/SE/SP/CP 逐页实施随编排层联调（tasks 6.5 注记）。
