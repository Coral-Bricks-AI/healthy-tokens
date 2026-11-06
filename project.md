# Overall Goal
Write a modular, reusable API and Website Prober CLI (`prober`) for testing websites, JSON REST APIs, and LLM inference endpoints.

# Technology & Principles
- **Language**: Python 3.11+ (tested on Python 3.13), idiomatic OOP + functional composition following SOLID principles.
- **Zero Mandatory External Dependencies**: Uses Python standard library (`tomllib`, `urllib.request`, `ssl`, `concurrent.futures`, `dataclasses`, `abc`, `re`, `json`, `time`, `argparse`, `pathlib`).
- **Architecture**:
  - **Fetchers** (`BaseFetcher` subclasses) execute requests, handle retries/timeouts/TLS/auth, and populate an immutable `ProbeResponse` dataclass (including parsed JSON and normalized `LLMResult` for LLM surfaces).
  - **Predicates** (`prober/predicates.py`) are pure, composable higher-order functions implementing comparison and matching operators (`equal`, `not_equal`, `bigger_than`, `smaller_than`, `between`, `contains`, `must_have`, `must_not_have`, `regex`).Every condition accepts a single value or an array of values.
  - **Checkers** (`BaseChecker` subclasses) evaluate specific aspects of `ProbeResponse` using the shared functional predicates or schema validation and return structured `CheckResult` objects.

---

# Application Components

## 1. Fetcher Types (`type = "..."`)
All fetchers support common parameters:
- `url`: Target endpoint URL (string).
- `headers`: Array of `"Header: Value"` strings or `{name = "...", value = "..."}` tables, or a key-value table.
- `bearer_token`: Optional string. When non-empty, sets `Authorization: Bearer <token>`.
- `ignore_verify_certificate_validity`: Boolean (default `false`). When `true`, skips TLS certificate and hostname verification.
- `timeout`: Duration string (`"200ms"`, `"10s"`, `"4m"`, default `"10s"`).
- `retries`: Integer (default `0`). Number of retry attempts after the initial attempt (e.g., `retries = 5` retries up to 5 times if transport or checker evaluation fails, succeeding as soon as an attempt passes and reporting the retry count during scanning and in the summary).
- `retry_delay`: Duration string between retries (default `"200ms"`).

### Supported Fetchers
1. `http_get`: HTTP `GET` request with optional `query_params` table. Automatically parses JSON if the response body is valid JSON.
2. `http_post`: HTTP `POST` request supporting raw `body` string, `form_data` table, or `json_body` table/array.
3. `http_json`: Generic HTTP JSON helper supporting any `method` (default `"GET"` or `"POST"`), automatically setting `Accept: application/json` and `Content-Type: application/json` when `json_body` is provided.
4. `openai_chat_request`: OpenAI Chat Completions surface (`POST /v1/chat/completions`).
   - Parameters: `model`, `messages`, `max_tokens`, `temperature`, `reasoning_effort`, `tools`, `tool_choice`, `stream` (`bool`), `extra_body` (`dict`).
   - Normalizes non-streaming JSON or SSE streaming (`text/event-stream`, `data: {...}`, `data: [DONE]`, and mid-stream error events at HTTP 200) into `ProbeResponse.llm`.
5. `openai_responses_request`: OpenAI Responses surface (`POST /v1/responses`).
   - Parameters: `model`, `input`, `instructions`, `max_output_tokens`, `reasoning`, `tools`, `store`, `background`, `previous_response_id`, `stream` (`bool`), `extra_body` (`dict`).
   - Normalizes `output` message text, `status`, `usage.cost`, and SSE events into `ProbeResponse.llm`.
6. `anthropic_messages_request`: Anthropic Messages surface (`POST /v1/messages`).
   - Parameters: `model`, `max_tokens`, `messages`, `system`, `stream` (`bool`), `extra_body` (`dict`).
   - Normalizes `content` text blocks, `stop_reason`, `usage`, and SSE frames (`message_start`, `content_block_delta`, `error`, `message_stop`) into `ProbeResponse.llm`.

---

## 2. Checker Types (`type = "..."`)
All checkers have a declarative `name` and `type`, and support single values or arrays for condition parameters:

1. `http_status`: Checks HTTP status code.
   - Conditions: `equal`, `not_equal`, `bigger_than`, `smaller_than`, `between` (`[min, max]`), `contains` (status in list).
