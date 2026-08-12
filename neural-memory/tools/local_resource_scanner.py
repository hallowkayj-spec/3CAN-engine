"""本地资源扫描器 — 将本机模型/下载/配置目录登记为 RES-* 节点.

扫描常见模型/下载/配置目录, 自动建 RES-* 节点到 3CAN.
下次 route "Gemma 本地路径" / "模型文件" / "下载文件" 直接命中.

触发: 手动或 cron 每周跑一次 (scripts/cron/weekly_resource_scan.sh)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

THREE_CAN = "http://localhost:9700"

HOME = Path.home()

# 扫描根目录清单. 可通过 THREECAN_RESOURCE_ROOTS_JSON 覆盖:
# [["/path/to/cache", "hf_hub", ["models--*", "datasets--*"]], ...]
SCAN_ROOTS = [
    (str(HOME / ".cache" / "huggingface" / "hub"), "hf_hub", ["models--*", "datasets--*"]),
    ("C:/ComfyUI-Git/models", "comfyui", ["checkpoints", "loras", "text_encoders", "clip_vision", "controlnet"]),
    (str(HOME / ".ollama" / "models"), "ollama", ["*"]),
    (str(HOME / ".lmstudio" / "models"), "lmstudio", ["*"]),
    (str(HOME / "AppData" / "Local" / "LM Studio" / "models"), "lmstudio_alt", ["*"]),
]

# 有意义的文件扩展名 (模型权重)
WEIGHT_EXTS = {".safetensors", ".bin", ".gguf", ".ckpt", ".pt", ".pth", ".onnx"}
MIN_SIZE_MB = 50  # 小于 50MB 的跳过 (非模型)


def _dir_info(d: Path) -> dict:
    """收集目录大小 + 文件清单."""
    total_bytes = 0
    file_count = 0
    big_files = []
    try:
        for root, _, files in os.walk(d):
            for f in files:
                p = Path(root) / f
                try:
                    sz = p.stat().st_size
                    total_bytes += sz
                    file_count += 1
                    if p.suffix.lower() in WEIGHT_EXTS and sz > MIN_SIZE_MB * 1024 * 1024:
                        big_files.append({"name": f, "size_mb": round(sz / 1048576, 1)})
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass
    return {
        "total_mb": round(total_bytes / 1048576, 1),
        "file_count": file_count,
        "weight_files": big_files[:10],  # top 10
    }


def scan_roots() -> list[dict]:
    """扫所有根目录, 返回每个子模块的资源记录."""
    resources = []
    scan_roots = SCAN_ROOTS
    if os.environ.get("THREECAN_RESOURCE_ROOTS_JSON"):
        scan_roots = json.loads(os.environ["THREECAN_RESOURCE_ROOTS_JSON"])
    for root_path, source, patterns in scan_roots:
        root = Path(root_path)
        if not root.exists():
            continue
        for pattern in patterns:
            if pattern == "*":
                subdirs = [p for p in root.iterdir() if p.is_dir()]
            else:
                subdirs = list(root.glob(pattern))
            for sd in subdirs:
                if not sd.is_dir():
                    continue
                info = _dir_info(sd)
                if info["file_count"] == 0:
                    continue
                # 只记有实际模型权重 (或显著大小) 的目录
                if not info["weight_files"] and info["total_mb"] < 20:
                    continue
                resources.append({
                    "source": source,
                    "path": str(sd),
                    "rel_name": sd.name,
                    "total_mb": info["total_mb"],
                    "weight_files": info["weight_files"],
                    "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
    return resources


def _node_id(source: str, name: str) -> str:
    """Stable node id from source + name."""
    safe = name.replace("/", "_").replace("\\", "_").replace(" ", "-")[:40]
    h = hashlib.md5(f"{source}:{name}".encode()).hexdigest()[:6]
    return f"RES-{source}-{safe}-{h}"


def writeback_res_nodes(resources: list[dict]) -> dict:
    """为每个 resource 建/更新 RES-* 节点."""
    created, updated, errors = 0, 0, 0
    for r in resources:
        nid = _node_id(r["source"], r["rel_name"])
        # 确认节点是否存在
        existing = requests.get(f"{THREE_CAN}/api/nodes/{nid}", timeout=10)
        name = f"本地资源 {r['source']}/{r['rel_name']} ({r['total_mb']} MB)"
        keywords = [
            r["rel_name"],
            r["source"],
            "本地模型",
            "local model",
            "下载路径",
            "download path",
            f"{r['source']} 路径",
            str(r["total_mb"]) + "MB",
        ]
        # 添加 weight file names 作 keywords
        for wf in r["weight_files"][:5]:
            keywords.append(wf["name"])

        description = f"本地资源扫描 {r['scanned_at']}\n路径: {r['path']}\n总大小: {r['total_mb']} MB, {len(r['weight_files'])} 权重文件"
        if r["weight_files"]:
            description += "\n主要文件:\n" + "\n".join(
                f"- {wf['name']} ({wf['size_mb']} MB)" for wf in r["weight_files"][:5]
            )

        node_payload = {
            "id": nid,
            "name": name[:95],
            "cluster": "工具链",
            "type": "reference",
            "primary_author": "resource-scanner",
            "activation_keywords": keywords,
            "content": {
                "description": description[:1500],
                "current_state": f"最近扫 {r['scanned_at']}, {r['total_mb']}MB",
            },
        }
        try:
            if existing.status_code == 200:
                upd = requests.put(f"{THREE_CAN}/api/nodes/{nid}", json={
                    "activation_keywords": keywords,
                }, timeout=10)
                if upd.status_code == 200:
                    updated += 1
            else:
                # create with force=true (绕开未来的 R1 guard, 这类是系统性扫描)
                cr = requests.post(f"{THREE_CAN}/api/nodes?force=true", json=node_payload, timeout=15)
                if cr.status_code in (200, 201):
                    created += 1
                else:
                    # Fallback: 无 force 也试
                    cr2 = requests.post(f"{THREE_CAN}/api/nodes", json=node_payload, timeout=15)
                    if cr2.status_code in (200, 201):
                        created += 1
                    else:
                        errors += 1
                        print(f"  ERROR create {nid}: {cr.status_code} {cr.text[:100]}")
        except Exception as e:
            errors += 1
            print(f"  EXCEPTION {nid}: {e}")

    return {"created": created, "updated": updated, "errors": errors, "total": len(resources)}


if __name__ == "__main__":
    print("Scanning local resources...")
    resources = scan_roots()
    print(f"Found {len(resources)} resource dirs with models")
    for r in resources[:10]:
        print(f"  {r['source']:12s} {r['rel_name']:45s} {r['total_mb']:8.1f}MB  ({len(r['weight_files'])} weights)")

    if len(sys.argv) > 1 and sys.argv[1] == "--dry":
        sys.exit(0)

    result = writeback_res_nodes(resources)
    print(f"\nDone: created={result['created']} updated={result['updated']} errors={result['errors']}")
