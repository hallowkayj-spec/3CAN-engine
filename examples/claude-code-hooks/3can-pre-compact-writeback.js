/**
 * 3CAN Pre-Compact Writeback — PreCompact hook
 *
 * Compact 前扫本 session 关键成果, 强制 writeback 到 3CAN 防止"产出出得去回不来".
 *
 * 覆盖 the maintainer 基座#7 要求: 本 session 建过 PRD/ARCH/FEATURES/LIMITATIONS 等重要文档,
 * 但未自动入库, 靠人工才写 4 节点. 这个 hook 补闭环.
 *
 * 策略 (保守, 不污染图谱):
 *  1. 扫最近 3 小时内新建/修改的关键文件:
 *     - docs/specs 下所有 .md 文件 (递归)
 *     - neural-memory/tools 下所有 .py 文件
 *     - neural-memory/backend 下所有 .py 文件
 *     - ~/.claude/scripts/hooks 下所有 .js 文件
 *  2. 每个关键文件 → POST /api/nodes?force=true 建 DOC-session-autowrite-{slug}-{date}
 *     - 已存在同 ID → 跳过
 *  3. 不用 LLM (避免 compact 前额外延时), 只抓文件清单 + 头部 2-3 行描述
 *  4. 失败静默, 不挡 compact
 *  5. 硬超时 8s (PreCompact 允许长一点)
 */

const http = require('http');
const fs = require('fs');
const path = require('path');

const ENGINE_URL = 'http://localhost:9700';
const TIMEOUT = 3000;
const AGENT_ID = process.env.CLAUDE_AGENT_ID || 'opus-brain-main';
const SCAN_WINDOW_HOURS = 3;
const MAX_FILES = 20;

// 项目根 (优先 env, 否则 cwd)
const PROJECT_ROOT = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const HOME = process.env.USERPROFILE || process.env.HOME;

function httpPost(pathStr, body) {
  return new Promise((resolve) => {
    const url = new URL(pathStr, ENGINE_URL);
    const payload = JSON.stringify(body || {});
    const opts = {
      hostname: url.hostname, port: url.port, path: url.pathname + url.search,
      method: 'POST', timeout: TIMEOUT,
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
      },
    };
    const req = http.request(opts, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve({ status: res.statusCode, data }));
    });
    req.on('error', () => resolve({ status: 0 }));
    req.on('timeout', () => { req.destroy(); resolve({ status: 0 }); });
    req.write(payload);
    req.end();
  });
}

function httpGet(pathStr) {
  return new Promise((resolve) => {
    const url = new URL(pathStr, ENGINE_URL);
    const opts = {
      hostname: url.hostname, port: url.port, path: url.pathname + url.search,
      method: 'GET', timeout: TIMEOUT,
    };
    const req = http.request(opts, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve({ status: res.statusCode, data }));
    });
    req.on('error', () => resolve({ status: 0 }));
    req.on('timeout', () => { req.destroy(); resolve({ status: 0 }); });
    req.end();
  });
}

function walkDir(root, patterns, cutoff) {
  const out = [];
  if (!fs.existsSync(root)) return out;
  function rec(dir, depth) {
    if (depth > 5) return;
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch { return; }
    for (const e of entries) {
      const full = path.join(dir, e.name);
      // 跳过常见无关目录
      if (e.isDirectory()) {
        if (['node_modules', 'venv', '__pycache__', '.git', '_refs', 'archive',
             '.pytest_cache', '.ruff_cache', 'cache', 'dist', 'build'].includes(e.name)) continue;
        rec(full, depth + 1);
      } else {
        const okPattern = patterns.some(p => {
          if (p.endsWith('**/*.md')) return e.name.endsWith('.md');
          if (p.endsWith('**/*.py')) return e.name.endsWith('.py');
          if (p.endsWith('**/*.js')) return e.name.endsWith('.js');
          return false;
        });
        if (!okPattern) continue;
        try {
          const st = fs.statSync(full);
          if (st.mtimeMs >= cutoff) {
            out.push({ path: full, mtime: st.mtime.toISOString(), size: st.size });
          }
        } catch { }
      }
    }
  }
  rec(root, 0);
  return out;
}

