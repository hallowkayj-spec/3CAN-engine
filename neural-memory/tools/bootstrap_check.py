"""3CAN Bootstrap Check — 基座#8 开源冷启动诊断 + 用户菜单

别人 clone 项目后第一次跑的入口. 做 5 件事:
  1. 环境检查: Python 版本 / 依赖 / 端口 / secrets
  2. 组件检查: backend / proxy / hooks / rules 是否到位
  3. 图谱体检: 节点数 / 孤立率 / 活跃度 / embedding cache
  4. Token 基线: skeleton vs full 测量
  5. 用户菜单: 交互式让用户选"跑健康扫描 / 跑 Leiden / 跑 GDI / 跑 skill_sync / 跳过"

核心原则 (the maintainer 明确):
- 3CAN 开源 = 引擎 + hooks + 规则 + 工具 + 文档 一整套
- 任何一个组件缺了, 部署效果 = 0
- 本工具是"部署验收 + onboarding 向导"
- 不删任何数据, 归档走 graph/archive/ 物理隔离

运行:
  python tools/bootstrap_check.py               # 交互模式
  python tools/bootstrap_check.py --non-interactive --report-only   # 生成报告不问
  python tools/bootstrap_check.py --auto-fix   # 自动修可修的 (比如缺目录)
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
THREE_CAN = "http://localhost:9700"

PY_REQ = (3, 11)
REQUIRED_HOOKS = [
    "3can-cold-start.js",
    "3can-prompt-observer.js",
    "3can-post-tool-capture.js",
    "3can-pre-compact-writeback.js",
]
REQUIRED_BACKEND_FILES = ["app.py", "graph_engine.py", "models.py"]
REQUIRED_PROXY_FILES = ["server.py"]
REQUIRED_TOOLS = [
    "skill_sync.py",
    "llm_guided_health.py",
    "leiden_community.py",
    "node_gdi_scorer.py",
    "session_aggregator.py",
    "bootstrap_check.py",
]


def color(s: str, c: str) -> str:
    # 简单 ANSI (Windows 新终端支持), 失败不影响
    codes = {"g": 32, "r": 31, "y": 33, "b": 34, "c": 36, "d": 2}
    if c not in codes or not sys.stdout.isatty():
        return s
    return f"\033[{codes[c]}m{s}\033[0m"


def check_port_listening(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        result = sock.connect_ex(("127.0.0.1", port))
        return result == 0
    finally:
        sock.close()


def check_environment() -> dict:
    report = {"section": "1. 环境", "checks": []}

    py_ok = sys.version_info[:2] >= PY_REQ
    report["checks"].append({
        "name": "Python 版本",
        "value": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "required": f">={PY_REQ[0]}.{PY_REQ[1]}",
        "ok": py_ok,
    })

    # 关键依赖
    for pkg in ["fastapi", "sentence_transformers", "numpy", "requests", "igraph", "leidenalg"]:
        try:
            __import__(pkg.replace("-", "_"))
            report["checks"].append({"name": f"包 {pkg}", "value": "installed", "ok": True})
        except ImportError:
            report["checks"].append({
                "name": f"包 {pkg}", "value": "missing",
                "required": "pip install " + pkg, "ok": False,
            })

    # 端口
    report["checks"].append({
        "name": "proxy 9700", "value": "listening" if check_port_listening(9700) else "free",
        "ok": True, "note": "listening=已起, free=未起 (后续可启)",
    })
    report["checks"].append({
        "name": "backend 9701 (green)", "value": "listening" if check_port_listening(9701) else "free",
        "ok": True,
    })
    report["checks"].append({
        "name": "backend 9702 (blue)", "value": "listening" if check_port_listening(9702) else "free",
        "ok": True,
    })

    # DeepSeek key (可选)
    secrets_f = HOME / ".claude" / "secrets.json"
    ds_key = None
    if secrets_f.exists():
        try:
            ds_key = (json.loads(secrets_f.read_text(encoding="utf-8")).get("deepseek") or {}).get("api_key")
        except Exception:
            pass
    report["checks"].append({
        "name": "DeepSeek API key",
        "value": "present" if ds_key else "absent",
        "ok": True,
        "note": "可选. LLM-guided 工具 (health/summary/curator) 需要. 无 key 则跳过这些工具",
    })

    return report


def check_components() -> dict:
    report = {"section": "2. 组件", "checks": []}

    # Backend
    backend_dir = ROOT / "backend"
    for f in REQUIRED_BACKEND_FILES:
        p = backend_dir / f
        report["checks"].append({"name": f"backend/{f}", "value": "exists" if p.exists() else "MISSING",
                                  "ok": p.exists()})
    # Proxy
    proxy_dir = ROOT / "proxy"
    for f in REQUIRED_PROXY_FILES:
        p = proxy_dir / f
        report["checks"].append({"name": f"proxy/{f}", "value": "exists" if p.exists() else "MISSING",
                                  "ok": p.exists()})
    # Tools
    tools_dir = ROOT / "tools"
    for f in REQUIRED_TOOLS:
        p = tools_dir / f
        report["checks"].append({"name": f"tools/{f}", "value": "exists" if p.exists() else "MISSING",
                                  "ok": p.exists()})

    # Hooks (~/.claude/scripts/hooks/)
    hooks_dir = HOME / ".claude" / "scripts" / "hooks"
    for h in REQUIRED_HOOKS:
        p = hooks_dir / h
        report["checks"].append({"name": f"hook {h}", "value": "exists" if p.exists() else "MISSING",
                                  "ok": p.exists(),
                                  "note": None if p.exists() else "复制自 3can-engine/hooks/ 到 ~/.claude/scripts/hooks/"})

    # settings.json hooks 注册
    settings_f = HOME / ".claude" / "settings.json"
    hooks_registered = {"PostToolUse": False, "PreCompact": False, "UserPromptSubmit": False, "SessionStart": False}
    if settings_f.exists():
        try:
            s = json.loads(settings_f.read_text(encoding="utf-8"))
            hooks_cfg = (s.get("hooks") or {})
            for evt in hooks_registered:
                if hooks_cfg.get(evt):
                    hooks_registered[evt] = True
        except Exception:
            pass
    for evt, ok in hooks_registered.items():
        report["checks"].append({
            "name": f"settings.json hook {evt}",
            "value": "registered" if ok else "MISSING",
            "ok": ok,
            "note": None if ok else "编辑 ~/.claude/settings.json 在 hooks 段注册",
        })

    return report


def check_graph_health() -> dict:
    report = {"section": "3. 图谱体检", "checks": []}
    if not check_port_listening(9700) and not check_port_listening(9701) and not check_port_listening(9702):
        report["checks"].append({
            "name": "引擎未启动", "value": "N/A", "ok": False,
            "note": "先启 backend+proxy, 再跑本体检",
        })
        return report

    import requests
    try:
        base = "http://localhost:9700" if check_port_listening(9700) else (
            "http://localhost:9701" if check_port_listening(9701) else "http://localhost:9702")
        stats = requests.get(f"{base}/api/stats", timeout=5).json()
        health = requests.get(f"{base}/api/health/scan", timeout=5).json()
    except Exception as e:
        report["checks"].append({"name": "引擎连接", "value": f"error: {str(e)[:60]}", "ok": False})
        return report

    n_total = stats.get("total_nodes", 0)
    n_active = stats.get("active_nodes", 0)
    n_edges = stats.get("total_edges", 0)
    orphan = health.get("orphan_pct", 0)
    zero_act = health.get("zero_activation_pct", 0)

    report["checks"].append({"name": "节点数", "value": str(n_total), "ok": n_total > 0,
                              "note": "新部署可能为 0, 属正常"})
    report["checks"].append({"name": "活跃节点", "value": f"{n_active} ({100*n_active/max(n_total,1):.0f}%)", "ok": True})
    report["checks"].append({"name": "边数", "value": str(n_edges), "ok": True})
    report["checks"].append({"name": "孤立节点占比", "value": f"{orphan:.1f}%", "ok": orphan < 70,
                              "note": "仅参考. 真实判定要用 llm_guided_health (LLM 语义判, 不看死指标)"})
    report["checks"].append({"name": "零激活占比", "value": f"{zero_act:.1f}%", "ok": True,
                              "note": "新部署高是正常, 用了一段时间后应降"})

    # Hash chain
    try:
        audit = requests.get(f"{base}/api/audit/verify", timeout=3).json()
        report["checks"].append({"name": "activity_log hash chain",
                                  "value": f"valid={audit.get('valid')} n={audit.get('n_entries')} breaks={len(audit.get('breaks',[]))}",
                                  "ok": bool(audit.get("valid"))})
    except Exception:
        pass

    return report


def measure_token_baseline(sample_query: str = "test query for baseline") -> dict:
    report = {"section": "4. Token 基线", "checks": []}
    if not (check_port_listening(9700) or check_port_listening(9701) or check_port_listening(9702)):
        report["checks"].append({"name": "引擎未起, 跳过", "value": "—", "ok": True})
        return report

    import requests
    base = "http://localhost:9700" if check_port_listening(9700) else (
        "http://localhost:9701" if check_port_listening(9701) else "http://localhost:9702")

    sizes = {}
    try:
        for mode in ["skeleton", "slim"]:
            r = requests.post(f"{base}/api/route",
                              json={"task": sample_query, "max_nodes": 5, "agent_id": "bootstrap-check", "mode": mode},
                              timeout=30)
            sizes[mode] = len(r.text.encode("utf-8"))
        # full
        r = requests.post(f"{base}/api/route",
                          json={"task": sample_query, "max_nodes": 5, "agent_id": "bootstrap-check"},
                          params={"detail": "true"}, timeout=30)
        sizes["full"] = len(r.text.encode("utf-8"))
    except Exception as e:
        report["checks"].append({"name": "route 测量失败", "value": str(e)[:80], "ok": False})
        return report

    for mode, b in sizes.items():
        report["checks"].append({"name": f"mode={mode} bytes", "value": str(b),
                                  "ok": True, "note": f"~{int(b/3.5)} tokens (估)"})
    if "skeleton" in sizes and "full" in sizes and sizes["full"] > 0:
        savings = 100 * (1 - sizes["skeleton"] / sizes["full"])
        report["checks"].append({"name": "skeleton vs full 节省", "value": f"{savings:.1f}%", "ok": savings > 50})

    return report


def render_report(sections: list[dict]) -> None:
    """人读报告."""
    print("\n" + "=" * 70)
    print(color("3CAN Bootstrap Check", "c"))
    print("=" * 70)
    for sec in sections:
        print(f"\n{color(sec['section'], 'b')}")
        for c in sec["checks"]:
            ok = c.get("ok", True)
            mark = color("OK ", "g") if ok else color("FAIL", "r")
            name = c["name"]
            val = c.get("value", "")
            note = c.get("note", "") or c.get("required", "")
            print(f"  [{mark}] {name:40s}  {val}")
            if not ok and note:
                print(f"          {color('→ ' + note, 'y')}")
            elif note and not ok:
                pass


def interactive_menu():
    """用户菜单: 互动式运行哪些工具."""
    print("\n" + "=" * 70)
    print(color("5. 互动菜单 (选你要做的)", "c"))
    print("=" * 70)
    options = [
        ("1", "启动 backend + proxy (如果还没起)",
         lambda: print("→ 运行: cd neural-memory/backend && python app.py --port 9701\n          cd neural-memory/proxy && python server.py")),
        ("2", "跑图谱健康扫描 (LLM 语义判, 不用死指标)",
         lambda: print("→ 运行: python tools/llm_guided_health.py --limit 20")),
        ("3", "跑 Leiden 社区检测 (提升 R@3)",
         lambda: print("→ 运行: python tools/leiden_community.py")),
        ("4", "跑 GDI 5 维资产打分",
         lambda: print("→ 运行: python tools/node_gdi_scorer.py")),
        ("5", "同步 SKILL.md 到 3CAN 节点",
         lambda: print("→ 运行: python tools/skill_sync.py")),
        ("6", "Session 聚合 (从 activity_log 自动生成 SES-auto-* 节点)",
         lambda: print("→ 运行: python tools/session_aggregator.py")),
        ("7", "物理归档 (status=archived 节点移到 graph/archive/)",
         lambda: print("→ 运行: python tools/archive_manager.py")),
        ("8", "跑内部 46-query benchmark",
         lambda: print("→ 运行: python benchmark/run_benchmark.py")),
        ("q", "退出", None),
    ]
    for key, label, _ in options:
        print(f"  [{key}] {label}")
    print()
    try:
        choice = input("选择 [1-8/q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    for key, label, fn in options:
        if key == choice and fn:
            print()
            fn()
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--non-interactive", action="store_true", help="只出报告不进菜单")
    ap.add_argument("--report-only", action="store_true", help="同 --non-interactive")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    sections = [
        check_environment(),
        check_components(),
        check_graph_health(),
        measure_token_baseline(),
    ]

    if args.json:
        print(json.dumps(sections, ensure_ascii=False, indent=2))
        return 0

    render_report(sections)

    # 汇总
    total = sum(len(s["checks"]) for s in sections)
    failed = sum(1 for s in sections for c in s["checks"] if not c.get("ok", True))
    print(f"\n{'=' * 70}")
    print(f"总计 {total} 检查, {color(str(failed)+' 失败', 'r' if failed else 'g')}")
    if failed:
        print(color("→ 修复失败项后重跑. 失败不阻塞使用, 但可能功能不完整.", "y"))

    if not (args.non_interactive or args.report_only):
        interactive_menu()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
