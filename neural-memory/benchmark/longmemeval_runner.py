"""3CAN vs LongMemEval — 行业公认 benchmark 诚实打分

参考: Wu et al. 2024, LongMemEval arxiv 2410.10813 (MIT).
协议: 每题给一个 haystack (多 session 历史对话), agent 用记忆答单问题.
打分: LLM-as-judge 比对 generated answer vs ground truth (pass/fail 二元).
计算: accuracy 整体 + 按 5 类 question_type (single-session / multi-session / temporal / knowledge-update).

3CAN 配置:
- 隔离实例 (port 9799, 独立 graph dir, 不污染主库)
- 每题: ingest haystack turns → route(question) 取 top-K → DeepSeek 答 → DeepSeek 判
- 公平性: retrieval = 3CAN; answer = DeepSeek-V3; judge = DeepSeek-V3 (自判有偏, 但 Mem0/Letta 评估也允许这样)

参考分 (官方论文 2024 数据, GPT-4o 作判):
- GPT-4o full context:  ~74%
- GPT-4o + Mem0:        ~57%
- GPT-4o + Letta:       ~53%
- BM25-only retrieval:  ~45%

运行:
  python benchmark/longmemeval_runner.py --limit 20 --port 9799
  python benchmark/longmemeval_runner.py --full --port 9799  # 全部 500 题, ~4h
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(os.environ.get("LONGMEMEVAL_ORACLE", Path(tempfile.gettempdir()) / "lme_hf" / "longmemeval_oracle"))
BACKEND_PY = ROOT / "backend" / "app.py"
OUT_DIR = ROOT / "benchmark" / "_longmemeval"

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
MAX_CONTEXT_TURNS = 10   # 喂给 answer LLM 的 top-K
INGEST_BATCH = 4         # 同时 POST /api/nodes 并发 (create_node 走 bisect insert, 高并发会竞争)
DEEPSEEK_CONCURRENT = 4


ANSWER_PROMPT = """You are an assistant with access to the user's prior conversations.
Use the retrieved memory below to answer the question. If memory is insufficient, say "I don't know".

Retrieved memory (top-{k} from 3CAN route, most relevant first):
{context}

Question ({ts}): {q}

Answer concisely in 1-2 sentences (match the style of the original conversation):"""


JUDGE_PROMPT = """Compare a model-generated answer to the ground-truth answer. Decide if the model answer is CORRECT.

Question: {q}
Ground truth: {gt}
Model answer: {pred}

Rules:
- CORRECT if the model answer is semantically equivalent to ground truth (substring / paraphrase / same fact).
- INCORRECT if it says "I don't know", hallucinates, or gives wrong specifics.
- Partial credit is not allowed — binary verdict only.

Output strict JSON:
{{"verdict": "correct" | "incorrect", "reason": "≤20 words"}}"""


# ──────── DeepSeek ────────

def _load_key() -> str | None:
    p = Path.home() / ".claude" / "secrets.json"
    if not p.exists():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("deepseek") or {}).get("api_key")
    except Exception:
        return None


def deepseek_call(api_key: str, prompt: str, want_json: bool = False, max_tokens: int = 200) -> dict | str | None:
    try:
        body = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        if want_json:
            body["response_format"] = {"type": "json_object"}
        r = requests.post(
            DEEPSEEK_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body, timeout=60,
        )
        if r.status_code != 200:
            return {"_error": f"http {r.status_code}"} if want_json else None
        txt = r.json()["choices"][0]["message"]["content"]
        return json.loads(txt) if want_json else txt.strip()
    except Exception as e:
        return {"_error": str(e)[:100]} if want_json else None


# ──────── Isolated 3CAN ────────

def spawn_isolated_backend(port: int, graph_dir: Path, venv_py: str, log_file: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["THREECAN_GRAPH_DIR"] = str(graph_dir)
    f = open(log_file, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [venv_py, "-u", str(BACKEND_PY), "--port", str(port)],
        cwd=str(BACKEND_PY.parent),
        env=env,
        stdout=f, stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        try:
            r = requests.get(f"http://localhost:{port}/api/stats", timeout=2)
            if r.status_code == 200:
                return proc
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"backend 启动失败 port={port}, log={log_file}")


def wait_for_node_count(port: int, expected: int, timeout_s: int = 60) -> bool:
    """轮询 /api/stats 直到 total_nodes == expected (reload 是异步的)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            r = requests.get(f"http://localhost:{port}/api/stats", timeout=3)
            if r.status_code == 200 and r.json().get("total_nodes") == expected:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def reset_graph(graph_dir: Path):
    """清空节点/边, 保留 agents.json."""
    nodes_dir = graph_dir / "nodes"
    if nodes_dir.exists():
        shutil.rmtree(nodes_dir)
    nodes_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    emb = graph_dir / "embeddings.npz"
    if emb.exists():
        emb.unlink()


