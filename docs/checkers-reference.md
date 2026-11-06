# Checkers Reference

A **Checker** evaluates a `ProbeResponse` produced by a Fetcher and returns a structured `CheckResult` (`passed`, `message`, `details`).

Checkers can be declared once under `[[checkers]]` (and imported across files) and referenced by `name` in any probe's `checkers = [...]` list, or declared inline inside a probe.

---

## 1. Unified Condition Predicates

All value-comparing checkers (`http_status`, `response_time`, `content`, `header`, `json_path`, `llm_response`) share a set of composable functional predicates defined in `prober/predicates.py`.

- **Arrays or Single Values**: Every condition parameter accepts an array (recommended, e.g. `equal = [200]`) or a single scalar value (`equal = 200`).
- **Case-Insensitive Naming**: Condition names can be written in idiomatic Python `snake_case` (`must_have`, `bigger_than`) or `PascalCase` (`MustHave`, `BiggerThan`).
- **Strict Validation**: Unknown or misspelled parameter keys in a checker definition raise a `ConfigError` at startup so typos are caught immediately.

| Condition Key (`snake_case` / `PascalCase`) | Supported Value Types | Behavior |
| :--- | :--- | :--- |
| `equal` / `Equal` | `[val, ...]` or `val` | Passes if the actual value equals `val` (or, when given a list `[a, b]` against a scalar actual value, passes if `actual` is in `[a, b]`). |
| `not_equal` / `NotEqual` | `[val, ...]` or `val` | Passes if the actual value is not equal to `val` (and not in `[a, b]`). |
| `bigger_than` / `BiggerThan` | `[num, ...]` or `num` | Passes if the numeric actual value is strictly greater (`>`) than every threshold in the list. |
| `smaller_than` / `SmallerThan` | `[num, ...]` or `num` | Passes if the numeric actual value is strictly less (`<`) than every threshold in the list. |
| `between` / `Between` | `[min, max]` or `[[min1, max1], ...]` | Passes if the numeric actual value satisfies `min <= actual <= max` for at least one range. |
| `contains` / `Contains` | `[item, ...]` or `item` | - If `actual` is a `str`: every expected string must be a substring of `actual`.<br>- If `actual` is a `list`/`dict`: every expected item/key must be in `actual`.<br>- If `actual` is a number (`int`/`float`): `actual` must be a member of the expected list. |
| `must_have` / `MustHave` | `true`, `false`, or `[item, ...]` | - If `true`: actual value must be present (not `None`).<br>- If `false`: actual value must be `None`.<br>- If `[item, ...]`: every substring/element must be present in `actual`. |
| `must_not_have` / `MustNotHave` | `true` or `[item, ...]` | - If `true`: actual value must be `None`.<br>- If `[item, ...]`: none of the forbidden substrings/elements may appear in `actual`. |
| `regex` / `Regex` | `["pattern", ...]` or `"pattern"` | Every regular expression in the list must match (`re.search` with `DOTALL`) the string representation of `actual`. |

---

## 2. Checker Types Catalog

### 2.1 `http_status` (Aliases: `HTTPStatus`, `status_http`, `status`)
Checks the HTTP response status code (`response.status_code`).

#### Allowed Parameters
- `name`, `type`
- Any unified predicate: `equal`, `not_equal`, `bigger_than`, `smaller_than`, `between`, `contains`

#### Examples
```toml
# Exact status match
[[checkers]]
name = "status_200"
type = "http_status"
equal = [200]

# Range check (any 2xx status)
[[checkers]]
name = "status_2xx"
type = "http_status"
between = [200, 299]

# Membership check (200 or 429)
[[checkers]]
name = "status_ok_or_rate_limited"
type = "http_status"
contains = [200, 429]
```

---

### 2.2 `response_time` (Aliases: `ResponseTime`, `latency`)
Checks the HTTP request duration (`response.duration_ms`).

#### Allowed Parameters
- `name`, `type`
- `max_duration` (`MaxDuration`): Duration string (or array of duration strings) such as `"200ms"`, `"5s"`, `"1m"`. Passes if `response.duration_ms <= max_duration`.
- Numeric predicates on milliseconds: `smaller_than`, `bigger_than`, `between`, `equal`.

