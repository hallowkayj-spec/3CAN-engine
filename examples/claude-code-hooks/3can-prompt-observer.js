/**
 * 3CAN Prompt Observer — UserPromptSubmit hook.
 *
 * Scans the maintainer's prompts for:
 *   1. 纠错信号 — "你错了/不对/不是/错了/stop/wrong/don't" → inject ERR 警示
 *   2. 新概念 — 未知专名 (2026 产品名/工具名) → inject WebSearch 提醒
 *
 * 不自动建节点. 只向 Claude 注入 additionalContext, 让主脑决策 route/WebSearch/writeback.
 * 这遵循 the maintainer 的"建节点前必 route"规则.
 */

const http = require('http');

const ENGINE_URL = 'http://localhost:9700';
const TIMEOUT = 2500;

function httpRequest(method, path, body) {
  return new Promise((resolve) => {
    const url = new URL(path, ENGINE_URL);
    const payload = body ? JSON.stringify(body) : null;
    const opts = {
      hostname: url.hostname, port: url.port, path: url.pathname + url.search,
      method, timeout: TIMEOUT,
      headers: payload ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {},
    };
    const req = http.request(opts, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch { resolve(null); } });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
    if (payload) req.write(payload);
    req.end();
  });
}

// ──────── 检测器 ────────

const CORRECTION_PATTERNS = [
  /你错了/, /不对/, /不是这样/, /错了(,|，|$)/, /搞错/, /弄错/,
  /停(止|下)/, /别再/, /不要(再|继续)/,
  /\bwrong\b/i, /\bdon['']t\b/i, /\bstop\b/i, /\bno,?\s+not\b/i, /\bnever\b/i,
  /没有(联网|核验|查)/, /应该是/, /其实是/,
];

const NEW_CONCEPT_PATTERNS = [
  // 模型/产品名 + 版本号: Gemma 4, GPT-5.4, Claude Opus 4.7, Llama 4
  /\b(Gemma|GPT|Claude|Llama|Qwen|DeepSeek|Mistral|Gemini|Phi|Yi|Doubao|Grok)[-\s]?(\d+(\.\d+)?[a-zA-Z]?)\b/g,
  // 带引号的新工具名
  /[「『"]([A-Z][a-zA-Z0-9-]{3,})[」』"]/g,
  /[「『"]([A-Za-z][a-z]+-[a-z][a-z-]+)[」』"]/g,
  // "X 是最新的" / "X 刚发布" / "看看 Y"
  /([A-Za-z][A-Za-z0-9-]{2,}\d*)\s*(是最新|刚发布|刚出|刚更新)/g,
];

function detectCorrection(text) {
  for (const p of CORRECTION_PATTERNS) {
    if (p.test(text)) return true;
  }
  return false;
}

function detectNewConcepts(text) {
  const found = new Set();
  for (const p of NEW_CONCEPT_PATTERNS) {
    let m;
    const re = new RegExp(p.source, p.flags);
    while ((m = re.exec(text)) !== null) {
      // 带引号/括号模式只要捕获组; 其他用 full match 保留版本号
      const term = ((p.source.includes('[「『"]') || p.source.includes('是最新')) ? m[1] : m[0]).trim();
      if (term && term.length >= 3 && term.length <= 40) found.add(term);
      if (m.index === re.lastIndex) re.lastIndex++;
    }
  }
  return [...found].slice(0, 5);
}

// ──────── 主逻辑 ────────

async function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
    setTimeout(() => resolve(data), 1000);
  });
}

const DEFAULT_LOG_ROOT = process.env.THREECAN_ENGINE_ROOT
  || process.env.THREECAN_PROJECT_DIR
  || process.cwd();
const LOG_PATH = process.env.THREECAN_PROMPT_OBSERVER_LOG
  || require('path').join(DEFAULT_LOG_ROOT, 'data', '_observer', 'prompt_log.jsonl');

function logPromptTriage(prompt, hasCorrection, concepts, unknown) {
  try {
    const fs = require('fs');
    const path = require('path');
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    const entry = {
      ts: new Date().toISOString(),
      prompt: prompt.slice(0, 1000),
      correction: hasCorrection,
      concepts,
      unknown,
    };
    fs.appendFileSync(LOG_PATH, JSON.stringify(entry) + '\n', { encoding: 'utf-8' });
  } catch {}
}

async function main() {
  const raw = await readStdin();
  let input;
  try { input = JSON.parse(raw); } catch { input = {}; }
  const prompt = input.prompt || input.user_message || '';
  if (!prompt || prompt.length < 4) {
    process.exit(0);
  }

  const hasCorrection = detectCorrection(prompt);
  const concepts = detectNewConcepts(prompt);

  const reminders = [];

  if (hasCorrection) {
    reminders.push(
      '[3CAN Observer] 检测到 the maintainer 纠错信号. 提醒:',
      '  1. 先 route 查现有 ERR-* 节点是否已记录同类错误',
      '  2. 若是新错误, 纠正后考虑 writeback 建 ERR-* (遵循 R1 先查再建)',
      '  3. 纠错内容可能是知识更新 — 若涉及我训练 cutoff 外的事实, 必须 WebSearch 核验',
    );
  }

  const unknown = [];
  if (concepts.length > 0) {
    for (const c of concepts) {
      const r = await httpRequest('POST', '/api/route', { task: c, max_nodes: 2, agent_id: 'prompt-observer' });
      const topScore = r && r.scores ? Math.max(0, ...Object.values(r.scores)) : 0;
      if (topScore < 0.04) unknown.push(c);
    }
    if (unknown.length > 0) {
      reminders.push(
        `[3CAN Observer] 检测到可能的新概念 (3CAN 未明确命中): ${unknown.join(', ')}`,
        '  → 若 the maintainer 引用的是 2026 年后产品/工具, 且超出你的 training cutoff, MUST WebSearch 核验再答',
        '  → ERR-gemma4-not-verified-2026-04-17 先例, 不要再犯',
      );
    }
  }

  if (hasCorrection || concepts.length > 0) {
    logPromptTriage(prompt, hasCorrection, concepts, unknown);
  }

  if (reminders.length === 0) {
    process.exit(0);
  }

  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'UserPromptSubmit',
      additionalContext: reminders.join('\n'),
    },
  }));
  process.exit(0);
}

main().catch(() => process.exit(0));
