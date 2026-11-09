"""Offline HTTP tests: slow telemetry must not stall unrelated Runtime requests."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(tmp_path / "graph"))
    monkeypatch.setenv("THREECAN_EMBEDDING_BACKEND", "hashing")
    monkeypatch.setenv("THREECAN_RERANKER_MODE", "off")
    path = Path(__file__).resolve().parents[1] / "backend/app.py"
    spec = importlib.util.spec_from_file_location(f"token_api_{tmp_path.name}", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.GRAPH_DIR = tmp_path / "graph"
    return module


@pytest.mark.parametrize("method,path,operation,local_function", [
    ("POST", "/api/token-usage/events", "record_event", False),
    ("GET", "/api/token-usage/summary", "summary", False),
    ("GET", "/api/token-usage/health", "health", False),
    ("GET", "/api/token-usage/status", "integration_status", False),
    ("GET", "/api/token-usage/overview", "overview", False),
    ("GET", "/api/token-usage/impact", "impact", False),
    ("GET", "/api/token-usage/codex-status", "collect_codex_status_events", True),
    ("POST", "/api/token-usage/import/codex-status", "import_codex_status_events", False),
    ("POST", "/api/token-usage/estimate", "estimate_tokens_for_payload", True),
])
def test_slow_telemetry_does_not_block_http_liveness(api, monkeypatch, method, path, operation, local_function):
    entered, release = threading.Event(), threading.Event()
    worker_ids = []

    def blocked_operation(*args, **kwargs):
        worker_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=3), "bounded test operation was not released"
        return {"sample": "retained"}

    if local_function:
        monkeypatch.setattr(api, operation, blocked_operation)
    else:
        store = SimpleNamespace(**{operation: blocked_operation})
        monkeypatch.setattr(api, "_get_token_usage_store", lambda: store)

    async def scenario():
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            held = asyncio.create_task(client.request(method, path, json={} if method == "POST" else None))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert not release.is_set(), "telemetry blocked the event loop until the watchdog fired"
                alive = await asyncio.wait_for(client.get("/api/health/live"), timeout=0.5)
                assert alive.status_code == 200 and alive.json()["alive"] is True
                assert not held.done(), "liveness must respond while the telemetry operation is still held"
                assert len(worker_ids) == 1 and worker_ids[0] != loop_thread
            finally:
                release.set()
                response = await asyncio.wait_for(held, timeout=3)
            assert response.status_code == 200
            body = response.json()
            assert (body["event"] if operation == "record_event" else body)["sample"] == "retained"

    # A native timer bounds the red test even if the old async handler stalls its loop.
    watchdog = threading.Timer(1.5, release.set)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.cancel()
        watchdog.join(timeout=2)


def test_concurrent_cold_store_initialization_is_singleton(api, monkeypatch):
    barrier = threading.Barrier(4)
    created = []

    def create(path):
        instance = object()
        created.append(instance)
        time.sleep(0.05)
        return instance

    def acquire(_):
        barrier.wait(timeout=2)
        return api._get_token_usage_store()

    monkeypatch.setattr(api, "TokenUsageStore", create)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(acquire, range(4)))
    assert len(created) == 1
    assert all(value is created[0] for value in values)


def test_http_store_contract_and_validation_are_retained(api):
    async def scenario():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for index in range(2):
                recorded = await client.post("/api/token-usage/events", json={
                    "request_id": f"offline-api-{index}", "provider": "fixture", "model": "fixture",
                    "input_tokens": 10, "output_tokens": 5})
                assert recorded.status_code == 200 and recorded.json()["ok"] is True
                assert recorded.json()["event"]["total_tokens"] == 15
            summary = await client.get("/api/token-usage/summary", params={"provider": "fixture"})
            assert summary.status_code == 200
            assert summary.json()["totals"]["total_tokens"] == 30
            assert (await client.get("/api/token-usage/summary", params={"group_by": "invalid"})).status_code == 400
            assert (await client.get("/api/token-usage/overview", params={"limit": 0})).status_code == 422
            assert (await client.get("/api/token-usage/overview", params={"limit": 51})).status_code == 422
    asyncio.run(scenario())


def test_auto_import_cold_store_acquisition_runs_in_existing_worker(api, monkeypatch):
    acquired = threading.Event()
    acquisition_threads = []

    def acquire():
        acquisition_threads.append(threading.get_ident())
        acquired.set()
        return SimpleNamespace(import_codex_status_events=lambda **kwargs: {"ok": True})

    monkeypatch.setattr(api, "_get_token_usage_store", acquire)
    monkeypatch.setenv("THREECAN_TOKEN_AUTO_IMPORT_INITIAL_DELAY", "0")

    async def scenario():
        loop_thread = threading.get_ident()
        job = asyncio.create_task(api._token_usage_auto_import_loop())
        try:
            assert await asyncio.to_thread(acquired.wait, 2)
            assert acquisition_threads == [acquisition_threads[0]]
            assert acquisition_threads[0] != loop_thread
        finally:
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job
    asyncio.run(scenario())
