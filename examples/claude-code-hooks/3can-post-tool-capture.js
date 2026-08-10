/**
 * 3CAN Post-Tool Capture — PostToolUse hook (v9.5 S66g 强化)
 *
 * Writes back every mutating tool call to 3CAN's activity log (hash-chained).
 *
 * Captures:
 *  - Edit / Write / MultiEdit / NotebookEdit → POST /api/activity/log with touched_files + ticket_id
 *  - Bash (mutating subcommands only) → POST /api/activity/log with command summary
 *  - SlashCommand / Skill → POST /api/skills/invoke for success-rate tracking
 *  - WebSearch → POST /api/activity/log with query
 *
 * Design principles (S66g 硬规则):
 *  - 不自动建新节点 (R1 先查再建)
 *  - 所有 mutating ops → activity_log (hash chain 审计)
 *  - 失败时 append 到 ~/.claude/logs/3can-writeback-fail.jsonl (不 silent drop)
 *  - Ticket id 如果 tool_input 里有, 透传到 activity.meta
 *  - 2.5s 硬超时
 */

const http = require('http');
const fs = require('fs');
const path = require('path');

const ENGINE_URL = 'http://localhost:9700';
const TIMEOUT = 2500;
const AGENT_ID = process.env.CLAUDE_AGENT_ID || 'opus-brain-main';

const WRITEBACK_FAIL_LOG = path.join(
  process.env.USERPROFILE || process.env.HOME,
  '.claude', 'logs', '3can-writeback-fail.jsonl');

// Bash mutating subcommands (mirror 3can-behavioral-gate.js BASH_HIGH_RISK — any
// command we'd have required a ticket for should also be written back afterwards)
const BASH_MUTATING = [
  /\brm\s+(-rf?|-fr?)\b/,
  /\bgit\s+(push|reset\s+--hard|branch\s+-[Dd])/,
  /\bgit\s+commit\b/,
  /\bnpm\s+(install|uninstall|publish)/,
  /\bpip\s+(install|uninstall)/,
  /\bdocker\s+(rm|rmi|system\s+prune)/,
  /\bcurl\s+.*(-X\s*)?(POST|PUT|DELETE|PATCH)/i,
  />\s*[\w./-]+/,
  /\bmv\s+|\bcp\s+-[rf]|\bchmod\b|\bchown\b/,
];

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
      res.on('end', () => {
        try { resolve({ ok: res.statusCode < 400, status: res.statusCode, data: JSON.parse(data) }); }
        catch { resolve({ ok: false, status: res.statusCode, raw: data.slice(0, 300) }); }
      });
    });
    req.on('error', (e) => resolve({ ok: false, error: String(e).slice(0, 200) }));
    req.on('timeout', () => { req.destroy(); resolve({ ok: false, error: 'timeout' }); });
    req.write(payload);
    req.end();
  });
}

function appendFailLog(record) {
  try {
    const dir = path.dirname(WRITEBACK_FAIL_LOG);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(WRITEBACK_FAIL_LOG, JSON.stringify(record) + '\n', 'utf-8');
  } catch { /* last-resort silent */ }
}

async function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
    setTimeout(() => resolve(data), 800);
  });
}

function extractTicketId(toolInput) {
  if (toolInput && typeof toolInput === 'object') {
    if (toolInput.meta && toolInput.meta['3can_ticket_id']) return toolInput.meta['3can_ticket_id'];
    if (toolInput._3can_ticket) return toolInput._3can_ticket;
    if (toolInput.__3can_ticket) return toolInput.__3can_ticket;
  }
  return process.env['THREECAN_TICKET_ID'] || null;
}

function inferSlashCommand(toolInput) {
  const cmd = toolInput?.command || toolInput?.name || '';
  const m = String(cmd).match(/^\/?([a-z0-9][a-z0-9_-]{2,40})/i);
  return m ? m[1] : null;
}

