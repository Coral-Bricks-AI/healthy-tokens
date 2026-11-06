# AGENTS.md — Architecture, Philosophy & Operational Guide for AI Agents

This document provides AI coding agents, autonomous workflows, and contributors with a complete mental model of **`cb-api-prober`**: its goals, Unix/Linux design philosophy, internal architecture, combinatoric building blocks, CLI usage, and subtle edge cases or wire-format divergences.

---

## 1. Goal & Purpose

`cb-api-prober` is a **zero-dependency Python 3.13 CLI prober** for:
1. **Websites & Static/Dynamic HTML/Markdown Endpoints** (status, latency, headers, regex/substring content checks).
2. **JSON REST APIs** (status codes, dot-notation & wildcard JSON paths, recursive JSON Schema validation, negative contract testing).
3. **LLM Inference Gateways** across **OpenAI Chat Completions** (`/v1/chat/completions`), **OpenAI Responses** (`/v1/responses`), and **Anthropic Messages** (`/v1/messages`) — both single-shot JSON and Server-Sent Events (`stream = true`) — with USD cost tracking and normalized response assertions.

---

## 2. Core Design Philosophy (Unix / Linux Philosophy)

The codebase follows classic Unix/Linux engineering principles:

1. **Do One Thing Well & Orthogonal Layers**:
   - **Fetchers** (`prober/fetchers.py`) only execute network requests, measure socket phase timings, and normalize the payload into an immutable `ProbeResponse` (plus optional `LLMResult` and `RequestTimings`). A Fetcher never decides whether a response "passed" or "failed".
   - **Checkers** (`prober/checkers.py`) only inspect a `ProbeResponse` and return a `CheckResult`. A Checker never performs network I/O or knows which Fetcher produced the response.
   - **Predicates** (`prober/predicates.py`) are pure functions `(actual_value) -> PredicateResult`. They work identically whether called by `http_status`, `response_time`, `content`, `header`, `json_path`, or `llm_response`.
   - **Runner** (`prober/runner.py`) only orchestrates concurrency (`ThreadPoolExecutor`), retry loops, and `abort_on_failure` signaling.
   - **Reporter** (`prober/reporter.py`) only formats streaming progress and summary/performance analytics to a text stream.

2. **Combinatoric Composition ($M \times N \times P$)**:
   - Instead of writing bespoke test scripts per endpoint, users combine **$M$ Fetcher types** $\times$ **$N$ Checker types** $\times$ **$P$ Functional Predicates** declaratively in TOML.
   - Named `[[checkers]]` defined once in `common/checkers.toml` can be composed with inline checker tables on any probe across any imported file.

3. **Zero External Runtime Dependencies**:
   - Everything runs on the **Python 3.13 standard library** (`tomllib`, `urllib.request`, `http.client`, `ssl`, `socket`, `concurrent.futures`, `statistics`, `dataclasses`, `argparse`, `unittest`). Never add third-party PyPI packages as mandatory runtime requirements.

4. **Deterministic Automation & Clean Exit Codes**:
   - Designed to be composed in shell pipelines, cron jobs, CI/CD workflows, and agent loops:
     - **Exit `0`**: All executed probes passed (including probes that failed initially but succeeded within their configured `retries`).
     - **Exit `1`**: One or more probes failed after exhausting all retries (or the scan was aborted via `--abort_on_failure`).
     - **Exit `2`**: Configuration error, unresolved variable, or path-traversal security violation (`ConfigSecurityError`).

---

## 3. How Everything Is Glued Together (Architecture & Data Flow)

```text
CLI (prober/cli.py)
 │
 ├─► 1. ConfigLoader (prober/config.py)
 │      • Parses root TOML via tomllib
 │      • Recursively loads relative `imports = [...]` (sandboxed to root config directory)
 │      • Merges `[variables]` + environment fallback + `--var KEY=VALUE` overrides
 │      • Interpolates `${var}` and `${var:-default}` across strings
 │      • Instantiates Fetchers (`create_fetcher`) and Checkers (`create_checker`)
 │      └─► Produces `LoadedScanConfig(probes: list[ProbeDefinition])`
 │
 ├─► 2. Execution Plan (prober/runner.py)
 │      • `filter_and_order_probes()`: filters by `--tags` (case-insensitive OR match)
 │      • Optionally shuffles order when `--randomize=true`
 │
 ├─► 3. ScanRunner & Retry Loop (prober/runner.py)
 │      • Dispatches probes across `ThreadPoolExecutor(max_workers=max_concurrent)`
 │      • `execute_single_probe()` runs up to `fetcher.retries + 1` attempts:
 │          a. `resp = probe.fetcher.execute_once()`
 │          b. `chk_results = [checker.check(resp) for checker in probe.checkers]`
 │          c. `all_passed = (resp.transport_error is None) and all(r.passed for r in chk_results)`
 │          d. If `all_passed`: immediately returns `ProbeOutcome(passed=True)`
 │          e. Otherwise: logs retry via `on_retry`, sleeps `retry_delay`, and retries
 │
 └─► 4. ConsoleReporter (prober/reporter.py)
        • Thread-safe live output (`[PASS]`, `[FAIL]`, `[RETRY k/N]`, optional `DEBUG`)
        • `print_summary()` renders `SCAN SUMMARY REPORT`, optional `--report-performance`
          analytics (histogram, percentiles, socket timings, status distribution),
          per-probe table, and detailed error diagnostics.
```

