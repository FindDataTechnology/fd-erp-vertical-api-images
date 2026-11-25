"""店目录（店目录 + 跨店检索）。

跨店发现的唯一入口。**只存指针与类目**：店面标识、展示名、公开读面地址、类目、关键词、
地区。价格与库存永远回原店读——目录存一份就会漂移成第二个真源。

三条判据（spec `trade-discovery`）：
1. 检索结果每条都带来源店与可访问地址；
2. 「未收录 / 无匹配 / 目录不可读」三态必须可区分，不能用空数组同时表示；
3. 目录是旁路：目录挂了店面照常。
"""
from .directory import Directory, DirectoryUnavailable, ShopEntry

__all__ = ["Directory", "DirectoryUnavailable", "ShopEntry"]
__version__ = "0.1.0"
