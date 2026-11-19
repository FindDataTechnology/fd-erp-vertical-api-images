"""fd-erp-vertical-api：ERP 行业垂直 REST 服务面（add-supply-chain-vertical 6.5）。

命名契约（fd-package-identity）：发行名 dash=fd-erp-vertical-api，
导入名 underscore=fd_erp_vertical_api。

边界铁律（同模板包 fd-erp-vertical-templates）：
1. 零旁路：全部业务读写经 fd_erp_vertical_templates 驱动（masterdata /
   pricing / orchestration / fulfillment / wms_sync / reports）+ KernelClient
   （McpKernelClient 生产形态 / InProcess 同形客户端），无平行取数路径；
2. 本包源码不 import 任何 DB 驱动、不写内核表名 SQL；
3. 落账仍走内核确认门：REST 面不直接落账（确认类动作只产生 pending proposal
   / 确认车道记录，绝不伪造已确认）；
4. 租户模型照内核：每进程 ERP_TENANT_ID 绑定唯一租户，每请求租户闸，
   跨租户拒绝；BYOK 凭证只经环境变量注入，零落库。
"""
from .errors import ApiError, ERROR_CATALOG

__version__ = "0.1.0"

__all__ = ["ApiError", "ERROR_CATALOG", "__version__"]
