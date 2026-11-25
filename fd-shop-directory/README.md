# fd-shop-directory

店目录：跨店发现的唯一入口（change `establish-ai-native-trade-discovery` 1.x）。

**只存指针与类目**——店面标识、展示名、公开读面地址、类目、关键词、地区。
价格与库存永远回原店读：目录存一份就会漂移成第二个真源。

## 三条判据

1. 检索结果每条都带来源店与可访问地址（能仅凭结果回答「这条是谁家的」）；
2. 「未收录 / 无匹配 / 目录不可读」三态必须可区分，不能用空数组同时表示
   （空数组被读成「网络里没人卖」，是发现层最贵的错误）；
3. 目录是旁路：目录挂了店面照常。损坏的 JSONL 报不可读，不静默当空目录。

## 跑

```bash
fd-shop-directory serve --host 0.0.0.0 --port 8099
# 环境：SHOP_DIRECTORY_DATA（缺省 /data/shop-directory.jsonl）
#       SHOP_DIRECTORY_WRITE_TOKEN（未设 ⇒ 写面一律 403，默认拒绝）

curl "localhost:8099/directory/search?category=饮品"
curl -X POST localhost:8099/directory/shops -H "x-directory-write-token: $TOK" \
  -d '{"tenant":"acme","title":"Acme","public_base":"https://erp.example.com/shop/acme",
       "categories":["饮品"],"keywords":["矿泉水"]}'
```

## 部署

与 `fd-erp-vertical-api` **同镜像、不同入口**（`fd-infra-deploy/all-services/prod/fd-shop-directory.yaml`），
独立 Deployment + 独立 PVC。不是塞进 ERP 进程——一进程一租户的约束下，跨店检索必须有
一个不绑定单租户的地方，但它仍只存指针，不绕过隔离。
