"""评测运行 → Markdown 验收报告。"""
from __future__ import annotations

from datetime import datetime

from alun.quality import metrics as m


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _status(value, target, *, higher=True) -> str:
    st = m.threshold_status(value, target, higher_is_better=higher)
    return {"pass": "通过", "fail": "未通过", "blocked": "待补证"}.get(st, st)


def render_markdown(run: dict) -> str:
    metrics = run.get("metrics") or {}
    summary = run.get("summary") or {}
    lines: list[str] = []
    lines.append(f"# A论 V1.1 质量验收报告（{run.get('run_id', '')}）")
    lines.append("")
    lines.append(f"- 黄金集版本：`{run.get('version', '')}`")
    lines.append(f"- 评测范围：`{run.get('suite', '')}`")
    lines.append(f"- 状态：**{run.get('status', '')}**")
    lines.append(f"- 开始时间：{run.get('created_at', '')}")
    lines.append(f"- 结束时间：{run.get('finished_at') or '—'}")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("## 一、结论")
    lines.append("")
    lines.append(f"**{summary.get('headline', '尚未生成结论')}**")
    if summary.get("failed"):
        lines.append("")
        lines.append("未通过项：" + "、".join(f"`{x}`" for x in summary["failed"]))
    if summary.get("blocked"):
        lines.append("")
        lines.append("待补证项：" + "、".join(f"`{x}`" for x in summary["blocked"]))
    lines.append("")
    checks = summary.get("checks") or []
    if checks:
        lines.append("| 指标 | 实测 | 目标 | 判定 | 说明 |")
        lines.append("|---|---:|---:|---|---|")
        for c in checks:
            target = c.get("target")
            if isinstance(target, str):
                target_txt = target
            elif target is None:
                target_txt = "跟踪项"
            elif c["id"] in ("parse_30p_p95", "generic_stem"):
                target_txt = f"≤{target}"
            else:
                target_txt = f"≥{target}"
            status_txt = {"pass": "通过", "fail": "未通过", "blocked": "待补证",
                          "warn": "未达标", "track": "基线"}.get(c.get("status"), c.get("status") or "—")
            lines.append(
                f"| {c.get('label')} | {_fmt(c.get('value'))} | {target_txt} | {status_txt} | {c.get('detail', '')} |"
            )
        lines.append("")

    parse = metrics.get("parse") or {}
    if parse:
        o = parse.get("overall") or {}
        lines.append("## 二、解析结构质量")
        lines.append("")
        lines.append(
            f"Micro：P={_fmt(o.get('precision'))}，R={_fmt(o.get('recall'))}，F1={_fmt(o.get('f1'))}，"
            f"Macro-F1={_fmt(o.get('macro_f1'))}，单篇最低={_fmt(o.get('min_doc_f1'))}；"
            f"边界准确率={_fmt(o.get('boundary_accuracy'))}，层级准确率={_fmt(o.get('level_accuracy'))}。"
        )
        lines.append("")
        lines.append("| 文档 | 标题数（金/测/匹配） | P | R | F1 | 边界 | 层级 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for d in parse.get("documents") or []:
            lines.append(
                f"| {d.get('title', '')[:34]} | {d.get('gold_total')}/{d.get('pred_total')}/{d.get('matched')} | "
                f"{_fmt(d.get('precision'))} | {_fmt(d.get('recall'))} | {_fmt(d.get('f1'))} | "
                f"{_fmt(d.get('boundary_accuracy'))} | {_fmt(d.get('level_accuracy'))} |"
            )
        lines.append("")
        cov = parse.get("coverage") or {}
        lines.append(
            f"覆盖：平均文本覆盖率 {_fmt(cov.get('mean_text_coverage'))}，"
            f"表格 {_fmt(cov.get('mean_table_coverage'))}，图表 {_fmt(cov.get('mean_figure_coverage'))}。"
        )
        bad = [d for d in (parse.get("documents") or []) if d.get("f1", 1) < m.TITLE_F1_TARGET]
        if bad:
            lines.append("")
            lines.append("低于 0.95 的样本与差异：")
            for d in bad:
                missing = "、".join(str(x.get("title")) for x in (d.get("missing") or [])[:5]) or "—"
                extra = "、".join(str(x.get("title")) for x in (d.get("extra") or [])[:5]) or "—"
                lines.append(f"- 《{d.get('title', '')[:28]}》F1={_fmt(d.get('f1'))}；漏识别：{missing}；多识别：{extra}")
        lines.append("")

    generation = metrics.get("generation") or {}
    if generation:
        g = generation.get("overall") or {}
        lines.append("## 三、生成与证据质量")
        lines.append("")
        lines.append(
            f"题目 {g.get('questions', 0)} 道；证据支持率 {_fmt(g.get('evidence_support_rate'))}；"
            f"节点可追溯率 {_fmt(g.get('node_traceability_rate'))}；失效节点 {g.get('stale_nodes', 0)} 道；"
            f"LLM 生成占比 {_fmt(1 - float(g.get('fallback_rate') or 0))}；首轮失败率 {_fmt(g.get('first_pass_failure_rate'))}。"
        )
        lines.append("")
        lines.append(f"泛称题干 {g.get('generic_stem', 0)} 道；完全重复题干 {g.get('duplicate_stems', 0)} 道。")
        lines.append("")
        lines.append("| 文档 | 题量 | 证据支持率 | 节点可追溯率 | 失效节点 |")
        lines.append("|---|---:|---:|---:|---:|")
        for d in generation.get("documents") or []:
            lines.append(
                f"| {d.get('title', '')[:34]} | {d.get('total', 0)} | {_fmt(d.get('evidence_support_rate'))} | "
                f"{_fmt(d.get('node_traceability_rate'))} | {d.get('stale_nodes', 0)} |"
            )
        lines.append("")

    grading = metrics.get("grading") or {}
    if grading:
        gs = grading.get("grading") or {}
        ans = grading.get("answerability") or {}
        lines.append("## 四、判分与可答性")
        lines.append("")
        lines.append(
            f"主观题一致率 {_fmt(gs.get('within10_rate'))}（n={gs.get('n', 0)}，MAE={_fmt(gs.get('mae'))}，"
            f"最大差值 {_fmt(gs.get('max_diff'))}）；题目可答性均分 {_fmt(ans.get('mean'))}（{ans.get('questions', 0)} 题 / {ans.get('reviews', 0)} 人次）。"
        )
        lines.append("")

    latency = metrics.get("latency") or {}
    if latency:
        lines.append("## 五、时延与稳定性")
        lines.append("")
        lines.append("| 链路 | n | P50 | P95 | P99 | 最大 | 单位 |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")
        for key, label in (("parse_all", "文档解析（全部样本）"), ("parse_30p", "30 页文档解析"), ("generation", "单题生成"), ("judge", "主观题判分")):
            p = latency.get(key) or {}
            unit = "秒" if key != "parse_all" and key != "parse_30p" else "秒"
            lines.append(
                f"| {label} | {p.get('n', 0)} | {_fmt(p.get('p50'), 2)} | {_fmt(p.get('p95'), 2)} | "
                f"{_fmt(p.get('p99'), 2)} | {_fmt(p.get('max'), 2)} | {unit} |"
            )
        lines.append("")

    defects = run.get("defects") or []
    if defects:
        lines.append("## 六、缺陷台账")
        lines.append("")
        lines.append("| 编号 | 级别 | 区域 | 描述 | 状态 |")
        lines.append("|---|---|---|---|---|")
        for d in defects:
            lines.append(f"| {d.get('defect_id')} | {d.get('severity')} | {d.get('area')} | {d.get('title')} | {d.get('status')} |")
        lines.append("")

    lines.append("## 附录：口径")
    lines.append("")
    lines.append("- 标题匹配：标题主体归一化后按相似度 + 页码惩罚做一对一匹配；边界允许 ±1 页；层级取解析节点 level。")
    lines.append("- 证据支持率：题目全部引文能在原文节点/全文文本中逐字定位的比例；节点可追溯率额外要求节点存在且页码落在节点页码范围。")
    lines.append("- 主观题一致率：人工参考分与模型分差值 ≤10 分的样本占比；极差目标 ≤10 分。")
    lines.append("- 时延：解析取流水线埋点，生成/判分取 ai_audit.latency_ms；P95 采用最近秩法。")
    lines.append("")
    lines.append("> 报告由 `python -m alun.quality.cli run` 生成；原始指标见 `data/quality/runs/`。")
    return "\n".join(lines) + "\n"
