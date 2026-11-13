#!/usr/bin/env bash
# 复用：登录兜底 + 打开模块路由 + 快照 + 截图
route="$1"
out="$2"
agent-browser open "http://127.0.0.1:3900${route}" > /dev/null 2>&1
agent-browser wait --load load > /dev/null 2>&1; agent-browser wait 1500 > /dev/null 2>&1
U=$(agent-browser get url 2>&1)
if echo "$U" | grep -q "/login"; then
  agent-browser open "http://127.0.0.1:3900/login" > /dev/null 2>&1; agent-browser wait --load load > /dev/null 2>&1
  agent-browser fill "input[placeholder='请输入用户名']" "e2etest" > /dev/null 2>&1
  agent-browser fill "input[placeholder='请输入密码']" "E2e@123456" > /dev/null 2>&1
  agent-browser snapshot -i > /tmp/lg.log 2>&1
  LR=$(grep -oE 'button "登录" \[ref=[a-z0-9]+\]' /tmp/lg.log | grep -oE 'e[0-9]+' | head -1)
  agent-browser click "$LR" > /dev/null 2>&1; agent-browser wait --load load > /dev/null 2>&1; agent-browser wait 1500 > /dev/null 2>&1
  agent-browser open "http://127.0.0.1:3900${route}" > /dev/null 2>&1; agent-browser wait --load load > /dev/null 2>&1; agent-browser wait 1500 > /dev/null 2>&1
fi
echo "FINAL_URL: $(agent-browser get url 2>&1)"
agent-browser snapshot -i > "$out" 2>&1
agent-browser screenshot "${out%.log}.png" > /dev/null 2>&1
echo "MARKERS:"; grep -E "heading|button|table|listitem|error|Error|异常|暂无" "$out" | head -25
