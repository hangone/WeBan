#!/bin/bash
set -e

# 启动 headless-shell CDP 服务（后台）
/headless-shell/run.sh 2>/dev/null &

# 等待 CDP 端口就绪
for i in $(seq 1 30); do
  if (echo >/dev/tcp/127.0.0.1/9222) 2>/dev/null; then
    exec "$@"
  fi
  sleep 0.5
done

echo "ERROR: headless-shell CDP port 9222 did not become ready within 15 seconds" >&2
exit 1