#### Examples
```toml
[[checkers]]
name = "fast_response_500ms"
type = "response_time"
max_duration = ["500ms"]

[[checkers]]
name = "latency_window"
type = "response_time"
between = [10, 5000] # in ms
```

---

### 2.3 `content` (Aliases: `body`, `html_content`)
Checks the raw response body (`response.text`) or its character length (`len(response.text)`). Ideal for HTML pages, Markdown docs, plain text, or raw body inspection.

#### Allowed Parameters
- `name`, `type`
- `target`: `"auto"` *(default)*, `"text"`, or `"length"`.
  - In `"auto"` mode:
    - String predicates (`must_have`, `must_not_have`, `contains`, `regex`, and `equal`/`not_equal` with string values) evaluate against `response.text`.
    - Numeric predicates (`bigger_than`, `smaller_than`, `between`, and `equal`/`not_equal` with numeric values) automatically evaluate against `len(response.text)`.
- Any unified predicate: `must_have`, `must_not_have`, `equal`, `not_equal`, `contains`, `bigger_than`, `smaller_than`, `between`, `regex`.

#### Examples
```toml
[[checkers]]
name = "docs_page_check"
type = "content"
must_have = ["coralbricks", "https://inference.coralbricks.ai"]
must_not_have = ["Internal Server Error", "403 Forbidden"]
regex = ["# CoralBricks.*"]
bigger_than = [100]      # Checks len(response.text) > 100
smaller_than = [500000]  # Checks len(response.text) < 500,000
```

---

### 2.4 `header` (Aliases: `http_header`, `headers`)
Checks an HTTP response header (case-insensitive header name lookup).

#### Allowed Parameters
- `name`, `type`
- `header` *(required)*: Header name to inspect (e.g. `"content-type"`, `"x-request-id"`, `"x-coral-pricing-plan"`).
- Any unified predicate: `must_have`, `must_not_have`, `equal`, `not_equal`, `contains`, `regex`. (If no predicate is provided, defaults to checking that the header exists.)

#### Examples
```toml
[[checkers]]
name = "header_request_id"
type = "header"
header = "x-request-id"
regex = ["^req_[A-Za-z0-9_-]+"]

[[checkers]]
name = "header_legacy_plan"
type = "header"
header = "x-coral-pricing-plan"
equal = ["legacy"]
```

---

### 2.5 `json_path` (Aliases: `json_field`, `json`)
Extracts a nested field or array projection from `response.json_data` using dot-path notation and evaluates predicates against the extracted value.

#### Path Syntax (`path`)
- Nested objects: `"usage.cost"`, `"error.code"`, `"provider.error_contract.rate_limit_status"`
- Array index (0-based or negative): `"choices.0.message.content"`, `"data.0.id"`
- Wildcard array projection (`*`): `"data.*.id"` (extracts a list of `id` values from every object in the `data` array!)
- Root object: `"$"` or `"."` (default)

#### Allowed Parameters
- `name`, `type`
- `path`: Dot-separated path string (default `"$"`).
- Any unified predicate: `must_have`, `must_not_have`, `equal`, `not_equal`, `bigger_than`, `smaller_than`, `between`, `contains`, `regex`.

#### Examples
```toml
# Verify error code in JSON error envelope
[[checkers]]
name = "error_api_key_missing"
type = "json_path"
path = "error.code"
equal = ["api_key_missing"]

# Verify usage.cost exists and is non-negative
[[checkers]]
name = "usage_cost_present"
type = "json_path"
path = "usage.cost"
must_have = true
bigger_than = [-0.000001]

# Wildcard projection across an array of model objects
[[checkers]]
name = "catalogue_has_deepseek"
type = "json_path"
path = "data.*.id"
contains = ["coralbricks/deepseek-v4.1-flash-fast"]
```

---

### 2.6 `json_schema` (Aliases: `JSONSchema`, `schema`)
Validates `response.json_data` (or a sub-path specified via `path`) against a declarative JSON Schema using the built-in zero-dependency validator (`prober/schema_validator.py`).

#### Allowed Parameters
- `name`, `type`
- `schema` *(required)*: Table defining the JSON Schema.
- `path` *(optional)*: Dot-path to validate a sub-element instead of the root JSON value.

