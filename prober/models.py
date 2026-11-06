"""Core immutable data models for Fetchers, Checkers, and Probe execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def format_usd(amount: float | int | None) -> str:
    """Format a numeric USD value as readable currency (e.g. '$0.00000348' or '$142.96') without scientific notation."""
    if amount is None or isinstance(amount, bool):
        return "N/A"
    val = float(amount)
    sign = "-" if val < 0 else ""
    abs_val = abs(val)
    if abs_val == 0.0:
        return "$0.00"
    if abs_val >= 0.01:
        return f"{sign}${abs_val:,.2f}"
    formatted = f"{abs_val:.10f}".rstrip("0").rstrip(".")
    if "." not in formatted:
        formatted += ".00"
    elif len(formatted.split(".")[1]) < 2:
        formatted += "0"
    return f"{sign}${formatted}"



@dataclass(frozen=True)
class LLMResult:
    """Normalized LLM response metadata across OpenAI Chat, Responses, and Anthropic Messages."""

    surface: str
    model: str | None = None
    assistant_text: str | None = None
    finish_reason: str | None = None
    status: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    stream_chunks_count: int = 0
    stream_done: bool = False
    stream_error: dict[str, Any] | None = None

    @property
    def tool_call_names(self) -> list[str]:
        names: list[str] = []
        for tc in self.tool_calls:
            if isinstance(tc, dict):
                fn = tc.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    names.append(fn["name"])
                elif isinstance(tc.get("name"), str):
                    names.append(tc["name"])
        return names


@dataclass(frozen=True)
class RequestTimings:
    """Granular socket & HTTP phase timings in milliseconds for a single request."""

    dns_lookup_ms: float = 0.0
    dialup_ms: float = 0.0
    req_write_ms: float = 0.0
    resp_wait_ms: float = 0.0
    resp_read_ms: float = 0.0


@dataclass(frozen=True)
class ProbeResponse:
    """Comprehensive immutable snapshot of a single HTTP/API/LLM fetch operation."""

    url: str
    method: str
    request_headers: dict[str, str]
    request_body: str | None
    status_code: int | None
    headers: dict[str, str]
    raw_body: bytes
    text: str
    json_data: Any | None
    llm: LLMResult | None
    duration_ms: float
    attempts: int = 1
    retry_errors: list[str] = field(default_factory=list)
    transport_error: str | None = None
    timings: RequestTimings | None = None

    def get_header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        return self.headers.get(name.lower())


@dataclass(frozen=True)
class CheckResult:
    """Evaluation outcome of a single Checker against a ProbeResponse."""

    checker_name: str
    checker_type: str
    passed: bool
    message: str
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProbeOutcome:
    """Final outcome of executing a Probe (including retries and all checker evaluations)."""

    probe_name: str
    tags: list[str]
    fetcher_type: str
    passed: bool
    aborted: bool
    response: ProbeResponse | None
    check_results: list[CheckResult]
    duration_ms: float
    retries_used: int = 0
    retry_history: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScanReport:
    """Aggregated summary of a full scan run."""

    config_path: str
    outcomes: list[ProbeOutcome]
    total_duration_ms: float
    aborted_early: bool = False

    @property
    def total_executed(self) -> int:
        return sum(1 for o in self.outcomes if not o.aborted)

    @property
    def total_passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed and not o.aborted)

    @property
    def total_failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.passed and not o.aborted)

    @property
    def total_aborted(self) -> int:
        return sum(1 for o in self.outcomes if o.aborted)

    @property
    def total_retries(self) -> int:
        return sum(o.retries_used for o in self.outcomes)

    @property
    def total_cost_usd(self) -> float:
        total = 0.0
        for o in self.outcomes:
            if o.response and o.response.llm and o.response.llm.cost is not None:
                total += float(o.response.llm.cost)
        return total

