"""3CAN Observer LLM Analyzer — S66d Wave 1 C 收尾

作用:
- 读 UserPromptSubmit hook 落的 prompt_log.jsonl (the maintainer 纠错 / 新概念)
- async DeepSeek 深析每条 → 生成 PROPOSED-* 节点 (status=dormant)
- R1 先查再建: route 查 top1 ≥ 0.045 则跳过, 不污染
- checkpoint 保存处理到第几行, 幂等增量

运行:
  python tools/observer_llm_analyzer.py          # 处理新增行
  python tools/observer_llm_analyzer.py --rescan # 忽略 checkpoint 全量

输出:
- PROPOSED-corr-{ts-slug} / PROPOSED-concept-{slug}  status=dormant
- the maintainer 审阅后: PUT /api/nodes/{id} 改 status=active + 改 id (去 PROPOSED- 前缀)
- data/_observer/analyzer_run.jsonl 记录每次运行 diff
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "_observer"
LOG_PATH = DATA_DIR / "prompt_log.jsonl"
CHECKPOINT = DATA_DIR / "analyzer_checkpoint.txt"
RUN_LOG = DATA_DIR / "analyzer_run.jsonl"

THREE_CAN = "http://localhost:9700"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

DUP_THRESHOLD = 0.045   # R1 防冗余阈值 (与 backend/app.py:166 一致)
MAX_CONCURRENT = 4
PER_CALL_TIMEOUT = 40


# ──────── 密钥 ────────

def _load_deepseek_key() -> str | None:
    secrets = Path.home() / ".claude" / "secrets.json"
    if not secrets.exists():
        return None
    try:
        s = json.loads(secrets.read_text(encoding="utf-8"))
        return (s.get("deepseek") or {}).get("api_key")
    except Exception:
        return None


# ──────── DeepSeek prompts ────────

PROMPT_CORRECTION = """你分析一条 the maintainer (项目 owner) 对 Claude 主脑的纠错, 判断是否值得记作 FEE (feedback 规则) 或 ERR (错误教训) 节点.

原始 prompt:
\"\"\"
{prompt}
\"\"\"

输出严格 JSON (无解释文字):
{{
  "is_rule_worthy": true/false,
  "node_type": "feedback" | "knowledge" | "decision" | "none",
  "rule": "≤50字的规则表述 (祈使句, e.g. '建节点前必 route')",
  "why": "≤80字, the maintainer 给的原因 (引用或推断)",
  "how_to_apply": "≤80字, 何时/何地套用",
  "activation_keywords": ["8-12 个中英双份关键词, 高区分度"],
  "proposed_id_slug": "16-char kebab-case 英文 slug",
  "priority": "high" | "medium" | "low"
}}

判断:
- is_rule_worthy=false 当: 情绪化抱怨 / 一次性打断 / 已有类似规则
- node_type=feedback 当: the maintainer 立新规则 ("以后要X / 别再Y")
- node_type=knowledge 当: the maintainer 讲事实/概念纠偏
- node_type=decision 当: the maintainer 做架构/方向决定
"""

PROMPT_CONCEPT = """你判断一个疑似"我训练 cutoff (2026-01) 后的新概念"是否值得建 REF (reference) 节点.

概念: {concept}
the maintainer 说的原文片段 (≤200字): {context}

输出严格 JSON:
{{
  "is_worth_tracking": true/false,
  "concept_kind": "product" | "tool" | "model" | "paper" | "person" | "other",
  "description": "≤100字, 这是什么 (基于你已知 + the maintainer 上下文)",
  "needs_web_search": true/false,
  "activation_keywords": ["8-12 个"],
  "proposed_id_slug": "16-char slug",
  "confidence": 0.0-1.0
}}

