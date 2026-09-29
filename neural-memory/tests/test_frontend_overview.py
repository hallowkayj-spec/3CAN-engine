import re
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


def _source() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def test_overview_uses_live_evidence_instead_of_legacy_claims() -> None:
    source = _source()

    assert "allStats.readiness" in source
    assert "effective_project_reality" in source
    assert "semanticQuality.status" in source
    assert "error_family_index" not in source
    assert "moduleProgress" not in source
    assert "2754/5000" not in source
    assert ">460<" not in source


def test_overview_distinguishes_deep_recheck_from_runtime_failure() -> None:
    source = _source()

    assert "function readinessView" in source
    assert re.search(r"cache\.deep_required\s*===\s*true", source)
    assert "需要深度核验" in source
    assert "没有可复用的已验证深度证据" in source
    assert "这不等于 Runtime 已故障" in source


def test_overview_does_not_expand_secret_registry_or_all_errors() -> None:
    source = _source()

    assert "API Keys & Secrets Registry" not in source
    assert "stat-secrets" not in source
    assert "密钥引用、私有网络拓扑和完整错误正文不在概览中展开" in source
    assert "原始 API 当前没有认证或 RBAC" in source
    assert "只能限制在 localhost" in source
    assert re.search(
        r"任务描述会进入本机\s*route buffer，其中前 60 字写入\s*activity", source
    )
    assert re.search(r"\.slice\(\s*0,\s*6\s*\)", source)


def test_frontend_has_accessible_navigation_and_safe_text_rendering() -> None:
    source = _source()

    assert re.search(r'<button\s+class="view-tab active"', source)
    assert 'aria-pressed="true"' in source
    assert 'aria-live="polite"' in source
    assert re.search(r'<label\s+class="sr-only"\s+for="search-input">', source)
    assert re.search(r'<label\s+class="sr-only"\s+for="route-input">', source)
    assert re.search(r'<label\s+for="size-slider">节点尺寸</label', source)
    assert "function escapeHtml" in source
    assert re.search(r"escapeHtml\(node\.name\s*\|\|\s*node\.id\)", source)
    assert re.search(r"escapeHtml\(error\.message\s*\|\|\s*error\)", source)
    assert re.search(r'<aside\s+id="panel"[^>]+inert', source)
    assert "panel.inert = false" in source
    assert "panel.inert = true" in source
    assert 'id="dashboard-content"></div>' in source


def test_frontend_uses_fixed_interval_polling_without_broken_websocket_loop() -> None:
    source = _source()

    assert re.search(r"setInterval\(\s*refreshStatsOnly,\s*60000\s*\)", source)
    assert "new WebSocket" not in source
    assert "setTimeout(connectWS" not in source
    assert "markStatsStale(error)" in source
    assert 'textContent = "Stale"' in source
    assert 'value === null || value === undefined || value === ""' in source


def test_graph_is_lazy_and_overview_remains_available_without_3d_dependency() -> None:
    source = _source()

    assert '<body data-view="overview">' in source
    assert '<script src="https://unpkg.com/3d-force-graph' not in source
    assert "function loadGraphDependency" in source
    assert "function ensureGraphData" in source
    assert "await ensureGraphData()" in source
    assert re.search(
        r"graphLoaded\s*\?\s*fetchJson\(['\"]\/api\/graph['\"]\)\s*:\s*Promise\.resolve\(null\)",
        source,
    )
    assert "script.integrity" in source
    assert "sha384-GNPicn8pBA2" in source
    assert re.search(
        r"if\s*\(\s*typeof ForceGraph3D\s*!==\s*['\"]function['\"]\s*\)",
        source,
    )
    assert "3D 图谱依赖未加载。项目概览与 API 状态仍可使用。" in source


def test_route_panel_consumes_current_route_response_schema() -> None:
    source = _source()

    assert re.search(
        r"Array\.isArray\(result\.nodes\)\s*\?\s*result\.nodes", source
    )
    assert re.search(
        r"Array\.isArray\(result\.activated_nodes\)\s*\?\s*result\.activated_nodes",
        source,
    )
    assert "agent_id: UI_AGENT_ID" in source
    assert "session_instance_id: UI_SESSION_ID" in source
    assert "confirm_low_confidence: confirmLowConfidence" in source
    assert 'error.status === 428' in source
    assert 'detail.error === "low_confidence_requires_confirmation"' in source
