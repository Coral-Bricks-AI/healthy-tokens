"""Fetcher hierarchy for HTTP, JSON REST, OpenAI Chat/Responses, and Anthropic Messages."""

from __future__ import annotations

from dataclasses import replace
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from prober.durations import parse_duration_seconds
from prober.models import LLMResult, ProbeResponse, RequestTimings

FETCHER_REGISTRY: dict[str, type[BaseFetcher]] = {}


def coerce_bool(value: Any, field_name: str = "boolean") -> bool:
    """Safely coerce booleans including string values interpolated from ${var}."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in ("true", "1", "yes", "y", "on"):
            return True
        if norm in ("false", "0", "no", "n", "off", ""):
            return False
    raise ValueError(f"Invalid boolean value {value!r} for {field_name!r}")


def coerce_optional_int(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Expected integer for {field_name!r}, got boolean {value!r}")
    return int(value)


def coerce_optional_float(value: Any, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Expected number for {field_name!r}, got boolean {value!r}")
    return float(value)


def register_fetcher(*names: str):
    """Class decorator registering a BaseFetcher subclass under one or more type names."""

    def _decorator(cls: type[BaseFetcher]) -> type[BaseFetcher]:
        for name in names:
            FETCHER_REGISTRY[name.strip().lower()] = cls
        return cls

    return _decorator


def parse_headers_config(raw_headers: Any) -> dict[str, str]:
    """Normalize headers config (array of 'K: V' strings, array of {name, value} dicts, or table)."""
    if not raw_headers:
        return {}
    normalized: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for k, v in raw_headers.items():
            normalized[str(k).strip()] = str(v).strip()
        return normalized
    if isinstance(raw_headers, list):
        for entry in raw_headers:
            if isinstance(entry, str):
                if ":" not in entry:
                    raise ValueError(
                        f"Invalid header string {entry!r}; expected 'Header-Name: value'"
                    )
                k, v = entry.split(":", 1)
                normalized[k.strip()] = v.strip()
            elif isinstance(entry, dict):
                if "name" in entry and "value" in entry:
                    normalized[str(entry["name"]).strip()] = str(entry["value"]).strip()
                else:
                    for k, v in entry.items():
                        normalized[str(k).strip()] = str(v).strip()
            else:
                raise ValueError(f"Unsupported header entry: {entry!r}")
        return normalized
    raise ValueError(f"Unsupported headers configuration type: {type(raw_headers).__name__}")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Optional redirect handler when follow_redirects=False."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TimingRecorder:
    """Mutable per-request phase timing collector."""

    def __init__(self) -> None:
        self.dns_lookup_ms: float = 0.0
        self.dialup_ms: float = 0.0
        self.req_write_ms: float = 0.0
        self.resp_wait_ms: float = 0.0
        self.resp_read_ms: float = 0.0

    def to_timings(self) -> RequestTimings:
        return RequestTimings(
            dns_lookup_ms=self.dns_lookup_ms,
            dialup_ms=self.dialup_ms,
            req_write_ms=self.req_write_ms,
            resp_wait_ms=self.resp_wait_ms,
            resp_read_ms=self.resp_read_ms,
        )


def _make_timed_handlers(
    recorder: _TimingRecorder,
    ssl_context: ssl.SSLContext | None,
) -> list[urllib.request.BaseHandler]:
    class _TimedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            t_dial = time.perf_counter_ns()
            t_dns = time.perf_counter_ns()
            try:
                socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
            except OSError:
                pass
            recorder.dns_lookup_ms += (time.perf_counter_ns() - t_dns) / 1_000_000.0
            super().connect()
            recorder.dialup_ms += (time.perf_counter_ns() - t_dial) / 1_000_000.0

        def send(self, data: Any) -> None:
            if self.sock is None and self.auto_open:
                self.connect()
            t0 = time.perf_counter_ns()
            super().send(data)
            recorder.req_write_ms += (time.perf_counter_ns() - t0) / 1_000_000.0

        def getresponse(self) -> http.client.HTTPResponse:
            t0 = time.perf_counter_ns()
            resp = super().getresponse()
            recorder.resp_wait_ms += (time.perf_counter_ns() - t0) / 1_000_000.0
            return resp

    class _TimedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            t_dial = time.perf_counter_ns()
            t_dns = time.perf_counter_ns()
            try:
                socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
            except OSError:
                pass
            recorder.dns_lookup_ms += (time.perf_counter_ns() - t_dns) / 1_000_000.0
            super().connect()
            recorder.dialup_ms += (time.perf_counter_ns() - t_dial) / 1_000_000.0

        def send(self, data: Any) -> None:
            if self.sock is None and self.auto_open:
                self.connect()
            t0 = time.perf_counter_ns()
            super().send(data)
            recorder.req_write_ms += (time.perf_counter_ns() - t0) / 1_000_000.0

        def getresponse(self) -> http.client.HTTPResponse:
            t0 = time.perf_counter_ns()
            resp = super().getresponse()
            recorder.resp_wait_ms += (time.perf_counter_ns() - t0) / 1_000_000.0
            return resp

    class _TimedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            return self.do_open(_TimedHTTPConnection, req)

    class _TimedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            return self.do_open(_TimedHTTPSConnection, req, context= self._context)

    handlers: list[urllib.request.BaseHandler] = [_TimedHTTPHandler()]
    if ssl_context is not None:
        handlers.append(_TimedHTTPSHandler(context=ssl_context))
    return handlers



class BaseFetcher(ABC):
    """Abstract base class for all Fetcher types."""

    fetcher_type: str = "base"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.url: str = str(config.get("url", "")).strip()
        if not self.url:
            raise ValueError(f"Fetcher {self.fetcher_type!r} requires a non-empty 'url'")

        self.headers: dict[str, str] = parse_headers_config(config.get("headers"))
        self.bearer_token: str | None = (
            str(config["bearer_token"]).strip()
            if config.get("bearer_token") is not None and str(config["bearer_token"]).strip() != ""
            else None
        )
        self.ignore_verify_certificate_validity: bool = coerce_bool(
            config.get("ignore_verify_certificate_validity", False),
            "ignore_verify_certificate_validity",
        )
        self.timeout_seconds: float = parse_duration_seconds(config.get("timeout", "10s"))
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"Fetcher {self.fetcher_type!r} timeout must be > 0, got {config.get('timeout')!r}"
            )

        self.retries: int = coerce_optional_int(config.get("retries", 0), "retries") or 0
        if self.retries < 0:
            raise ValueError(
                f"Fetcher {self.fetcher_type!r} retries must be >= 0, got {self.retries}"
            )

        self.retry_delay_seconds: float = parse_duration_seconds(
            config.get("retry_delay", "200ms")
        )
        self.follow_redirects: bool = coerce_bool(
            config.get("follow_redirects", True), "follow_redirects"
        )

    @abstractmethod
    def prepare_request(self) -> tuple[str, str, dict[str, str], bytes | None, str | None]:
        """Return (url, method, headers, body_bytes, body_debug_text)."""

    def enrich_response(self, response: ProbeResponse) -> ProbeResponse:
        """Hook for subclasses (e.g. LLM fetchers) to attach normalized domain structures."""
        return response

    def _build_base_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        merged: dict[str, str] = {"User-Agent": "cb-api-prober/1.0"}
        if extra:
            merged.update(extra)
        merged.update(self.headers)
        if self.bearer_token:
            merged["Authorization"] = f"Bearer {self.bearer_token}"
        return merged

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self.ignore_verify_certificate_validity:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context()

    def execute_once(self) -> ProbeResponse:
        """Perform a single fetch execution without retry loop."""
        url, method, req_headers, body_bytes, body_text = self.prepare_request()
        req = urllib.request.Request(
            url=url,
            data=body_bytes,
            headers=req_headers,
            method=method.upper(),
        )

        recorder = _TimingRecorder()
        ssl_ctx = self._build_ssl_context() if url.lower().startswith("https://") else None
        handlers = _make_timed_handlers(recorder, ssl_ctx)
        if not self.follow_redirects:
            handlers.append(_NoRedirectHandler())

        opener = urllib.request.build_opener(*handlers)
        start_ns = time.perf_counter_ns()

        status_code: int | None = None
        resp_headers: dict[str, str] = {}
        raw_body: bytes = b""
        transport_error: str | None = None

        try:
            with opener.open(req, timeout=self.timeout_seconds) as resp:
                status_code = resp.getcode()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                t_read = time.perf_counter_ns()
                raw_body = resp.read()
                recorder.resp_read_ms += (time.perf_counter_ns() - t_read) / 1_000_000.0
        except urllib.error.HTTPError as http_err:
            status_code = http_err.code
            if http_err.headers:
                resp_headers = {k.lower(): v for k, v in http_err.headers.items()}
            try:
                t_read = time.perf_counter_ns()
                raw_body = http_err.read() if http_err.fp else b""
                recorder.resp_read_ms += (time.perf_counter_ns() - t_read) / 1_000_000.0
            except Exception:
                raw_body = b""
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
            transport_error = f"{type(exc).__name__}: {exc}"

        duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        text = raw_body.decode("utf-8", errors="replace") if raw_body else ""

        json_data: Any | None = None
        if text:
            stripped = text.strip()
            if stripped.startswith(("{", "[")) or "application/json" in resp_headers.get(
                "content-type", ""
            ).lower():
                try:
                    json_data = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    json_data = None

        base_resp = ProbeResponse(
            url=url,
            method=method.upper(),
            request_headers=req_headers,
            request_body=body_text,
            status_code=status_code,
            headers=resp_headers,
            raw_body=raw_body,
            text=text,
            json_data=json_data,
            llm=None,
            duration_ms=duration_ms,
            attempts=1,
            retry_errors=[],
            transport_error=transport_error,
            timings=recorder.to_timings(),
        )
        return self.enrich_response(base_resp)


@register_fetcher("http_get", "get")
class HttpGetFetcher(BaseFetcher):
    """HTTP GET Fetcher with optional query_params."""

    fetcher_type = "http_get"

    def prepare_request(self) -> tuple[str, str, dict[str, str], bytes | None, str | None]:
        url = self.url
        query_params = self.config.get("query_params")
        if isinstance(query_params, dict) and query_params:
            qs = urllib.parse.urlencode(
                {str(k): str(v) for k, v in query_params.items()}, doseq=True
            )
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"
        headers = self._build_base_headers()
        return url, "GET", headers, None, None


@register_fetcher("http_post", "post")
class HttpPostFetcher(BaseFetcher):
    """HTTP POST Fetcher supporting raw body, form_data, or json_body."""

    fetcher_type = "http_post"

    def prepare_request(self) -> tuple[str, str, dict[str, str], bytes | None, str | None]:
        extra_headers: dict[str, str] = {}
        body_text: str | None = None

        if "json_body" in self.config and self.config["json_body"] is not None:
            extra_headers["Content-Type"] = "application/json"
            body_text = json.dumps(self.config["json_body"])
        elif "form_data" in self.config and isinstance(self.config["form_data"], dict):
            extra_headers["Content-Type"] = "application/x-www-form-urlencoded"
            body_text = urllib.parse.urlencode(
                {str(k): str(v) for k, v in self.config["form_data"].items()}
            )
        elif "body" in self.config and self.config["body"] is not None:
            body_text = str(self.config["body"])

        headers = self._build_base_headers(extra_headers)
        body_bytes = body_text.encode("utf-8") if body_text is not None else b""
        return self.url, "POST", headers, body_bytes, body_text


@register_fetcher("http_json", "http_json_request", "json")
class HttpJsonFetcher(BaseFetcher):
    """HTTP JSON helper supporting any HTTP method and automatic JSON headers/serialization."""

    fetcher_type = "http_json"

    def prepare_request(self) -> tuple[str, str, dict[str, str], bytes | None, str | None]:
        has_body = "json_body" in self.config and self.config["json_body"] is not None
        default_method = "POST" if has_body else "GET"
        method = str(self.config.get("method", default_method)).upper()

        extra_headers: dict[str, str] = {"Accept": "application/json"}
        body_text: str | None = None
        body_bytes: bytes | None = None

        if has_body:
            extra_headers["Content-Type"] = "application/json"
            body_text = json.dumps(self.config["json_body"])
            body_bytes = body_text.encode("utf-8")

        url = self.url
        query_params = self.config.get("query_params")
        if isinstance(query_params, dict) and query_params:
            qs = urllib.parse.urlencode(
                {str(k): str(v) for k, v in query_params.items()}, doseq=True
            )
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"

        headers = self._build_base_headers(extra_headers)
        return url, method, headers, body_bytes, body_text


def _parse_sse_events(raw_text: str) -> list[tuple[str | None, str]]:
    """Parse Server-Sent Events stream into list of (event_type, data_payload)."""
    events: list[tuple[str | None, str]] = []
    current_event: str | None = None
    data_lines: list[str] = []

    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                events.append((current_event, "\n".join(data_lines)))
                current_event = None
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())

    if data_lines:
        events.append((current_event, "\n".join(data_lines)))
    return events


@register_fetcher("openai_chat_request", "openai_llm_chat_request")
class OpenAIChatFetcher(BaseFetcher):
    """OpenAI Chat Completions Fetcher (/v1/chat/completions) with streaming & tool-call normalization."""

    fetcher_type = "openai_chat_request"

    def prepare_request(self) -> tuple[str, str, dict[str, str], bytes | None, str | None]:
        payload: dict[str, Any] = {}
        if "model" in self.config:
            payload["model"] = self.config["model"]

        if "messages" in self.config:
            payload["messages"] = self.config["messages"]
        elif "prompt" in self.config:
            messages: list[dict[str, Any]] = []
            if "system" in self.config:
                messages.append({"role": "system", "content": str(self.config["system"])})
            messages.append({"role": "user", "content": str(self.config["prompt"])})
            payload["messages"] = messages

        if "max_tokens" in self.config and self.config["max_tokens"] is not None:
            payload["max_tokens"] = coerce_optional_int(self.config["max_tokens"], "max_tokens")
        if "temperature" in self.config and self.config["temperature"] is not None:
            payload["temperature"] = coerce_optional_float(self.config["temperature"], "temperature")
        if "top_p" in self.config and self.config["top_p"] is not None:
            payload["top_p"] = coerce_optional_float(self.config["top_p"], "top_p")
        if "stream" in self.config and self.config["stream"] is not None:
            payload["stream"] = coerce_bool(self.config["stream"], "stream")

        for optional_key in ("reasoning_effort", "tools", "tool_choice"):
            if optional_key in self.config and self.config[optional_key] is not None:
                payload[optional_key] = self.config[optional_key]

        if isinstance(self.config.get("extra_body"), dict):
            payload.update(self.config["extra_body"])

        body_text = json.dumps(payload)
        headers = self._build_base_headers(
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        )
        return self.url, "POST", headers, body_text.encode("utf-8"), body_text

    def enrich_response(self, response: ProbeResponse) -> ProbeResponse:
        content_type = response.get_header("content-type") or ""
        is_sse = "text/event-stream" in content_type.lower() or (
            coerce_bool(self.config.get("stream", False), "stream") and "data:" in response.text
        )

        if is_sse:
            llm = self._parse_chat_sse(response.text)
        elif isinstance(response.json_data, dict):
            llm = self._parse_chat_json(response.json_data)
        else:
            return response

        return replace(response, llm=llm)

    def _parse_chat_json(self, data: dict[str, Any]) -> LLMResult:
        choices = data.get("choices")
        assistant_text: str | None = None
        finish_reason: str | None = None
        tool_calls: list[dict[str, Any]] = []

        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            finish_reason = first.get("finish_reason")
            msg = first.get("message")
            if isinstance(msg, dict):
                if msg.get("content") is not None:
                    assistant_text = str(msg["content"])
                if isinstance(msg.get("tool_calls"), list):
                    tool_calls = msg["tool_calls"]

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        stream_error = data.get("error") if isinstance(data.get("error"), dict) else None

        return LLMResult(
            surface="openai_chat",
            model=data.get("model"),
            assistant_text=assistant_text,
            finish_reason=finish_reason,
            status="error" if stream_error else "completed",
            tool_calls=tool_calls,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            cost=usage.get("cost"),
            stream_chunks_count=0,
            stream_done=False,
            stream_error=stream_error,
        )

    def _parse_chat_sse(self, raw_text: str) -> LLMResult:
        events = _parse_sse_events(raw_text)
        text_parts: list[str] = []
        model: str | None = None
        finish_reason: str | None = None
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        stream_done = False
        stream_error: dict[str, Any] | None = None
        chunks_count = 0

        for event_type, payload_str in events:
            if payload_str == "[DONE]":
                stream_done = True
                continue
            try:
                chunk = json.loads(payload_str)
            except (ValueError, json.JSONDecodeError):
                continue

            if not isinstance(chunk, dict):
                continue

            if "error" in chunk and isinstance(chunk["error"], dict):
                stream_error = chunk["error"]
                continue
            if event_type == "error" or chunk.get("type") == "error":
                stream_error = chunk.get("error") if isinstance(chunk.get("error"), dict) else chunk
                continue

            chunks_count += 1
            if chunk.get("model"):
                model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]

            choices = chunk.get("choices")
            if isinstance(choices, list) and choices:
                c0 = choices[0] if isinstance(choices[0], dict) else {}
                if c0.get("finish_reason"):
                    finish_reason = c0["finish_reason"]
                delta = c0.get("delta")
                if isinstance(delta, dict):
                    if delta.get("content"):
                        text_parts.append(str(delta["content"]))
                    if isinstance(delta.get("tool_calls"), list):
                        tool_calls.extend(delta["tool_calls"])

        return LLMResult(
            surface="openai_chat",
            model=model,
            assistant_text="".join(text_parts) if text_parts else None,
            finish_reason=finish_reason,
            status="error" if stream_error else ("completed" if stream_done else "incomplete"),
            tool_calls=tool_calls,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            cost=usage.get("cost"),
            stream_chunks_count=chunks_count,
            stream_done=stream_done,
            stream_error=stream_error,
        )


@register_fetcher("openai_responses_request", "openai_llm_responses_request")
class OpenAIResponsesFetcher(BaseFetcher):
    """OpenAI Responses Surface Fetcher (/v1/responses) with JSON & SSE support."""

    fetcher_type = "openai_responses_request"

    def prepare_request(self) -> tuple[str, str, dict[str, str], bytes | None, str | None]:
        method = str(self.config.get("method", "POST")).upper()
        if method == "GET":
            headers = self._build_base_headers({"Accept": "application/json"})
            return self.url, "GET", headers, None, None

        payload: dict[str, Any] = {}
        for key in (
            "model",
            "input",
            "instructions",
            "reasoning",
            "tools",
            "tool_choice",
            "previous_response_id",
        ):
            if key in self.config and self.config[key] is not None:
                payload[key] = self.config[key]

        if "max_output_tokens" in self.config and self.config["max_output_tokens"] is not None:
            payload["max_output_tokens"] = coerce_optional_int(
                self.config["max_output_tokens"], "max_output_tokens"
            )
        if "temperature" in self.config and self.config["temperature"] is not None:
            payload["temperature"] = coerce_optional_float(
                self.config["temperature"], "temperature"
            )
        if "top_p" in self.config and self.config["top_p"] is not None:
            payload["top_p"] = coerce_optional_float(self.config["top_p"], "top_p")
        for bool_key in ("store", "background", "stream"):
            if bool_key in self.config and self.config[bool_key] is not None:
                payload[bool_key] = coerce_bool(self.config[bool_key], bool_key)

        if isinstance(self.config.get("extra_body"), dict):
            payload.update(self.config["extra_body"])

        body_text = json.dumps(payload)
        headers = self._build_base_headers(
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        )
        return self.url, method, headers, body_text.encode("utf-8"), body_text

    def enrich_response(self, response: ProbeResponse) -> ProbeResponse:
        content_type = response.get_header("content-type") or ""
        is_sse = "text/event-stream" in content_type.lower() or (
            coerce_bool(self.config.get("stream", False), "stream") and "data:" in response.text
        )

        if is_sse:
            llm = self._parse_responses_sse(response.text)
        elif isinstance(response.json_data, dict):
            llm = self._parse_responses_json(response.json_data)
        else:
            return response

        return replace(response, llm=llm)

    def _parse_responses_json(self, data: dict[str, Any]) -> LLMResult:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        output_items = data.get("output")
        if isinstance(output_items, list):
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message" and isinstance(item.get("content"), list):
                    for block in item["content"]:
                        if isinstance(block, dict) and block.get("text") is not None:
                            text_parts.append(str(block["text"]))
                elif item.get("type") in ("function_call", "tool_call"):
                    tool_calls.append(item)

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        err = data.get("error") if isinstance(data.get("error"), dict) else None

        return LLMResult(
            surface="openai_responses",
            model=data.get("model"),
            assistant_text="".join(text_parts) if text_parts else None,
            finish_reason=data.get("status"),
            status=data.get("status"),
            tool_calls=tool_calls,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            cost=usage.get("cost"),
            stream_chunks_count=0,
            stream_done=False,
            stream_error=err,
        )

    def _parse_responses_sse(self, raw_text: str) -> LLMResult:
        events = _parse_sse_events(raw_text)
        text_parts: list[str] = []
        model: str | None = None
        status: str | None = None
        usage: dict[str, Any] = {}
        stream_done = False
        stream_error: dict[str, Any] | None = None
        chunks_count = 0

        for event_type, payload_str in events:
            if payload_str == "[DONE]":
                stream_done = True
                continue
            try:
                frame = json.loads(payload_str)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(frame, dict):
                continue

            frame_type = frame.get("type") or event_type
            if frame_type == "error" or (isinstance(frame.get("error"), dict) and frame.get("error")):
                stream_error = frame.get("error") if isinstance(frame.get("error"), dict) else frame
                continue

            chunks_count += 1
            if frame.get(" delta") or frame.get("delta"):
                delta = frame.get("delta")
                if isinstance(delta, str):
                    text_parts.append(delta)
            if frame_type in ("response.completed", "response.done"):
                stream_done = True
                resp_obj = frame.get("response")
                if isinstance(resp_obj, dict):
                    model = resp_obj.get("model") or model
                    status = resp_obj.get("status") or status
                    if isinstance(resp_obj.get("usage"), dict):
                        usage = resp_obj["usage"]

        return LLMResult(
            surface="openai_responses",
            model=model,
            assistant_text="".join(text_parts) if text_parts else None,
            finish_reason=status or ("completed" if stream_done else None),
            status="error" if stream_error else (status or ("completed" if stream_done else "incomplete")),
            tool_calls=[],
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            cost=usage.get("cost"),
            stream_chunks_count=chunks_count,
            stream_done=stream_done,
            stream_error=stream_error,
        )


@register_fetcher(
    "anthropic_messages_request",
    "anthropic_llm_chat_request",
    "antrhopic_llm_chat_request",
)
class AnthropicMessagesFetcher(BaseFetcher):
    """Anthropic Messages Surface Fetcher (/v1/messages) with JSON & SSE support."""

    fetcher_type = "anthropic_messages_request"

    def prepare_request(self) -> tuple[str, str, dict[str, str], bytes | None, str | None]:
        payload: dict[str, Any] = {}
        if "model" in self.config:
            payload["model"] = self.config["model"]
        payload["max_tokens"] = coerce_optional_int(self.config.get("max_tokens", 16), "max_tokens") or 16

        if "messages" in self.config:
            payload["messages"] = self.config["messages"]
        elif "prompt" in self.config:
            payload["messages"] = [{"role": "user", "content": str(self.config["prompt"])}]

        if "stream" in self.config and self.config["stream"] is not None:
            payload["stream"] = coerce_bool(self.config["stream"], "stream")
        if "temperature" in self.config and self.config["temperature"] is not None:
            payload["temperature"] = coerce_optional_float(
                self.config["temperature"], "temperature"
            )
        if "top_p" in self.config and self.config["top_p"] is not None:
            payload["top_p"] = coerce_optional_float(self.config["top_p"], "top_p")

        for key in ("system", "tools", "tool_choice"):
            if key in self.config and self.config[key] is not None:
                payload[key] = self.config[key]

        if isinstance(self.config.get("extra_body"), dict):
            payload.update(self.config["extra_body"])

        body_text = json.dumps(payload)
        headers = self._build_base_headers(
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        )
        return self.url, "POST", headers, body_text.encode("utf-8"), body_text

    def enrich_response(self, response: ProbeResponse) -> ProbeResponse:
        content_type = response.get_header("content-type") or ""
        is_sse = "text/event-stream" in content_type.lower() or (
            coerce_bool(self.config.get("stream", False), "stream") and "data:" in response.text
        )

        if is_sse:
            llm = self._parse_anthropic_sse(response.text)
        elif isinstance(response.json_data, dict):
            llm = self._parse_anthropic_json(response.json_data)
        else:
            return response

        return replace(response, llm=llm)

    def _parse_anthropic_json(self, data: dict[str, Any]) -> LLMResult:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        content = data.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text") is not None:
                    text_parts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    tool_calls.append(block)

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        total_tok = (
            (in_tok or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (out_tok or 0)
            if (in_tok is not None or out_tok is not None)
            else None
        )
        err = data.get("error") if isinstance(data.get("error"), dict) else None

        return LLMResult(
            surface="anthropic_messages",
            model=data.get("model"),
            assistant_text="".join(text_parts) if text_parts else None,
            finish_reason=data.get("stop_reason"),
            status="error" if (data.get("type") == "error" or err) else "completed",
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=total_tok,
            cost=usage.get("cost"),
            stream_chunks_count=0,
            stream_done=False,
            stream_error=err,
        )

    def _parse_anthropic_sse(self, raw_text: str) -> LLMResult:
        events = _parse_sse_events(raw_text)
        text_parts: list[str] = []
        model: str | None = None
        stop_reason: str | None = None
        stream_done = False
        stream_error: dict[str, Any] | None = None
        chunks_count = 0
        in_tok: int | None = None
        out_tok: int | None = None

        for event_type, payload_str in events:
            if payload_str == "[DONE]":
                stream_done = True
                continue
            try:
                frame = json.loads(payload_str)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(frame, dict):
                continue

            frame_type = frame.get("type") or event_type
            if frame_type == "error" or "error" in frame:
                stream_error = frame.get("error") if isinstance(frame.get("error"), dict) else frame
                continue

            chunks_count += 1
            if frame_type == "message_start":
                msg = frame.get("message")
                if isinstance(msg, dict):
                    model = msg.get("model") or model
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        in_tok = u.get("input_tokens", in_tok)
                        out_tok = u.get("output_tokens", out_tok)
            elif frame_type == "content_block_delta":
                delta = frame.get("delta")
                if isinstance(delta, dict) and delta.get("text") is not None:
                    text_parts.append(str(delta["text"]))
            elif frame_type == "message_delta":
                delta = frame.get("delta")
                if isinstance(delta, dict) and delta.get("stop_reason"):
                    stop_reason = delta["stop_reason"]
                u = frame.get("usage")
                if isinstance(u, dict) and u.get("output_tokens") is not None:
                    out_tok = u["output_tokens"]
            elif frame_type == "message_stop":
                stream_done = True

        return LLMResult(
            surface="anthropic_messages",
            model=model,
            assistant_text="".join(text_parts) if text_parts else None,
            finish_reason=stop_reason,
            status="error" if stream_error else ("completed" if stream_done else "incomplete"),
            tool_calls=[],
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=(in_tok or 0) + (out_tok or 0)
            if (in_tok is not None or out_tok is not None)
            else None,
            cost=None,
            stream_chunks_count=chunks_count,
            stream_done=stream_done,
            stream_error=stream_error,
        )


def create_fetcher(config: dict[str, Any]) -> BaseFetcher:
    """Instantiate a registered BaseFetcher from a TOML fetcher table."""
    raw_type = str(config.get("type", "")).strip().lower()
    if not raw_type:
        raise ValueError("Fetcher configuration is missing 'type'")
    cls = FETCHER_REGISTRY.get(raw_type)
    if cls is None:
        available = ", ".join(sorted(FETCHER_REGISTRY.keys()))
        raise ValueError(f"Unknown fetcher type {raw_type!r}. Available types: {available}")
    return cls(config)