def reload_backend(port: int):
    r = requests.post(f"http://localhost:{port}/api/reload", timeout=30)
    return r.status_code == 200


# ──────── 数据转换 ────────

def turn_to_node(conv_id: str, sess_idx: int, turn_idx: int, turn: dict, sess_date: str) -> dict:
    """LongMemEval 的一条 turn → 3CAN node."""
    role = turn.get("role", "user")
    content = turn.get("content", "")[:800]
    nid = f"LME-{conv_id}-s{sess_idx}t{turn_idx}"
    return {
        "id": nid,
        "name": f"[{role}] {content[:50]}",
        "cluster": "longmemeval",
        "type": "reference",
        "content": {
            "description": content,
            "current_state": f"session={sess_idx} turn={turn_idx} role={role} date={sess_date}",
            "notes": f"session_date={sess_date}",
        },
        "activation_keywords": [role, f"session-{sess_idx}", sess_date[:10]] + _extract_kws(content),
        "priority": "medium",
        "primary_author": "longmemeval-runner",
    }


_STOPWORDS = set("a an the is are was were be been being have has had do does did will would could should may might must can to of in on at by for with about as i you he she we they me us them my your his her our their this that these those and or but not no yes so if then than as than when where what who why how".split())


def _extract_kws(text: str, max_kw: int = 8) -> list[str]:
    import re
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z]{4,}|[\u4e00-\u9fa5]{2,}", text)]
    freq = Counter(t for t in tokens if t not in _STOPWORDS)
    return [w for w, _ in freq.most_common(max_kw)]


def ingest_question(port: int, entry: dict) -> int:
    """把一题的所有 haystack turns 写入 3CAN. 返回节点数."""
    qid = entry["question_id"][:20]
    sessions = entry.get("haystack_sessions", [])
    dates = entry.get("haystack_dates", [])
    count = 0
    payloads = []
    for si, session in enumerate(sessions):
        sess_date = dates[si] if si < len(dates) else ""
        for ti, turn in enumerate(session):
            n = turn_to_node(qid, si, ti, turn, sess_date)
            payloads.append(n)
    # 批量 POST (并发)
    def _post(p):
        try:
            r = requests.post(f"http://localhost:{port}/api/nodes?force=true", json=p, timeout=10)
            return r.status_code < 400
        except Exception:
            return False
    with cf.ThreadPoolExecutor(max_workers=INGEST_BATCH) as ex:
        results = list(ex.map(_post, payloads))
    count = sum(results)
    return count


