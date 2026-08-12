#!/usr/bin/env python3
"""R18 瘦身执行脚本 — Phase 1-3 一键执行。

Phase 1: RUL 21个 → dormant (rules已context自动注入, 节点冗余)
Phase 2: MEM activation<3 → dormant (pre-3CAN旧快照, 保留14个活跃)
Phase 3: DOC+HO 129同源对 → merge DOC入HO (auto_bootstrap双份冗余)

前置: 引擎必须在线 (localhost:9700)
安全: 永不删除, 只改status / merge. the maintainer随时可通过 /api/nodes/{id} 召回.
"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path
import requests

BASE = "http://localhost:9700"
APPROVER = os.environ.get("THREECAN_MAINTENANCE_APPROVER", "3can-maintainer")
PAYLOAD_DIR = Path(os.environ.get("THREECAN_MAINTENANCE_PAYLOAD_DIR", tempfile.gettempdir()))


def _load_payload(filename: str):
    path = PAYLOAD_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"maintenance payload missing: {path}. Set THREECAN_MAINTENANCE_PAYLOAD_DIR "
            "or pass a directory containing phase1_rul.json/phase2_mem.json/phase3_merge.json."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def check_engine():
    try:
        r = requests.get(f"{BASE}/api/stats", timeout=3)
        stats = r.json()
        print(f"引擎在线: {stats['total_nodes']}节点 {stats['total_edges']}边")
        return True
    except Exception as e:
        print(f"引擎离线: {e}")
        return False


def phase1_rul_dormant():
    """RUL 21个 → dormant"""
    payload = _load_payload("phase1_rul.json")
    print(f"\n=== Phase 1: RUL {len(payload['node_ids'])}个 → dormant ===")
    r = requests.post(f"{BASE}/api/nodes/batch-dormant", json=payload, timeout=30)
    result = r.json()
    print(f"  结果: {result.get('dormant_count', 0)}个已dormant")
    return result


def phase2_mem_dormant():
    """MEM activation<3 → dormant"""
    payload = _load_payload("phase2_mem.json")
    print(f"\n=== Phase 2: MEM {len(payload['node_ids'])}个 → dormant ===")
    r = requests.post(f"{BASE}/api/nodes/batch-dormant", json=payload, timeout=30)
    result = r.json()
    print(f"  结果: {result.get('dormant_count', 0)}个已dormant")
    return result


def phase3_doc_ho_merge():
    """DOC+HO 129对 → merge DOC入HO"""
    pairs = _load_payload("phase3_merge.json")
    print(f"\n=== Phase 3: DOC+HO {len(pairs)}对 → merge ===")
    success = 0
    errors = []
    for i, pair in enumerate(pairs):
        try:
            r = requests.post(f"{BASE}/api/nodes/merge", json={
                "keep_id": pair["keep"],
                "remove_id": pair["remove"],
                "approver": APPROVER,
            }, timeout=10)
            if r.status_code == 200:
                success += 1
            else:
                errors.append(f"{pair['remove']}: {r.status_code}")
        except Exception as e:
            errors.append(f"{pair['remove']}: {e}")

        if (i + 1) % 20 == 0:
            print(f"  进度: {i+1}/{len(pairs)}, 成功={success}")

    print(f"  结果: {success}/{len(pairs)} 成功merge")
    if errors:
        print(f"  错误: {errors[:5]}")
    return {"merged": success, "errors": len(errors)}


def final_stats():
    r = requests.get(f"{BASE}/api/stats", timeout=3)
    stats = r.json()
    print("\n=== 终态 ===")
    print(f"  总节点: {stats['total_nodes']} (active={stats['active_nodes']})")
    print(f"  总边: {stats['total_edges']}")

    # Run health scan
    try:
        r = requests.get(f"{BASE}/api/health/scan", timeout=60)
        health = r.json()
        print(f"  health_score: {health.get('health_score', 'N/A')}")
        print(f"  孤节点: {health.get('orphan_nodes', '?')} ({health.get('orphan_pct', '?')}%)")
        print(f"  零激活: {health.get('zero_activation', '?')} ({health.get('zero_activation_pct', '?')}%)")
        for alert in health.get("alerts", []):
            print(f"  ⚠️ {alert}")
    except Exception:
        print("  (health scan跳过)")


if __name__ == "__main__":
    if not check_engine():
        print("请先启动引擎再运行本脚本")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("\n[DRY RUN] 只展示将执行的操作:")
        p1 = _load_payload("phase1_rul.json")
        p2 = _load_payload("phase2_mem.json")
        p3 = _load_payload("phase3_merge.json")
        print(f"  Phase 1: {len(p1['node_ids'])} RUL → dormant")
        print(f"  Phase 2: {len(p2['node_ids'])} MEM → dormant")
        print(f"  Phase 3: {len(p3)} DOC+HO → merge")
        print(f"  预估: 1249 → ~{1249 - len(p1['node_ids']) - len(p2['node_ids']) - len(p3)} active")
        sys.exit(0)

    print("\n开始执行 Phase 1-3 (永不删除, 只改status/merge)...")
    input("按 Enter 继续, Ctrl+C 取消...")

    t0 = time.time()
    phase1_rul_dormant()
    phase2_mem_dormant()
    phase3_doc_ho_merge()
    elapsed = time.time() - t0

    print(f"\n总耗时: {elapsed:.1f}s")
    final_stats()