判断:
- is_worth_tracking=false 当: 是常见词/已收录产品/不确定是否新
- needs_web_search=true 当: 你 confidence < 0.6
"""


# ──────── 3CAN 调用 ────────

def _route_dup_check(task: str, agent_id: str = "observer-analyzer") -> float:
    """R1 查重: 返回 top1 score (0 if 无匹配)."""
    try:
        r = requests.post(
            f"{THREE_CAN}/api/route",
            json={"task": task, "max_nodes": 3, "agent_id": agent_id, "mode": "skeleton"},
            timeout=10,
        )
        if r.status_code != 200:
            return 0.0
        scores = r.json().get("scores", {}) or {}
        return max(scores.values()) if scores else 0.0
    except Exception:
        return 0.0


def _create_proposed_node(node_id: str, name: str, ntype: str, desc: str,
                          kws: list[str], priority: str,
                          source_prompt: str, source_ts: str,
                          extra_notes: str = "") -> dict:
    """POST /api/nodes force=true (跳过 proxy 的 R1, 因已自查). status=dormant 待审."""
    body = {
        "id": node_id,
        "name": name,
        "cluster": "项目文档",
        "type": ntype,
        "status": "dormant",
        "content": {
            "description": desc,
            "current_state": f"PROPOSED by DeepSeek {time.strftime('%Y-%m-%d')}, 待 the maintainer 审",
            "notes": f"source_ts={source_ts}\nsource_prompt_preview={source_prompt[:300]}\n{extra_notes}",
        },
        "activation_keywords": kws,
        "priority": priority,
        "primary_author": "observer-analyzer",
    }
    try:
        r = requests.post(f"{THREE_CAN}/api/nodes?force=true", json=body, timeout=15)
        return {"ok": r.status_code in (200, 201), "status": r.status_code, "body": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ──────── DeepSeek 调用 ────────

def _deepseek_call(api_key: str, prompt: str) -> dict | None:
    try:
        r = requests.post(
            DEEPSEEK_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_tokens": 800,
            },
            timeout=PER_CALL_TIMEOUT,
        )
        if r.status_code != 200:
            return {"_error": f"http {r.status_code}: {r.text[:200]}"}
        txt = r.json()["choices"][0]["message"]["content"]
        return json.loads(txt)
    except Exception as e:
        return {"_error": str(e)[:200]}


# ──────── 单条处理 ────────

def _slug(text: str, n: int = 16) -> str:
    """生成 URL-safe 短 slug."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not base:
        base = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    return base[:n].strip("-") or "x"


def process_correction(entry: dict, api_key: str) -> dict:
    prompt_text = entry.get("prompt", "")
    ts = entry.get("ts", "")
    analysis = _deepseek_call(api_key, PROMPT_CORRECTION.format(prompt=prompt_text[:1500]))
    if not analysis or analysis.get("_error"):
        return {"kind": "correction", "ts": ts, "skip": "deepseek_fail", "error": (analysis or {}).get("_error")}
    if not analysis.get("is_rule_worthy"):
        return {"kind": "correction", "ts": ts, "skip": "not_rule_worthy"}

    rule = analysis.get("rule", "")[:80]
    ntype = analysis.get("node_type", "feedback")
    if ntype not in ("feedback", "knowledge", "decision"):
        ntype = "feedback"

    # R1 查重
    dup_score = _route_dup_check(rule)
    if dup_score >= DUP_THRESHOLD:
        return {"kind": "correction", "ts": ts, "skip": f"dup_score={dup_score:.3f}", "rule": rule}

    slug = analysis.get("proposed_id_slug") or _slug(rule)
    prefix = {"feedback": "FEE", "knowledge": "DOC", "decision": "DEC"}[ntype]
    node_id = f"PROPOSED-{prefix}-{slug}-{ts[:10].replace('-', '')}"
    desc = f"{rule}\n\n**Why:** {analysis.get('why', '')}\n**How to apply:** {analysis.get('how_to_apply', '')}"
    kws = [k for k in (analysis.get("activation_keywords") or []) if isinstance(k, str)][:15]
    if not kws:
        kws = [rule[:15], "proposed review"]

    result = _create_proposed_node(
        node_id=node_id,
        name=f"[PROPOSED] {rule[:60]}",
        ntype=ntype,
        desc=desc,
        kws=kws + ["pending review", "observer proposed"],
        priority=analysis.get("priority", "medium"),
        source_prompt=prompt_text,
        source_ts=ts,
    )
    return {"kind": "correction", "ts": ts, "node_id": node_id, "rule": rule,
            "dup_score": dup_score, "created": result.get("ok"), "err": result.get("body") if not result.get("ok") else None}