2. `response_time`: Checks request latency (`duration_ms`).
   - Conditions: `max_duration` (`"200ms"`, `"2s"`, etc.), `bigger_than`, `smaller_than`, `between`.
3. `content`: Checks raw response text/HTML (or `target = "length"` for character length).
   - Conditions: `must_have`, `must_not_have`, `equal`, `not_equal`, `contains`, `bigger_than`, `smaller_than`, `between`, `regex`.
4. `header`: Case-insensitive HTTP response header inspection (`header = "x-coral-pricing-plan"`).
   - Conditions: `must_have` (header exists; if non-boolean string/array provided, checks substring match), `must_not_have`, `equal`, `not_equal`, `contains`, `regex`.
5. `json_path`: Extracts a value from parsed JSON via dot/index path (`path = "usage.cost"`, `"data.0.id"`, `"error.code"`).
   - Conditions: `must_have`, `must_not_have`, `equal`, `not_equal`, `bigger_than`, `smaller_than`, `between`, `contains`, `regex`.
6. `json_schema`: Validates parsed JSON (or a sub-path via optional `path`) against a JSON Schema (`schema = { type = "object", required = [...], properties = {...} }`).
7. `llm_response`: Evaluates normalized `ProbeResponse.llm` fields (`field = "assistant_text" | "finish_reason" | "model" | "cost" | "total_tokens" | "input_tokens" | "output_tokens" | "stream_done" | "tool_call_names" | "surface"`).
   - Conditions: `must_have`, `must_not_have`, `equal`, `not_equal`, `bigger_than`, `smaller_than`, `between`, `contains`, `regex`, plus `expect_stream_error` (`bool`) and `expect_tool_calls` (`bool`).

---

# Scan Config Specification (TOML)
Scan configs are declarative TOML files located under e.g. `scan-config/coralbricks/*`.

## Variables (`[variables]`)
Central variables defined under `[variables]`:
```toml
[variables]
api_key = "${CORALBRICKS_API_KEY:-}"
base_url = "https://inference.coralbricks.ai/v1"
```
- Referenced anywhere in scan configs via `${var_name}` or `${var_name:-default}`.
- Resolution priority:
  1. CLI override (`--var key=value`)
  2. Config `[variables]` (unless empty and environment fallback exists)
  3. Environment variable (`os.environ[key]` or `os.environ[key.upper()]`)
  4. Default value in `${var_name:-default}`

## Directives (`imports` & `[[checkers]]`)
- `imports = ["common/variables.toml", "common/checkers.toml"]`:
  - Relative to the current config file's directory.
  - **Security restriction**: Imported paths MUST resolve within the entry scan-config's directory or its subdirectories (`Path.resolve().is_relative_to(entry_dir)`). Escaping via `..` outside the entry directory or using absolute external paths raises a `ConfigSecurityError`.
- `[[checkers]]`: Array of named checker definitions reusable across probes in the current or importing files.

## Probes (`[[probes]]`)
Each probe defines:
- `name`: Human-readable probe name.
- `tags`: Array of strings (normalized to lowercase and trimmed). Filterable via `--tags="foo, baz, barz "`.
- `fetcher`: Inline table specifying ONE fetcher (`type = "..."`, `url = "..."`, `retries = 3`, `timeout = "10s"`, etc.).
- `checkers`: Array of checker names (strings referencing defined `[[checkers]]`) or inline checker tables.

---

# Prober CLI Interface
```bash
./prober --scan-config="scan-config/coralbricks/main.toml" \
         --tags="public,errors" \
         --max-concurrent=20 \
         --randomize=false \
         --abort_on_failure \
         --include-debug=false \
         --var coralbricks_api_key="cb_..."
```
- `--scan-config`: Path to the entry TOML scan configuration file (required).
- `--tags`: Optional comma-separated list of tags (trimmed, lowercased). Runs probes matching at least one tag.
- `--max-concurrent`: Maximum number of concurrent worker threads (default `10`).
- `--randomize`: `"true"` / `"false"` (or flag) to shuffle probes prior to execution.
- `--abort_on_failure` / `--abort-on-failure`: Stop scheduling/running remaining probes upon the first probe failure.
- `--include-debug`: `"true"` / `"false"` (or flag) to print formatted HTTP request and response details to the console.
- `--var`: Repeatable `KEY=VALUE` variable override.
