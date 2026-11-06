# Fetchers Reference

A **Fetcher** is responsible for executing an HTTP, JSON REST, or LLM API call, capturing timing and transport metadata, and building an immutable `ProbeResponse` object (including parsed `json_data` and normalized `LLMResult` data) for Checkers to inspect.

Each `[[probes]]` block specifies **exactly one** fetcher via `[probes.fetcher]` (or inline `fetcher = { ... }`).

---

## 1. Common Fetcher Parameters

Every fetcher type supports these shared parameters:

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `type` | `string` | *(required)* | Fetcher type identifier (case-insensitive). |
| `url` | `string` | *(required)* | Full target URL (`http://` or `https://`). Supports `${var}` interpolation. |
| `headers` | `array` or `table` | `[]` | Custom HTTP request headers. Can be an array of `"Name: Value"` strings, an array of `{ name = "...", value = "..." }` tables, or a `{ "Header-Name" = "Value" }` table. |
| `bearer_token` | `string` | `None` | When non-empty, automatically sets `Authorization: Bearer <token>`. If empty (`""`), no `Authorization` header is added. |
| `ignore_verify_certificate_validity` | `bool` | `false` | When `true`, disables SSL/TLS certificate chain and hostname verification (useful for self-signed staging/internal endpoints). |
| `timeout` | `string` or `number` | `"10s"` | Per-attempt request timeout. Supports `"200ms"`, `"10s"`, `"4m"`, etc. Must be `> 0`. |
| `retries` | `int` | `0` | Number of retry attempts after the initial attempt if transport or any checker fails (`retries = 5` means up to 6 total attempts). Must be `>= 0`. |
| `retry_delay` | `string` or `number` | `"200ms"` | Wait duration between retries (e.g. `"200ms"`, `"1s"`). |
| `follow_redirects` | `bool` | `true` | Whether HTTP `3xx` redirects are automatically followed. |

### Ways to Specify `headers` in TOML
All three formats below are supported on any fetcher:

```toml
# 1. Array of "Header: Value" strings
headers = [
    "X-Custom-Env: staging",
    "Accept-Language: en-US",
]

# 2. Array of { name, value } tables
headers = [
    { name = "X-Custom-Env", value = "staging" },
]

# 3. Inline table
headers = { "X-Custom-Env" = "staging", "Accept-Language" = "en-US" }
```

---

## 2. Fetcher Types Catalog

### 2.1 `http_get` (Aliases: `HTTP_GET`, `get`)
Performs an HTTP `GET` request. If the response body is valid JSON, `response.json_data` is automatically populated alongside `response.text`.

#### Specific Parameters
| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `query_params` | `table` | `{}` | Optional URL query parameters appended & URL-encoded onto `url`. |

#### Example
```toml
[[probes]]
name = "Fetch Public HTML Page"
tags = ["web"]
checkers = ["status_200"]
[probes.fetcher]
type = "http_get"
url = "https://www.coralbricks.ai/docs.md"
query_params = { version = "2" }
headers = ["Accept: text/markdown, text/plain"]
ignore_verify_certificate_validity = false
timeout = "5s"
retries = 2
```

---

### 2.2 `http_post` (Aliases: `HTTP_POST`, `post`)
Performs an HTTP `POST` request with a JSON payload, URL-encoded form data, or raw string body.

#### Specific Parameters
| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `json_body` | `table` or `array` | `None` | Serializes the value as JSON and sets `Content-Type: application/json`. |
| `form_data` | `table` | `None` | URL-encodes key-value pairs and sets `Content-Type: application/x-www-form-urlencoded`. |
| `body` | `string` | `None` | Sends raw string bytes as the request body. |

#### Example
```toml
[[probes]]
name = "POST Webhook Payload"
tags = ["api"]
checkers = ["status_200"]
[probes.fetcher]
type = "http_post"
url = "https://api.example.com/v1/webhook"
bearer_token = "${api_key}"
json_body = { event = "ping", timestamp = 1793779436 }
timeout = "10s"
```

---

