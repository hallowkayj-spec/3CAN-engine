"""创建接口契约层(INTF)节点 — 解决3.5/7的根因。"""
import io
import os
import sys
import json
import urllib.request

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

API = "http://localhost:9701"

def post(path, data):
    d = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(f"{API}{path}", d, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())

nodes = [
    {
        "id": "INTF-distill-output", "name": "蒸馏输出格式约定",
        "cluster": "接口契约", "layer": "L1", "type": "reference", "priority": "high",
        "content": {
            "description": "teacher_parallel.py的输出JSONL格式约定",
            "current_state": "DeepSeek 2066条已完成, Volcengine待跑",
            "tech_stack": ["teacher_parallel.py", "JSONL"],
            "key_files": ["scripts/distillation/teacher_parallel.py"],
            "notes": "字段命名(L98): f'output_{teacher}' -> deepseek=output_deepseek, volcengine=output_volcengine(非output_doubao!). 文件命名: answers_{teacher}.jsonl -> answers_deepseek.jsonl / answers_volcengine.jsonl. 无test_前缀. 每条含: instruction, input, system, category, output_{teacher}, _idx.",
        },
        "activation_keywords": ["output_deepseek", "output_volcengine", "output_doubao", "teacher_parallel", "answers_deepseek", "answers_volcengine", "JSONL格式", "蒸馏输出", "字段名", "教师答案"],
    },
    {
        "id": "INTF-kb-entries", "name": "KB entries表Schema",
        "cluster": "接口契约", "layer": "L1", "type": "reference", "priority": "high",
        "content": {
            "description": "knowledge.db entries表完整字段定义, 所有入库操作必须遵守",
            "current_state": "2754条, 15个字段",
            "tech_stack": ["SQLite", "FTS5"],
            "key_files": ["data/wu_kb/knowledge.db", "tools/distillery/kb_ingestion.py"],
            "notes": "15字段: id, category(中文), subcategory, title(截100), content(Q+A+要点), platform, source_url, source_type(distilled/cleaned/crawled), reliability(0-1), confidence(0-1), freshness, collected_at, last_verified, tags(JSON array), data_quality_score(0-1). FTS5: entries_fts(rowid,title,content,category,tags). 入库后必须同步FTS5+重建FAISS.",
        },
        "activation_keywords": ["entries表", "knowledge.db", "KB入库", "schema", "FTS5", "source_type", "reliability", "data_quality_score", "entries字段"],
    },
    {
        "id": "INTF-judge-verdict", "name": "Judge评分结果格式",
        "cluster": "接口契约", "layer": "L1", "type": "reference", "priority": "medium",
        "content": {
            "description": "LLM-as-Judge评分标准输出JSON格式",
            "tech_stack": ["doubao-seed", "LLM-as-Judge"],
            "key_files": ["tools/distillery/answer_merger.py", "tools/distillery/single_answer_judge.py"],
            "notes": "5维度: actionability/specificity/accuracy/completeness/evidence(各1-5). 3硬检: has_specific_numbers/no_vague_advice/has_platform_path(不通过上限3.5). verdict: gold/silver/bronze/reject. 输出: best_model, best_score, verdict, merged_answer, key_facts[], reasoning.",
        },
        "activation_keywords": ["judge", "verdict", "gold", "silver", "bronze", "reject", "评分", "LLM-as-Judge", "answer_merger", "评判格式"],
    },
    {
        "id": "INTF-llm-provider", "name": "LLM Provider调用接口",
        "cluster": "接口契约", "layer": "L0", "type": "reference", "priority": "high",
        "content": {
            "description": "tools/llm/provider.py统一LLM调用, 所有LLM调用必须走此模块",
            "tech_stack": ["httpx", "OpenAI-compatible"],
            "key_files": ["tools/llm/provider.py"],
            "api_refs": ["VOLCENGINE_API_KEY", "GEMINI_API_KEY"],
            "notes": "call_text/call_json(prompt,system_prompt,task_name,provider,model,max_tokens,temperature,timeout). provider: volcengine/gemini/anthropic/minimax. 不指定provider时自动路由可能选Gemini(贵). 蒸馏/judge必须显式provider='volcengine'.",
        },
        "activation_keywords": ["call_text", "call_json", "provider", "volcengine", "gemini", "LLM调用", "provider.py", "httpx"],
    },
    {
        "id": "INTF-faiss-rebuild", "name": "FAISS索引重建接口",
        "cluster": "接口契约", "layer": "L1", "type": "reference", "priority": "medium",
        "content": {
            "description": "vec_retriever.py FAISS索引构建和检索",
            "tech_stack": ["FAISS", "BGE-M3"],
            "key_files": ["tools/advisor/vec_retriever.py", "data/faiss_index/"],
            "notes": "build_index(): entries(content>=30字) -> BGE-M3编码 -> IndexFlatIP -> index.faiss+meta.json. vec_search(query,top_k). 模型: BAAI/bge-m3, normalize_embeddings=True.",
        },
        "activation_keywords": ["FAISS", "build_index", "vec_search", "vec_retriever", "向量索引", "重建索引"],
    },
    {
        "id": "INTF-advisor-api", "name": "Advisor API端点清单",
        "cluster": "接口契约", "layer": "L2", "type": "reference", "priority": "medium",
        "content": {
            "description": "serve.py 36个API端点及关键参数",
            "tech_stack": ["FastAPI", "uvicorn"],
            "key_files": ["tools/advisor/serve.py"],
            "notes": "核心: POST /api/understand(VLM), POST /api/plan(运营计划), POST /api/coaching-action, POST /api/asset/generate, GET /api/dashboard/stats. 前端: advisor.html(8878). kb-monitor.html, neural_network.html.",
        },
        "activation_keywords": ["serve.py", "API端点", "advisor", "FastAPI", "/api/plan", "/api/understand", "8878"],
    },
    {
        "id": "INTF-6schema-tables", "name": "6-Schema数据库表结构",
        "cluster": "接口契约", "layer": "L1", "type": "reference", "priority": "high",
        "content": {
            "description": "6-Schema闭环7张SQLite表, 所有运营数据CRUD必须通过db.py",
            "tech_stack": ["SQLite WAL", "Pydantic"],
            "key_files": ["tools/platform_core/db.py", "tools/platform_core/schemas.py"],
            "notes": "7表: merchant_profiles, suggestions, action_logs, outcomes, attribution_tags, next_strategies + entries(KB). 闭环: merchant->suggestion->action->outcome->attribution->next_strategy->merchant.",
        },
        "activation_keywords": ["6-Schema", "merchant_profile", "suggestion", "action_log", "outcome", "attribution", "db.py", "数据库表"],
    },
]

