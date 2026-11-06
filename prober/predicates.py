"""Composable functional predicates for Checker condition evaluation."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

PredicateResult = tuple[bool, str]
PredicateFn = Callable[[Any], PredicateResult]

SUPPORTED_CONDITIONS: tuple[str, ...] = (
    "equal",
    "not_equal",
    "bigger_than",
    "smaller_than",
    "between",
    "contains",
    "must_have",
    "must_not_have",
    "regex",
)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _preview(value: Any, max_len: int = 120) -> str:
    if isinstance(value, float) and not isinstance(value, bool):
        if value != 0.0 and abs(value) < 0.01:
            return f"{value:.10f}".rstrip("0").rstrip(".")
    rendered = repr(value)
    if len(rendered) > max_len:
        return rendered[: max_len - 3] + "..."
    return rendered


def make_equal_predicate(expected: Any) -> PredicateFn:
    def _check(actual: Any) -> PredicateResult:
        if isinstance(expected, list) and not isinstance(actual, list):
            if actual in expected:
                return True, f"equal to {_preview(actual)} (in {_preview(expected)})"
            return False, f"expected one of {_preview(expected)}, got {_preview(actual)}"
        if actual == expected:
            return True, f"equal to {_preview(expected)}"
        return False, f"expected equal to {_preview(expected)}, got {_preview(actual)}"

    return _check


def make_not_equal_predicate(expected: Any) -> PredicateFn:
    def _check(actual: Any) -> PredicateResult:
        if isinstance(expected, list) and not isinstance(actual, list):
            if actual not in expected:
                return True, f"not in {_preview(expected)}"
            return False, f"expected value not in {_preview(expected)}, got {_preview(actual)}"
        if actual != expected:
            return True, f"not equal to {_preview(expected)}"
        return False, f"expected not equal to {_preview(expected)}, got {_preview(actual)}"

    return _check


def make_bigger_than_predicate(expected: Any) -> PredicateFn:
    thresholds = _as_list(expected)

    def _check(actual: Any) -> PredicateResult:
        if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False, f"expected numeric value for bigger_than, got {_preview(actual)}"
        for threshold in thresholds:
            if not (actual > float(threshold)):
                return False, f"expected > {threshold}, got {actual}"
        return True, f"{actual} > {thresholds if len(thresholds) > 1 else thresholds[0]}"

    return _check


def make_smaller_than_predicate(expected: Any) -> PredicateFn:
    thresholds = _as_list(expected)

    def _check(actual: Any) -> PredicateResult:
        if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False, f"expected numeric value for smaller_than, got {_preview(actual)}"
        for threshold in thresholds:
            if not (actual < float(threshold)):
                return False, f"expected < {threshold}, got {actual}"
        return True, f"{actual} < {thresholds if len(thresholds) > 1 else thresholds[0]}"

    return _check


def make_between_predicate(expected: Any) -> PredicateFn:
    if (
        isinstance(expected, list)
        and len(expected) == 2
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in expected)
    ):
        ranges = [(float(expected[0]), float(expected[1]))]
    elif isinstance(expected, list) and all(
        isinstance(r, list) and len(r) == 2 for r in expected
    ):
        ranges = [(float(r[0]), float(r[1])) for r in expected]
    else:
        raise ValueError(
            f"'between' requires [min, max] or [[min, max], ...], got {expected!r}"
        )

    def _check(actual: Any) -> PredicateResult:
        if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False, f"expected numeric value for between, got {_preview(actual)}"
        for low, high in ranges:
            if low <= float(actual) <= high:
                return True, f"{actual} is between [{low}, {high}]"
        return False, f"expected {actual} to be between {ranges}"

    return _check


def make_contains_predicate(expected: Any) -> PredicateFn:
    items = _as_list(expected)

    def _check(actual: Any) -> PredicateResult:
        if actual is None:
            return False, f"expected value containing {_preview(expected)}, got None"
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            if actual in items:
                return True, f"{actual} in {_preview(items)}"
            return False, f"expected {actual} to be in {_preview(items)}"
        if isinstance(actual, str):
            missing = [str(item) for item in items if str(item) not in actual]
            if missing:
                return False, f"missing substring(s) {_preview(missing)} in {_preview(actual)}"
            return True, f"contains {_preview(items)}"
        if isinstance(actual, (list, tuple, set, dict)):
            missing = [item for item in items if item not in actual]
            if missing:
                return False, f"missing element(s) {_preview(missing)} in {_preview(actual)}"
            return True, f"contains {_preview(items)}"
        return False, f"cannot evaluate 'contains' on {_preview(actual)}"

    return _check


def make_must_have_predicate(expected: Any) -> PredicateFn:
    def _check(actual: Any) -> PredicateResult:
        if expected is True:
            if actual is not None:
                return True, f"value is present ({_preview(actual)})"
            return False, "expected value to be present, got None"
        if expected is False:
            if actual is None:
                return True, "value is absent as expected"
            return False, f"expected value to be absent, got {_preview(actual)}"

        items = _as_list(expected)
        if actual is None:
            return False, f"must_have {_preview(items)} failed because actual value is None"
        if isinstance(actual, str):
            missing = [str(item) for item in items if str(item) not in actual]
            if missing:
                return False, f"must_have missing substring(s) {_preview(missing)} in {_preview(actual)}"
            return True, f"has required substring(s) {_preview(items)}"
        if isinstance(actual, (list, tuple, set, dict)):
            missing = [item for item in items if item not in actual]
            if missing:
                return False, f"must_have missing item(s) {_preview(missing)} in {_preview(actual)}"
            return True, f"has required item(s) {_preview(items)}"
        if actual in items:
            return True, f"has required value {_preview(actual)}"
        return False, f"must_have expected {_preview(items)}, got {_preview(actual)}"

    return _check


def make_must_not_have_predicate(expected: Any) -> PredicateFn:
    def _check(actual: Any) -> PredicateResult:
        if expected is True:
            if actual is None:
                return True, "value is absent (None)"
            return False, f"expected value to be absent, got {_preview(actual)}"

        items = _as_list(expected)
        if actual is None:
            return True, "value is None (contains none of forbidden items)"
        if isinstance(actual, str):
            found = [str(item) for item in items if str(item) in actual]
            if found:
                return False, f"must_not_have found forbidden substring(s) {_preview(found)}"
            return True, f"does not contain {_preview(items)}"
        if isinstance(actual, (list, tuple, set, dict)):
            found = [item for item in items if item in actual]
            if found:
                return False, f"must_not_have found forbidden item(s) {_preview(found)}"
            return True, f"does not contain {_preview(items)}"
        if actual in items:
            return False, f"must_not_have found forbidden value {_preview(actual)}"
        return True, f"value {_preview(actual)} not in forbidden {_preview(items)}"

    return _check


def make_regex_predicate(expected: Any) -> PredicateFn:
    patterns = [re.compile(str(p), re.DOTALL) for p in _as_list(expected)]

    def _check(actual: Any) -> PredicateResult:
        if actual is None:
            return False, "cannot match regex against None"
        text = actual if isinstance(actual, str) else str(actual)
        for pat in patterns:
            if not pat.search(text):
                return False, f"regex {pat.pattern!r} did not match {_preview(text)}"
        return True, f"matched regex {[p.pattern for p in patterns]}"

    return _check


_PREDICATE_BUILDERS: dict[str, Callable[[Any], PredicateFn]] = {
    "equal": make_equal_predicate,
    "not_equal": make_not_equal_predicate,
    "bigger_than": make_bigger_than_predicate,
    "smaller_than": make_smaller_than_predicate,
    "between": make_between_predicate,
    "contains": make_contains_predicate,
    "must_have": make_must_have_predicate,
    "must_not_have": make_must_not_have_predicate,
    "regex": make_regex_predicate,
}


def build_predicates(config: dict[str, Any]) -> list[tuple[str, PredicateFn]]:
    """Build an ordered list of (condition_name, predicate_fn) from a checker configuration dict."""
    predicates: list[tuple[str, PredicateFn]] = []
    for key, builder in _PREDICATE_BUILDERS.items():
        if key in config and config[key] is not None:
            predicates.append((key, builder(config[key])))
    return predicates


def evaluate_predicates(
    actual: Any,
    predicates: list[tuple[str, PredicateFn]],
    subject_label: str,
) -> tuple[bool, list[str]]:
    """Evaluate all predicates against `actual`, returning (all_passed, details)."""
    details: list[str] = []
    all_passed = True
    for cond_name, fn in predicates:
        passed, msg = fn(actual)
        status_tag = "OK" if passed else "FAIL"
        details.append(f"[{status_tag}] {subject_label} ({cond_name}): {msg}")
        if not passed:
            all_passed = False
    return all_passed, details
