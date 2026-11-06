# CoralBricks Inference API — Endpoint Reference

Reference documentation for the customer-facing endpoints at `https://inference.coralbricks.ai/v1` and `https://www.coralbricks.ai/docs.md`.

- **Base URL:** `https://inference.coralbricks.ai/v1`
- **Auth:** `Authorization: Bearer cb_<key>` (keys look like `cb_…` or `ak_…`, minted at
  `https://www.coralbricks.ai/api-keys`). Spend comes from a prepaid credit balance.
- **Wire format:** OpenAI-compatible, plus an Anthropic-compatible surface at `/v1/messages`.
- **Pricing plan header:** Returned on inference responses via `x-coral-pricing-plan` (e.g. `legacy`).

---

## 1. List models

**Endpoint:** `https://inference.coralbricks.ai/v1/models`
**Method:** `GET`
**Request:** empty body. **Works with no API key** (public catalogue, 200 anonymously).
**Description:** model catalogue. Anonymous calls return the public contract (v2.0 shape,
public pricing, capabilities, reliability limits). Authenticated calls return the catalogue
enabled for the calling key in the flat shape with organization-specific pricing and live serving telemetry.

**Reply (anonymous, 200):**

```json
{
  "object": "list",
  "contract_version": "2.0",
  "provider": {
    "id": "coralbricks",
    "status_url": "https://trust.coralbricks.ai/#incidents",
    "support_contact": "mailto:hello@coralbricks.ai",
    "incident_contact": "mailto:privacy@coralbricks.ai",
    "regions": ["global"],
    "request_id_header": "x-request-id",
    "error_contract": {
      "rate_limit_status": 429,
      "overload_status": 503,
      "retry_after_header": "Retry-After",
      "account_quota_error_codes": ["insufficient_credits"]
    }
  },
  "data": [
    {
      "id": "coralbricks/deepseek-v4.1-flash-fast",
      "object": "model",
      "owned_by": "coralbricks",
      "name": "DeepSeek V4.1 Flash Fast",
      "type": "chat",
      "context_length": 1048576,
      "max_output_tokens": 393216,
      "endpoints": ["chat/completions"],
      "input_modalities": ["text", "image"],
      "output_modalities": ["text"],
      "capabilities": {
        "streaming": true, "tools": true, "structured_output": true,
        "reasoning": true, "prompt_caching": true
      },
      "pricing": {
        "currency": "USD", "unit": "per_1m_tokens",
        "input": "0.45", "output": "1.80", "cached_input": "0.009",
        "cache_write": "0.00", "minimum_request": "0"
      },
      "lifecycle": {
        "status": "active", "deprecation_at": null,
        "retirement_at": null, "replacement_model_id": null
      },
      "reliability": {
        "first_token_timeout_seconds": 20.0,
        "completion_timeout_seconds": 900.0,
        "stream_idle_timeout_seconds": 300.0,
        "capacity_scope": "model_region"
      }
    }
  ]
}
```

**Reply (authenticated, 200):** flat rows, organization pricing, plus 30-minute and 1-day serving
stats measured on GPUs:

```json
{
  "object": "list",
  "data": [
    {
      "id": "deepseek-v4.1-flash-fast",
      "object": "model",
      "owned_by": "coralbricks",
      "context_length": 1048576,
      "created": 1789000000,
      "pricing": {
        "cache_write_per_m": 0.08,
        "cached_input_per_m": 0,
        "input_per_m": 0.01,
        "output_per_m": 1.2
      },
      "reasoning": {
        "default_effort": "none",
        "disable": {"reasoning_effort": "none"},
        "mandatory": false,
        "supported_efforts": ["low", "high", "max"]
      },
      "supports_chat": true, "supports_image_input": true,
      "supports_reasoning": true, "supports_tools": true,
      "cache_hit_rate_last_1d": 96.1, "cache_hit_rate_last_30m": 95.8,
      "decode_speed_last_1d": 477.2, "decode_speed_last_30m": 451.5,
      "latency_last_1d": 0.48, "latency_last_30m": 0.56
    },
    {
      "id": "deepseek-v4.1-flash",
      "pricing": {"cache_write_per_m": 0.0, "cached_input_per_m": 0.008,
                  "input_per_m": 0.19, "output_per_m": 0.6},
      "decode_speed_last_1d": 224.2, "latency_last_1d": 0.96
    }
  ]
}
```

`decode_speed_*` is output tokens/sec one caller sees while streaming. `latency_*` is median
time to first token in seconds. A model too quiet in a window carries no field, never a 0.

**HTTP headers, OK case:** `Content-Type: application/json`, `x-request-id: req_…`,
`vary: Origin`, `Content-Length`.

---

## 2. Credit balance

**Endpoint:** `https://inference.coralbricks.ai/v1/quota`
**Method:** `GET`
**Request:** empty body. **Key required.**
**Description:** prepaid balance of the calling key. No credit gate, works at zero balance.
A request that would run against a non-positive balance is rejected up front with
`429 insufficient_credits`.

