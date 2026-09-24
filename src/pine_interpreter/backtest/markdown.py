"""Plain-English Markdown reports for batch Pine backtests."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Literal

from pine_interpreter.backtest.batch import BatchBacktestReport, StrategyResult
from pine_interpreter.backtest.models import BacktestReport, BacktestValidationError

Ranking = Literal["return", "risk-adjusted", "drawdown"]


def ranked_strategies(
    report: BatchBacktestReport,
    *,
    ranking: Ranking = "return",
    min_trades: int = 1,
) -> tuple[StrategyResult, ...]:
    """Return eligible strategies in a stable, documented order.

    Only strategies that completed at least one trade are eligible. The
    default ranking is raw return, with drawdown and trade count used as
    tie-breakers. A ranking is a baseline comparison, not evidence that a
    strategy will work on future data.
    """

    if min_trades < 1:
        raise BacktestValidationError("min_trades must be at least one")
    eligible = [
        result
        for result in report.results
        if result.status == "backtested"
        and result.total_return_pct is not None
        and result.trades >= min_trades
    ]
    if ranking == "return":

        def key(result: StrategyResult) -> tuple[float, float, int, str]:
            return (
                -float(result.total_return_pct or 0.0),
                float(result.max_drawdown_pct or 0.0),
                -result.trades,
                result.name.casefold(),
            )

    elif ranking == "risk-adjusted":

        def key(result: StrategyResult) -> tuple[float, float, int, str]:
            result_value = float(result.total_return_pct or 0.0)
            drawdown = max(float(result.max_drawdown_pct or 0.0), 0.01)
            return (-result_value / drawdown, -result_value, -result.trades, result.name.casefold())

    elif ranking == "drawdown":

        def key(result: StrategyResult) -> tuple[float, float, int, str]:
            return (
                float(result.max_drawdown_pct or 0.0),
                -float(result.total_return_pct or 0.0),
                -result.trades,
                result.name.casefold(),
            )

    else:
        raise BacktestValidationError(f"unknown ranking: {ranking}")
    return tuple(sorted(eligible, key=key))


def build_top_strategies_markdown(
    report: BatchBacktestReport,
    *,
    top_count: int = 100,
    ranking: Ranking = "return",
    min_trades: int = 1,
    candle_start: datetime | None = None,
    candle_end: datetime | None = None,
) -> str:
    """Build a readable Markdown table of the strongest eligible strategies."""

    if top_count < 1:
        raise BacktestValidationError("top_count must be at least one")
    ranked = ranked_strategies(report, ranking=ranking, min_trades=min_trades)
    candle_start = candle_start or report.data_start
    candle_end = candle_end or report.data_end
    selected = ranked[:top_count]
    lines = [
        f"# Top {top_count} Pine Strategies",
        "",
        "## Plain-English summary",
        (
            f"This list contains up to {top_count} strategies from the "
            f"{report.exchange}:{report.symbol}:{report.timeframe} baseline. "
            "Only strategies that produced at least one completed trade are eligible."
        ),
        "",
        f"- **Ranking method:** {_ranking_description(ranking)}",
        f"- **Minimum trades:** {min_trades}",
        f"- **Strategies eligible:** {len(ranked)}",
        f"- **Candles used:** {report.candles}",
        f"- **Runtime version:** `{report.runtime_version}`",
    ]
    period = _format_period(candle_start, candle_end)
    if period:
        lines.append(f"- **Candle period:** {period}")
    lines.extend(
        [
            "",
            "The ranking is a baseline result, not a promise of future performance. "
            "A high return can come from a short sample, a lucky period, or an "
            "overfit strategy, so these candidates should be tested again on unseen data.",
            "",
            "## Strongest results",
            "",
        ]
    )
    if not selected:
        lines.append("No strategies met the eligibility and ranking requirements.")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "| Rank | Strategy | Completed trades | Return | Max drawdown | "
            "Final equity | Source file |",
            "| ---: | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for rank, result in enumerate(selected, start=1):
        lines.append(
            "| "
            + " | ".join(
                (
                    str(rank),
                    _escape_cell(result.name),
                    str(result.trades),
                    _format_percent(result.total_return_pct),
                    _format_percent(result.max_drawdown_pct),
                    _format_number(result.final_equity),
                    f"`{_escape_code(_display_source_path(result.path))}`",
                )
            )
            + " |"
        )
    lines.extend(["", "## What the leaders did", ""])
    for rank, result in enumerate(selected[:10], start=1):
        lines.append(
            f"{rank}. **{_escape_inline(result.name)}** returned "
            f"{_format_percent(result.total_return_pct)} over {result.trades} completed "
            f"trades, with a maximum drawdown of {_format_percent(result.max_drawdown_pct)}."
        )
    lines.extend(
        [
            "",
            "## How to read the numbers",
            "",
            "- **Return** is the change in account equity from the starting cash balance.",
            "- **Max drawdown** is the largest peak-to-trough decline seen during the test.",
            "- **Completed trades** counts round trips; an open position is not counted.",
            "- **Source file** identifies the archived Pine script by filename.",
            "",
            "The full machine-readable results remain in the companion JSON and CSV reports.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_overall_markdown(
    report: BatchBacktestReport,
    *,
    ranking: Ranking = "return",
    min_trades: int = 1,
    top_count: int = 10,
    candle_start: datetime | None = None,
    candle_end: datetime | None = None,
) -> str:
    """Build a non-technical summary of the complete baseline run."""

    summary = report.summary
    total = int(summary["total"])
    trade_results = [
        result
        for result in report.results
        if result.status == "backtested" and result.total_return_pct is not None
    ]
    returns = [float(result.total_return_pct or 0.0) for result in trade_results]
    drawdowns = [float(result.max_drawdown_pct or 0.0) for result in trade_results]
    ranked = ranked_strategies(report, ranking=ranking, min_trades=min_trades)
    candle_start = candle_start or report.data_start
    candle_end = candle_end or report.data_end
    positive = sum(value > 0 for value in returns)
    negative = sum(value < 0 for value in returns)
    flat = len(returns) - positive - negative
    approximation_count = sum(bool(result.approximations) for result in report.results)
    library_count = sum(bool(result.library_dependencies) for result in report.results)
    lines = [
        "# Overall Pine Backtest Baseline",
        "",
        "## Executive summary",
        "",
        (
            f"This run attempted {total:,} archived Pine strategies against "
            f"{report.candles:,} {report.exchange} {report.symbol} {report.timeframe} candles. "
            "It is intended to establish a broad technical baseline, not to certify "
            "strategies as profitable or safe."
        ),
        "",
        f"- **Run time:** {_format_duration(float(summary['elapsed_seconds']))}",
        f"- **Runtime version:** `{report.runtime_version}`",
        f"- **Strategies attempted:** {total:,}",
        f"- **Produced completed trades:** {len(trade_results):,}",
        f"- **Ran without completed orders:** {int(summary['no_orders']):,}",
        f"- **Could not be evaluated:** {total - len(report.successful):,}",
        f"- **Used an explicit approximation:** {approximation_count:,}",
        f"- **Used a local Pine library:** {library_count:,}",
        f"- **Duplicate source groups:** {len(report.duplicate_hashes):,}",
        "- **Detailed shortlist:** See the companion `top-100-strategies.md` report.",
    ]
    period = _format_period(candle_start, candle_end)
    if period:
        lines.append(f"- **Candle period:** {period}")
    if report.data_hash:
        lines.append(f"- **Candle fingerprint:** `{report.data_hash}`")
    if report.data_source:
        lines.append(f"- **Candle source:** `{report.data_source}`")
    if report.data_warnings:
        lines.append(f"- **Data warnings:** {'; '.join(report.data_warnings)}")
    lines.extend(
        [
            "",
            "## Results at a glance",
            "",
            "| Outcome | Count | What it means |",
            "| --- | ---: | --- |",
            _table_row(
                "Backtested with trades",
                f"{int(summary['backtested']):,}",
                "The runtime ran and closed at least one trade.",
            ),
            _table_row(
                "No orders",
                f"{int(summary['no_orders']):,}",
                "The runtime ran but did not close a trade on this data.",
            ),
            _table_row(
                "Validation errors",
                f"{int(summary['validation_errors']):,}",
                "The current runtime could not evaluate part of the strategy.",
            ),
            _table_row(
                "Execution-limit stops",
                f"{int(summary['execution_limits']):,}",
                "The strategy exceeded the safety step budget.",
            ),
            _table_row(
                "Runtime errors",
                f"{int(summary['runtime_errors']):,}",
                "An unexpected runtime problem stopped the strategy.",
            ),
            _table_row(
                "Parse errors",
                f"{int(summary['parse_errors']):,}",
                "The source could not be parsed.",
            ),
            "",
            "## Performance snapshot",
            "",
        ]
    )
    if returns:
        best = (
            ranked[0]
            if ranked
            else max(trade_results, key=lambda item: float(item.total_return_pct or 0))
        )
        worst = min(trade_results, key=lambda item: float(item.total_return_pct or 0))
        lines.extend(
            [
                (
                    f"- **{positive:,}** strategies with trades finished positive; "
                    f"**{negative:,}** finished negative; **{flat:,}** were approximately flat."
                ),
                (
                    "- The median return among trade-producing strategies was "
                    f"**{_format_percent(median(returns))}**; the average was "
                    f"**{_format_percent(mean(returns))}**."
                ),
                f"- The median maximum drawdown was **{_format_percent(median(drawdowns))}**.",
                (
                    f"- The strongest eligible result was **{_escape_inline(best.name)}** "
                    f"at **{_format_percent(best.total_return_pct)}**."
                ),
                (
                    f"- The weakest trade-producing result was **{_escape_inline(worst.name)}** "
                    f"at **{_format_percent(worst.total_return_pct)}**."
                ),
                "",
                "## Top candidates",
                "",
                (
                    f"The table below uses {_ranking_description(ranking)} and requires "
                    f"at least {min_trades} completed trade(s)."
                ),
                "",
                "| Rank | Strategy | Trades | Return | Max drawdown |",
                "| ---: | --- | ---: | ---: | ---: |",
            ]
        )
        for rank, result in enumerate(ranked[: max(1, top_count)], start=1):
            lines.append(
                f"| {rank} | {_escape_cell(result.name)} | {result.trades} | "
                f"{_format_percent(result.total_return_pct)} | "
                f"{_format_percent(result.max_drawdown_pct)} |"
            )
    else:
        lines.append(
            "No strategy produced a completed trade in this run, so there is no "
            "performance ranking."
        )
    errors = [result for result in report.results if result.error]
    if errors:
        categories = Counter(_categorize_error(result.error or "") for result in errors)
        lines.extend(
            [
                "",
                "## Main evaluation blockers",
                "",
                "These are the most common reasons a strategy could not be evaluated. "
                "They describe coverage gaps in the compact runtime, not necessarily "
                "bad Pine source.",
                "",
                "| Blocker | Strategies | Share of recorded errors |",
                "| --- | ---: | ---: |",
            ]
        )
        for category, count in categories.most_common(12):
            lines.append(
                _table_row(
                    category,
                    f"{count:,}",
                    _format_percent(count / len(errors) * 100),
                )
            )
    lines.extend(
        [
            "",
            "## Important interpretation notes",
            "",
            (
                "1. This is one market, one timeframe, and one historical sample. "
                "It does not establish out-of-sample performance."
            ),
            (
                "2. The baseline uses the compact Pine execution model documented in "
                "`docs/backtesting.md`; unsupported TradingView features are recorded "
                "rather than silently treated as trading logic."
            ),
            (
                "3. Orders are modeled at candle close with the configured fee and "
                "slippage assumptions. Real fills can be worse, especially during fast markets."
            ),
            (
                "4. A strategy with few trades can rank highly by chance. Review trade "
                "count, drawdown, and robustness together."
            ),
            (
                "5. The top list is a screening step. Do not deploy candidates without "
                "longer-history testing, multiple markets, walk-forward testing, and "
                "out-of-sample validation."
            ),
            "",
            "## Recommended next step",
            "",
            (
                "Take the top candidates from `top-100-strategies.md`, remove duplicate "
                "implementations, and rerun them on a longer, non-overlapping period and "
                "at least one additional market. Compare return, drawdown, trade count, "
                "and stability rather than return alone."
            ),
        ]
    )
    config = report.config
    if config is not None:
        lines.extend(
            [
                "",
                "## Recorded execution assumptions",
                "",
                f"- Starting cash: {_format_number(config.initial_cash)}",
                f"- Fee rate: {_format_percent(config.fee_rate * 100)}",
                f"- Slippage: {config.slippage_bps:g} basis points",
                f"- Default quantity: {_format_quantity(config.default_qty)}",
                f"- Pyramiding entries: {config.pyramiding}",
                f"- Short entries: {'allowed' if config.allow_short else 'disabled'}",
                f"- Close open position at end: {'yes' if config.close_at_end else 'no'}",
                f"- Execution-step limit: {config.max_execution_steps:,}",
                f"- Execution-time limit: {config.max_execution_seconds:g}s",
                f"- Intrabar policy: {config.intrabar_policy}",
            ]
        )
    return "\n".join(lines) + "\n"


def build_benchmark_markdown(benchmarks: Mapping[str, BacktestReport]) -> str:
    """Build a compact comparison table for standard research baselines."""

    lines = [
        "## Benchmark comparison",
        "",
        "These baselines use the same candle window and broker assumptions as the strategy run.",
        "",
        "| Benchmark | Completed trades | Return | Max drawdown | Final equity |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, benchmark in benchmarks.items():
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape_cell(name.replace("_", " ").title()),
                    str(len(benchmark)),
                    _format_percent(benchmark.metrics["total_return_pct"]),
                    _format_percent(benchmark.metrics["max_drawdown_pct"]),
                    _format_number(benchmark.final_equity),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_plain_english_reports(
    report: BatchBacktestReport,
    output_dir: str | Path = "reports",
    *,
    top_count: int = 100,
    ranking: Ranking = "return",
    min_trades: int = 1,
    candle_start: datetime | None = None,
    candle_end: datetime | None = None,
) -> tuple[Path, Path]:
    """Write separate top-strategy and overall Markdown reports."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    top_path = directory / f"top-{top_count}-strategies.md"
    overall_path = directory / "overall-baseline.md"
    top_path.write_text(
        build_top_strategies_markdown(
            report,
            top_count=top_count,
            ranking=ranking,
            min_trades=min_trades,
            candle_start=candle_start,
            candle_end=candle_end,
        ),
        encoding="utf-8",
    )
    overall_path.write_text(
        build_overall_markdown(
            report,
            ranking=ranking,
            min_trades=min_trades,
            top_count=min(10, top_count),
            candle_start=candle_start,
            candle_end=candle_end,
        ),
        encoding="utf-8",
    )
    return top_path, overall_path


