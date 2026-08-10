/**
 * 3CAN Cold Start — MCP-first (~30 token注入)
 *
 * MCP tools已在mcp.json配好, agent自动可用route/read_node/writeback等。
 * Hook只做: 检测引擎在线 → 注入一句话提示。
 * 离线时回退到curl指令。
 */

const http = require('http');

const ENGINE_URL = 'http://localhost:9700';
const TIMEOUT = 3000;

function httpRequest(method, path) {
  return new Promise((resolve) => {
    const url = new URL(path, ENGINE_URL);
    const opts = {
      hostname: url.hostname, port: url.port, path: url.pathname + url.search,
      method, timeout: TIMEOUT,
    };
    const req = http.request(opts, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch { resolve(null); } });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.end();
  });
}

async function main() {
  const stats = await httpRequest('GET', '/api/stats');

  if (!stats || !stats.total_nodes) {
    // 引擎离线 — 可选 gate 只提示诊断和受支持恢复
    console.log(JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'SessionStart',
        additionalContext: [
          '═══════════════════════════════════════════════════════════',
          '[3CAN OPTIONAL GATE] 引擎离线 (localhost:9700 无响应或空图)',
          '═══════════════════════════════════════════════════════════',
          '',
          '暂停依赖 3CAN 记忆的 mutation，并保存当前上下文。',
          '  1) 只读诊断: netstat/tasklist/curl + 项目 verify_project.py',
          '  2) 使用项目支持的 service manager / Supervisor 请求恢复',
          '  3) 不要由 hook/wrapper 直接启动、终止或替换进程',
          '  4) 按项目 profile 验证 typed readiness，不使用统一节点阈值',
          '',
          '如果项目启用了 PreToolUse behavioral-gate，它会按项目配置',
          '暂停相应 mutating 工具；3CAN 引擎本身不依赖该 hook。',
        ].join('\n'),
      },
    }));
    process.exit(0);
  }

  // 引擎在线 — MCP-first, 最小注入
  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'SessionStart',
      additionalContext: `[3CAN在线] ${stats.total_nodes}节点 ${stats.total_edges}边 | 用MCP tool查记忆(route/read_node/writeback), 不要grep memory/`,
    },
  }));
  process.exit(0);
}

main().catch(() => process.exit(0));
