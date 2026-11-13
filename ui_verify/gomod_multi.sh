#!/usr/bin/env bash
# 全量路由走查（优化版）：一次登录，批量 open+snapshot+screenshot（串行）。
# 关键优化：用固定短等待代替 wait --load load（SPA 长连接导致 load 事件迟迟不触发）。
outdir="$1"; routesfile="$2"
mkdir -p "$outdir"
base="http://127.0.0.1:3900"
agent-browser open "$base/login" >/dev/null 2>&1
agent-browser wait 1500 >/dev/null 2>&1
agent-browser fill "input[placeholder='请输入用户名']" "e2etest" >/dev/null 2>&1
agent-browser fill "input[placeholder='请输入密码']" "E2e@123456" >/dev/null 2>&1
agent-browser snapshot -i >/tmp/lg.log 2>&1
LR=$(grep -oE 'button "登录" \[ref=[a-z0-9]+\]' /tmp/lg.log | grep -oE 'e[0-9]+' | head -1)
agent-browser click "$LR" >/dev/null 2>&1
agent-browser wait 2000 >/dev/null 2>&1
i=0
while IFS= read -r r; do
  [ -z "$r" ] && continue
  i=$((i+1))
  agent-browser open "$base$r" >/dev/null 2>&1
  agent-browser wait 2000 >/dev/null 2>&1
  safe=$(echo "$r" | tr '/:' '__')
  out="$outdir/$(printf '%03d' $i)_${safe}.log"
  agent-browser snapshot -i > "$out" 2>&1
  agent-browser screenshot "${out%.log}.png" >/dev/null 2>&1
  url=$(agent-browser get url 2>&1)
  flags=$(grep -E "404|Not Found|页面不存在|找不到|路由不存在|服务器错误|500" "$out" | head -3 | tr '\n' '|')
  echo "[$i] $r -> $url ${flags:+FLAGS:$flags}"
done < "$routesfile"
echo "DONE total=$i"
