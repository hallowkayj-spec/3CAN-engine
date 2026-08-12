/**
 * 3CAN Cold Start — MCP-first (~30 token注入)
 *
 * MCP tools已在mcp.json配好, agent自动可用route/read_node/writeback等。
 * Hook只做: 检测引擎在线 → 注入一句话提示。
 * 离线时报告 typed unavailable；普通项目任务不拥有 runtime 生命周期。
 */

const http = require('http');

const ENGINE_URL = process.env.THREECAN_URL || process.env.THREECAN_BASE_URL || 'http://127.0.0.1:9700';
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
    // 引擎离线 — 本地工作继续；依赖 3CAN 的步骤延期。
    console.log(JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'SessionStart',
        additionalContext: [
          '═══════════════════════════════════════════════════════════',
          '[3CAN UNAVAILABLE] 运行时无响应或空图',
          '═══════════════════════════════════════════════════════════',
          '',
          '本地 Git、编码、构建和离线测试可以继续。',
          'route / ticket / writeback 暂记为 UNAVAILABLE，恢复后再执行。',
          '生产 runtime 只能由机器级 operator / Supervisor 恢复；',
          '普通 hook、wrapper 和 Workorder 不得启动、终止或替换 9700。',
          '',
          '破坏性、外部写入、凭据和生产门禁不因 3CAN 离线而放宽。',
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
