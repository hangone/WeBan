#!/bin/bash
set -e

# 启动 headless-shell CDP 服务（后台）
# 默认 --single-process：1 核小服务器上 Chrome 多进程互相饿死，CDP 鼠标
# 事件实测从 0.06s 拖到 55s；单进程模式恢复实时响应。WB_SINGLE_PROCESS=0
# 可关闭（多核机器想用多进程时）。
if [ "${WB_SINGLE_PROCESS:-1}" = "1" ]; then
  /headless-shell/headless-shell \
    --no-sandbox --single-process \
    --remote-debugging-address=0.0.0.0 --remote-debugging-port=9222 \
    >/dev/null 2>&1 &
else
  /headless-shell/run.sh >/dev/null 2>&1 &
fi

# 等待 CDP 端口就绪
for i in $(seq 1 30); do
  if (echo >/dev/tcp/127.0.0.1/9222) 2>/dev/null; then
    exec /app/WeBan "$@"
  fi
  sleep 0.5
done

echo "ERROR: headless-shell CDP port 9222 did not become ready within 15 seconds" >&2
exit 1