def route_and_build_context(port: int, question: str,
                            k: int = MAX_CONTEXT_TURNS,
                            eval_mode: str = "slim") -> str:
    """route 取 top-k 返回拼接上下文.

    eval_mode=slim (default): backend-shaped 120-char summary — benchmark will reflect
    what a normal agent sees. Small facts past char 120 will be stripped; this is the
    authentic measurement of "3CAN default mode + LongMemEval".

    eval_mode=full: opt-in for evaluation — returns full node.content.description so
    the LLM judge sees the complete retrieved turn. Use when you specifically want to
    measure retrieval quality independent of pack-layer truncation.
    """
    mode = eval_mode if eval_mode in ("skeleton", "slim", "full") else "slim"
    cap = 600 if mode == "full" else 200
    try:
        r = requests.post(
            f"http://localhost:{port}/api/route",
            json={"task": question, "max_nodes": k, "agent_id": "longmemeval",
                  "mode": mode, "confirm_low_confidence": True, "allow_degraded": True},
            timeout=120,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        nodes = data.get("activated_nodes") or data.get("nodes") or []
        parts = []
        for n in nodes:
            content = n.get("content") or {}
            desc = content.get("description") or n.get("summary") or ""
            state = content.get("current_state") or n.get("current_state") or ""
            parts.append(f"- [{state[:40]}] {desc[:cap]}")
        return "\n".join(parts)
    except Exception:
        return ""


# ──────── 单题 pipeline ────────

def process_question(entry: dict, port: int, api_key: str,
                     eval_mode: str = "slim", apply_str_fix: bool = True) -> dict:
    qid = entry["question_id"]
    q = entry["question"]
    # apply_str_fix=False → original behavior (crash on int answer for multi-session counts).
    # Retained as flag so ablation can reproduce the pre-S66f failure mode honestly.
    gt = str(entry["answer"]) if apply_str_fix else entry["answer"]
    qtype = entry["question_type"]
    ts = entry.get("question_date", "")

    t0 = time.perf_counter()
    # 注意: 已在 main() 里 reset + ingest, 这里只跑 route + answer + judge
    context = route_and_build_context(port, q, eval_mode=eval_mode)
    route_ms = int((time.perf_counter() - t0) * 1000)

    if not context:
        return {"qid": qid, "type": qtype, "verdict": "incorrect",
                "reason": "route returned empty", "route_ms": route_ms}

    prompt = ANSWER_PROMPT.format(k=MAX_CONTEXT_TURNS, context=context, ts=ts, q=q)
    pred = deepseek_call(api_key, prompt, want_json=False, max_tokens=150)
    if not pred:
        return {"qid": qid, "type": qtype, "verdict": "incorrect",
                "reason": "answer LLM failed", "route_ms": route_ms}

    judge = deepseek_call(api_key, JUDGE_PROMPT.format(q=q, gt=gt, pred=pred),
                         want_json=True, max_tokens=80)
    if not judge or judge.get("_error"):
        return {"qid": qid, "type": qtype, "verdict": "incorrect",
                "reason": "judge failed", "pred": pred[:80], "route_ms": route_ms}

    verdict = judge.get("verdict", "incorrect")
    return {
        "qid": qid, "type": qtype, "verdict": verdict,
        "reason": judge.get("reason", ""),
        "pred": pred[:120], "gt": gt[:120],
        "route_ms": route_ms,
        "total_s": round(time.perf_counter() - t0, 1),
    }


# ──────── 主流程 ────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="pilot 数量")
    ap.add_argument("--full", action="store_true", help="跑全部")
    ap.add_argument("--port", type=int, default=9799)
    ap.add_argument("--venv-py", default=sys.executable)
    ap.add_argument("--graph-dir", default=str(Path(tempfile.gettempdir()) / "3can_lme_graph"))
    ap.add_argument("--seed-offset", type=int, default=0, help="跳过前 N 题 (续跑用)")
    ap.add_argument("--eval-mode", choices=["skeleton", "slim", "full"], default="slim",
                    help="pack mode sent to /api/route. slim (default) reflects agent "
                         "default behavior; full is opt-in for evaluation-time retrieval.")
    ap.add_argument("--reset-per-question", action="store_true",
                    help="Reset graph between questions (strict isolation, LongMemEval "
                         "protocol). Default OFF → cumulative ingest (matches pre-S66f).")
    ap.add_argument("--no-str-fix", action="store_true",
                    help="Do NOT wrap gt with str() — reproduces pre-S66f multi-session "
                         "crash for ablation baseline.")
    args = ap.parse_args()

    if not DATASET.exists():
        print(f"FATAL: dataset 不存在 {DATASET}")
        return 2

    api_key = _load_key()
    if not api_key:
        print("FATAL: no DeepSeek key")
        return 2

    print(f"[runner] 加载 {DATASET}...")
    full = json.load(open(DATASET, encoding="utf-8"))
    queries = full if args.full else full[args.seed_offset:args.seed_offset + args.limit]
    print(f"[runner] 跑 {len(queries)}/{len(full)} 题")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    graph_dir = Path(args.graph_dir)
    reset_graph(graph_dir)

    backend_log = OUT_DIR / f"backend_{args.port}.log"
    print(f"[runner] 启隔离 backend port={args.port} graph={graph_dir} log={backend_log}")
    proc = spawn_isolated_backend(args.port, graph_dir, args.venv_py, backend_log)
    print(f"[runner] backend pid={proc.pid}")

    results = []
    try:
        for i, entry in enumerate(queries, 1):
            t0 = time.perf_counter()
            qid_short = entry["question_id"][:16]
            try:
                # --reset-per-question: strict isolation per LongMemEval/Mem0/Letta protocol.
                # Default off → cumulative ingest across Qs (pre-S66f behavior, lets BGE-M3
                # cross-pollinate similar life-topic turns).
                if args.reset_per_question and i > 1:
                    reset_graph(graph_dir)
                    reload_backend(args.port)
                    wait_for_node_count(args.port, 0, timeout_s=30)
                n_ingest = ingest_question(args.port, entry)
                # barrier: ensure all ingested nodes are visible before route
                wait_for_node_count(args.port, n_ingest, timeout_s=60)
                ingest_s = round(time.perf_counter() - t0, 1)

                res = process_question(entry, args.port, api_key,
                                       eval_mode=args.eval_mode,
                                       apply_str_fix=not args.no_str_fix)
                res["ingest_nodes"] = n_ingest
                res["ingest_s"] = ingest_s
            except Exception as e:
                res = {"qid": entry["question_id"], "type": entry["question_type"],
                       "verdict": "incorrect", "reason": f"runner_exception: {str(e)[:100]}"}
            results.append(res)

            verdict_glyph = "OK" if res.get("verdict") == "correct" else "X "
            print(f"  [{i:3d}/{len(queries)}] {verdict_glyph} {qid_short} {res.get('type','?')[:18]:18s} "
                  f"ingest={res.get('ingest_s',0)}s route={res.get('route_ms',0)}ms total={res.get('total_s',0)}s "
                  f"reason={res.get('reason','-')[:40]}", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # 聚合
    correct = sum(1 for r in results if r["verdict"] == "correct")
    total = len(results)
    overall = correct / total if total else 0

    by_type = defaultdict(list)
    for r in results:
        by_type[r["type"]].append(r["verdict"] == "correct")

    variant_tag = (f"eval_mode={args.eval_mode} "
                   f"reset_per_q={args.reset_per_question} "
                   f"str_fix={not args.no_str_fix}")
    print(f"\n{'='*70}")
    print(f"LongMemEval-oracle  n={total}  accuracy={overall:.4f}  ({correct}/{total})")
    print(f"variant: {variant_tag}")
    print(f"{'='*70}")
    print("By question_type:")
    for qt, verdicts in sorted(by_type.items()):
        acc = sum(verdicts) / len(verdicts)
        print(f"  {qt:30s}  n={len(verdicts):3d}  acc={acc:.3f}")
    print()
    print("Reference (2026 SOTA on LongMemEval, GPT-4o+ judges):")
    print("  OMEGA (GPT-4.1):          0.954")
    print("  Mastra (GPT-5-mini):      0.949")
    print("  Mem0 token-efficient:     0.934")
    print("  Emergence AI RAG:         0.860")
    print("  Zep / Graphiti:           0.712")
    print("  GPT-4o oracle baseline:   0.87-0.92  (this dataset = oracle)")
    print("  GPT-4o non-oracle:        0.606-0.640")
    print("Caveats for this run: judge=DeepSeek (self-judge bias +5-10%);")
    print("                      dataset=oracle (easier than LongMemEvalS);")
    print(f"                      runner variant={variant_tag}")

    out = OUT_DIR / f"longmemeval_{int(time.time())}.json"
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dataset": "LongMemEval-oracle",
        "n_queries": total,
        "accuracy_overall": round(overall, 4),
        "answer_llm": "deepseek-chat",
        "judge_llm": "deepseek-chat",
        "variant": {
            "eval_mode": args.eval_mode,
            "reset_per_question": args.reset_per_question,
            "apply_str_fix": not args.no_str_fix,
        },
        "note": "DeepSeek 作答+判, 与 paper 的 GPT-4o 判不完全可比",
        "by_type": {qt: round(sum(v)/len(v), 4) for qt, v in by_type.items()},
        "results": results,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
