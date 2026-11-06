"""Concurrent Probe execution engine with tag filtering, retries, randomization, and fail-fast abort."""

from __future__ import annotations

import concurrent.futures
import random
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from prober.config import ProbeDefinition
from prober.models import CheckResult, ProbeOutcome, ProbeResponse, ScanReport

OnProbeCompleteCallback = Callable[[ProbeOutcome], None]
OnProbeRetryCallback = Callable[[str, int, int, str], None]


def parse_tags_filter(raw_tags: str | None) -> set[str]:
    """Parse '--tags=\"foo, baz, barz \"' into a set of trimmed lowercase strings."""
    if not raw_tags:
        return set()
    return {part.strip().lower() for part in raw_tags.split(",") if part.strip()}


def filter_and_order_probes(
    probes: list[ProbeDefinition],
    tags_filter: set[str] | None = None,
    randomize: bool = False,
    seed: int | None = None,
) -> list[ProbeDefinition]:
    """Filter probes by tags (if non-empty) and optionally shuffle them."""
    if tags_filter:
        selected = [p for p in probes if tags_filter.intersection(p.tags)]
    else:
        selected = list(probes)

    if randomize:
        rng = random.Random(seed)
        rng.shuffle(selected)

    return selected


def execute_single_probe(
    probe: ProbeDefinition,
    abort_event: threading.Event | None = None,
    on_retry: OnProbeRetryCallback | None = None,
) -> ProbeOutcome:
    """Execute a single ProbeDefinition with retry support and checker evaluation."""
    if abort_event is not None and abort_event.is_set():
        return ProbeOutcome(
            probe_name=probe.name,
            tags=probe.tags,
            fetcher_type=probe.fetcher.fetcher_type,
            passed=False,
            aborted=True,
            response=None,
            check_results=[],
            duration_ms=0.0,
            retries_used=0,
            retry_history=[],
        )

    max_retries = max(0, probe.fetcher.retries)
    retry_history: list[str] = []
    last_response: ProbeResponse | None = None
    last_check_results: list[CheckResult] = []
    probe_start_ns = time.perf_counter_ns()

    for attempt_idx in range(max_retries + 1):
        if attempt_idx > 0 and abort_event is not None and abort_event.is_set():
            break

        resp = probe.fetcher.execute_once()
        chk_results = [checker.check(resp) for checker in probe.checkers]
        all_passed = (resp.transport_error is None) and all(r.passed for r in chk_results)

        # Attach attempt metadata to response
        last_response = replace(
            resp,
            attempts=attempt_idx + 1,
            retry_errors=list(retry_history),
        )
        last_check_results = chk_results

        if all_passed:
            return ProbeOutcome(
                probe_name=probe.name,
                tags=probe.tags,
                fetcher_type=probe.fetcher.fetcher_type,
                passed=True,
                aborted=False,
                response=last_response,
                check_results=last_check_results,
                duration_ms=last_response.duration_ms,
                retries_used=attempt_idx,
                retry_history=retry_history,
            )

        # Failed this attempt; determine reason for retry log
        failed_msgs = [
            f"{r.checker_name}: {r.message}" for r in chk_results if not r.passed
        ]
        reason = resp.transport_error or "; ".join(failed_msgs) or "check failed"
        retry_history.append(f"Attempt {attempt_idx + 1} ({resp.duration_ms:.1f}ms): {reason}")

        if attempt_idx < max_retries:
            if on_retry is not None:
                on_retry(probe.name, attempt_idx + 1, max_retries, reason)
            delay = probe.fetcher.retry_delay_seconds
            if delay > 0:
                if abort_event is not None:
                    if abort_event.wait(timeout=delay):
                        break
                else:
                    time.sleep(delay)

    total_probe_ms = (time.perf_counter_ns() - probe_start_ns) / 1_000_000.0
    retries_used = max(0, (last_response.attempts - 1) if last_response else 0)
    return ProbeOutcome(
        probe_name=probe.name,
        tags=probe.tags,
        fetcher_type=probe.fetcher.fetcher_type,
        passed=False,
        aborted=False,
        response=last_response,
        check_results=last_check_results,
        duration_ms=last_response.duration_ms if (last_response and retries_used == 0) else total_probe_ms,
        retries_used=retries_used,
        retry_history=retry_history,
    )


