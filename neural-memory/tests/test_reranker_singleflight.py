from __future__ import annotations

import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def _load_graph_engine():
    name = "graph_engine_reranker_singleflight"
    spec = importlib.util.spec_from_file_location(name, BACKEND / "graph_engine.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _install_fake_sentence_transformers(monkeypatch, *, delay: float = 0.03):
    state = {"constructors": 0, "predicts": 0}
    state_lock = threading.Lock()
    constructor_started = threading.Event()

    class FakeCrossEncoder:
        def __init__(self, model_name: str, *, max_length: int):
            assert model_name == "BAAI/bge-reranker-v2-m3"
            assert max_length == 512
            with state_lock:
                state["constructors"] += 1
            constructor_started.set()
            time.sleep(delay)

        def predict(self, pairs):
            assert pairs
            with state_lock:
                state["predicts"] += 1
            return [1.0] * len(pairs)

    fake_module = ModuleType("sentence_transformers")
    fake_module.CrossEncoder = FakeCrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    return state, constructor_started


def test_shared_loader_constructs_and_warms_once_under_concurrency(monkeypatch):
    module = _load_graph_engine()
    state, _ = _install_fake_sentence_transformers(monkeypatch)

    with ThreadPoolExecutor(max_workers=8) as pool:
        models = list(pool.map(lambda _: module._load_cn_reranker_singleflight(), range(8)))

    assert len({id(model) for model in models}) == 1
    assert state == {"constructors": 1, "predicts": 1}


def test_background_warmup_and_foreground_loader_share_one_flight(monkeypatch):
    module = _load_graph_engine()
    state, constructor_started = _install_fake_sentence_transformers(
        monkeypatch,
        delay=0.08,
    )
    monkeypatch.setenv("THREECAN_RERANKER_MODE", "adaptive")
    monkeypatch.setenv("THREECAN_RERANKER_WARMUP", "background")

    engine = object.__new__(module.GraphEngine)
    engine._cn_reranker_loading = False
    engine._cn_reranker_warmup_started = False
    engine._cn_reranker_warmup_error = ""
    engine._cn_reranker_warmup_thread = None

    engine._start_reranker_warmup()
    assert constructor_started.wait(timeout=1.0)

    with ThreadPoolExecutor(max_workers=1) as pool:
        foreground = pool.submit(module._load_cn_reranker_singleflight)
        engine._cn_reranker_warmup_thread.join(timeout=2.0)
        foreground_model = foreground.result(timeout=2.0)

    assert not engine._cn_reranker_warmup_thread.is_alive()
    assert engine._cn_reranker is foreground_model
    assert state == {"constructors": 1, "predicts": 1}