async function captureSkillInvoke(toolInput, toolResponse, durationMs) {
  const cmd = inferSlashCommand(toolInput);
  if (!cmd) return;
  const candidates = [`SKILL-user-${cmd}`, `SKILL-project-${cmd}`, `SKILL-plugin-${cmd}`];
  const outcome = (toolResponse?.error || toolResponse?.is_error) ? 'fail' : 'success';
  for (const skillId of candidates) {
    const r = await httpPost('/api/skills/invoke', {
      skill_id: skillId,
      agent_id: AGENT_ID,
      outcome,
      duration_s: durationMs ? durationMs / 1000 : null,
      notes: 'auto-captured by PostToolUse hook',
    });
    if (r.ok) return true;
  }
  return false;
}

async function captureActivity(action, detail, affectedNodes, meta) {
  const body = {
    agent_id: AGENT_ID,
    action,
    detail: (detail || '').slice(0, 400),
    affected_nodes: affectedNodes || [],
    meta: meta || {},
  };
  if (meta && meta.ticket_id) body.ticket_id = meta.ticket_id;
  const r = await httpPost('/api/activity/log', body);
  if (!r.ok) {
    appendFailLog({
      ts: new Date().toISOString(),
      reason: 'activity_log_failed',
      http_status: r.status || null, error: r.error || null, raw: r.raw || null,
      body,
    });
    return false;
  }
  return true;
}

function summarizeEdit(toolName, toolInput) {
  const fp = toolInput?.file_path || toolInput?.notebook_path || toolInput?.path || '';
  if (!fp) return { detail: null, fp: '' };
  const base = fp.split(/[\\/]/).pop();
  if (toolName === 'Write') return { detail: `wrote ${base}`, fp };
  if (toolName === 'Edit' || toolName === 'MultiEdit') return { detail: `edited ${base}`, fp };
  if (toolName === 'NotebookEdit') return { detail: `nb-edited ${base}`, fp };
  return { detail: null, fp };
}

async function main() {
  const raw = await readStdin();
  let input;
  try { input = JSON.parse(raw); } catch { process.exit(0); }

  const toolName = input.tool_name || input.toolName || '';
  const toolInput = input.tool_input || input.toolInput || {};
  const toolResponse = input.tool_response || input.toolResponse || {};
  const durationMs = input.duration_ms || 0;

  const interesting = ['SlashCommand', 'Skill', 'Edit', 'Write', 'MultiEdit',
                       'NotebookEdit', 'WebSearch', 'Bash'];
  if (!interesting.includes(toolName)) {
    process.exit(0);
  }

  const ticketId = extractTicketId(toolInput);

  try {
    if (toolName === 'SlashCommand' || toolName === 'Skill') {
      const ok = await captureSkillInvoke(toolInput, toolResponse, durationMs);
      if (!ok) {
        // No matching SKILL-* node; still log as activity for audit
        const cmdName = inferSlashCommand(toolInput) || 'unknown';
        await captureActivity('skill_invoke_unmatched', `cmd=${cmdName} no SKILL-* node matched`, [],
                              { tool_name: toolName, ticket_id: ticketId });
      }
    } else if (['Edit', 'Write', 'MultiEdit', 'NotebookEdit'].includes(toolName)) {
      const { detail, fp } = summarizeEdit(toolName, toolInput);
      if (detail) {
        await captureActivity('file_change', detail, [],
                              { tool_name: toolName, file_path: fp.slice(0, 200),
                                ticket_id: ticketId });
      }
    } else if (toolName === 'Bash') {
      const cmd = (toolInput.command || '').slice(0, 400);
      const isMutating = BASH_MUTATING.some((p) => p.test(cmd));
      if (isMutating) {
        await captureActivity('bash_mutating', cmd.slice(0, 200), [],
                              { tool_name: 'Bash', ticket_id: ticketId });
      }
    } else if (toolName === 'WebSearch') {
      const q = toolInput?.query || '';
      if (q) {
        await captureActivity('web_search', `q="${q.slice(0, 160)}"`, [],
                              { tool_name: 'WebSearch' });
      }
    }
  } catch (e) {
    appendFailLog({
      ts: new Date().toISOString(),
      reason: 'handler_exception',
      error: String(e).slice(0, 300),
      tool_name: toolName,
    });
  }

  process.exit(0);
}

main().catch((e) => {
  appendFailLog({
    ts: new Date().toISOString(),
    reason: 'main_exception',
    error: String(e).slice(0, 300),
  });
  process.exit(0);
});
