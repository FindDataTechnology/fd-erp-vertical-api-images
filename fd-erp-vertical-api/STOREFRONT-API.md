# 店面公开面契约（storefront public surface）

> change `establish-ai-native-ecommerce-stack` tasks 0.4（spec `erp-rest-surface-contract`
> 「店面公开路由另立契约表且不进页面集表」）。
> 实现：`fd-erp-vertical-api/fd_erp_vertical_api/storefront.py`；闸：
> `fd-erp/tools/check_rest_surface.py`（与实现双向对拍，悬空/未声明都判红）。

## 0. 身份口径

本表**逐条标注身份要求**，三种取值：

| 取值 | 含义 |
|---|---|
| `匿名` | 不带任何身份头、不带登录态即可读；理由与声明出处必须写在本行 |
| `商家` | 需商家侧身份（后台登录态 + 到账确认权）；本版尚未实现 |
| `买家凭据` | 需下单时签发的订单凭据；本版尚未实现 |

**匿名不许靠路径前缀自动放行**：`/shop/` 前缀豁免出身份闸的是**一份精确路径清单**
（`app.SHOP_PATHS`），新增店面路由必须同时改清单与本表，否则 `check_rest_surface` 判红。
租户校验不因此消失——店面路由以路径参数 `{tenant}` 与本进程绑定租户比对，不符一律 404
（与 UE 侧「不泄露存在性」同口径）。

## 1. 公开读面（匿名）

| 方法 | 路径 | 权限 | 身份 |
|---|---|---|---|
| GET | `/shop/{tenant}/products` | 无（公开读面） | 匿名 |
| GET | `/shop/{tenant}/products/{product_id}` | 无（公开读面） | 匿名 |

### 匿名理由与声明出处

- **为什么匿名**：店面的读者是尚未建立任何关系的零售访客，要求客户档案等于把 B2B 身份
  强加给零售入口（`ecommerce-storefront` 的能力前提）。
- **出参口径**：只出「零售渠道可见 + 已上架 + 有零售价」的商品；不含 `entity_id`、
  成本价、供应商、B2B 客户专属价。商品编码（catalog code）是对外商品把手，保留。
- **零售渠道未配置 = 店面关闭**：读面返回空集合，不回落成全量商品可见。
- **声明出处**：`openspec/changes/establish-ai-native-ecommerce-stack/specs/ecommerce-storefront/spec.md`
  （「公开读面匿名可读且只出零售渠道商品」与「零售价是独立口径且带出处」两条 requirement）。

## 2. 零售下单（匿名 / 商家）

| 方法 | 路径 | 权限 | 身份 |
|---|---|---|---|
| POST | `/shop/{tenant}/orders` | 无（预付单不占信用） | 匿名 |
| GET | `/shop/{tenant}/orders/{order_ref}` | 无（凭不透明单号） | 匿名 |
| POST | `/shop/{tenant}/orders/{order_ref}/payment` | 无 grant（商家侧动作） | 商家 |

### 为什么下单也匿名

零售买家没有客户档案、没有登录态（`ecommerce-storefront-checkout`「匿名下单只要求履约必需信息」）。
**归属不靠身份头**：下单必须给 `shipTo`（收货人 + 电话），缺则 400 并指名缺项；
系统 MUST NOT 回落成「某个默认客户」。

订单号是不透明随机串（`SO-` + 10 位 hex），因此「凭单号查自己那一单」不需要登录态，
同时不泄露他人订单——B2B 订单号经此路径一律 404。

### 为什么到账登记要商家身份

`payment` 是**商家**确认钱到自己账户的动作，不是买家能做的。买家侧不可达（401）。
首版用 `X-ERP-Tenant` 头挡（网关 PAT 落地前的演示形态，与 SP/CP 面同法）。

### 预付与信用

预付单 MUST NOT 查信用、MUST NOT 占额度、MUST NOT 产生占用行
（`erp-credit-reservation`「预付单不进入信用占用生命周期」）。
未登记到账的预付单 MUST NOT 被确认（确认即放货），违者 409 `WORKFLOW_STATE`。
