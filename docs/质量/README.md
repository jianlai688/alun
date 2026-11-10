# A论 V1.1 质量与验收材料

| 文档 | 内容 |
|---|---|
| [01-黄金集与标注手册](01-黄金集与标注手册.md) | 黄金集 v1 构成、标题/KP/证据/可答性/rubric 标注规则、匹配与 F1 口径、双人标注与仲裁流程 |
| [02-V1.1-质量验收报告](02-V1.1-质量验收报告.md) | 基线 vs 验收、P0/P1 缺陷修复、指标明细、性能与容量、遗留问题与签署 |
| [03-并发压测报告](03-并发压测报告.md) | 10 并发解析与 3 并发 12 题全流程压测结果与瓶颈分析 |

代码与数据：

- 黄金集：`alun/quality/goldens/golden-v1/`
- 评测引擎：`alun/quality/metrics.py`、`runner.py`、`cli.py`
- 管理端看板：`/admin` → 「质量验收」
- 原始运行：`alun/data/quality/runs/`
- 压测原始数据：`alun/data/quality/stress-*.json|md`

快速回归：

```powershell
cd D:\AI论文
python -m alun.quality.cli run --suite all --report
```
