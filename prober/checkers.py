"""Checker hierarchy composing functional predicates to validate ProbeResponse."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from prober.durations import parse_duration_ms
from prober.fetchers import coerce_bool
from prober.models import CheckResult, ProbeResponse
from prober.predicates import SUPPORTED_CONDITIONS, build_predicates, evaluate_predicates
from prober.schema_validator import validate_json_schema

CHECKER_REGISTRY: dict[str, type[BaseChecker]] = {}

_CAMEL_TO_SNAKE_MAP: dict[str, str] = {
    "musthave": "must_have",
    "mustnothave": "must_not_have",
    "biggerthan": "bigger_than",
    "smallerthan": "smaller_than",
    "equal": "equal",
    "notequal": "not_equal",
    "contains": "contains",
    "between": "between",
    "maxduration": "max_duration",
    "regex": "regex",
}

_BASE_METADATA_KEYS: frozenset[str] = frozenset({"name", "type"})


def normalize_checker_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    """Normalize condition keys (supporting both snake_case and PascalCase from project.md)."""
    normalized: dict[str, Any] = {}
    for k, v in raw_config.items():
        collapsed = k.replace("_", "").lower()
        canonical_key = _CAMEL_TO_SNAKE_MAP.get(collapsed, k.lower())
        normalized[canonical_key] = v
    return normalized


def register_checker(*names: str):
    """Class decorator registering a BaseChecker subclass under one or more type names."""

    def _decorator(cls: type[BaseChecker]) -> type[BaseChecker]:
        for name in names:
            CHECKER_REGISTRY[name.strip().lower()] = cls
        return cls

    return _decorator


def extract_json_path(data: Any, path: str | None) -> tuple[bool, Any]:
    """Extract a value from parsed JSON using dot/index notation (e.g. 'data.0.id' or 'usage.cost').

    Returns (found, value). Supports '*' to project across a list of objects.
    """
    if not path or path in ("$", "."):
        return (data is not None), data

    clean_path = path.lstrip("$.")
    if not clean_path:
        return (data is not None), data

    parts = clean_path.split(".")
    return _resolve_parts(data, parts)


def _resolve_parts(current: Any, parts: list[str]) -> tuple[bool, Any]:
    if not parts:
        return True, current

    head, *tail = parts
    if head == "*":
        if not isinstance(current, list):
            return False, None
        projected: list[Any] = []
        for elem in current:
            found, val = _resolve_parts(elem, tail)
            if found:
                projected.append(val)
        return (len(projected) > 0), projected

    if isinstance(current, dict):
        if head not in current:
            return False, None
        return _resolve_parts(current[head], tail)

    if isinstance(current, list):
        try:
            idx = int(head)
        except ValueError:
            return False, None
        if idx < -len(current) or idx >= len(current):
            return False, None
        return _resolve_parts(current[idx], tail)

    return False, None


class BaseChecker(ABC):
    """Abstract base class for all Checker types."""

    checker_type: str = "base"
    extra_allowed_keys: frozenset[str] = frozenset()

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = normalize_checker_config(config)
        self._validate_known_keys()

    def _validate_known_keys(self) -> None:
        allowed = _BASE_METADATA_KEYS | set(SUPPORTED_CONDITIONS) | set(self.extra_allowed_keys)
        unknown = set(self.config.keys()) - allowed
        if unknown:
            raise ValueError(
                f"Checker {self.name!r} ({self.checker_type}) has unknown parameter(s): "
                f"{sorted(unknown)}. Allowed: {sorted(allowed)}"
            )

    def check(self, response: ProbeResponse) -> CheckResult:
        """Evaluate this checker against a ProbeResponse."""
        if response.transport_error:
            return CheckResult(
                checker_name=self.name,
                checker_type=self.checker_type,
                passed=False,
                message=f"Transport failure: {response.transport_error}",
                details=[f"[FAIL] transport_error: {response.transport_error}"],
            )
        return self._evaluate(response)

    @abstractmethod
    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        """Subclass-specific evaluation logic."""


@register_checker("http_status", "status_http", "httpstatus", "status")
class HttpStatusChecker(BaseChecker):
    """Validates HTTP response status code."""

    checker_type = "http_status"

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self._predicates = build_predicates(self.config)
        if not self._predicates:
            raise ValueError(
                f"Checker {self.name!r} (http_status) defines no condition predicates"
            )

    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        passed, details = evaluate_predicates(
            response.status_code, self._predicates, f"status_code={response.status_code}"
        )
        summary = (
            f"HTTP status {response.status_code} passed"
            if passed
            else f"HTTP status {response.status_code} failed check"
        )
        return CheckResult(
            checker_name=self.name,
            checker_type=self.checker_type,
            passed=passed,
            message=summary,
            details=details,
        )


@register_checker("response_time", "responsetime", "latency")
class ResponseTimeChecker(BaseChecker):
    """Validates request duration in milliseconds (supports max_duration + numeric predicates)."""

    checker_type = "response_time"
    extra_allowed_keys = frozenset({"max_duration"})

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self._predicates = build_predicates(self.config)
        self._max_limits: list[tuple[Any, float]] = []
        if "max_duration" in self.config and self.config["max_duration"] is not None:
            raw_max = self.config["max_duration"]
            max_items = raw_max if isinstance(raw_max, list) else [raw_max]
            for item in max_items:
                self._max_limits.append((item, parse_duration_ms(item)))
        if not self._max_limits and not self._predicates:
            raise ValueError(
                f"Checker {self.name!r} (response_time) requires 'max_duration' or numeric conditions"
            )

    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        details: list[str] = []
        all_passed = True
        actual_ms = round(response.duration_ms, 2)

        for raw_item, limit_ms in self._max_limits:
            ok = response.duration_ms <= limit_ms
            tag = "OK" if ok else "FAIL"
            details.append(
                f"[{tag}] duration_ms={actual_ms}ms <= max_duration={limit_ms:.1f}ms ({raw_item})"
            )
            if not ok:
                all_passed = False

        if self._predicates:
            pred_ok, pred_details = evaluate_predicates(
                actual_ms, self._predicates, f"duration_ms={actual_ms}ms"
            )
            details.extend(pred_details)
            if not pred_ok:
                all_passed = False

        summary = (
            f"Response time {actual_ms}ms within bounds"
            if all_passed
            else f"Response time {actual_ms}ms exceeded threshold"
        )
        return CheckResult(
            checker_name=self.name,
            checker_type=self.checker_type,
            passed=all_passed,
            message=summary,
            details=details,
        )


def _is_numeric_expectation(val: Any) -> bool:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return True
    if isinstance(val, list) and val and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) for x in val
    ):
        return True
    return False


@register_checker("content", "body", "html_content")
class ContentChecker(BaseChecker):
    """Validates raw text/HTML response content or body length."""

    checker_type = "content"
    extra_allowed_keys = frozenset({"target"})

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self._predicates = build_predicates(self.config)
        if not self._predicates:
            raise ValueError(f"Checker {self.name!r} (content) defines no condition predicates")

    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        target_mode = str(self.config.get("target", "auto")).lower()
        details: list[str] = []
        all_passed = True

        for cond_name, fn in self._predicates:
            raw_expected = self.config.get(cond_name)
            use_length = (
                target_mode == "length"
                or cond_name in ("bigger_than", "smaller_than", "between")
                or (
                    target_mode == "auto"
                    and cond_name in ("equal", "not_equal")
                    and _is_numeric_expectation(raw_expected)
                )
            )
            if use_length:
                subject: Any = len(response.text)
                label = f"content.length={subject}"
            else:
                subject = response.text
                label = "content.text"

            ok, msg = fn(subject)
            tag = "OK" if ok else "FAIL"
            details.append(f"[{tag}] {label} ({cond_name}): {msg}")
            if not ok:
                all_passed = False

        summary = "Content check passed" if all_passed else "Content check failed"
        return CheckResult(
            checker_name=self.name,
            checker_type=self.checker_type,
            passed=all_passed,
            message=summary,
            details=details,
        )


@register_checker("header", "http_header", "headers")
class HeaderChecker(BaseChecker):
    """Validates HTTP response headers (case-insensitive header name)."""

    checker_type = "header"
    extra_allowed_keys = frozenset({"header"})

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self.header_name = str(self.config.get("header", "")).strip()
        if not self.header_name:
            raise ValueError(f"Checker {self.name!r} (header) requires a 'header' field")
        self._predicates = build_predicates(self.config)

    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        actual_val = response.get_header(self.header_name)
        if not self._predicates:
            passed = actual_val is not None
            msg = (
                f"Header {self.header_name!r} present ({actual_val!r})"
                if passed
                else f"Header {self.header_name!r} missing"
            )
            return CheckResult(
                checker_name=self.name,
                checker_type=self.checker_type,
                passed=passed,
                message=msg,
                details=[f"[{'OK' if passed else 'FAIL'}] {msg}"],
            )

        passed, details = evaluate_predicates(
            actual_val, self._predicates, f"header[{self.header_name!r}]"
        )
        summary = (
            f"Header {self.header_name!r} check passed"
            if passed
            else f"Header {self.header_name!r} check failed"
        )
        return CheckResult(
            checker_name=self.name,
            checker_type=self.checker_type,
            passed=passed,
            message=summary,
            details=details,
        )


@register_checker("json_path", "json_field", "json")
class JsonPathChecker(BaseChecker):
    """Extracts a value from JSON response via dot/index path and evaluates predicates."""

    checker_type = "json_path"
    extra_allowed_keys = frozenset({"path"})

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self.path = str(self.config.get("path", "$")).strip()
        self._predicates = build_predicates(self.config)

    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        if response.json_data is None:
            return CheckResult(
                checker_name=self.name,
                checker_type=self.checker_type,
                passed=False,
                message="Response body is not valid JSON",
                details=["[FAIL] json_data is None (response was not valid JSON)"],
            )

        found, value = extract_json_path(response.json_data, self.path)
        if not self._predicates:
            passed = found and value is not None
            return CheckResult(
                checker_name=self.name,
                checker_type=self.checker_type,
                passed=passed,
                message=f"JSON path {self.path!r} {'found' if passed else 'missing'}",
                details=[f"[{'OK' if passed else 'FAIL'}] json_path[{self.path!r}] = {value!r}"],
            )

        target_val = value if found else None
        passed, details = evaluate_predicates(target_val, self._predicates, f"json[{self.path!r}]")
        summary = (
            f"JSON path {self.path!r} passed"
            if passed
            else f"JSON path {self.path!r} failed check"
        )
        return CheckResult(
            checker_name=self.name,
            checker_type=self.checker_type,
            passed=passed,
            message=summary,
            details=details,
        )


@register_checker("json_schema", "jsonschema", "schema")
class JsonSchemaChecker(BaseChecker):
    """Validates parsed JSON (or a sub-path) against a declarative JSON Schema."""

    checker_type = "json_schema"
    extra_allowed_keys = frozenset({"schema", "path"})

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        if not isinstance(self.config.get("schema"), dict):
            raise ValueError(
                f"Checker {self.name!r} (json_schema) requires a 'schema' table/dict"
            )

    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        if response.json_data is None:
            return CheckResult(
                checker_name=self.name,
                checker_type=self.checker_type,
                passed=False,
                message="Cannot validate JSON Schema: response is not valid JSON",
                details=["[FAIL] json_data is None"],
            )

        schema = self.config["schema"]
        subpath = self.config.get("path")
        if subpath:
            found, target_data = extract_json_path(response.json_data, str(subpath))
            if not found:
                return CheckResult(
                    checker_name=self.name,
                    checker_type=self.checker_type,
                    passed=False,
                    message=f"JSON Schema target path {subpath!r} not found",
                    details=[f"[FAIL] path {subpath!r} does not exist in response JSON"],
                )
            root_label = f"$.{subpath}"
        else:
            target_data = response.json_data
            root_label = "$"

        errors = validate_json_schema(target_data, schema, path=root_label)
        passed = len(errors) == 0
        if passed:
            return CheckResult(
                checker_name=self.name,
                checker_type=self.checker_type,
                passed=True,
                message="JSON Schema validation passed",
                details=[f"[OK] {root_label} conforms to schema"],
            )
        return CheckResult(
            checker_name=self.name,
            checker_type=self.checker_type,
            passed=False,
            message=f"JSON Schema validation failed ({len(errors)} error(s))",
            details=[f"[FAIL] {err}" for err in errors],
        )


@register_checker("llm_response", "llm")
class LLMResponseChecker(BaseChecker):
    """Dedicated checker for normalized LLM responses (OpenAI Chat/Responses & Anthropic Messages)."""

    checker_type = "llm_response"
    extra_allowed_keys = frozenset({"field", "expect_stream_error", "expect_tool_calls"})

    _ALLOWED_FIELDS = {
        "surface",
        "model",
        "assistant_text",
        "finish_reason",
        "status",
        "tool_calls",
        "tool_call_names",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost",
        "stream_chunks_count",
        "stream_done",
        "stream_error",
    }

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        self.field_name = str(self.config.get("field", "assistant_text")).strip()
        if self.field_name not in self._ALLOWED_FIELDS:
            raise ValueError(
                f"Checker {self.name!r} (llm_response): unknown field {self.field_name!r}. "
                f"Allowed: {sorted(self._ALLOWED_FIELDS)}"
            )
        self._predicates = build_predicates(self.config)
        if (
            not self._predicates
            and "expect_stream_error" not in self.config
            and "expect_tool_calls" not in self.config
        ):
            raise ValueError(
                f"Checker {self.name!r} (llm_response) defines no predicates or assertions"
            )

    def _evaluate(self, response: ProbeResponse) -> CheckResult:
        if response.llm is None:
            return CheckResult(
                checker_name=self.name,
                checker_type=self.checker_type,
                passed=False,
                message="Response has no normalized LLMResult data",
                details=["[FAIL] response.llm is None"],
            )

        llm = response.llm
        details: list[str] = []
        all_passed = True

        if "expect_stream_error" in self.config:
            want_err = coerce_bool(self.config["expect_stream_error"], "expect_stream_error")
            has_err = llm.stream_error is not None
            ok = want_err == has_err
            details.append(
                f"[{'OK' if ok else 'FAIL'}] expect_stream_error={want_err} (actual error={llm.stream_error!r})"
            )
            if not ok:
                all_passed = False

        if "expect_tool_calls" in self.config:
            want_tc = coerce_bool(self.config["expect_tool_calls"], "expect_tool_calls")
            has_tc = len(llm.tool_calls) > 0
            ok = want_tc == has_tc
            details.append(
                f"[{'OK' if ok else 'FAIL'}] expect_tool_calls={want_tc} (tool_calls={llm.tool_call_names!r})"
            )
            if not ok:
                all_passed = False

        actual_val = getattr(llm, self.field_name)
        if self._predicates:
            pred_ok, pred_details = evaluate_predicates(
                actual_val, self._predicates, f"llm.{self.field_name}"
            )
            details.extend(pred_details)
            if not pred_ok:
                all_passed = False

        summary = (
            f"LLM response check ({self.field_name}) passed"
            if all_passed
            else f"LLM response check ({self.field_name}) failed"
        )
        return CheckResult(
            checker_name=self.name,
            checker_type=self.checker_type,
            passed=all_passed,
            message=summary,
            details=details,
        )


def create_checker(config: dict[str, Any], default_name: str | None = None) -> BaseChecker:
    """Instantiate a registered BaseChecker from a TOML checker definition."""
    name = str(config.get("name") or default_name or "").strip()
    if not name:
        raise ValueError("Checker definition is missing a 'name'")
    raw_type = str(config.get("type", "")).strip().lower()
    if not raw_type:
        raise ValueError(f"Checker {name!r} is missing 'type'")
    cls = CHECKER_REGISTRY.get(raw_type)
    if cls is None:
        available = ", ".join(sorted(CHECKER_REGISTRY.keys()))
        raise ValueError(
            f"Checker {name!r} has unknown type {raw_type!r}. Available types: {available}"
        )
    return cls(name=name, config=config)