**Reply (200):**

```json
{
  "object": "credits",
  "currency": "USD",
  "balance": 100.00,
  "balance_cents": 10000,
  "pending_charge_cents": 0
}
```

`balance` = `balance_cents - pending_charge_cents`, i.e. effective spendable amount.

**HTTP headers, OK case:** `Content-Type: application/json`, `x-request-id: req_…`,
`vary: Origin`, `Content-Length`.

---

## 3. Chat Completions

**Endpoint:** `https://inference.coralbricks.ai/v1/chat/completions`
**Method:** `POST`
**Description:** stateless, single-shot OpenAI Chat Completions surface.

**Request:**

```json
{
  "model": "deepseek-v4.1-flash-fast",
  "messages": [
    {"role": "system", "content": "…"},
    {"role": "user", "content": "say OK"}
  ],
  "max_tokens": 8,
  "stream": false,
  "temperature": 0.7,
  "reasoning_effort": "none",
  "tools": [
    {"type": "function",
     "function": {"name": "get_weather", "description": "w",
                  "parameters": {"type": "object",
                                 "properties": {"city": {"type": "string"}},
                                 "required": ["city"]}}}
  ],
  "tool_choice": "auto"
}
```

- `reasoning_effort`: `none` | `low` | `high` | `max`. Aliases `minimal`→`low`, `medium`→`high`,
  `xhigh`→`max`. Anything else is `400 unsupported_value`. DeepSeek V4.1 Flash reasons at
  `none` unless requested, and takes no token budget.
- `max_tokens` is output only; context is 1,048,576 and max output 393,216.
- Images: `input_modalities` includes `image`; unsupported content types return
  `400 unsupported_content_type` instead of being dropped.
- Prompt caching is automatic, no flags.

**Reply (200, non-streaming):**

```json
{
  "id": "req_00000000000000000000000000000001",
  "created": 1793779436,
  "model": "deepseek-v4.1-flash-fast",
  "object": "chat.completion",
  "choices": [
    {"index": 0,
     "message": {"role": "assistant", "content": "OK"},
     "finish_reason": "stop"}
  ],
  "usage": {
    "prompt_tokens": 6,
    "total_tokens": 8,
    "completion_tokens": 2,
    "prompt_tokens_details": {
      "cached_tokens": 0,
      "cache_write_tokens": 6,
      "billable_cache_write_tokens": 6,
      "cache_write_blocks": 3
    },
    "completion_tokens_details": {"reasoning_tokens": 0},
    "cost": 2.94e-06,
    "cost_details": {
      "currency": "USD",
      "input": 0.0,
      "cached_input": 0.0,
      "cache_write": 5.4e-07,
      "output": 2.4e-06
    }
  },
  "provider": "coralbricks"
}
```

`usage.cost` and `usage.cost_details` report per-request cost split by meter. With a tool call,
`finish_reason` is `"tool_calls"` and `message.tool_calls` carries
`[{"id": "call_…", "type": "function", "function": {"name": …, "arguments": "{\"city\": \"London\"}"}}]`
(arguments is a JSON **string**).

**Reply (200, `stream: true`):** `text/event-stream`, one `data: {…}` frame per chunk with
`object: "chat.completion.chunk"`, terminated by `data: [DONE]`. A request rejected after the
stream opened reports the error as a single SSE event, still at HTTP 200: read the event,
not the status.

**HTTP headers, OK case:**
`Content-Type: application/json` (or `text/event-stream; charset=utf-8` when streaming),
`x-request-id: req_…`, `x-coral-pricing-plan: legacy`.

---

## 4. Responses (OpenAI Responses surface)

**Endpoint:** `https://inference.coralbricks.ai/v1/responses`
**Method:** `POST` (`GET` on the collection → 405)
**Description:** OpenAI Responses agent surface, same model IDs and prices as Chat Completions.
When zero-data-retention is active on an organization, stateful persistence features return explicit errors:

- `background: true` → `400 unsupported_parameter`: *"background responses are not available
  for this organization: they need the result kept until it is fetched."*
- `previous_response_id` → `400`: *"its data-retention setting keeps no request content, so
  earlier turns cannot be replayed. Send the full conversation in `input` instead."*
- `GET /v1/responses/{id}` → `404 response_not_found` (nothing is stored).

**Request:**

```json
{
  "model": "deepseek-v4.1-flash-fast",
  "input": "name one colour",
  "max_output_tokens": 16,
  "instructions": "optional system text",
  "reasoning": {"effort": "none"},
  "tools": [],
  "store": false
}
```

**Reply (200):**