### Module Map

| File | Responsibility |
| :--- | :--- |
| `prober.py` / `prober` / `prober.cmd` | Cross-platform entrypoints invoking `prober.cli.main()`. |
| `prober/__main__.py` | Allows `python ./prober` or `python -m prober` execution. |
| `prober/cli.py` | CLI argument parser, UTF-8 stream setup, orchestration, and exit code return (`0`, `1`, `2`). |
| `prober/models.py` | Immutable dataclasses: `RequestTimings`, `LLMResult`, `ProbeResponse`, `PredicateResult`, `CheckResult`, `ProbeOutcome`, `ScanReport`. |
| `prober/durations.py` | Duration parser (`parse_duration_ms`, `parse_duration_seconds`) supporting `"200ms"`, `"10s"`, `"4m"`. |
| `prober/predicates.py` | Functional predicate engine (`equal`, `not_equal`, `bigger_than`, `smaller_than`, `between`, `contains`, `must_have`, `must_not_have`, `regex`). |
| `prober/schema_validator.py` | Zero-dependency recursive JSON Schema validator (`type`, `required`, `properties`, `additionalProperties`, `items`, `minItems`, `maxItems`, `enum`, `const`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `anyOf`, `oneOf`, `allOf`). |
| `prober/fetchers.py` | `BaseFetcher` ABC, `_TimedHTTPConnection`/`_TimedHTTPSConnection` socket timing instrumentation, SSE parser, and concrete fetchers (`http_get`, `http_post`, `http_json`, `openai_chat_request`, `openai_responses_request`, `anthropic_messages_request`). |
| `prober/checkers.py` | `BaseChecker` ABC, `extract_json_path` (with `*` wildcard projection), and concrete checkers (`http_status`, `response_time`, `content`, `header`, `json_path`, `json_schema`, `llm_response`). |
| `prober/config.py` | Recursive TOML loader, path-traversal confinement (`ConfigSecurityError`), cycle detection, `${var}` interpolation, and named checker resolution. |
| `prober/runner.py` | Tag filtering, randomization, `execute_single_probe` retry loop, and `ScanRunner` (`ThreadPoolExecutor` + `abort_on_failure`). |
| `prober/reporter.py` | `ConsoleReporter`, `format_usd` currency formatter, 11-bin response time histogram, percentile calculator, socket phase timing breakdown, and summary table. |
| `tests/test_prober.py` | Self-contained `unittest` suite with a local `ThreadingHTTPServer` mock server (`_MockCoralBricksHandler`) testing all 43 CoralBricks probes and edge cases offline. |

---

## 4. How Agents Should Run the Tool

### Environment Note on Windows
On Windows machines where `py.exe` or the Windows Store `python.exe` stub is not configured, invoke Python 3.13 directly or use `run_coralbricks.bat` / `prober.cmd` which auto-detect `%LOCALAPPDATA%\Programs\Python\Python313\python.exe`.

### Common Commands

```powershell
# 1. Run the offline unit test suite (always run this after modifying code!)
C:\Users\andi\AppData\Local\Programs\Python\Python313\python.exe -m unittest discover -s tests -v

# 2. Run the full CoralBricks scan suite via the batch runner (includes --report-performance=true)
.\run_coralbricks.bat

# 3. Run only a specific subset of probes using --tags (fast targeted verification)
.\run_coralbricks.bat --tags="smoke"
.\run_coralbricks.bat --tags="models, quota"
.\run_coralbricks.bat --tags="public"

# 4. Debug a failing probe with full HTTP request/response headers & body
.\run_coralbricks.bat --tags="quota" --include-debug=true --max-concurrent=1

# 5. Stop immediately on the first failing probe
.\run_coralbricks.bat --abort_on_failure
```

---

## 5. Special Cases, Subtle Nuances & Divergences

When modifying the prober or authoring new TOML scan configs, pay close attention to these special cases:

### A. CoralBricks API Wire-Format Divergences
1. **Different `401 Unauthorized` Error Envelopes by Surface**:
   - **OpenAI & Quota endpoints** (`/v1/chat/completions`, `/v1/responses`, `/v1/quota`) return OpenAI's error envelope with `error.code = "missing_api_key"` and `error.type = "invalid_request_error"`.
   - **Anthropic Messages endpoint** (`/v1/messages`) returns Anthropic's error envelope: `{"type": "error", "error": {"type": "authentication_error", "message": "missing api key"}}` (note: top-level `type = "error"` and no `error.code` field).
