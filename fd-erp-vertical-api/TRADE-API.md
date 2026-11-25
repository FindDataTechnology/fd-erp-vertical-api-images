# 交易协商面契约（trade negotiation surface）

> change `establish-ai-native-trade-discovery` tasks 2.x（spec `trade-negotiation`）。
> 实现：`fd-erp-vertical-api/fd_erp_vertical_api/trade.py`；编排：
> `fd-erp-vertical-templates/fd_erp_vertical_templates/negotiation.py`。
> 闸：`fd-erp/tools/check_rest_surface.py`（与实现双向对拍，悬空/未声明都判红）。

## 0. 身份口径

| 取值 | 含义 |
|---|---|
| `商家` | 需租户身份头 `X-ERP-Tenant`（本进程绑定租户），跨租户 403 |
| `商家 + 委托` | 同上，另可带网关验签后的 `X-ERP-Grant-*`（agent 代委托人办事） |

本表**不进**四张页面集表（协商不是页面，是 API 面动作），与店面公开面同法单立一份。
协商面**不是匿名面**：它读写的是商家的报价承诺与买方的询价单，匿名开放等于让任何人
以商家的名义对外报价。

## 1. 询价（商家 + 委托）

| 方法 | 路径 | 权限 | 身份 |
|---|---|---|---|
| POST | `/erp/supply-chain/v1/trade/demands` | `trade.write` | 商家 + 委托 |

### 纪律

- **不接受价格字段**：`lines[]` 里的 `expected_price` / `price` / `unit_price` / `amount`
  任一出现即 400，不做静默忽略（静默忽略会让调用方以为自己的出价被采纳）。
  理由见 `trade-negotiation`「询价单声明需求，不声明价格」。
- 必填：`lines`（非空，每行 `productId` + `qty`）、`neededBy`、`shipTo.name`。
- 未关闭询价单上限 20/买方（`X-ERP-Trade-Buyer`，缺省 `agent`）——不设上限就是一个
  免费的通知轰炸入口。
- 声明出处：`openspec/changes/establish-ai-native-trade-discovery/specs/trade-negotiation/spec.md`

## 2. 报价（商家）

| 方法 | 路径 | 权限 | 身份 |
|---|---|---|---|
| POST | `/erp/supply-chain/v1/trade/quotes` | `trade.write` | 商家 |

### 纪律

- 必填：`demandRef`、`unitPrice`、`currency`、`validUntil`、`leadTimeDays`、`moq`、
  `qtyOffered`。缺有效期即 400（报价是**有时效的承诺**，不接受无限期）。
- `validUntil` 必须在未来；`qtyOffered >= moq`（总量小于起订量的报价永远无法成交，直接拒）。
- 声明出处：同上「报价是有时效的承诺」。

## 3. 接受报价 → 订单（商家 + 委托）

| 方法 | 路径 | 权限 | 身份 |
|---|---|---|---|
| POST | `/erp/supply-chain/v1/trade/quotes/{quote_ref}/accept` | `trade.write` | 商家 + 委托 |

### 纪律

- 成交价 MUST 取自报价（不回落到零售层）；订单事实记 `quote_ref` 使成交价可回溯。
- 拒绝条件（均**不产生订单**）：报价已过期 / 数量低于 `moq` / 报价剩余可成交量不足。
- 携带 `X-ERP-Grant-*` 时 MUST 过预算闸（`BUDGET_EXHAUSTED` 422）；金额由服务端按报价算，
  请求体里的 `amount` 一律忽略。
- **零资金动作**：接受只产生订单（`payment_state=awaiting_payment`），不写任何资金类事件
  ——与 `erp-board` 二清红线同一口径。
- 声明出处：同上「接受报价生成订单且成交价可回溯」「接受报价仍受委托预算闸约束」
  「协商全程不产生资金动作」。
