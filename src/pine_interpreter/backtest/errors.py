"""Stable error categories and provenance helpers for backtests."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pine_interpreter.backtest.models import (
    BacktestExecutionLimitError,
    BacktestExecutionTimeoutError,
    BacktestValidationError,
)
from pine_interpreter.diagnostics import PineError, PineRuntimeError, PineSyntaxError


class BacktestIssueCategory(StrEnum):
    """Machine-readable categories for batch and research reports."""

    PARSE = "parse_error"
    VALIDATION = "validation_error"
    EXECUTION_LIMIT = "execution_limit"
    EXECUTION_TIMEOUT = "execution_timeout"
    RUNTIME = "runtime_error"
    IO = "io_error"
    DATA = "data_error"
    WORKER = "worker_error"
    UNSUPPORTED_NAMESPACE = "unsupported_namespace"
    UNKNOWN_BUILTIN = "unknown_builtin"
    TYPE_MISMATCH = "type_mismatch"
    MISSING_VALUE = "missing_value"
    INVALID_ORDER = "invalid_order"
    INSUFFICIENT_DATA = "insufficient_data"
    UNKNOWN = "unknown_error"


def classify_error(error: BaseException) -> BacktestIssueCategory:
    """Map an exception to a stable category for aggregation and filtering."""

    if isinstance(error, PineSyntaxError):
        return BacktestIssueCategory.PARSE
    if isinstance(error, BacktestExecutionLimitError):
        return BacktestIssueCategory.EXECUTION_LIMIT
    if isinstance(error, BacktestExecutionTimeoutError):
        return BacktestIssueCategory.EXECUTION_TIMEOUT
    if isinstance(error, (FileNotFoundError, PermissionError, OSError)):
        return BacktestIssueCategory.IO
    if isinstance(error, PineRuntimeError):
        return BacktestIssueCategory.RUNTIME
    if isinstance(error, BacktestValidationError):
        message = str(error).casefold()
        if "unknown strategy member" in message or "unsupported namespace" in message:
            return BacktestIssueCategory.UNSUPPORTED_NAMESPACE
        if "unknown pine identifier" in message or "unknown builtin" in message:
            return BacktestIssueCategory.UNKNOWN_BUILTIN
        if "required numeric value" in message or "none" in message or "na" in message:
            return BacktestIssueCategory.MISSING_VALUE
        if "length" in message or "not enough candles" in message:
            return BacktestIssueCategory.INSUFFICIENT_DATA
        if any(token in message for token in ("order", "quantity", "cash", "pyramiding", "stop")):
            return BacktestIssueCategory.INVALID_ORDER
        if any(token in message for token in ("type", "expects", "sequence", "tuple")):
            return BacktestIssueCategory.TYPE_MISMATCH
        return BacktestIssueCategory.VALIDATION
    if isinstance(error, (TypeError, ValueError)):
        return BacktestIssueCategory.DATA
    return BacktestIssueCategory.UNKNOWN


def error_location(error: BaseException) -> str | None:
    """Return a source location string when the exception carries one."""

    if isinstance(error, PineError) and error.location is not None:
        return str(error.location)
    location = getattr(error, "location", None)
    return None if location is None else str(location)


def source_digest(source: str | bytes) -> str:
    """Return a stable SHA-256 digest for a Pine source file."""

    payload = source.encode("utf-8") if isinstance(source, str) else source
    return hashlib.sha256(payload).hexdigest()
