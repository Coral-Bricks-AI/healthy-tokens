"""Comprehensive unit and integration test suite for cb-api-prober with local Mock HTTP Server."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from prober.checkers import create_checker, extract_json_path
from prober.cli import main
from prober.config import ConfigError, ConfigSecurityError, load_scan_config
from prober.durations import parse_duration_ms, parse_duration_seconds
from prober.fetchers import create_fetcher
from prober.models import format_usd
from prober.predicates import build_predicates, evaluate_predicates
from prober.reporter import ConsoleReporter
from prober.runner import ScanRunner, filter_and_order_probes, parse_tags_filter
from prober.schema_validator import validate_json_schema


class _MockCoralBricksHandler(BaseHTTPRequestHandler):
    """Local HTTP mock server emulating HTML, flaky endpoints, and the full CoralBricks API."""

    flaky_counter: int = 0
    flaky_lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Keep test output clean

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-request-id", "req_mock1234567890")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(
        self,
        status: int,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _is_authenticated(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == "Bearer cb_test_secret"

    def do_GET(self) -> None:
        # 1. Website / HTML / Docs surfaces
        if self.path.startswith("/html"):
            self._send_text(
                200,
                "<html><body><h1>Welcome to CoralBricks Prober</h1></body></html>",
                content_type="text/html; charset=utf-8",
            )
            return

        if self.path == "/docs.md":
            self._send_text(
                200,
                "# CoralBricks Documentation\nBase URL: https://inference.coralbricks.ai/v1\n"
                "Welcome to coralbricks inference documentation with over 100 characters of text.",
                content_type="text/markdown; charset=utf-8",
            )
            return

        if self.path == "/llms.txt":
            self._send_text(
                200,
                "# CoralBricks LLMs Index\n- https://www.coralbricks.ai/docs.md\n",
            )
            return

        if self.path == "/":
            self._send_text(
                200,
                "<html><title>CoralBricks Trust & Status</title><body>All systems operational</body></html>",
                content_type="text/html; charset=utf-8",
            )
            return

        # 2. Flaky endpoint for retry verification
        if self.path.startswith("/flaky"):
            with self.flaky_lock:
                _MockCoralBricksHandler.flaky_counter += 1
                attempt = _MockCoralBricksHandler.flaky_counter
            if attempt < 3:
                self._send_json(503, {"error": {"code": "overloaded", "attempt": attempt}})
            else:
                self._send_json(200, {"status": "ok", "attempt": attempt})
            return

        # 3. GET /v1/models (anonymous vs authenticated shapes)
        if self.path == "/v1/models":
            if self._is_authenticated():
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": "deepseek-v4.1-flash-fast",
                                "object": "model",
                                "owned_by": "coralbricks",
                                "context_length": 1048576,
                                "pricing": {
                                    "cache_write_per_m": 0.08,
                                    "cached_input_per_m": 0,
                                    "input_per_m": 0.01,
                                    "output_per_m": 1.2,
                                },
                                "reasoning": {
                                    "default_effort": "none",
                                    "supported_efforts": ["low", "high", "max"],
                                },
                                "supports_chat": True,
                                "supports_image_input": True,
                                "supports_reasoning": True,
                                "supports_tools": True,
                            },
                            {
                                "id": "deepseek-v4.1-flash",
                                "object": "model",
                                "owned_by": "coralbricks",
                                "context_length": 1048576,
                                "pricing": {
                                    "cache_write_per_m": 0.0,
                                    "cached_input_per_m": 0.008,
                                    "input_per_m": 0.19,
                                    "output_per_m": 0.6,
                                },
                                "supports_chat": True,
                                "supports_reasoning": True,
                                "supports_tools": True,
                            },
                        ],
                    },
                )
            else:
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "contract_version": "2.0",
                        "provider": {
                            "id": "coralbricks",
                            "status_url": "https://trust.coralbricks.ai/#incidents",
                            "request_id_header": "x-request-id",
                            "error_contract": {
                                "rate_limit_status": 429,
                                "overload_status": 503,
                                "account_quota_error_codes": ["insufficient_credits"],
                            },
                        },
                        "data": [
                            {
                                "id": "coralbricks/deepseek-v4.1-flash-fast",
                                "object": "model",
                                "owned_by": "coralbricks",
                                "type": "chat",
                                "context_length": 1048576,
                                "max_output_tokens": 393216,
                                "endpoints": ["chat/completions"],
                                "input_modalities": ["text", "image"],
                                "output_modalities": ["text"],
                                "capabilities": {
                                    "streaming": True,
                                    "tools": True,
                                    "structured_output": True,
                                    "reasoning": True,
                                    "prompt_caching": True,
                                },
                                "pricing": {
                                    "currency": "USD",
                                    "unit": "per_1m_tokens",
                                    "input": "0.45",
                                    "output": "1.80",
                                },
                                "lifecycle": {"status": "active"},
                                "reliability": {
                                    "first_token_timeout_seconds": 20.0,
                                    "completion_timeout_seconds": 900.0,
                                    "stream_idle_timeout_seconds": 300.0,
                                },
                            }
                        ],
                    },
                )
            return

        # 4. GET /v1/quota
        if self.path == "/v1/quota":
            if not self._is_authenticated():
                self._send_json(
                    401,
                    {
                        "error": {
                            "message": "missing api key",
                            "type": "invalid_request_error",
                            "param": None,
                            "code": "missing_api_key",
                        }
                    },
                )
                return
            self._send_json(
                200,
                {
                    "object": "credits",
                    "currency": "USD",
                    "balance": 100.00,
                    "balance_cents": 10000,
                    "pending_charge_cents": 0,
                },
            )
            return

        # 5. GET /v1/responses -> 405 method_not_allowed
        if self.path == "/v1/responses":
            self._send_json(
                405,
                {
                    "error": {
                        "message": "Method Not Allowed",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "method_not_allowed",
                    }
                },
            )
            return

        # 6. GET /v1/responses/{id} -> 404 response_not_found (except sub-routes like /input_items -> not_found)
        if self.path.startswith("/v1/responses/") and not self.path.endswith("/input_items"):
            self._send_json(
                404,
                {
                    "error": {
                        "message": "Response not found",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "response_not_found",
                    }
                },
            )
            return

        # 7. All other paths -> 404 not_found
        self._send_json(
            404,
            {
                "error": {
                    "message": "Not Found",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "not_found",
                }
            },
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            data = {}

        # Non-existent POST routes return 404 not_found even without auth
        if self.path not in ("/v1/chat/completions", "/v1/responses", "/v1/messages", "/echo"):
            self._send_json(
                404,
                {
                    "error": {
                        "message": "Not Found",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "not_found",
                    }
                },
            )
            return

        if self.path == "/echo":
            self._send_json(
                200,
                {
                    "received": data,
                    "custom_header": self.headers.get("X-Custom-Test", ""),
                },
            )
            return

        # Auth check for inference routes
        if not self._is_authenticated():
            if self.path == "/v1/messages":
                self._send_json(
                    401,
                    {
                        "type": "error",
                        "error": {
                            "type": "authentication_error",
                            "message": "missing api key",
                        },
                    },
                )
            else:
                self._send_json(
                    401,
                    {
                        "error": {
                            "message": "missing api key",
                            "type": "invalid_request_error",
                            "param": None,
                            "code": "missing_api_key",
                        }
                    },
                )
            return

        # Model validation
        model = data.get("model", "")
        if model == "nope":
            self._send_json(
                404,
                {
                    "error": {
                        "message": "The model 'nope' does not exist or you do not have access to it.",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "model_not_public",
                    }
                },
            )
            return

        # 1. OpenAI Chat Completions (/v1/chat/completions)
        if self.path == "/v1/chat/completions":
            effort = data.get("reasoning_effort")
            if effort is not None and effort not in (
                "none",
                "low",
                "high",
                "max",
                "minimal",
                "medium",
                "xhigh",
            ):
                self._send_json(
                    400,
                    {
                        "error": {
                            "message": f"Unsupported reasoning_effort: {effort}",
                            "type": "invalid_request_error",
                            "param": "reasoning_effort",
                            "code": "unsupported_value",
                        }
                    },
                )
                return

            if data.get("stream") is True:
                if data.get("trigger_stream_error") is True:
                    sse_err = (
                        'data: {"error":{"message":"stream aborted mid-flight","code":"stream_error"}}\n\n'
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("x-coral-pricing-plan", "legacy")
                    self.send_header("Content-Length", str(len(sse_err)))
                    self.end_headers()
                    self.wfile.write(sse_err)
                    return

                sse_payload = (
                    'data: {"id":"req_1","model":"deepseek-v4.1-flash-fast","object":"chat.completion.chunk",'
                    '"choices":[{"index":0,"delta":{"content":"O"},"finish_reason":null}]}\n\n'
                    'data: {"id":"req_1","model":"deepseek-v4.1-flash-fast","object":"chat.completion.chunk",'
                    '"choices":[{"index":0,"delta":{"content":"K"},"finish_reason":"stop"}],'
                    '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7,"cost":0.000002}}\n\n'
                    "data: [DONE]\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("x-coral-pricing-plan", "legacy")
                self.send_header("Content-Length", str(len(sse_payload)))
                self.end_headers()
                self.wfile.write(sse_payload)
                return

            if data.get("tools"):
                self._send_json(
                    200,
                    {
                        "id": "req_tc",
                        "created": 1793779436,
                        "object": "chat.completion",
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "get_weather",
                                                "arguments": '{"city": "London"}',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 8,
                            "total_tokens": 18,
                            "prompt_tokens_details": {"cached_tokens": 0},
                            "completion_tokens_details": {"reasoning_tokens": 0},
                            "cost": 0.00001,
                            "cost_details": {
                                "currency": "USD",
                                "input": 0.0,
                                "cached_input": 0.0,
                                "cache_write": 0.0,
                                "output": 0.00001,
                            },
                        },
                        "provider": "coralbricks",
                    },
                    extra_headers={"x-coral-pricing-plan": "legacy"},
                )
                return

            self._send_json(
                200,
                {
                    "id": "req_chat",
                    "created": 1793779436,
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 6,
                        "completion_tokens": 2,
                        "total_tokens": 8,
                        "prompt_tokens_details": {"cached_tokens": 0},
                        "completion_tokens_details": {"reasoning_tokens": 0},
                        "cost": 2.94e-6,
                        "cost_details": {
                            "currency": "USD",
                            "input": 0.0,
                            "cached_input": 0.0,
                            "cache_write": 5.4e-7,
                            "output": 2.4e-6,
                        },
                    },
                    "provider": "coralbricks",
                },
                extra_headers={"x-coral-pricing-plan": "legacy"},
            )
            return

        # 2. OpenAI Responses (/v1/responses)
        if self.path == "/v1/responses":
            if data.get("background") is True:
                self._send_json(
                    400,
                    {
                        "error": {
                            "message": "background responses are not available for this organization",
                            "type": "invalid_request_error",
                            "param": "background",
                            "code": "unsupported_parameter",
                        }
                    },
                )
                return

            if data.get("previous_response_id") is not None:
                self._send_json(
                    400,
                    {
                        "error": {
                            "message": "data-retention setting keeps no request content",
                            "type": "invalid_request_error",
                            "param": "previous_response_id",
                            "code": "unsupported_parameter",
                        }
                    },
                )
                return

            self._send_json(
                200,
                {
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 1793779437,
                    "status": "completed",
                    "model": model,
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "Blue"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 6,
                        "output_tokens": 2,
                        "total_tokens": 8,
                        "cost": 2.94e-6,
                        "cost_details": {
                            "currency": "USD",
                            "input": 0.0,
                            "cached_input": 0.0,
                            "cache_write": 5.4e-7,
                            "output": 2.4e-6,
                        },
                    },
                    "provider": "coralbricks",
                    "store": False,
                    "background": False,
                },
                extra_headers={"x-coral-pricing-plan": "legacy"},
            )
            return

        # 3. Anthropic Messages (/v1/messages)
        if self.path == "/v1/messages":
            if data.get("stream") is True:
                sse_payload = (
                    'event: message_start\ndata: {"type":"message_start","message":{"model":"deepseek-v4.1-flash-fast","usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
                    'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"OK"}}\n\n'
                    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
                    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("x-coral-pricing-plan", "legacy")
                self.send_header("Content-Length", str(len(sse_payload)))
                self.end_headers()
                self.wfile.write(sse_payload)
                return

            self._send_json(
                200,
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "OK"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 6, "output_tokens": 2},
                },
                extra_headers={"x-coral-pricing-plan": "legacy"},
            )
            return


class ProberTestSuite(unittest.TestCase):
    server: ThreadingHTTPServer
    server_thread: threading.Thread
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockCoralBricksHandler)
        port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_durations(self) -> None:
        self.assertAlmostEqual(parse_duration_ms("200ms"), 200.0)
        self.assertAlmostEqual(parse_duration_ms("10s"), 10_000.0)
        self.assertAlmostEqual(parse_duration_ms("4m"), 240_000.0)
        self.assertAlmostEqual(parse_duration_seconds("1500ms"), 1.5)
        with self.assertRaises(ValueError):
            parse_duration_ms("invalid")

    def test_format_usd_currency(self) -> None:
        self.assertEqual(format_usd(3.48e-06), "$0.00000348")
        self.assertEqual(format_usd(2.94e-06), "$0.00000294")
        self.assertEqual(format_usd(0.0), "$0.00")
        self.assertEqual(format_usd(0.5), "$0.50")
        self.assertEqual(format_usd(100.00), "$100.00")
        self.assertEqual(format_usd(None), "N/A")

    def test_predicates_and_arrays(self) -> None:
        preds = build_predicates(
            {
                "equal": [200, 201],
                "bigger_than": [199],
                "smaller_than": [300],
                "between": [200, 204],
                "contains": [200, 201, 202],
            }
        )
        ok, _ = evaluate_predicates(200, preds, "status")
        self.assertTrue(ok)

        text_preds = build_predicates(
            {
                "must_have": ["Welcome", "CoralBricks"],
                "must_not_have": ["Forbidden", "Error"],
                "regex": [r"<h1>Welcome.*</h1>"],
            }
        )
        ok_txt, _ = evaluate_predicates(
            "<html><h1>Welcome to CoralBricks</h1></html>", text_preds, "body"
        )
        self.assertTrue(ok_txt)

    def test_json_path_extraction_and_wildcard(self) -> None:
        sample = {
            "object": "list",
            "data": [
                {"id": "model_a", "price": 0.1},
                {"id": "model_b", "price": 0.2},
            ],
        }
        found, val = extract_json_path(sample, "data.1.id")
        self.assertTrue(found)
        self.assertEqual(val, "model_b")

        found_wild, ids = extract_json_path(sample, "data.*.id")
        self.assertTrue(found_wild)
        self.assertEqual(ids, ["model_a", "model_b"])

    def test_json_schema_validator(self) -> None:
        schema = {
            "type": "object",
            "required": ["object", "balance"],
            "properties": {
                "object": {"type": "string", "const": "credits"},
                "balance": {"type": "number", "minimum": 0},
            },
        }
        self.assertEqual(
            validate_json_schema({"object": "credits", "balance": 100.00}, schema),
            [],
        )
        errs = validate_json_schema({"object": "wrong", "balance": -5}, schema)
        self.assertEqual(len(errs), 2)

    def test_custom_headers_and_tls_flag_on_fetchers(self) -> None:
        fetcher = create_fetcher(
            {
                "type": "http_post",
                "url": f"{self.base_url}/echo",
                "headers": ["X-Custom-Test: hello-world"],
                "json_body": {"ping": "pong"},
                "ignore_verify_certificate_validity": True,
            }
        )
        self.assertTrue(fetcher.ignore_verify_certificate_validity)
        resp = fetcher.execute_once()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json_data["custom_header"], "hello-world")
        self.assertEqual(resp.json_data["received"]["ping"], "pong")

    def test_sse_midstream_error_at_http_200(self) -> None:
        """CoralBricks reports mid-stream errors as an SSE frame at HTTP 200."""
        fetcher = create_fetcher(
            {
                "type": "openai_chat_request",
                "url": f"{self.base_url}/v1/chat/completions",
                "bearer_token": "cb_test_secret",
                "model": "deepseek-v4.1-flash-fast",
                "prompt": "test",
                "stream": True,
                "extra_body": {"trigger_stream_error": True},
            }
        )
        resp = fetcher.execute_once()
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.llm)
        self.assertIsNotNone(resp.llm.stream_error)

        no_err_checker = create_checker(
            {
                "name": "must_not_error",
                "type": "llm_response",
                "expect_stream_error": False,
            }
        )
        chk_res = no_err_checker.check(resp)
        self.assertFalse(chk_res.passed)

    def test_config_security_blocks_traversal_and_absolute_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside_file = root / "outside.toml"
            outside_file.write_text("[variables]\nx = '1'\n", encoding="utf-8")

            subdir = root / "sub"
            subdir.mkdir()
            entry_escape = subdir / "entry.toml"
            entry_escape.write_text('imports = ["../outside.toml"]\n', encoding="utf-8")

            with self.assertRaises(ConfigSecurityError):
                load_scan_config(entry_escape)

            entry_abs = subdir / "entry_abs.toml"
            entry_abs.write_text(
                f'imports = [{json.dumps(str(outside_file))}]\n', encoding="utf-8"
            )
            with self.assertRaises(ConfigSecurityError):
                load_scan_config(entry_abs)

    def test_circular_import_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            a_file = root / "a.toml"
            b_file = root / "b.toml"
            a_file.write_text('imports = ["b.toml"]\n', encoding="utf-8")
            b_file.write_text('imports = ["a.toml"]\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_scan_config(a_file)

    def test_full_coralbricks_scan_config_against_mock_server(self) -> None:
        """Run all 43 probes defined in scan-config/coralbricks/main.toml against the local Mock Server."""
        cfg_path = (
            Path(__file__).resolve().parent.parent
            / "scan-config"
            / "coralbricks"
            / "main.toml"
        )
        loaded = load_scan_config(
            cfg_path,
            cli_vars={
                "base_url": f"{self.base_url}/v1",
                "website_url": self.base_url,
                "trust_url": self.base_url,
                "coralbricks_api_key": "cb_test_secret",
            },
        )
        self.assertEqual(len(loaded.probes), 43)

        buf = io.StringIO()
        reporter = ConsoleReporter(include_debug=False, stream=buf)
        runner = ScanRunner(
            max_concurrent=16,
            abort_on_failure=False,
            on_probe_complete=reporter.on_probe_complete,
            on_probe_retry=reporter.on_retry,
        )
        report = runner.run(str(cfg_path), loaded.probes)
        reporter.print_summary(report)

        self.assertEqual(
            report.total_failed,
            0,
            msg=f"Expected 0 failures in CoralBricks suite, got report:\n{buf.getvalue()}",
        )
        self.assertEqual(report.total_passed, 43)

    def test_edge_cases_coercion_typos_and_content_length_equal(self) -> None:
        # 1. String "false" from ${var} must coerce to False, not True
        f_false = create_fetcher(
            {
                "type": "http_get",
                "url": "https://example.com",
                "ignore_verify_certificate_validity": "false",
                "retries": "3",
            }
        )
        self.assertFalse(f_false.ignore_verify_certificate_validity)
        self.assertEqual(f_false.retries, 3)

        # 2. Negative retries or zero timeout must raise ValueError
        with self.assertRaises(ValueError):
            create_fetcher({"type": "http_get", "url": "https://example.com", "retries": -1})
        with self.assertRaises(ValueError):
            create_fetcher({"type": "http_get", "url": "https://example.com", "timeout": "0s"})

        # 3. Unknown parameter typo on checker must raise ValueError
        with self.assertRaises(ValueError):
            create_checker({"name": "typo_chk", "type": "http_status", "equall": [200]})

        # 4. ContentChecker equal with numeric vs string expectations
        from prober.models import ProbeResponse

        dummy_resp = ProbeResponse(
            url="http://localhost",
            method="GET",
            request_headers={},
            request_body=None,
            status_code=200,
            headers={},
            raw_body=b"hello world!",
            text="hello world!",
            json_data=None,
            llm=None,
            duration_ms=5.0,
        )
        len_chk = create_checker({"name": "len_eq", "type": "content", "equal": [12]})
        self.assertTrue(len_chk.check(dummy_resp).passed)

        str_chk = create_checker(
            {"name": "str_eq", "type": "content", "Equal": ["hello world!"]}
        )
        self.assertTrue(str_chk.check(dummy_resp).passed)

    def test_retries_and_debug_output_against_mock_server(self) -> None:
        _MockCoralBricksHandler.flaky_counter = 0
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "flaky_scan.toml"
            cfg_file.write_text(
                f"""
