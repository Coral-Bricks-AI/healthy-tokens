"""Command-line interface for the API and Website Prober."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from prober.config import ConfigError, ConfigSecurityError, load_scan_config
from prober.reporter import ConsoleReporter
from prober.runner import ScanRunner, filter_and_order_probes, parse_tags_filter


def parse_bool_arg(value: str | bool) -> bool:
    """Parse boolean CLI arguments supporting both '--flag=true/false' and bare '--flag'."""
    if isinstance(value, bool):
        return value
    norm = str(value).strip().lower()
    if norm in ("1", "true", "yes", "y", "on"):
        return True
    if norm in ("0", "false", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(f"Expected boolean value (true/false), got {value!r}")


def parse_var_overrides(raw_vars: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if not raw_vars:
        return overrides
    for item in raw_vars:
        if "=" not in item:
            raise ValueError(f"Invalid --var argument {item!r}; expected KEY=VALUE")
        k, v = item.split("=", 1)
        overrides[k.strip()] = v
    return overrides


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prober",
        description="Modular API, Website, and LLM Inference Prober CLI.",
    )
    parser.add_argument(
        "--scan-config",
         "--scan_config",
        dest="scan_config",
        required=True,
        help="Path to the entry TOML scan configuration file.",
    )
    parser.add_argument(
        "--tags",
        dest="tags",
        default=None,
        help="Comma-separated list of tags to filter probes (e.g. 'foo, baz, barz ').",
    )
    parser.add_argument(
        "--max-concurrent",
        "--max_concurrent",
        dest="max_concurrent",
        type=int,
        default=10,
        help="Maximum number of concurrent probes (default: 10).",
    )
    parser.add_argument(
        "--randomize",
        dest="randomize",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool_arg,
        help="Randomize probe execution order (true/false).",
    )
    parser.add_argument(
        "--abort_on_failure",
        "--abort-on-failure",
        dest="abort_on_failure",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool_arg,
        help="Abort remaining probes on the first probe failure.",
    )
    parser.add_argument(
        "--include-debug",
        "--include_debug",
        dest="include_debug",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool_arg,
        help="Print full HTTP request and response debug details (true/false).",
    )
    parser.add_argument(
        "--report-performance",
        "--report_performance",
        dest="report_performance",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool_arg,
        help="Include detailed performance summary, response time histogram, percentiles, and phase timings (true/false).",
    )
    parser.add_argument(
        "--var",
        dest="vars",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override or supply a scan-config variable (repeatable).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        cli_vars = parse_var_overrides(args.vars)
        loaded_cfg = load_scan_config(args.scan_config, cli_vars=cli_vars)
    except ConfigSecurityError as sec_err:
        sys.stderr.write(f"[SECURITY ERROR] {sec_err}\n")
        return 2
    except (ConfigError, ValueError) as cfg_err:
        sys.stderr.write(f"[CONFIG ERROR] {cfg_err}\n")
        return 2

    tags_filter = parse_tags_filter(args.tags)
    selected_probes = filter_and_order_probes(
        loaded_cfg.probes,
        tags_filter=tags_filter,
        randomize=args.randomize,
    )

    reporter = ConsoleReporter(
        include_debug=args.include_debug,
        report_performance=args.report_performance,
    )
    if not selected_probes:
        sys.stdout.write(
            f"No probes matched filter (tags={sorted(tags_filter) if tags_filter else 'ALL'}) "
            f"in {loaded_cfg.entry_path}.\n"
        )
        return 0

    sys.stdout.write(
        f"Starting scan: {len(selected_probes)} probe(s) from {loaded_cfg.entry_path.name} "
        f"(max_concurrent={args.max_concurrent}, randomize={args.randomize}, "
        f"abort_on_failure={args.abort_on_failure}, include_debug={args.include_debug})\n"
    )
    sys.stdout.write("-" * 72 + "\n")

    runner = ScanRunner(
        max_concurrent=args.max_concurrent,
        abort_on_failure=args.abort_on_failure,
        on_probe_complete=reporter.on_probe_complete,
        on_probe_retry=reporter.on_retry,
    )
    report = runner.run(str(loaded_cfg.entry_path), selected_probes)
    reporter.print_summary(report)

    return 0 if (report.total_failed == 0 and not report.aborted_early) else 1
