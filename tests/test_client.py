"""Offline contract checks for the MCP adapter and operator settings."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from memoryshield_agent.client import BackendError, MemoryClient, MemoryMessage
from memoryshield_agent.config import Settings
from memoryshield_agent.server import create_server


SENTINEL = "private-upstream-payload-sentinel"


def valid_receipt(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(200, json={
        "success": True,
        "request_id": payload["request_id"],
        "session_id": payload["session_id"],
        "user_id": payload["user_id"],
        "written": len(payload["messages"]),
    })


class MemoryClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler, *, settings=None):
        settings = settings or Settings(
            "https://memory.example.test/memory/v1", "configured-person", "test-token",
        )
        client = MemoryClient(settings, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def test_remember_preserves_base_path_and_binds_namespace_and_auth(self):
        requests = []

        def handler(request):
            requests.append(request)
            return valid_receipt(request)

        client = self.make_client(handler)
        message = MemoryMessage(
            role="user", content="Remember the invented color teal.", timestamp=1700000000123,
        )
        result = await client.remember([message], "conversation-1", "write-1")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://memory.example.test/memory/v1/add")
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")
        self.assertEqual(json.loads(request.content), {
            "user_id": "configured-person",
            "session_id": "conversation-1",
            "request_id": "write-1",
            "messages": [message.model_dump()],
        })
        self.assertEqual(result, {
            "success": True, "request_id": "write-1",
            "session_id": "conversation-1", "written": 1,
        })

    async def test_tools_do_not_expose_operator_namespace_as_an_argument(self):
        client = self.make_client(valid_receipt)
        server = create_server(client.settings, client)
        tool_result = server.list_tools()
        tools = await tool_result if inspect.isawaitable(tool_result) else tool_result
        self.assertEqual(
            {tool.name for tool in tools},
            {"memoryshield_remember", "memoryshield_recall", "memoryshield_status"},
        )
        for tool in tools:
            properties = tool.input_schema.get("properties", {})
            with self.subTest(tool=tool.name):
                self.assertNotIn("user_id", properties)
                self.assertNotIn("namespace", properties)
                self.assertNotIn("api_key", properties)
                self.assertNotIn("base_url", properties)
        with self.assertRaises(ValidationError):
            MemoryMessage(content="Invented text", user_id="another-person")

    async def test_remember_requires_matching_receipt_echoes_and_count(self):
        mutations = (
            {"success": False}, {"success": 1}, {"request_id": "other-write"},
            {"session_id": "other-conversation"}, {"user_id": "another-person"},
            {"written": True}, {"written": "1"}, {"written": -1},
            {"written": 0}, {"written": 2},
        )
        for change in mutations:
            with self.subTest(change=change):
                def handler(request):
                    receipt = valid_receipt(request).json()
                    receipt.update(change)
                    return httpx.Response(200, json=receipt)

                client = self.make_client(handler)
                with self.assertRaisesRegex(BackendError, "backend_invalid_add_receipt"):
                    await client.remember([MemoryMessage(content="Invented text")], "s-1", "r-1")

    async def test_timeout_returns_reusable_request_id_and_does_not_retry(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ReadTimeout(SENTINEL, request=request)
            return valid_receipt(request)

        client = self.make_client(handler)
        messages = [MemoryMessage(content="An invented preference.")]
        with self.assertRaises(BackendError) as caught:
            await client.remember(messages, "s-1", None)
        error = str(caught.exception)
        self.assertIn("backend_timeout", error)
        self.assertNotIn(SENTINEL, error)
        request_id = re.search(r"request_id=(mcp-[0-9a-f]+)", error).group(1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(requests[0].content)["request_id"], request_id)
        result = await client.remember(messages, "s-1", request_id)
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(requests[0].content, requests[1].content)

    async def test_http_failures_hide_payloads_and_do_not_follow_redirects(self):
        for status in (401, 409, 503, 302):
            requests = []

            def handler(request):
                requests.append(request)
                return httpx.Response(
                    status, json={"detail": SENTINEL},
                    headers={"Location": "https://unexpected.example.test/" + SENTINEL},
                )

            with self.subTest(status=status):
                client = self.make_client(handler)
                with self.assertRaises(BackendError) as caught:
                    await client.remember([MemoryMessage(content="Invented text")], "s-1", "r-1")
                error = str(caught.exception)
                self.assertIn(f"backend_http_{status}", error)
                self.assertIn("request_id=r-1", error)
                self.assertNotIn(SENTINEL, error)
                self.assertNotIn("test-token", error)
                self.assertEqual(len(requests), 1)

    async def test_connection_failure_hides_upstream_exception(self):
        def handler(request):
            raise httpx.ConnectError(SENTINEL, request=request)

        client = self.make_client(handler)
        with self.assertRaises(BackendError) as caught:
            await client.recall("invented query", 5)
        self.assertEqual(str(caught.exception), "backend_connection_failed")

    async def test_malformed_json_and_wrong_root_are_sanitized(self):
        for body in (b"{bad-json-" + SENTINEL.encode(), b"[1, 2]", b"null", b'"text"'):
            with self.subTest(body=body):
                client = self.make_client(lambda request: httpx.Response(200, content=body))
                with self.assertRaises(BackendError) as caught:
                    await client.recall("invented query", 5)
                self.assertIn(str(caught.exception), {"backend_invalid_json", "backend_invalid_response"})
                self.assertNotIn(SENTINEL, str(caught.exception))

    async def test_recall_preserves_evidence_and_source_metadata(self):
        evidence = {
            "id": "raw_1", "content": "Original invented statement.", "score": 0.82,
            "created_at": "2026-01-01T00:00:00Z", "session_id": "original-session",
            "role": "user", "source_rid": 1, "seq": 2, "excerpt": True,
        }
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [evidence]})

        client = self.make_client(handler)
        result = await client.recall("What was the invented statement?", 5)
        self.assertEqual(result, {"memories": [evidence], "count": 1})
        self.assertEqual(str(requests[0].url), "https://memory.example.test/memory/v1/search")
        self.assertEqual(json.loads(requests[0].content), {
            "query": "What was the invented statement?", "user_id": "configured-person", "top_k": 5,
        })

    async def test_recall_rejects_invalid_envelopes_and_evidence(self):
        valid = {"id": "raw_1", "content": "Invented text.", "score": 0.5}
        bodies = (
            {}, {"data": None}, {"data": {}}, {"data": [valid, valid]},
            {"data": [None]}, {"data": ["text"]}, {"data": [{"id": "x"}]},
            {"data": [{**valid, "id": 5}]}, {"data": [{**valid, "content": []}]},
            {"data": [{**valid, "score": True}]}, {"data": [{**valid, "score": "0.5"}]},
        )
        for body in bodies:
            with self.subTest(body=body):
                client = self.make_client(lambda request: httpx.Response(200, json=body))
                with self.assertRaisesRegex(BackendError, "backend_invalid_search_"):
                    await client.recall("invented query", 1)
        for score in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(score=score):
                raw = '{"data":[{"id":"x","content":"invented","score":' + score + '}]}'
                client = self.make_client(lambda request: httpx.Response(200, content=raw))
                with self.assertRaisesRegex(BackendError, "backend_invalid_search_evidence"):
                    await client.recall("invented query", 1)

    async def test_empty_search_results_are_valid(self):
        client = self.make_client(lambda request: httpx.Response(200, json={"data": []}))
        self.assertEqual(await client.recall("invented query", 1), {"memories": [], "count": 0})

    async def test_blank_and_oversized_writes_make_no_request(self):
        requests = []

        def handler(request):
            requests.append(request)
            return valid_receipt(request)

        client = self.make_client(handler)
        batches = ([], [MemoryMessage(content=" ")], [MemoryMessage(content="x" * 32000)] * 3)
        for messages in batches:
            with self.subTest(messages=len(messages)):
                with self.assertRaises(BackendError):
                    await client.remember(messages, "s-1", "r-1")
        with self.assertRaisesRegex(BackendError, "query_must_contain_nonempty_text"):
            await client.recall(" \n", 1)
        self.assertEqual(requests, [])

    async def test_status_exposes_only_documented_health_fields(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={
                "status": "ok", "version": "v3.17.1", "add_model": "gpt-4o-mini",
                "embedding_model": "text-embedding-v4", "add_model_configured": True,
                "rerank_enabled": False, "rerank_model": None,
                "api_key": SENTINEL, "internal_details": SENTINEL,
            })

        client = self.make_client(handler)
        result = await client.status()
        self.assertEqual(result["check"], "backend_configuration")
        self.assertEqual(result["version"], "v3.17.1")
        self.assertNotIn(SENTINEL, json.dumps(result))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.path, "/memory/v1/health")


class SettingsTests(unittest.TestCase):
    def test_remote_backend_requires_https_and_token(self):
        for url, token in (
            ("http://memory.example.test", "test-token"),
            ("https://memory.example.test", ""),
        ):
            with self.subTest(url=url, token_present=bool(token)):
                with self.assertRaisesRegex(ValueError, "Remote backends require HTTPS"):
                    Settings(url, "person", token)
        self.assertEqual(Settings("https://memory.example.test", "person", "test-token").user_id, "person")

    def test_local_loopback_allows_http_without_token(self):
        for url in ("http://127.0.0.1:18080", "http://localhost:18080", "http://[::1]:18080"):
            with self.subTest(url=url):
                self.assertEqual(Settings(url, "person").api_key, "")

    def test_namespace_is_required_even_for_local_backend(self):
        for value in ("", " \n", "x" * 201):
            with self.subTest(namespace_length=len(value)):
                with self.assertRaisesRegex(ValueError, "MEMORYSHIELD_USER_ID"):
                    Settings("http://127.0.0.1", value)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MEMORYSHIELD_USER_ID"):
                Settings.from_env()

    def test_unsafe_url_parts_and_invalid_ports_are_rejected(self):
        for url in (
            "ftp://memory.example.test", "https://name:password@memory.example.test",
            "https://memory.example.test?token=value", "https://memory.example.test#part",
            "https://memory.example.test:invalid", "https://memory.example.test:65536",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    Settings(url, "person", "test-token")

    def test_timeout_range_and_single_line_tokens(self):
        for timeout in (0, 1801, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "MEMORYSHIELD_TIMEOUT_SECONDS"):
                    Settings("http://127.0.0.1", "person", timeout_seconds=timeout)
        for token in ("line\nnext", "line\rnext"):
            with self.assertRaisesRegex(ValueError, "single line"):
                Settings("http://127.0.0.1", "person", token)

    def test_token_file_fallback_handles_bom_and_whitespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "token.txt"
            path.write_text("  file-token\n", encoding="utf-8-sig")
            with patch.dict(os.environ, {
                "MEMORYSHIELD_URL": "https://memory.example.test/",
                "MEMORYSHIELD_USER_ID": "person",
                "MEMORYSHIELD_API_KEY_FILE": str(path),
            }, clear=True):
                settings = Settings.from_env()
            self.assertEqual(settings.api_key, "file-token")
            self.assertEqual(settings.base_url, "https://memory.example.test")

    def test_explicit_token_takes_priority_over_token_file(self):
        with patch.dict(os.environ, {
            "MEMORYSHIELD_USER_ID": "person", "MEMORYSHIELD_API_KEY": "  env-token  ",
            "MEMORYSHIELD_API_KEY_FILE": "nonexistent-test-token-file.txt",
        }, clear=True):
            self.assertEqual(Settings.from_env().api_key, "env-token")

    def test_unreadable_token_file_raises_sanitized_error(self):
        with patch.dict(os.environ, {
            "MEMORYSHIELD_USER_ID": "person",
            "MEMORYSHIELD_API_KEY_FILE": "nonexistent-test-token-file-" + SENTINEL + ".txt",
        }, clear=True):
            with self.assertRaises(ValueError) as caught:
                Settings.from_env()
        self.assertEqual(str(caught.exception), "Cannot read MEMORYSHIELD_API_KEY_FILE")

    def test_settings_repr_hides_api_token(self):
        settings = Settings("https://memory.example.test", "person", SENTINEL)
        self.assertNotIn(SENTINEL, repr(settings))


if __name__ == "__main__":
    unittest.main()