#### Supported JSON Schema Keywords
- `type`: `"object"`, `"array"`, `"string"`, `"number"`, `"integer"`, `"boolean"`, `"null"`, or an array of types (`["string", "null"]`).
- `nullable`: `true` / `false` convenience flag.
- `const` & `enum`: Exact value or allowed set of values.
- **Object**: `required` (array of strings), `properties` (table of property schemas), `additionalProperties` (`bool`).
- **Array**: `items` (schema for array elements), `minItems`, `maxItems`.
- **Number / Integer**: `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`.
- **String**: `minLength`, `maxLength`, `pattern` (regex).
- **Combinators**: `anyOf`, `oneOf`, `allOf`.

#### Example
```toml
[[checkers]]
name = "quota_schema"
type = "json_schema"
[checkers.schema]
type = "object"
required = ["object", "currency", "balance", "balance_cents", "pending_charge_cents"]
[checkers.schema.properties.object]
type = "string"
const = "credits"
[checkers.schema.properties.currency]
type = "string"
const = "USD"
[checkers.schema.properties.balance]
type = "number"
[checkers.schema.properties.balance_cents]
type = "integer"
[checkers.schema.properties.pending_charge_cents]
type = "integer"
```

---

### 2.7 `llm_response` (Aliases: `llm`)
Inspects the normalized `response.llm` (`LLMResult`) structure populated by `openai_chat_request`, `openai_responses_request`, or `anthropic_messages_request` (both non-streaming JSON and SSE streaming).

#### Allowed Parameters
- `name`, `type`
- `field`: Which normalized `LLMResult` attribute to evaluate (default `"assistant_text"`):
  - `"assistant_text"`: Extracted assistant message text (`str | None`)
  - `"finish_reason"`: `"stop"`, `"tool_calls"`, `"end_turn"`, `"completed"`, etc.
  - `"status"`: `"completed"`, `"error"`, `"incomplete"`
  - `"model"`: Model identifier string
  - `"tool_call_names"`: `list[str]` of function names invoked by the model
  - `"tool_calls"`: Raw list of tool call dicts
  - `"input_tokens"`, `"output_tokens"`, `"total_tokens"`: Token usage integers
  - `"cost"`: Request cost (`float | None`)
  - `"stream_done"`: `bool` (`True` when `[DONE]` / `message_stop` / `response.completed` arrived)
  - `"stream_chunks_count"`: `int` number of SSE chunks parsed
  - `"stream_error"`: `dict | None` (non-null if an error frame arrived during streaming)
  - `"surface"`: `"openai_chat"` | `"openai_responses"` | `"anthropic_messages"`
- `expect_stream_error` (`bool`, optional): Assert whether an SSE stream error occurred (`false` asserts `llm.stream_error is None`).
- `expect_tool_calls` (`bool`, optional): Assert whether the model emitted at least one tool call.
- Any unified predicate (`equal`, `not_equal`, `contains`, `must_have`, `must_not_have`, `bigger_than`, `smaller_than`, `between`, `regex`) evaluated against the selected `field`.

#### Examples
```toml
# Verify assistant text contains "OK"
[[checkers]]
name = "llm_text_says_ok"
type = "llm_response"
field = "assistant_text"
contains = ["OK"]

# Verify SSE stream completed cleanly without mid-stream error events
[[checkers]]
name = "llm_stream_completed"
type = "llm_response"
field = "stream_done"
equal = [true]
expect_stream_error = false

# Verify tool calling invoked `get_weather`
[[checkers]]
name = "llm_has_weather_tool_call"
type = "llm_response"
field = "tool_call_names"
expect_tool_calls = true
contains = ["get_weather"]
```

---

## 3. Referencing vs. Inlining Checkers in Probes

In `[[probes]]`, the `checkers` array can mix reusable checker names (defined in any imported config) and inline one-off checker tables:

```toml
[[probes]]
name = "Anonymous Models Catalogue (GET /v1/models)"
tags = ["public", "models", "smoke"]
checkers = [
    "status_200",          # References [[checkers]] name = "status_200"
    "latency_fast_5s",     # References [[checkers]] name = "latency_fast_5s"
    "public_models_schema",# References [[checkers]] name = "public_models_schema"
    # Inline checker table for a probe-specific check:
    { name = "has_deepseek_flash_fast", type = "json_path", path = "data.*.id", contains = ["coralbricks/deepseek-v4.1-flash-fast"] },
]
[probes.fetcher]
type = "http_get"
url = "${base_url}/models"
```
