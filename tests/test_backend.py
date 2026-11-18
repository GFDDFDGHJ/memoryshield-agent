"""Real backend contract checks with synthetic data and mocked paid providers.

Install backend/requirements.txt before running this optional integration test.
No API keys are required. Outbound socket connections are blocked during it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1] / "backend"
DEPENDENCIES = ("fastapi", "httpx", "openai", "sqlite_vec")


class BackendContractTests(unittest.TestCase):
    @unittest.skipUnless(
        all(importlib.util.find_spec(name) is not None for name in DEPENDENCIES),
        "Optional backend dependencies are required. Install backend/requirements.txt.",
    )
    def test_real_add_search_with_mocked_providers(self) -> None:
        for line in (BACKEND / "PROVENANCE.txt").read_text(encoding="utf-8").splitlines():
            if len(line) > 66 and line[64:66] == "  ":
                digest, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256((BACKEND / name).read_bytes()).hexdigest(), digest)
        with tempfile.TemporaryDirectory(prefix="memoryshield-agent-test-") as directory:
            database = Path(directory) / "memory.db"
            environment = {
                "AML_DB_PATH": str(database),
                "AML_AUTH_TOKEN": "offline-test-token",
                "OPENAI_API_KEY": "offline-test-key",
                "EMBEDDING_API_KEY": "offline-test-key",
                "DASHSCOPE_API_KEY": "offline-test-key",
                "AML_RERANK": "false",
                "EMBEDDING_DIM": "1024",
                "AML_VERSION": "v3.17.1",
                "AML_GOVERNANCE": "false",
                "AML_ADD_SELECTION_FAILURE_POLICY": "raw_on_unmatched",
                "AML_ADD_LLM_TIMEOUT": "60",
                "AML_ADD_LLM_TOTAL_TIMEOUT": "180",
                "AML_ADD_LLM_CONCURRENCY": "2",
                "AML_ADD_LLM_CHUNK_CHARS": "12000",
                "AML_ADD_LLM_MAX_OUTPUT_TOKENS": "2048",
            }
            original_connect = socket.socket.connect

            def connect_loopback_only(sock, address):
                # Windows uses loopback sockets to create an asyncio socketpair.
                if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
                    return original_connect(sock, address)
                raise AssertionError("Outbound network forbidden in offline test")

            with patch.dict(os.environ, environment), patch(
                "socket.create_connection", side_effect=AssertionError("Network forbidden in offline test")
            ), patch(
                "socket.socket.connect", new=connect_loopback_only
            ):
                sys.path.insert(0, str(BACKEND))
                try:
                    import app
                    from add_memory import AddMemoryError, ExtractionResult
                    from fastapi.testclient import TestClient

                    class OfflineExtractor:
                        def extract(self, messages):
                            return ExtractionResult([], 1, 1, "gpt-4o-mini", 1)

                    def embed(texts, **kwargs):
                        return [[1.0] + [0.0] * 1023 for _ in texts]

                    with patch.object(app, "_audit"), patch.object(app.store, "embed", embed), patch.object(
                        app.store, "_add_extractor", OfflineExtractor()
                    ), TestClient(app.app) as client:
                        health = client.get("/health")
                        self.assertEqual(health.status_code, 200)
                        self.assertEqual(health.json()["version"], "v3.17.1")
                        headers = {"Authorization": "Bearer offline-test-token"}
                        payload = {
                            "user_id": "offline-user",
                            "session_id": "offline-session",
                            "request_id": "offline-request",
                            "messages": [{"role": "user", "content": "I prefer quiet libraries."}],
                        }
                        self.assertEqual(client.post("/add", json=payload).status_code, 401)
                        first = client.post("/add", json=payload, headers=headers)
                        self.assertEqual(first.status_code, 200, first.text)
                        self.assertEqual(first.json()["written"], 1)
                        replay = client.post("/add", json=payload, headers=headers)
                        self.assertEqual(replay.status_code, 200)
                        self.assertEqual(replay.json()["written"], 1)
                        database_connection = sqlite3.connect(database)
                        try:
                            self.assertEqual(database_connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
                        finally:
                            database_connection.close()
                        changed = {**payload, "messages": [{"role": "user", "content": "I prefer noisy cafes."}]}
                        self.assertEqual(client.post("/add", json=changed, headers=headers).status_code, 409)
                        search = {"user_id": "offline-user", "query": "What do I prefer?", "top_k": 5}
                        recalled = client.post("/search", json=search, headers=headers)
                        self.assertEqual(recalled.status_code, 200, recalled.text)
                        self.assertTrue(recalled.json()["data"])
                        isolated = client.post("/search", json={**search, "user_id": "another-offline-user"}, headers=headers)
                        self.assertEqual(isolated.status_code, 200)
                        self.assertEqual(isolated.json()["data"], [])
                        retry_payload = {**payload, "request_id": "retry-after-failure"}
                        with patch.object(app.store._add_extractor, "extract", side_effect=AddMemoryError("Required Add model request failed")):
                            failure = client.post("/add", json=retry_payload, headers=headers)
                        self.assertEqual(failure.status_code, 503)
                        database_connection = sqlite3.connect(database)
                        try:
                            self.assertEqual(database_connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
                            self.assertEqual(database_connection.execute("SELECT COUNT(*) FROM add_requests WHERE request_id=?", ("retry-after-failure",)).fetchone()[0], 0)
                        finally:
                            database_connection.close()
                        recovered = client.post("/add", json=retry_payload, headers=headers)
                        self.assertEqual(recovered.status_code, 200, recovered.text)
                finally:
                    if "app" in sys.modules:
                        sys.modules["app"].store._pool.shutdown(wait=True)
                        sys.modules["app"].store._client.close()
                    sys.path.remove(str(BACKEND))


if __name__ == "__main__":
    unittest.main()
