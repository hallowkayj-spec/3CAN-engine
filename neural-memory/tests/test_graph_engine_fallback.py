from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def load_graph_engine():
    sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location("graph_engine_under_test", BACKEND / "graph_engine.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["graph_engine_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_hashing_embedding_fallback_matches_bge_dimension():
    graph_engine = load_graph_engine()
    model = graph_engine._HashingEmbeddingModel()

    embeddings = model.encode(["3CAN token hook status", "RPA KB backend continuation"])

    assert embeddings.shape == (2, graph_engine._EMBEDDING_DIM)
    assert graph_engine._EMBEDDING_DIM == 1024
    assert model.backend_id == "hashing-blake2b-char-ngram-v1"
    assert embeddings.sum() > 0


def test_graph_traversal_is_a_rank_preserving_secondary_signal():
    graph_engine = load_graph_engine()
    engine = graph_engine.GraphEngine.__new__(graph_engine.GraphEngine)
    engine.edges = [
        graph_engine.Edge(
            source="DOC-anchor",
            target="INTF-neighbor",
            type="depends_on",
            weight=1.0,
        )
    ]
    scores = {
        "DOC-anchor": 0.0328,
        "DEC-second": 0.0320,
        "PROC-third": 0.0315,
        "INTF-neighbor": 0.0290,
    }
    anchors = {"DOC-anchor", "DEC-second", "PROC-third"}

    metadata = engine._apply_graph_traversal_boost(scores, anchors)

    assert scores["INTF-neighbor"] < min(scores[node_id] for node_id in anchors)
    assert metadata["rank_preserving"] is True
    assert metadata["anchor_floor"] == 0.0315


def test_query_expander_domain_aliases_are_enabled_by_default():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(BACKEND))
    from query_expander import QueryExpander

    expander = QueryExpander(backends=["domain_aliases"])
    expanded = expander.expand("semantic route impact", top_k=4)
    queries = [item[0] for item in expanded]

    assert expander.enabled is True
    assert "domain_alias" in expander.active_adapters
    assert any("bge" in query for query in queries)
    assert any("context engine" in query for query in queries)


def test_cilin_adapter_uses_local_dictionary(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(BACKEND))
    cilin_path = tmp_path / "cilin.txt"
    cilin_path.write_text("Aa01A01= 运营 经营 操盘\n", encoding="utf-8")
    monkeypatch.setenv("CILIN_PATH", str(cilin_path))
    from expansions import cn_cilin

    monkeypatch.setattr(cn_cilin, "_JIEBA_OK", False)

    expander = cn_cilin.CilinExpander()
    expanded = expander.expand("运营方案", top_k=2)
    queries = [item.query for item in expanded]

    assert expander.available() is True
    assert "经营方案" in queries or "操盘方案" in queries


def test_route_uses_query_expansion_and_exposes_meta(tmp_path, monkeypatch):
    graph_dir = tmp_path / "graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    nodes = [
        {
            "id": "DEC-semantic-route",
            "name": "Semantic route precision",
            "cluster": "3CAN",
            "type": "decision",
            "status": "active",
            "priority": "high",
            "content": {"description": "BGE reranker hybrid retrieval and query expansion precision"},
            "activation_keywords": ["bge", "reranker", "hybrid", "retrieval", "query expansion"],
        },
        {
            "id": "DOC-unrelated",
            "name": "Unrelated note",
            "cluster": "Docs",
            "type": "knowledge",
            "status": "active",
            "priority": "low",
            "content": {"description": "general project note"},
            "activation_keywords": ["general", "note"],
        },
    ]
    for node in nodes:
        (nodes_dir / f"{node['id']}.json").write_text(json.dumps(node), encoding="utf-8")

    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    graph_engine = load_graph_engine()
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    engine = graph_engine.GraphEngine()
    req = graph_engine.RoutingRequest(task="semantic", max_nodes=1, include_edges=False, agent_id="unit")

    result = engine.route(req)

    assert result.activated_nodes[0].id == "DEC-semantic-route"
    assert result.route_meta["expanded_query_changed"] is True
    assert result.route_meta["query_expansion"]["variants"]
    assert result.route_meta["embedding_backend"] == "hashing-blake2b-char-ngram-v1"