[variables]
base = "{self.base_url}"

[[checkers]]
name = "status_200"
type = "http_status"
equal = [200]

[[probes]]
name = "Flaky Endpoint With Retries"
tags = ["flaky", "retry"]
checkers = [
    "status_200",
    {{ name = "attempt_is_3", type = "json_path", path = "attempt", equal = [3] }}
]
[probes.fetcher]
type = "http_json"
url = "${{base}}/flaky"
retries = 4
retry_delay = "10ms"
""",
                encoding="utf-8",
            )
            loaded = load_scan_config(cfg_file)
            buf = io.StringIO()
            reporter = ConsoleReporter(include_debug=True, stream=buf)
            runner = ScanRunner(
                max_concurrent=2,
                abort_on_failure=False,
                on_probe_complete=reporter.on_probe_complete,
                on_probe_retry=reporter.on_retry,
            )
            report = runner.run(str(cfg_file), loaded.probes)
            reporter.print_summary(report)

            self.assertEqual(report.total_passed, 1)
            self.assertEqual(report.total_retries, 2)
            output_str = buf.getvalue()
            self.assertIn("[RETRY 1/4]", output_str)
            self.assertIn("[RETRY 2/4]", output_str)
            self.assertIn("DEBUG [Flaky Endpoint With Retries]", output_str)

    def test_abort_on_failure_and_tag_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "abort_scan.toml"
            cfg_file.write_text(
                f"""
