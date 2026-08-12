"""3CAN Short-Code Curator — S66d Wave 2

问题: benchmark short-code query 失败案例都是"目标节点没打此短码 kw".
例: query 'E6' 期望 MEM-roundE-train, 但该节点 kws 只有 'roundE'/'Round E', 没 'E6'.

任务: 对 code_index 里 ambiguous (多候选) 的短码, LLM 裁哪个节点最配, 提议补该 kw.

流程:
1. 扫 code_index 找 ambiguous (≥2 候选) 短码
2. 每个短码: 把候选节点 name/desc/kws 喂 DeepSeek
3. LLM 排序: 主节点 / 次节点 / 无关节点
4. 输出 PROPOSED: (短码, 主节点, 建议补 kw) → data/_short_code_proposals.json
5. the maintainer 审 + apply 脚本 PUT /api/nodes 补 kw

纯只读, 不直改节点.

运行:
  python tools/short_code_curator.py --limit 20  # 审前 20 个歧义短码
  python tools/short_code_curator.py --apply --dry # dry-run 应用
  python tools/short_code_curator.py --apply       # 真改
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
OUT_DIR = ROOT / "data" / "_short_code"
PROPOSAL = OUT_DIR / "short_code_proposals.json"

THREE_CAN = "http://localhost:9700"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

CODE_PATTERN = re.compile(r"\b([A-Z]{1,5}\d{1,4}[a-z]?)\b")
MAX_CONCURRENT = 4
PER_CALL_TIMEOUT = 30

PROMPT = """你是 3CAN 图谱的短代码消歧助手.

一个短代码 '{code}' 在以下 {n} 个节点的文本里出现过, 但应该只属于"最配"的 1-2 个节点的
activation_keywords. 判断哪些节点该补, 哪些只是偶然提到.

候选节点:
{candidates}

输出严格 JSON:
{{
  "primary_node_id": "最配那个节点的 ID (必填, 若都不配填 'none')",
  "secondary_node_ids": ["次配节点 (≤2 个), 也值得补 kw"],
  "mention_only_ids": ["只是偶然提到, 不该补"],
  "confidence": 0.0-1.0,
  "reason": "≤30字为什么选 primary"
}}