def test_graph_traversal_anchor_set_is_independent_of_requested_result_size(
    tmp_path,
    monkeypatch,
):
    graph_dir = tmp_path / "graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    for index in range(20):
        node = {
            "id": f"DOC-route-{index:02d}",
            "name": f"Route evidence {index:02d}",
            "cluster": "3CAN",
            "type": "knowledge",
            "status": "active",
            "priority": "medium",
            "content": {"description": f"semantic route evidence {index:02d}"},
            "activation_keywords": ["semantic", "route", f"evidence-{index:02d}"],
        }
        (nodes_dir / f"{node['id']}.json").write_text(
            json.dumps(node),
            encoding="utf-8",
        )

    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    graph_engine = load_graph_engine()
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    engine = graph_engine.GraphEngine()

    narrow = engine.route(
        graph_engine.RoutingRequest(
            task="semantic route evidence",
            max_nodes=3,
            include_edges=False,
            agent_id="unit-narrow",
        )
    )
    wide = engine.route(
        graph_engine.RoutingRequest(
            task="semantic route evidence",
            max_nodes=12,
            include_edges=False,
            agent_id="unit-wide",
        )
    )

    assert narrow.route_meta["graph_traversal_boost"]["anchor_count"] == 10
    assert wide.route_meta["graph_traversal_boost"]["anchor_count"] == 10


def test_cooccurrence_expansion_is_stable_for_equal_frequency_terms():
    graph_engine = load_graph_engine()
    engine = graph_engine.GraphEngine.__new__(graph_engine.GraphEngine)
    engine.nodes = {}
    engine._cooccurrence = {
        "semantic": {"zeta-3", "zeta-2", "zeta-1"},
        "route": {"alpha-3", "alpha-2", "alpha-1"},
    }

    expanded = engine._expand_query("route semantic")

    assert expanded == "route semantic alpha-1 alpha-2 alpha-3 zeta-1 zeta-2"


def test_natural_language_reuses_only_discriminative_short_code_index(
    tmp_path,
    monkeypatch,
):
    graph_dir = tmp_path / "graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    nodes = [
        {
            "id": "DOC-t8-recovery",
            "name": "Recovery evidence",
            "cluster": "3CAN",
            "type": "knowledge",
            "status": "active",
            "priority": "medium",
            "content": {"description": "T8 canonical recovery"},
            "activation_keywords": ["recovery"],
        }
    ]
    nodes.extend(
        {
            "id": f"DOC-ai45-{index:02d}",
            "name": f"AI45 evidence {index:02d}",
            "cluster": "3CAN",
            "type": "knowledge",
            "status": "active",
            "priority": "medium",
            "content": {"description": "AI45 broad project label"},
            "activation_keywords": ["project"],
        }
        for index in range(16)
    )
    for node in nodes:
        (nodes_dir / f"{node['id']}.json").write_text(
            json.dumps(node),
            encoding="utf-8",
        )

    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    graph_engine = load_graph_engine()
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    engine = graph_engine.GraphEngine()

    discriminative = engine.route(
        graph_engine.RoutingRequest(
            task="How should T8 be recovered in this project?",
            max_nodes=3,
            include_edges=False,
            agent_id="unit-t8",
        )
    )
    broad = engine.route(
        graph_engine.RoutingRequest(
            task="What is the current AI45 project status?",
            max_nodes=3,
            include_edges=False,
            agent_id="unit-ai45",
        )
    )

    assert discriminative.route_meta["code_signal"] == {
        "mode": "embedded",
        "tokens": ["T8"],
        "resolved_count": 1,
    }
    assert discriminative.route_meta["rrf_weights"][3] == 5.0
    assert "DOC-t8-recovery" in discriminative.route_meta["semantic_result_ids"]
    assert broad.route_meta["code_signal"] == {
        "mode": "none",
        "tokens": ["AI45"],
        "resolved_count": 0,
    }
    assert broad.route_meta["rrf_weights"][3] == 0.0
