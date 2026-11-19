"""错误码封闭集自检（复用语义：新码必须进 CATALOG；四类基础语义齐备）。"""
from __future__ import annotations

from .errors import ERROR_CATALOG

#: 四类基础语义（内核 error_codes.REQUIRED_CLASSES 同集）
REQUIRED_CLASSES = {"not_wired", "not_found", "upstream", "invalid_argument"}


def assert_closed_catalog() -> list[str]:
    """封闭集断言：四类语义可观测区分，缺类即抛。返回登记码清单。"""
    classes = {m.semantic_class for m in ERROR_CATALOG.values()}
    missing = REQUIRED_CLASSES - classes
    if missing:
        raise AssertionError(f"error catalog missing semantic classes: {sorted(missing)}")
    return sorted(ERROR_CATALOG)
