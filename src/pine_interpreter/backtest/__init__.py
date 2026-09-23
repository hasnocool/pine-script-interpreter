"""Public API for CCXT-backed Pine backtests."""

from pine_interpreter.backtest.batch import (
    BatchBacktestReport,
    StrategyResult,
    discover_strategy_files,
    run_strategy_batch,
)
from pine_interpreter.backtest.data import (
    CCXTDataFeed,
    candles_to_rows,
    fetch_public_ohlcv,
    load_candles,
    save_candles,
)
from pine_interpreter.backtest.engine import BacktestEngine, PineBacktester
from pine_interpreter.backtest.markdown import (
    Ranking,
    build_overall_markdown,
    build_top_strategies_markdown,
    ranked_strategies,
    write_plain_english_reports,
)
from pine_interpreter.backtest.models import (
    BacktestConfig,
    BacktestError,
    BacktestExecutionLimitError,
    BacktestReport,
    BacktestValidationError,
    Candle,
    EquityPoint,
    Trade,
    print_report,
)

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestError",
    "BacktestExecutionLimitError",
    "BacktestReport",
    "BacktestValidationError",
    "BatchBacktestReport",
    "Ranking",
    "build_overall_markdown",
    "build_top_strategies_markdown",
    "CCXTDataFeed",
    "Candle",
    "EquityPoint",
    "PineBacktester",
    "StrategyResult",
    "Trade",
    "discover_strategy_files",
    "candles_to_rows",
    "fetch_public_ohlcv",
    "load_candles",
    "print_report",
    "run_strategy_batch",
    "save_candles",
    "ranked_strategies",
    "write_plain_english_reports",
]