### 2.3 `http_json` (Aliases: `HTTP_JSON`, `http_json_request`, `json`)
A convenience helper for JSON REST APIs supporting any HTTP method (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`). Automatically sets `Accept: application/json` and (when `json_body` is present) `Content-Type: application/json`.

#### Specific Parameters
| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `method` | `string` | `"POST"` if `json_body` is set, else `"GET"` | HTTP verb (`"GET"`, `"POST"`, `"PUT"`, `"DELETE"`, `"PATCH"`). |
| `json_body` | `table` or `array` | `None` | JSON payload to serialize and send. |
| `query_params` | `table` | `{}` | Optional URL query parameters. |

#### Example
```toml
[[probes]]
name = "Check Credit Quota"
tags = ["auth", "quota"]
checkers = ["status_200", "quota_schema"]
[probes.fetcher]
type = "http_json"
method = "GET"
url = "${base_url}/quota"
bearer_token = "${coralbricks_api_key}"
timeout = "5s"
retries = 2
```

---

### 2.4 `openai_chat_request` (Aliases: `OPENAI_LLM_CHAT_REQUEST`)
Dedicated fetcher for OpenAI-compatible Chat Completions endpoints (`POST /v1/chat/completions`).
Supports single-shot completions, function/tool calling, and Server-Sent Events (`stream = true`), normalizing the output into `response.llm` (`LLMResult`).

#### Specific Parameters
| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `model` | `string` | *(recommended)* | Model ID (e.g. `"deepseek-v4.1-flash-fast"`). |
| `messages` | `array of tables` | `None` | OpenAI messages array (`[{ role = "user", content = "say OK" }]`). |
| `prompt` | `string` | `None` | Shorthand for a single user message if `messages` is omitted. |
| `system` | `string` | `None` | Optional system message prepended when using `prompt`. |
| `max_tokens` | `int` | `None` | Maximum completion tokens. |
| `temperature` | `float` | `None` | Sampling temperature. |
| `top_p` | `float` | `None` | Nucleus sampling parameter. |
| `reasoning_effort` | `string` | `None` | Reasoning effort (`"none"`, `"low"`, `"high"`, `"max"`). |
| `tools` | `array of tables` | `None` | OpenAI function tools definitions. |
| `tool_choice` | `string` or `table` | `None` | Tool choice mode (e.g. `"auto"`, `"none"`). |
| `stream` | `bool` | `false` | When `true`, parses `text/event-stream` SSE chunks (`chat.completion.chunk`), `[DONE]`, and mid-stream error events. |
| `extra_body` | `table` | `{}` | Additional arbitrary fields merged into the top-level JSON request payload. |

#### Example
```toml
[[probes]]
name = "Chat Completions Single-Shot"
tags = ["auth", "chat"]
checkers = ["status_200", "llm_text_says_ok"]
[probes.fetcher]
type = "openai_chat_request"
url = "${base_url}/chat/completions"
bearer_token = "${coralbricks_api_key}"
model = "deepseek-v4.1-flash-fast"
messages = [
    { role = "system", content = "Reply concisely." },
    { role = "user", content = "say OK" },
]
max_tokens = 8
reasoning_effort = "none"
stream = false
timeout = "15s"
retries = 2
```

---

### 2.5 `openai_responses_request` (Aliases: `OPENAI_LLM_RESPONSES_REQUEST`)
Dedicated fetcher for the OpenAI Responses API surface (`POST /v1/responses`). Normalizes `output` message blocks, `status`, `usage.cost`, and SSE events into `response.llm`.

#### Specific Parameters
| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `model` | `string` | *(recommended)* | Model ID (e.g. `"deepseek-v4.1-flash-fast"`). |
| `input` | `string` or `array` | *(recommended)* | User input string or multi-turn input array. |
| `instructions` | `string` | `None` | Optional system instructions. |
| `max_output_tokens` | `int` | `None` | Maximum output token budget. |
| `reasoning` | `table` | `None` | Reasoning config table, e.g. `{ effort = "none" }`. |
| `tools` | `array` | `None` | Tools array. |
| `tool_choice` | `string` or `table` | `None` | Tool selection mode. |
| `store` | `bool` | `None` | Whether to store the response on the server. |
| `background` | `bool` | `None` | Run response in background mode (can be used to test `400 unsupported_parameter` when disabled). |
| `previous_response_id` | `string` | `None` | Previous response ID for multi-turn replay. |
| `stream` | `bool` | `false` | Enable SSE streaming. |
| `extra_body` | `table` | `{}` | Extra fields merged into the JSON request payload. |

#### Example
```toml
[[probes]]
name = "OpenAI Responses Single-Shot"
tags = ["auth", "responses"]
checkers = ["status_200", "openai_responses_schema", "llm_text_non_empty"]
[probes.fetcher]
type = "openai_responses_request"
url = "${base_url}/responses"
bearer_token = "${coralbricks_api_key}"
model = "deepseek-v4.1-flash-fast"
input = "name one colour"
max_output_tokens = 16
reasoning = { effort = "none" }
store = false
```

---

### 2.6 `anthropic_messages_request` (Aliases: `ANTHROPIC_LLM_CHAT_REQUEST`, `ANTRHOPIC_LLM_CHAT_REQUEST`)
Dedicated fetcher for the Anthropic Messages wire format (`POST /v1/messages`). Normalizes `content` text/tool_use blocks, `stop_reason`, token usage, and SSE frames (`message_start`, `content_block_delta`, `message_delta`, `error`, `message_stop`) into `response.llm`.

#### Specific Parameters
| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `model` | `string` | *(recommended)* | Model ID (e.g. `"deepseek-v4.1-flash-fast"`). |
| `max_tokens` | `int` | `16` | Maximum output tokens (required by Anthropic wire format). |
| `messages` | `array of tables` | `None` | Messages array (`[{ role = "user", content = "say OK" }]`). |
| `prompt` | `string` | `None` | Shorthand for a single user message if `messages` is omitted. |
| `system` | `string` | `None` | Optional top-level system prompt. |
| `stream` | `bool` | `false` | When `true`, parses Anthropic SSE event frames. |
| `temperature` | `float` | `None` | Sampling temperature. |
| `top_p` | `float` | `None` | Nucleus sampling parameter. |
| `tools` | `array` | `None` | Anthropic tool definitions. |
| `extra_body` | `table` | `{}` | Extra fields merged into the JSON request payload. |

#### Example
```toml
[[probes]]
name = "Anthropic Messages Streaming"
tags = ["auth", "anthropic", "stream"]
checkers = ["status_200", "header_content_sse", "llm_stream_completed", "llm_text_says_ok"]
[probes.fetcher]
type = "anthropic_messages_request"
url = "${base_url}/messages"
bearer_token = "${coralbricks_api_key}"
model = "deepseek-v4.1-flash-fast"
max_tokens = 8
stream = true
messages = [
    { role = "user", content = "say OK" },
]
```

---

## 3. Normalized `ProbeResponse` & `LLMResult` Data Model

Every Fetcher produces a `ProbeResponse` object with the following fields available to Checkers:

- `status_code` (`int | None`) — Checked by `http_status`
- `duration_ms` (`float`) — Checked by `response_time`
- `headers` (`dict[str, str]`, lowercased keys) — Checked by `header`
- `text` (`str`) — Checked by `content`
- `json_data` (`Any | None`) — Checked by `json_path` and `json_schema`
- `llm` (`LLMResult | None`) — Checked by `llm_response`:
  - `surface`: `"openai_chat"` | `"openai_responses"` | `"anthropic_messages"`
  - `model`: Model string returned by the server
  - `assistant_text`: Combined text output (assembled across content blocks or SSE delta frames)
  - `finish_reason`: `"stop"`, `"tool_calls"`, `"end_turn"`, `"completed"`, etc.
  - `status`: `"completed"`, `"error"`, or `"incomplete"`
  - `tool_calls`: Raw list of tool call objects
  - `tool_call_names`: Extracted list of called function names (e.g. `["get_weather"]`)
  - `input_tokens`, `output_tokens`, `total_tokens`: Normalized token counts
  - `cost`: Per-request cost (e.g. `2.94e-06` on CoralBricks)
  - `stream_chunks_count`: Number of SSE data frames received
  - `stream_done`: `True` if `[DONE]` or `message_stop` / `response.completed` was received
  - `stream_error`: Populated if an error event arrived inside an HTTP 200 SSE stream
