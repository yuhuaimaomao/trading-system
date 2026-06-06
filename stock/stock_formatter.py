"""个股分析报告格式化 — Telegram/CLI/管线 dict 三通道输出。"""

from stock.stock_schemas import StockAnalysisReport


def to_cli(report: StockAnalysisReport) -> str:
    """终端输出（可稍宽）。"""
    lines = [
        f"━━━ {report.symbol} {report.name or ''} ━━━",
    ]

    for r in report.results:
        status = "✅" if r.ok else "⚠️"
        if r.error:
            lines.append(f"  [{r.dimension}] ❌ {r.error}")
            continue

        lines.append(f"  [{r.dimension}] {status}")
        for c in r.conclusions:
            lines.append(f"    {c}")
        for f in r.risk_flags:
            lines.append(f"    ⚠️  {f}")

    # 汇总
    if report.aggregated:
        lines.append("  ── 综合 ──")
        for k, v in report.aggregated.items():
            lines.append(f"    {k}: {v}")

    return "\n".join(lines)


def to_telegram(report: StockAnalysisReport, max_len: int = 1000) -> str:
    """Telegram 紧凑输出。"""
    lines = [f"📊 {report.symbol} {report.name or ''}"]

    for r in report.results:
        if r.error:
            lines.append(f"  [{r.dimension}] ❌ {r.error}")
            continue

        emoji = "✅" if r.ok else "⚠️"
        short = r.conclusions[:2]  # 最多 2 条结论
        if short:
            lines.append(f"  {emoji} {r.dimension}: {'; '.join(short)}")
        if r.risk_flags:
            lines.append(f"  ⚠️  {'; '.join(r.risk_flags[:2])}")

    result = "\n".join(lines)
    if len(result) > max_len:
        result = result[: max_len - 3] + "..."
    return result


def to_dict(report: StockAnalysisReport) -> dict:
    """管线 dict 输出（给策略/Watcher 程序消费）。"""
    return {
        "symbol": report.symbol,
        "name": report.name,
        "results": [
            {
                "dimension": r.dimension,
                "ok": r.ok,
                "conclusions": r.conclusions,
                "risk_flags": r.risk_flags,
                "data": r.data,
                "error": r.error,
            }
            for r in report.results
        ],
        "aggregated": report.aggregated,
    }
