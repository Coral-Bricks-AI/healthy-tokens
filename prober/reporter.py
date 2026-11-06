"""Console progress and final report formatter with optional request/response debug and performance analytics."""

from __future__ import annotations

from collections import Counter
import json
import math
import statistics
import sys
from typing import TextIO

from prober.models import ProbeOutcome, ProbeResponse, ScanReport, format_usd


def _format_json_or_text(text: str | None, max_chars: int = 4000) -> str:
    if not text:
        return "<empty>"
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(pretty) > max_chars:
                return pretty[:max_chars] + "\n... [truncated]"
            return pretty
        except Exception:
            pass
    if len(text) > max_chars:
        return text[:max_chars] + "\n... [truncated]"
    return text


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Calculate the given percentile (0..100) from a non-empty sorted list of floats."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    low_idx = int(math.floor(rank))
    high_idx = int(math.ceil(rank))
    if low_idx == high_idx:
        return sorted_vals[low_idx]
    weight = rank - low_idx
    return sorted_vals[low_idx] * (1.0 - weight) + sorted_vals[high_idx] * weight


def _build_histogram(
    sorted_secs: list[float], num_bins: int = 11, bar_width: int = 40
) -> list[tuple[float, int, str]]:
    """Build a response time histogram of (bucket_upper_sec, count, bar_str)."""
    if not sorted_secs:
        return []
    fastest = sorted_secs[0]
    slowest = sorted_secs[-1]

    if math.isclose(fastest, slowest, abs_tol=1e-9):
        return [(fastest, len(sorted_secs), "■" * bar_width)]

    step = (slowest - fastest) / (num_bins - 1)
    counts = [0] * num_bins
    for val in sorted_secs:
        idx = int(round((val - fastest) / step))
        idx = max(0, min(num_bins - 1, idx))
        counts[idx] += 1

    max_count = max(counts) if counts else 1
    rows: list[tuple[float, int, str]] = []
    for i in range(num_bins):
        mark = fastest + i * step
        c = counts[i]
        if c == 0:
            bar_len = 0
        else:
            bar_len = int(round((c / max_count) * bar_width))
            if bar_len == 0:
                bar_len = 1
        rows.append((mark, c, "■" * bar_len))
    return rows