edges = [
    {"source": "INTF-distill-output", "target": "MOD-distill", "type": "informs", "description": "蒸馏输出格式->蒸馏管线"},
    {"source": "INTF-kb-entries", "target": "MOD-kb", "type": "informs", "description": "entries表schema->KB"},
    {"source": "INTF-judge-verdict", "target": "MOD-distill", "type": "informs", "description": "Judge格式->蒸馏评判"},
    {"source": "INTF-llm-provider", "target": "SEC-volcengine", "type": "requires", "description": "LLM调用->火山引擎"},
    {"source": "INTF-faiss-rebuild", "target": "MOD-kb", "type": "feeds_into", "description": "FAISS重建->KB检索"},
    {"source": "INTF-advisor-api", "target": "MOD-advisor", "type": "informs", "description": "API端点->教练引擎"},
    {"source": "INTF-6schema-tables", "target": "MOD-advisor", "type": "informs", "description": "6-Schema->教练引擎"},
    {"source": "INTF-distill-output", "target": "INTF-judge-verdict", "type": "feeds_into", "description": "蒸馏输出->Judge评判"},
    {"source": "INTF-judge-verdict", "target": "INTF-kb-entries", "type": "feeds_into", "description": "Judge通过->KB入库"},
    {"source": "INTF-kb-entries", "target": "INTF-faiss-rebuild", "type": "triggers", "description": "KB入库->FAISS重建"},
]

print("=== Creating INTF nodes ===")
for n in nodes:
    try:
        resp = post("/api/nodes", n)
        print(f"  [+] {resp['id']}: {resp['name']}")
    except Exception as e:
        print(f"  [skip] {n['id']}: {e}")

print("\n=== Creating edges ===")
for e in edges:
    try:
        post("/api/edges", e)
        print(f"  {e['source']} -> {e['target']}")
    except Exception as ex:
        print(f"  [skip] {e['source']}->{e['target']}: {ex}")

req = urllib.request.Request(f"{API}/api/stats")
stats = json.loads(urllib.request.urlopen(req).read())
print(f"\nTotal: {stats['total_nodes']} nodes, {stats['total_edges']} edges")