class ScanRunner:
    """Executes a list of ProbeDefinitions concurrently with optional abort-on-failure."""

    def __init__(
        self,
        max_concurrent: int = 10,
        abort_on_failure: bool = False,
        on_probe_complete: OnProbeCompleteCallback | None = None,
        on_probe_retry: OnProbeRetryCallback | None = None,
    ) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.abort_on_failure = abort_on_failure
        self.on_probe_complete = on_probe_complete
        self.on_probe_retry = on_probe_retry

    def run(self, config_path: str, probes: list[ProbeDefinition]) -> ScanReport:
        start_ns = time.perf_counter_ns()
        abort_event = threading.Event()
        callback_lock = threading.Lock()
        outcomes_by_idx: dict[int, ProbeOutcome] = {}

        def _safe_retry_cb(probe_name: str, attempt: int, max_retries: int, reason: str) -> None:
            if self.on_probe_retry is not None:
                with callback_lock:
                    self.on_probe_retry(probe_name, attempt, max_retries, reason)

        def _worker(idx: int, probe: ProbeDefinition) -> tuple[int, ProbeOutcome]:
            outcome = execute_single_probe(
                probe,
                abort_event=abort_event,
                on_retry=_safe_retry_cb,
            )
            if not outcome.passed and not outcome.aborted and self.abort_on_failure:
                abort_event.set()
            if self.on_probe_complete is not None and not outcome.aborted:
                with callback_lock:
                    self.on_probe_complete(outcome)
            return idx, outcome

        if not probes:
            return ScanReport(
                config_path=config_path,
                outcomes=[],
                total_duration_ms=0.0,
                aborted_early=False,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            future_to_idx: dict[concurrent.futures.Future[tuple[int, ProbeOutcome]], int] = {}
            for idx, probe in enumerate(probes):
                if self.abort_on_failure and abort_event.is_set():
                    outcomes_by_idx[idx] = ProbeOutcome(
                        probe_name=probe.name,
                        tags=probe.tags,
                        fetcher_type=probe.fetcher.fetcher_type,
                        passed=False,
                        aborted=True,
                        response=None,
                        check_results=[],
                        duration_ms=0.0,
                    )
                    continue
                fut = pool.submit(_worker, idx, probe)
                future_to_idx[fut] = idx

            for fut in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[fut]
                try:
                    _, outcome = fut.result()
                    outcomes_by_idx[idx] = outcome
                except Exception as exc:
                    probe = probes[idx]
                    outcomes_by_idx[idx] = ProbeOutcome(
                        probe_name=probe.name,
                        tags=probe.tags,
                        fetcher_type=probe.fetcher.fetcher_type,
                        passed=False,
                        aborted=False,
                        response=None,
                        check_results=[],
                        duration_ms=0.0,
                        retry_history=[f"Unhandled runner exception: {exc}"],
                    )
                    if self.abort_on_failure:
                        abort_event.set()

                if self.abort_on_failure and abort_event.is_set():
                    for pending_fut, pending_idx in future_to_idx.items():
                        if not pending_fut.done() and pending_fut.cancel():
                            p = probes[pending_idx]
                            outcomes_by_idx[pending_idx] = ProbeOutcome(
                                probe_name=p.name,
                                tags=p.tags,
                                fetcher_type=p.fetcher.fetcher_type,
                                passed=False,
                                aborted=True,
                                response=None,
                                check_results=[],
                                duration_ms=0.0,
                            )

        ordered_outcomes = [outcomes_by_idx[i] for i in range(len(probes))]
        total_duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        return ScanReport(
            config_path=config_path,
            outcomes=ordered_outcomes,
            total_duration_ms=total_duration_ms,
            aborted_early=abort_event.is_set() and self.abort_on_failure,
        )