class ConsoleReporter:
    """Formats live probe events, retry notifications, debug dumps, and final summary reports."""

    def __init__(
        self,
        include_debug: bool = False,
        report_performance: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.include_debug = include_debug
        self.report_performance = report_performance
        self.stream: TextIO = stream or sys.stdout

    def _writeln(self, text: str = "") -> None:
        try:
            self.stream.write(text + "\n")
        except UnicodeEncodeError:
            safe_text = text.replace("■", "#").encode("ascii", errors="replace").decode("ascii")
            self.stream.write(safe_text + "\n")
        self.stream.flush()

    def on_retry(self, probe_name: str, attempt: int, max_retries: int, reason: str) -> None:
        self._writeln(f"  [RETRY {attempt}/{max_retries}] {probe_name} -> {reason}")

    def on_probe_complete(self, outcome: ProbeOutcome) -> None:
        status_label = "PASS" if outcome.passed else "FAIL"
        retry_suffix = (
            f" (retries: {outcome.retries_used})" if outcome.retries_used > 0 else ""
        )
        tags_str = f" [{', '.join(outcome.tags)}]" if outcome.tags else ""
        http_code = (
            f" HTTP {outcome.response.status_code}"
            if (outcome.response and outcome.response.status_code is not None)
            else ""
        )
        cost_suffix = (
            f" (cost: {format_usd(outcome.response.llm.cost)})"
            if (
                outcome.response
                and outcome.response.llm
                and outcome.response.llm.cost is not None
            )
            else ""
        )
        self._writeln(
            f"[{status_label}] {outcome.probe_name}{tags_str} "
            f"({outcome.fetcher_type}{http_code}) - {outcome.duration_ms:.2f} ms{cost_suffix}{retry_suffix}"
        )

        if not outcome.passed:
            if outcome.response and outcome.response.transport_error:
                self._writeln(f"       Transport Error: {outcome.response.transport_error}")
            for chk in outcome.check_results:
                if not chk.passed:
                    self._writeln(
                        f"       Checker [{chk.checker_name}] ({chk.checker_type}) FAILED: {chk.message}"
                    )
                    for detail in chk.details:
                        if detail.startswith("[FAIL]"):
                            self._writeln(f"         -> {detail}")

        if self.include_debug and outcome.response is not None:
            self._print_debug_block(outcome.probe_name, outcome.response)

    def _print_debug_block(self, probe_name: str, resp: ProbeResponse) -> None:
        self._writeln("  " + "-" * 66)
        self._writeln(f"  DEBUG [{probe_name}]")
        self._writeln(f"  >>> REQUEST: {resp.method} {resp.url}")
        self._writeln("  >>> REQUEST HEADERS:")
        for k, v in resp.request_headers.items():
            self._writeln(f"      {k}: {v}")
        self._writeln("  >>> REQUEST BODY:")
        for line in _format_json_or_text(resp.request_body).splitlines():
            self._writeln(f"      {line}")

        self._writeln(
            f"  <<< RESPONSE: status={resp.status_code} duration={resp.duration_ms:.2f} ms attempts={resp.attempts}"
        )
        if resp.transport_error:
            self._writeln(f"  <<< TRANSPORT ERROR: {resp.transport_error}")
        self._writeln("  <<< RESPONSE HEADERS:")
        for k, v in resp.headers.items():
            self._writeln(f"      {k}: {v}")
        self._writeln("  <<< RESPONSE BODY:")
        for line in _format_json_or_text(resp.text).splitlines():
            self._writeln(f"      {line}")
        if resp.llm is not None:
            self._writeln(
                f"  <<< NORMALIZED LLM: surface={resp.llm.surface} model={resp.llm.model} "
                f"finish_reason={resp.llm.finish_reason} text={resp.llm.assistant_text!r} "
                f"tokens={resp.llm.total_tokens} cost={format_usd(resp.llm.cost)}"
            )
        self._writeln("  " + "-" * 66)

    def _print_performance_report(self, report: ScanReport) -> None:
        valid_outcomes = [
            o for o in report.outcomes if not o.aborted and o.response is not None
        ]
        if not valid_outcomes:
            return

        total_sec = report.total_duration_ms / 1000.0
        durations_sec = sorted(o.duration_ms / 1000.0 for o in valid_outcomes)
        fastest_sec = durations_sec[0]
        slowest_sec = durations_sec[-1]
        avg_sec = statistics.mean(durations_sec)
        median_sec = statistics.median(durations_sec)
        rps = len(valid_outcomes) / total_sec if total_sec > 0 else 0.0

        self._writeln("-" * 72)
        self._writeln("Performance Summary:")
        self._writeln(
            f"  Total:        {total_sec:.4f} secs ({report.total_duration_ms:.2f} ms)"
        )
        self._writeln(
            f"  Slowest:      {slowest_sec:.4f} secs ({slowest_sec * 1000.0:.2f} ms)"
        )
        self._writeln(
            f"  Fastest:      {fastest_sec:.4f} secs ({fastest_sec * 1000.0:.2f} ms)"
        )
        self._writeln(
            f"  Average:      {avg_sec:.4f} secs ({avg_sec * 1000.0:.2f} ms)"
        )
        self._writeln(
            f"  Median:       {median_sec:.4f} secs ({median_sec * 1000.0:.2f} ms)"
        )
        self._writeln(f"  Requests/sec: {rps:.4f}")

        self._writeln("\nResponse time histogram:")
        for mark, count, bar in _build_histogram(durations_sec, num_bins=11, bar_width=40):
            count_field = f"[{count}]"
            self._writeln(f"  {mark:6.3f} {count_field:<7} |{bar}")

        self._writeln("\nLatency distribution:")
        for pct in (10, 25, 50, 75, 90, 95, 99):
            p_sec = _percentile(durations_sec, pct)
            self._writeln(f"  {pct:>2}% in {p_sec:.4f} secs ({p_sec * 1000.0:.2f} ms)")

        timed_resps = [
            o.response for o in valid_outcomes if o.response and o.response.timings is not None
        ]
        if timed_resps:
            def _phase_stats(values_ms: list[float]) -> tuple[float, float, float]:
                if not values_ms:
                    return 0.0, 0.0, 0.0
                secs = [v / 1000.0 for v in values_ms]
                return statistics.mean(secs), min(secs), max(secs)

            dialup_avg, dialup_min, dialup_max = _phase_stats(
                [r.timings.dialup_ms for r in timed_resps if r.timings]
            )
            dns_avg, dns_min, dns_max = _phase_stats(
                [r.timings.dns_lookup_ms for r in timed_resps if r.timings]
            )
            write_avg, write_min, write_max = _phase_stats(
                [r.timings.req_write_ms for r in timed_resps if r.timings]
            )
            wait_avg, wait_min, wait_max = _phase_stats(
                [r.timings.resp_wait_ms for r in timed_resps if r.timings]
            )
            read_avg, read_min, read_max = _phase_stats(
                [r.timings.resp_read_ms for r in timed_resps if r.timings]
            )

            self._writeln("\nDetails (average, fastest, slowest):")
            self._writeln(
                f"  DNS+dialup:   {dialup_avg:.4f} secs, {dialup_min:.4f} secs, {dialup_max:.4f} secs"
            )
            self._writeln(
                f"  DNS-lookup:   {dns_avg:.4f} secs, {dns_min:.4f} secs, {dns_max:.4f} secs"
            )
            self._writeln(
                f"  req write:    {write_avg:.4f} secs, {write_min:.4f} secs, {write_max:.4f} secs"
            )
            self._writeln(
                f"  resp wait:    {wait_avg:.4f} secs, {wait_min:.4f} secs, {wait_max:.4f} secs"
            )
            self._writeln(
                f"  resp read:    {read_avg:.4f} secs, {read_min:.4f} secs, {read_max:.4f} secs"
            )

        status_counts: Counter[str] = Counter()
        for o in valid_outcomes:
            if o.response and o.response.status_code is not None:
                status_counts[str(o.response.status_code)] += 1
            elif o.response and o.response.transport_error:
                status_counts["ERROR"] += 1

        if status_counts:
            self._writeln("\nStatus code distribution:")
            for code in sorted(status_counts.keys()):
                cnt = status_counts[code]
                word = "response" if cnt == 1 else "responses"
                self._writeln(f"  [{code}] {cnt} {word}")

    def print_summary(self, report: ScanReport) -> None:
        total_sec = report.total_duration_ms / 1000.0
        self._writeln("\n" + "=" * 72)
        self._writeln("SCAN SUMMARY REPORT")
        self._writeln("=" * 72)
        self._writeln(f"Config           : {report.config_path}")
        self._writeln(
            f"Overall Duration : {report.total_duration_ms:.2f} ms ({total_sec:.3f} s)"
        )
        self._writeln(f"Probes Executed  : {report.total_executed} / {len(report.outcomes)}")
        self._writeln(f"Passed           : {report.total_passed}")
        self._writeln(f"Failed (Errors)  : {report.total_failed}")
        if report.total_aborted > 0:
            self._writeln(f"Aborted/Skipped  : {report.total_aborted} (abort_on_failure triggered)")
        self._writeln(f"Total Retries    : {report.total_retries}")
        if report.total_cost_usd > 0:
            self._writeln(f"Total LLM Cost   : {format_usd(report.total_cost_usd)}")

        if self.report_performance:
            self._print_performance_report(report)

        self._writeln("-" * 72)
        self._writeln(f"{'STATUS':<8} {'DURATION (ms)':>14} {'RETRIES':>8}  {'PROBE NAME'}")
        self._writeln("-" * 72)

        for outcome in report.outcomes:
            if outcome.aborted:
                status = "ABORTED"
            elif outcome.passed:
                status = "PASS"
            else:
                status = "FAIL"
            self._writeln(
                f"{status:<8} {outcome.duration_ms:>11.2f} ms {outcome.retries_used:>8}  {outcome.probe_name}"
            )

        failures = [o for o in report.outcomes if not o.passed and not o.aborted]
        if failures:
            self._writeln("\n" + "-" * 72)
            self._writeln(f"FAILURE DETAILS ({len(failures)} failed probe(s))")
            self._writeln("-" * 72)
            for idx, fail in enumerate(failures, start=1):
                self._writeln(
                    f"{idx}. {fail.probe_name} ({fail.fetcher_type}, {fail.duration_ms:.2f} ms, retries={fail.retries_used})"
                )
                if fail.response and fail.response.transport_error:
                    self._writeln(f"   - Transport Error: {fail.response.transport_error}")
                for chk in fail.check_results:
                    if not chk.passed:
                        self._writeln(
                            f"   - Checker [{chk.checker_name}] ({chk.checker_type}): {chk.message}"
                        )
                        for detail in chk.details:
                            if detail.startswith("[FAIL]"):
                                self._writeln(f"       {detail}")
                if fail.retry_history and len(fail.retry_history) > 1:
                    self._writeln("   - Retry History:")
                    for rh in fail.retry_history:
                        self._writeln(f"       {rh}")

        self._writeln("=" * 72)
