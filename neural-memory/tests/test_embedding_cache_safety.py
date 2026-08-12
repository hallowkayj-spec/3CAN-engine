from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOOLS = ROOT / "tools"
DIMENSION = 1024


def _load_module(name: str, path: Path):
    sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cache_modules():
    return [
        _load_module("graph_engine_cache_test", BACKEND / "graph_engine.py"),
        _load_module(
            "build_topic_neighbors_cache_test", TOOLS / "build_topic_neighbors.py"
        ),
        _load_module("edge_inferrer_cache_test", TOOLS / "edge_inferrer.py"),
    ]


def _write_valid_cache(path: Path) -> None:
    np.savez(
        path,
        ids=np.array(["NODE-a", "NODE-b"]),
        embeddings=np.ones((2, DIMENSION), dtype=np.float32),
        backend_id=np.array(["hashing-blake2b-char-ngram-v1"]),
    )


def _read_cache(module, path: Path):
    result = module._read_embedding_cache(path)
    return result[0], result[1]


def test_valid_embedding_cache_loads_in_all_consumers(tmp_path, cache_modules):
    cache = tmp_path / "embeddings.npz"
    _write_valid_cache(cache)

    for module in cache_modules:
        ids, embeddings = _read_cache(module, cache)
        assert ids == ["NODE-a", "NODE-b"]
        assert embeddings.shape == (2, DIMENSION)
        assert embeddings.dtype == np.float32


@pytest.mark.parametrize(
    ("ids", "embeddings", "extra"),
    [
        (
            np.array([{"node": "NODE-a"}], dtype=object),
            np.ones((1, DIMENSION), dtype=np.float32),
            {},
        ),
        (
            np.array(["NODE-a"]),
            np.array([[{"value": 1}]], dtype=object),
            {},
        ),
        (
            np.array([["NODE-a"]]),
            np.ones((1, DIMENSION), dtype=np.float32),
            {},
        ),
        (
            np.array(["NODE-a"]),
            np.ones((1, DIMENSION), dtype=np.int64),
            {},
        ),
        (
            np.array(["NODE-a", "NODE-b"]),
            np.ones((1, DIMENSION), dtype=np.float32),
            {},
        ),
        (
            np.array(["NODE-a"]),
            np.ones((1, DIMENSION - 1), dtype=np.float32),
            {},
        ),
        (
            np.array(["NODE-a"]),
            np.full((1, DIMENSION), np.nan, dtype=np.float32),
            {},
        ),
        (
            np.array(["NODE-a"]),
            np.ones((1, DIMENSION), dtype=np.float32),
            {"unexpected": np.array(["payload"])},
        ),
    ],
    ids=[
        "pickled-object-ids",
        "pickled-object-embeddings",
        "ids-rank",
        "embedding-dtype",
        "row-alignment",
        "embedding-dimension",
        "non-finite-values",
        "unexpected-array",
    ],
)
def test_malformed_embedding_cache_is_rejected_by_all_consumers(
    tmp_path, cache_modules, ids, embeddings, extra
):
    cache = tmp_path / "embeddings.npz"
    np.savez(
        cache,
        ids=ids,
        embeddings=embeddings,
        backend_id=np.array(["hashing-blake2b-char-ngram-v1"]),
        **extra,
    )

    for module in cache_modules:
        with pytest.raises(ValueError):
            module._read_embedding_cache(cache)


def test_graph_engine_rebuilds_instead_of_loading_pickled_cache(
    tmp_path, cache_modules, monkeypatch
):
    graph_engine = cache_modules[0]
    cache = tmp_path / "embeddings.npz"
    np.savez(
        cache,
        ids=np.array(["NODE-a"], dtype=object),
        embeddings=np.ones((1, DIMENSION), dtype=np.float32),
        backend_id=np.array(["hashing-blake2b-char-ngram-v1"]),
    )
    monkeypatch.setattr(graph_engine, "EMBEDDINGS_FILE", cache)

    engine = graph_engine.GraphEngine.__new__(graph_engine.GraphEngine)
    engine.nodes = {"NODE-a": object()}
    engine._node_embeddings = {}
    engine._embedding_backend_id = Mock(
        return_value="hashing-blake2b-char-ngram-v1"
    )
    engine._embedding_source_manifest = Mock(return_value="test-manifest")
    engine._rebuild_embeddings = Mock()

    engine._load_or_build_embeddings()

    engine._rebuild_embeddings.assert_called_once_with()
    assert engine._node_embeddings == {}


def test_tool_entrypoints_fail_closed_on_pickled_cache(
    tmp_path, cache_modules, monkeypatch
):
    cache = tmp_path / "embeddings.npz"
    np.savez(
        cache,
        ids=np.array(["NODE-a"], dtype=object),
        embeddings=np.ones((1, DIMENSION), dtype=np.float32),
    )
    topic_neighbors, edge_inferrer = cache_modules[1:]
    monkeypatch.setattr(topic_neighbors, "EMBEDDINGS_FILE", cache)
    monkeypatch.setattr(edge_inferrer, "EMBEDDINGS_FILE", cache)

    with pytest.raises(SystemExit) as exc:
        topic_neighbors.load_embeddings()
    assert exc.value.code == 1
    assert edge_inferrer.load_embeddings() is None
