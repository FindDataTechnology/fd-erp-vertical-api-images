"""histogram 秒级桶集 —— **生成文件，勿手改**。

真源：`fd-erp/shared/semantics-contract.json` → `metrics.canonical.histogram_seconds_buckets`
生成器：`python3 fd-erp/tools/gen_metrics_buckets.py`
为什么生成：两处 histogram 各自手写时，一边加了 10.0 上界、另一边没有，而两边**各自都
自洽**（单测绿、Prometheus 也不报错）——只有跨服务比才看得出，于是必须有单一出处。
"""
from __future__ import annotations

SECONDS_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