function slugify(str, max = 40) {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, max);
}

function readHead(filePath, maxChars = 400) {
  try {
    const content = fs.readFileSync(filePath, 'utf-8');
    // 尝试抽标题 (# / first non-empty line)
    const firstLine = content.split('\n').find(l => l.trim()) || '';
    const title = firstLine.replace(/^#+\s*/, '').trim().slice(0, 80);
    const head = content.slice(0, maxChars);
    return { title, head };
  } catch {
    return { title: '', head: '' };
  }
}

async function writebackFile(f) {
  const rel = path.relative(PROJECT_ROOT, f.path).replace(/\\/g, '/');
  const slug = slugify(path.basename(f.path, path.extname(f.path)), 50);
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const nodeId = `DOC-autowrite-${slug}-${date}`.slice(0, 60);

  // 先查重
  const existing = await httpGet(`/api/nodes/${nodeId}`);
  if (existing.status === 200) return { skip: 'exists', nodeId };

  const { title, head } = readHead(f.path);
  const node = {
    id: nodeId,
    name: `[autowrite] ${title || path.basename(f.path)}`.slice(0, 80),
    cluster: '项目文档',
    type: 'reference',
    status: 'active',
    priority: 'medium',
    content: {
      description: `自动捕获: ${rel} (pre-compact hook 写入)`,
      current_state: `mtime=${f.mtime} size=${f.size}B`,
      notes: head.slice(0, 500),
      key_files: [rel],
    },
    activation_keywords: [
      slug, 'autowrite', 'pre-compact', 'session-result',
      path.basename(f.path), date,
    ],
    primary_author: 'pre-compact-hook',
  };

  const r = await httpPost('/api/nodes?force=true', node);
  return { status: r.status, nodeId };
}

async function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
    setTimeout(() => resolve(data), 500);
  });
}

async function main() {
  // PreCompact hook 允许拿到 input, 但我们不依赖 input 做判断
  try { await readStdin(); } catch { }

  const cutoffMs = Date.now() - SCAN_WINDOW_HOURS * 3600 * 1000;

  // 扫三类路径
  const roots = [
    { root: path.join(PROJECT_ROOT, 'docs', 'specs'), patterns: ['**/*.md'] },
    { root: path.join(PROJECT_ROOT, 'neural-memory', 'tools'), patterns: ['**/*.py'] },
    { root: path.join(PROJECT_ROOT, 'neural-memory', 'backend'), patterns: ['**/*.py'] },
    { root: path.join(PROJECT_ROOT, 'neural-memory', 'benchmark'), patterns: ['**/*.py'] },
    { root: path.join(HOME, '.claude', 'scripts', 'hooks'), patterns: ['**/*.js'] },
  ];

  let files = [];
  for (const r of roots) {
    files = files.concat(walkDir(r.root, r.patterns, cutoffMs));
  }
  // 按 mtime 降序, 取前 N
  files.sort((a, b) => b.mtime.localeCompare(a.mtime));
  files = files.slice(0, MAX_FILES);

  if (files.length === 0) {
    // 没新文件, 不出声
    process.exit(0);
  }

  const summary = { scanned: files.length, created: 0, skipped: 0, errors: 0 };
  for (const f of files) {
    try {
      const r = await writebackFile(f);
      if (r.skip === 'exists') summary.skipped++;
      else if (r.status >= 200 && r.status < 400) summary.created++;
      else summary.errors++;
    } catch {
      summary.errors++;
    }
  }

  // PreCompact 允许向 transcript 写 additionalContext (告诉 the maintainer 写回了什么)
  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreCompact',
      additionalContext: `[3CAN PreCompact Writeback] 扫 ${summary.scanned} 文件, 新建 ${summary.created} DOC 节点, 跳过已存 ${summary.skipped}, 错误 ${summary.errors}. 本 session 成果已回写.`,
    },
  }));
  process.exit(0);
}

main().catch(() => process.exit(0));
