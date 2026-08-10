#!/bin/bash
# Neural Memory — Session启动Hook
# 功能：自动查询route API，输出相关子图摘要到stdout
# Claude Code的SessionStart hook会将stdout注入context
#
# 用法：在 ~/.claude/settings.json 的 hooks.SessionStart 中配置
# 依赖：neural-memory后端运行在 localhost:9700

API="http://localhost:9700"

# 检查服务是否在跑
if ! curl -s --connect-timeout 1 "$API/api/stats" > /dev/null 2>&1; then
  echo "[NeuralMemory] Server offline (localhost:9700)"
  exit 0
fi

# 获取统计
STATS=$(curl -s "$API/api/stats" 2>/dev/null)
NODES=$(echo "$STATS" | python -c "import json,sys; print(json.load(sys.stdin)['total_nodes'])" 2>/dev/null || echo "?")
EDGES=$(echo "$STATS" | python -c "import json,sys; print(json.load(sys.stdin)['total_edges'])" 2>/dev/null || echo "?")
BLOCKED=$(echo "$STATS" | python -c "import json,sys; print(json.load(sys.stdin)['blocked_nodes'])" 2>/dev/null || echo "0")

echo "[NeuralMemory] Graph: ${NODES} nodes, ${EDGES} edges, ${BLOCKED} blocked"
echo "[NeuralMemory] Dashboard: http://localhost:9700"
echo "[NeuralMemory] Route API: POST http://localhost:9700/api/route {\"task\":\"...\",\"max_nodes\":5}"