[variables]
base = "{self.base_url}"

[[checkers]]
name = "expect_200"
type = "http_status"
equal = [200]

[[probes]]
name = "Failing Probe 1"
tags = ["fail_group"]
checkers = ["expect_200"]
[probes.fetcher]
type = "http_get"
url = "${{base}}/does-not-exist"

[[probes]]
name = "Subsequent Probe 2"
tags = ["fail_group"]
checkers = ["expect_200"]
[probes.fetcher]
type = "http_get"
url = "${{base}}/html"
""",
                encoding="utf-8",
            )
            loaded = load_scan_config(cfg_file)
            tags = parse_tags_filter("  FAIL_GROUP , other_tag ")
            filtered = filter_and_order_probes(loaded.probes, tags_filter=tags, randomize=False)
            self.assertEqual(len(filtered), 2)

            buf = io.StringIO()
            reporter = ConsoleReporter(include_debug=False, stream=buf)
            runner = ScanRunner(
                max_concurrent=1,
                abort_on_failure=True,
                on_probe_complete=reporter.on_probe_complete,
            )
            report = runner.run(str(cfg_file), filtered)
            self.assertTrue(report.aborted_early)
            self.assertEqual(report.total_failed, 1)
            self.assertEqual(report.total_aborted, 1)

    def test_performance_report_output(self) -> None:
        cfg_path = (
            Path(__file__).resolve().parent.parent
            / "scan-config"
            / "coralbricks"
            / "main.toml"
        )
        loaded = load_scan_config(
            cfg_path,
            cli_vars={
                "base_url": f"{self.base_url}/v1",
                "website_url": self.base_url,
                "trust_url": self.base_url,
                "coralbricks_api_key": "cb_test_secret",
            },
        )
        buf = io.StringIO()
        reporter = ConsoleReporter(include_debug=False, report_performance=True, stream=buf)
        runner = ScanRunner(
            max_concurrent=8,
            abort_on_failure=False,
            on_probe_complete=reporter.on_probe_complete,
        )
        report = runner.run(str(cfg_path), loaded.probes[:10])
        reporter.print_summary(report)

        output = buf.getvalue()
        self.assertIn("Performance Summary:", output)
        self.assertIn("Median:", output)
        self.assertIn("Requests/sec:", output)
        self.assertIn("Response time histogram:", output)
        self.assertIn("Latency distribution:", output)
        self.assertIn("Details (average, fastest, slowest):", output)
        self.assertIn("DNS+dialup:", output)
        self.assertIn("Status code distribution:", output)
        for res in report.outcomes:
            self.assertIsNotNone(res.response)
            self.assertIsNotNone(res.response.timings)


if __name__ == "__main__":
    unittest.main()