```json
{
  "id": "resp_00000000000000000000000000000001",
  "object": "response",
  "created_at": 1793779437,
  "status": "completed",
  "model": "deepseek-v4.1-flash-fast",
  "output": [
    {"type": "message",
     "id": "msg_00000000000000000000000000000001",
     "role": "assistant",
     "status": "completed",
     "content": [{"type": "output_text", "text": "OK", "annotations": [], "logprobs": []}]}
  ],
  "usage": {
    "input_tokens": 6,
    "output_tokens": 2,
    "total_tokens": 8,
    "input_tokens_details": {
      "cached_tokens": 0, "cache_write_tokens": 6, "billable_cache_write_tokens": 6
    },
    "output_tokens_details": {"reasoning_tokens": 0},
    "reasoning_output_tokens": 0,
    "cost": 2.94e-06,
    "cost_details": {"currency": "USD", "input": 0.0, "cached_input": 0.0,
                     "cache_write": 5.4e-07, "output": 2.4e-06}
  },
  "provider": "coralbricks",
  "error": null,
  "incomplete_details": null,
  "instructions": null,
  "metadata": {},
  "tools": [],
  "tool_choice": "auto",
  "parallel_tool_calls": true,
  "temperature": 1.0,
  "top_p": 1.0,
  "max_output_tokens": 16,
  "previous_response_id": null,
  "store": false,
  "background": false,
  "reasoning": {"effort": "none"},
  "truncation": "disabled",
  "service_tier": "default",
  "user": null
}
```

**HTTP headers, OK case:** `Content-Type: application/json`, `x-request-id: req_…`,
`x-coral-pricing-plan: legacy`.

---

## 5. Messages (Anthropic-compatible)

**Endpoint:** `https://inference.coralbricks.ai/v1/messages`
**Method:** `POST`
**Description:** Anthropic Messages wire format for clients that speak it.

**Request:**

```json
{
  "model": "deepseek-v4.1-flash-fast",
  "max_tokens": 8,
  "messages": [{"role": "user", "content": "say OK"}]
}
```

**Reply (200):**

```json
{
  "id": "msg_000000000000000000000001",
  "type": "message",
  "role": "assistant",
  "model": "deepseek-v4.1-flash-fast",
  "content": [{"type": "text", "text": "OK"}],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 6,
    "output_tokens": 2
  }
}
```

Errors on this surface use Anthropic's envelope:
`{"type": "error", "error": {"type": "invalid_request_error", "message": "…"}}` (or `"type": "authentication_error"` on 401).

**HTTP headers, OK case:** `Content-Type: application/json`, `x-request-id: req_…`,
`x-coral-pricing-plan: legacy`. SSE frames on `stream: true` are
`message_start`, `ping`, `content_block_delta`, `error`, `message_stop`.

---

## Error shapes

| Case | Status | Body |
| --- | --- | --- |
| No key on an inference call | 401 | `{"error":{"message":"missing api key — pass 'Authorization: Bearer …'; mint keys at https://www.coralbricks.ai/api-keys; docs: https://www.coralbricks.ai/docs.md","type":"invalid_request_error","param":null,"code":"missing_api_key"}}` |
| Unknown / unenabled model | 404 | `{"error":{"message":"The model 'nope' does not exist or you do not have access to it.","type":"invalid_request_error","param":null,"code":"model_not_public"}}` |
| Retired model | 404 | same envelope, `code: "model_retired"`, message names the replacement |
| Balance would go negative | 429 | `code: "insufficient_credits"` (rejected up front) |
| Rate limited / overloaded | 429 / 503 | honour `Retry-After` |
| Bad enum value (e.g. `reasoning_effort`) | 400 | `code: "unsupported_value"` |
| Parameter not allowed for org | 400 | `code: "unsupported_parameter"`, `param` names it |
| Wrong method on a route | 405 | `code: "method_not_allowed"` |
| Path that does not exist | 404 | `{"error":{"message":"Not Found","type":"invalid_request_error","param":null,"code":"not_found"}}` |
| Prompt over the window | 400 | wording: `prompt is too long: N tokens > 1048576 maximum` |
| Streaming request rejected after open | **200** | error arrives as the stream's only event |

---

## Paths that do NOT exist (404)

`/v1/completions`, `/v1/embeddings`, `/v1/usage`, `/v1/credits`, `/v1/me`, `/v1/account`,
`/v1/billing`, `/v1/keys`, `/v1/rerank`, `/v1/tokenize`, `/v1/files`, `/v1/batches`,
`/v1/fine_tuning/jobs`, `/v1/audio/transcriptions`, `/v1/audio/speech`,
`/v1/images/generations`, `/health`, `/v1/status`, `/metrics`,
`/v1/responses/{id}/input_items`.

Only chat, responses, messages, models and quota are customer-facing.

---

## Non-API surfaces

- `https://www.coralbricks.ai/docs.md` — full documentation page as raw markdown.
- `https://www.coralbricks.ai/llms.txt` and `/llms-full.txt` — machine-readable index.
- `https://www.coralbricks.ai/api/public/models` — public catalogue JSON (use `/v1/models` anonymously for non-browser clients).
- `https://trust.coralbricks.ai/#incidents` — status page referenced by the provider block.
- Support: `hello@coralbricks.ai`; incident/privacy: `privacy@coralbricks.ai`.
