"""A论 V1.1 质量与验收：黄金集、评测指标与回归运行。"""

from .metrics import (  # noqa: F401
    EVIDENCE_TARGET,
    TITLE_F1_TARGET,
    structure_metrics,
)

__all__ = ["structure_metrics", "TITLE_F1_TARGET", "EVIDENCE_TARGET"]