判断规则:
- primary = 短代码是其**核心标识** (ID 含此码 / name 标此码 / 整个节点都讲此码)
- secondary = 紧密相关的兄弟节点 (e.g. session 配 handoff)
- mention_only = 只是文中顺带提 (e.g. FEE 讲"S62 搞错了", 这不是 S62 的主节点)
"""


def _load_key() -> str | None:
    p = Path.home() / ".claude" / "secrets.json"
    if not p.exists():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("deepseek") or {}).get("api_key")
    except Exception:
        return None


def load_nodes() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in NODES_DIR.glob("*.json")}


def build_code_index(nodes_by_id: dict[str, dict]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = defaultdict(list)
    for nid, n in nodes_by_id.items():
        if n.get("status") == "dormant":
            continue
        c = n.get("content", {}) or {}
        text = f"{nid} {n.get('name','')} {c.get('description','') or ''} {' '.join(n.get('activation_keywords',[]))}"
        for code in CODE_PATTERN.findall(text.upper()):
            if nid not in idx[code]:
                idx[code].append(nid)
    return idx


def has_code_in_kws(node: dict, code: str) -> bool:
    """判断节点的 activation_keywords 是否已含此 code (不区分大小写)."""
    code_upper = code.upper()
    for kw in node.get("activation_keywords", []):
        if isinstance(kw, str) and kw.upper().strip() == code_upper:
            return True
    return False


def deepseek_call(api_key: str, prompt: str) -> dict:
    try:
        r = requests.post(
            DEEPSEEK_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 300,
            },
            timeout=PER_CALL_TIMEOUT,
        )
        if r.status_code != 200:
            return {"_error": f"http {r.status_code}: {r.text[:200]}"}
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        return {"_error": str(e)[:120]}


def curate_code(code: str, nids: list[str], nodes_by_id: dict[str, dict], api_key: str) -> dict:
    """对一个歧义短码: LLM 排出 primary/secondary/mention."""
    # 候选节点摘要
    desc_lines = []
    for nid in nids[:10]:  # 最多 10 候选 (避免 prompt 过长)
        n = nodes_by_id.get(nid)
        if not n:
            continue
        c = n.get("content", {}) or {}
        desc_lines.append(
            f"- {nid}\n"
            f"  name: {(n.get('name','') or '')[:80]}\n"
            f"  type/cluster: {n.get('type','')}/{n.get('cluster','')}\n"
            f"  desc: {(c.get('description','') or '')[:150]}\n"
            f"  kws: {n.get('activation_keywords', [])[:6]}"
        )
    prompt = PROMPT.format(code=code, n=len(nids), candidates="\n".join(desc_lines))

    ans = deepseek_call(api_key, prompt)
    if ans.get("_error"):
        return {"code": code, "candidates": nids, "error": ans["_error"]}

    primary = ans.get("primary_node_id", "none")
    secondary = ans.get("secondary_node_ids") or []
    confidence = ans.get("confidence", 0.0)

    # 计算建议补的 (primary + secondary) 中 kws 还没含此 code 的
    to_add = []
    for nid in [primary] + secondary:
        if nid == "none" or nid not in nodes_by_id:
            continue
        if not has_code_in_kws(nodes_by_id[nid], code):
            to_add.append(nid)

    return {
        "code": code,
        "candidates": nids,
        "primary": primary,
        "secondary": secondary,
        "mention_only": ans.get("mention_only_ids", []),
        "confidence": confidence,
        "reason": ans.get("reason", ""),
        "to_add_code_to_kws": to_add,  # 真正可执行的操作列表
    }


def apply_proposals(proposals: list[dict], dry_run: bool = True) -> dict:
    stats = {"kws_added": 0, "nodes_updated": 0, "errors": []}
    updated = set()
    for p in proposals:
        code = p["code"]
        for nid in p.get("to_add_code_to_kws", []):
            if dry_run:
                print(f"  [DRY] add kw='{code}' to {nid} (confidence={p.get('confidence',0)})")
                stats["kws_added"] += 1
                updated.add(nid)
                continue
            try:
                r = requests.get(f"{THREE_CAN}/api/nodes/{nid}", timeout=10)
                if r.status_code != 200:
                    stats["errors"].append(f"{nid}: fetch http {r.status_code}")
                    continue
                node = r.json()
                kws = list(node.get("activation_keywords", []))
                if code.upper() in {k.upper().strip() for k in kws if isinstance(k, str)}:
                    continue
                kws.append(code)
                put = requests.put(f"{THREE_CAN}/api/nodes/{nid}",
                                   json={"activation_keywords": kws}, timeout=15)
                if put.status_code in (200, 201):
                    stats["kws_added"] += 1
                    updated.add(nid)
                else:
                    stats["errors"].append(f"{nid}: put http {put.status_code} {put.text[:100]}")
            except Exception as e:
                stats["errors"].append(f"{nid}: {str(e)[:80]}")
    stats["nodes_updated"] = len(updated)
    return stats


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--min-candidates", type=int, default=2, help="只审 ≥N 候选的歧义短码")
    p.add_argument("--max-candidates", type=int, default=10)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry", action="store_true", help="和 --apply 一起, dry-run")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.apply:
        if not PROPOSAL.exists():
            print(f"[sc-curator] 无 proposal {PROPOSAL}")
            return 1
        data = json.loads(PROPOSAL.read_text(encoding="utf-8"))
        stats = apply_proposals(data.get("proposals", []), dry_run=args.dry)
        print(f"[sc-curator] apply: {stats}")
        return 0

    api_key = _load_key()
    if not api_key:
        print("[sc-curator] FATAL: no DeepSeek key")
        return 2

    print("[sc-curator] loading nodes + building code_index...")
    nodes_by_id = load_nodes()
    idx = build_code_index(nodes_by_id)

    # 歧义短码: ≥2 候选 且 ≤max-candidates
    ambiguous = [(c, nids) for c, nids in idx.items()
                 if args.min_candidates <= len(nids) <= args.max_candidates]
    ambiguous.sort(key=lambda x: -len(x[1]))
    ambiguous = ambiguous[:args.limit]

    print(f"[sc-curator] 待审 {len(ambiguous)} 歧义短码 (min={args.min_candidates} max={args.max_candidates})")

    proposals: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
        futs = {ex.submit(curate_code, c, nids, nodes_by_id, api_key): c
                for c, nids in ambiguous}
        for fut in cf.as_completed(futs):
            try:
                proposals.append(fut.result())
            except Exception as e:
                proposals.append({"error": str(e)[:100]})

    # 统计
    ok = [p for p in proposals if not p.get("error") and p.get("primary") != "none"]
    to_add_total = sum(len(p.get("to_add_code_to_kws", [])) for p in ok)
    high_conf = [p for p in ok if p.get("confidence", 0) >= 0.7]

    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_audited": len(ambiguous),
        "successful": len(ok),
        "high_confidence": len(high_conf),
        "total_kws_to_add": to_add_total,
        "proposals": proposals,
    }
    PROPOSAL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[sc-curator] 写入 {PROPOSAL}")
    print(f"[sc-curator] 摘要: {len(ok)}/{len(ambiguous)} 成功, {len(high_conf)} 高置信, 建议补 {to_add_total} kw")
    for p in sorted(high_conf, key=lambda x: -x["confidence"])[:10]:
        tgt = p.get("to_add_code_to_kws") or []
        print(f"  - {p['code']:8s} → primary={p.get('primary','')[:40]}  conf={p.get('confidence',0):.2f}  add_to={len(tgt)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
