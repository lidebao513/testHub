#!/usr/bin/env bash
# ============================================================================
# M4 端到端联调验证脚本（HTTP 层）
#
# 前置：
#   1) testhub 已在 127.0.0.1:8000 运行（Django + MySQL，且已 migrate，含 testgen_integration app）
#   2) testgen sidecar 已在 127.0.0.1:8100 运行（services/testgen/.venv）
#   3) JWT 为 testhub 有效鉴权 token；TK 为 testgen X-Auth-Token
#
# 说明：本脚本覆盖 M4-1 主链路 + M4-2 安全闭环 + M4-3 部分失败路径，
#       需在具备完整运行环境的主机执行；本机（无 Django/MySQL）仅跑
#       apps/testgen_integration/tests/test_e2e_m4.py（注入式 + 真实 sidecar）。
# ============================================================================
set -euo pipefail

HUB="http://127.0.0.1:8000"
TG="http://127.0.0.1:8100"
JWT="${JWT:-}"
TK="${TK:-}"

echo "== 1) 探活（两侧）=="
curl -s -o /dev/null -w "testhub schema: %{http_code}\n" "$HUB/api/schema/"
curl -s "$TG/health"; echo

echo "== 2) testhub 触发代码生成（经 M1 后端代理 /api/testgen/generate）=="
TASK=$(curl -s -X POST "$HUB/api/testgen/generate" \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"repo_url":"<被测仓库>","scopes":["正常","安全","边界","异常"]}' | jq -r .task_id)
echo "TASK=$TASK"

echo "== 3) 轮询 testgen 任务直到终态（testhub 侧轮询走 client.poll → testgen /api/v1/tasks/{id}）=="
# 注：testhub 不另开 /api/testgen/tasks 端点；轮询由 testgen_integration.client.poll 直连 testgen。
for i in $(seq 1 30); do
  STATE=$(curl -s "$TG/api/v1/tasks/$TASK" | jq -r '.state // .status')
  echo "  poll $i -> $STATE"
  case "$STATE" in
    success|failed|cancelled) break ;;
  esac
  sleep 3
done

echo "== 4) 回流为 testhub TestCase（/api/testgen/sync；文档原称 import，代码命名为 sync）=="
curl -s -X POST "$HUB/api/testgen/sync" \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d "{\"project_id\":1,\"testgen_project_id\":1}" | jq .
# 若 testgen 尚未建 project，可先取真实 project_id：
#   curl -s "$TG/api/v1/projects" | jq '.projects[0].project_id'

echo "== 5) 在 testhub 侧查看回流用例 =="
curl -s "$HUB/api/testcases/testcases/?source=testgen" | jq '.results | length'

echo "== 6) M4-2 五维安全闭环：对 security 用例执行观测调 /verdict =="
curl -s -X POST "$TG/api/v1/verdict" \
  -H "X-Auth-Token: $TK" -H "Content-Type: application/json" \
  -d '{"category":"安全","dimension":"安全-越权","auth_mode":"required","observed_status":200,"expected_denied":true}' \
  | jq .   # 期望 verdict=unsafe 且 reason 指出"应拒却放通"

echo "== 7) 确认 testhub 自动在 defects 建 security 缺陷 =="
curl -s "$HUB/api/defects/defects/?source=testgen_verdict" | jq '.results | length'

echo "== 8) M4-3 红线：误传 execute=true 必须被拒（422）=="
curl -s -o /dev/null -w "execute=true -> %{http_code} (期望 422)\n" \
  -X POST "$TG/api/v1/generate" -H "X-Auth-Token: $TK" \
  -H "Content-Type: application/json" -d '{"repo_url":"x","execute":true}'

echo "DONE"