def _ranking_description(ranking: Ranking) -> str:
    return {
        "return": "raw total return, highest first; lower drawdown breaks ties",
        "risk-adjusted": "return divided by maximum drawdown, highest first",
        "drawdown": "lowest maximum drawdown first, with return as a tie-breaker",
    }[ranking]


def _format_period(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return ""
    return f"{start.isoformat()} through {end.isoformat()}"


def _format_percent(value: float | int | None) -> str:
    return "n/a" if value is None else f"{float(value):.2f}%"


def _format_number(value: float | int | None) -> str:
    return "n/a" if value is None else f"{float(value):,.2f}"


def _format_quantity(value: float) -> str:
    return f"{value:.6g}"


def _format_duration(seconds: float) -> str:
    minutes, remainder = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {remainder}s"
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def _table_row(*cells: str) -> str:
    return "| " + " | ".join(_escape_cell(cell) for cell in cells) + " |"


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _escape_inline(value: str) -> str:
    return _escape_cell(value).replace("*", "\\*").replace("_", "\\_")


def _escape_code(value: str) -> str:
    return value.replace("`", "'")


def _display_source_path(path: str) -> str:
    return f"Strategies/{Path(path).name}"


def _categorize_error(error: str) -> str:
    normalized = re.sub(r"\s+", " ", error.strip().lower())
    if "execution step limit" in normalized:
        return "Runaway loop or excessive runtime steps"
    if "unknown pine identifier" in normalized:
        return "Unknown Pine identifier or constant"
    if "unsupported pine call target" in normalized:
        return "Unsupported call target"
    if "expected numeric pine value" in normalized:
        return "Required numeric value was unavailable"
    if "tuple assignment" in normalized:
        return "Tuple assignment could not be matched"
    if "ta.length" in normalized:
        return "Invalid technical-analysis length"
    if "order exceeds available cash" in normalized:
        return "Order exceeded available cash"
    if "object of type" in normalized or "has no len" in normalized:
        return "Unexpected missing object or value"
    if "unsupported pine" in normalized:
        return "Other unsupported Pine runtime feature"
    if "source cannot be empty" in normalized:
        return "Empty source"
    return error.split(":", 1)[0][:80] or "Unclassified error"