2. **Pricing Schema Divergence on `GET /v1/models` (Anonymous vs. Authenticated)**:
   - An **anonymous** `GET /v1/models` call returns `x-coral-pricing-plan: v2.0` where each model entry contains a nested `pricing` object.
   - An **authenticated** `GET /v1/models` call for an organization on the legacy plan returns `x-coral-pricing-plan: legacy` with flat `input_price` and `output_price` fields.
3. **Mid-Stream Errors Arrive at HTTP `200`**:
   - When `stream = true`, HTTP status `200` and `Content-Type: text/event-stream` are sent before generation completes. If an upstream failure occurs mid-stream, the server emits an error SSE frame (`data: {"error": ...}` or `event: error`).
   - Always pair `http_status` (`200`) with an `llm_response` checker that asserts `expect_stream_error = false` (default) and `stream_done = true`.
4. **Non-Existent `POST` Routes Return `404` Before Auth**:
   - Unrecognized routes (e.g. `POST /v1/embeddings`, `POST /v1/completions`) return `HTTP 404` with `error.code = "not_found"` even when no `Authorization` header is supplied.
5. **Scientific-Notation Micro-Costs**:
   - Inference responses report `usage.cost` in USD floats such as `2.94e-06` or `3.48e-06`. Always format costs through `format_usd()` in `prober/reporter.py` so output is rendered as readable currency (`$0.00000348`) rather than raw scientific notation.

### B. Prober Engine Special Cases
1. **Boolean Coercion from `${var}` Strings**:
   - When a TOML field like `ignore_verify_certificate_validity = "${skip_tls}"` is interpolated from a variable, its runtime type is a `str` (`"false"` or `"true"`). Never use raw `bool(val)` on config values (since `bool("false") == True` in Python); always use `coerce_bool()` in `prober/fetchers.py`.
2. **`content` Checker `equal` Predicate Dual Behavior**:
   - In `ContentChecker` (`prober/checkers.py`), if `equal` is given numeric values (e.g. `equal = [1024]`), it checks the **byte length** of the response body (`len(response.raw_body)`), matching `bigger_than` / `smaller_than` / `between`.
   - If `equal` (or `Equal`) is given string values (e.g. `equal = ["OK"]`), it checks exact string equality against `response.text`.
3. **Wildcard `*` Projection in `json_path`**:
   - `extract_json_path(data, "data.*.id")` projects across a JSON array and returns a Python `list` of all element `id`s.
   - In `prober/predicates.py`, the `contains` predicate handles both directions:
     - If the actual extracted value is a `list` (e.g. `["model_a", "model_b", "model_c"]`), `contains = ["model_a", "model_b"]` asserts that **every** expected item is present in the extracted list.
     - If the actual extracted value is a scalar (e.g. HTTP status `200`), `contains = [200, 201]` asserts that the scalar is a member of the allowed list.
4. **Lazy `http.client` Socket Connection & `req_write_ms` Timing**:
   - Python's `http.client.HTTPConnection.endheaders()` calls `self.send()`, which lazily calls `self.connect()` if `self.sock is None`.
   - In `_TimedHTTPConnection.send()` and `_TimedHTTPSConnection.send()` (`prober/fetchers.py`), `if self.sock is None and self.auto_open: self.connect()` **must** be called before starting the `req_write_ms` timer so that DNS + TCP/TLS handshake time (`dialup_ms`) is not double-counted inside `req write`.
5. **Cold-Start Concurrent TLS Handshake Latency**:
   - When `max_concurrent=10` threads start simultaneously against an HTTPS endpoint, all 10 threads perform parallel DNS lookups and TLS handshakes (`DNS+dialup` ~250–450ms). Latency checkers in `scan-config/coralbricks/common/checkers.toml` are calibrated to the observed baseline plus a `+200ms` safety buffer (`400ms` docs, `800ms` API errors, `1000ms` models/quota, `1200ms` trust page, `1500ms`/`2000ms` LLM inference).
6. **Path-Traversal Confinement on `imports`**:
   - `load_scan_config()` in `prober/config.py` resolves the root `--scan-config` file's parent directory as the security boundary. Any `imports = [...]` entry that uses an absolute path or escapes the root config directory via `..` immediately raises `ConfigSecurityError` (exit code `2`).

---

## 6. How to Extend the Prober

### Adding a New Fetcher Type
1. Subclass `BaseFetcher` in `prober/fetchers.py`.
2. Decorate the class with `@register_fetcher("your_fetcher_type")`.
3. Implement `execute_once(self) -> ProbeResponse` (using `self._perform_http_request(...)` for automatic header normalization, TLS handling, and socket phase timing).

### Adding a New Checker Type
1. Subclass `BaseChecker` in `prober/checkers.py`.
2. Decorate the class with `@register_checker("your_checker_type")`.
3. Validate any custom config keys against `ALLOWED_PREDICATE_KEYS | {...}` in `__init__` (so typos in TOML configs fail fast with a `ValueError`).
4. Implement `check(self, response: ProbeResponse) -> CheckResult`.