def process_concept(concept: str, entry: dict, api_key: str) -> dict:
    ts = entry.get("ts", "")
    context = (entry.get("prompt", "") or "")[:400]
    analysis = _deepseek_call(api_key, PROMPT_CONCEPT.format(concept=concept, context=context))
    if not analysis or analysis.get("_error"):
        return {"kind": "concept", "ts": ts, "concept": concept, "skip": "deepseek_fail"}
    if not analysis.get("is_worth_tracking"):
        return {"kind": "concept", "ts": ts, "concept": concept, "skip": "not_worth"}

    dup_score = _route_dup_check(concept)
    if dup_score >= DUP_THRESHOLD:
        return {"kind": "concept", "ts": ts, "concept": concept, "skip": f"dup_score={dup_score:.3f}"}

    slug = analysis.get("proposed_id_slug") or _slug(concept)
    node_id = f"PROPOSED-REF-{slug}-{ts[:10].replace('-', '')}"
    desc = analysis.get("description", "")[:400]
    needs_web = analysis.get("needs_web_search", False)
    confidence = analysis.get("confidence", 0.0)
    kws = [k for k in (analysis.get("activation_keywords") or []) if isinstance(k, str)][:15]
    if concept not in kws:
        kws.insert(0, concept)

    extra = f"concept_kind={analysis.get('concept_kind','other')}\nconfidence={confidence}\nneeds_web_search={needs_web}"
    result = _create_proposed_node(
        node_id=node_id,
        name=f"[PROPOSED] {concept}",
        ntype="reference",
        desc=desc,
        kws=kws + ["pending review", "observer proposed"],
        priority="low",
        source_prompt=context,
        source_ts=ts,
        extra_notes=extra,
    )
    return {"kind": "concept", "ts": ts, "concept": concept, "node_id": node_id,
            "confidence": confidence, "needs_web_search": needs_web,
            "created": result.get("ok"), "err": result.get("body") if not result.get("ok") else None}


# ──────── 主流程 ────────

def _read_checkpoint() -> int:
    if not CHECKPOINT.exists():
        return 0
    try:
        return int(CHECKPOINT.read_text().strip())
    except Exception:
        return 0


def _write_checkpoint(n: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(str(n), encoding="utf-8")


def _append_run_log(entries: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rescan", action="store_true", help="忽略 checkpoint 全量重跑")
    parser.add_argument("--limit", type=int, default=50, help="单次最多处理多少行")
    parser.add_argument("--dry-run", action="store_true", help="只 DeepSeek 分析, 不建节点")
    args = parser.parse_args()

    if not LOG_PATH.exists():
        print(f"[observer-analyzer] 无日志 {LOG_PATH}, 跳过")
        return 0

    api_key = _load_deepseek_key()
    if not api_key:
        print("[observer-analyzer] FATAL: DeepSeek key 未找到 (~/.claude/secrets.json)")
        return 2

    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    start = 0 if args.rescan else _read_checkpoint()
    pending = lines[start:start + args.limit]
    if not pending:
        print(f"[observer-analyzer] no new entries (checkpoint={start}, total={len(lines)})")
        return 0

    print(f"[observer-analyzer] processing {len(pending)} entries from line {start}")

    # 展平为任务列表: correction + concepts 每条独立 DeepSeek call
    tasks: list[tuple[str, dict, str | None]] = []
    for raw in pending:
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if entry.get("correction"):
            tasks.append(("correction", entry, None))
        for c in (entry.get("unknown") or []):
            tasks.append(("concept", entry, c))

    results: list[dict] = []
    if args.dry_run:
        print(f"[observer-analyzer] dry-run: 将处理 {len(tasks)} tasks (correction + concept)")
    else:
        with cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
            futs = []
            for kind, entry, concept in tasks:
                if kind == "correction":
                    futs.append(ex.submit(process_correction, entry, api_key))
                else:
                    futs.append(ex.submit(process_concept, concept, entry, api_key))
            for fut in cf.as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    results.append({"error": str(e)[:200]})

        _append_run_log(results)

    _write_checkpoint(start + len(pending))

    created = sum(1 for r in results if r.get("created"))
    skipped = sum(1 for r in results if r.get("skip"))
    print(f"[observer-analyzer] done: processed={len(tasks)} created={created} skipped={skipped} failed={len(results) - created - skipped}")
    for r in results:
        if r.get("created"):
            print(f"  + {r.get('node_id')}  {r.get('rule') or r.get('concept','')}")
        elif r.get("skip"):
            print(f"  - skip {r.get('kind')} ts={r.get('ts','')[:10]}: {r.get('skip')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
