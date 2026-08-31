#!/bin/bash
set -euo pipefail

# 启动 headless-shell CDP 服务（后台）
# 默认 --single-process：1 核小服务器上 Chrome 多进程互相饿死，CDP 鼠标
# 事件实测从 0.06s 拖到 55s；单进程模式恢复实时响应。WB_SINGLE_PROCESS=0
# 可关闭（多核机器想用多进程时）。
chrome_args=(
  --no-sandbox
  --use-gl=angle
  --use-angle=swiftshader
  --remote-debugging-address=127.0.0.1
  --remote-debugging-port=9222
)
if [ "${WB_SINGLE_PROCESS:-1}" = "1" ]; then
  chrome_args+=(--single-process)
fi
/headless-shell/headless-shell "${chrome_args[@]}" >/dev/null 2>&1 &

# 等待 CDP 端口就绪
cdp_ready=0
for i in $(seq 1 30); do
  if (echo >/dev/tcp/127.0.0.1/9222) 2>/dev/null; then
    cdp_ready=1
    break
  fi
  sleep 0.5
done

if [ "$cdp_ready" != "1" ]; then
  echo "ERROR: headless-shell CDP port 9222 did not become ready within 15 seconds" >&2
  exit 1
fi

# Docker 的 CMD 默认为 /app/WeBan；用户直接传 --help 等参数时 CMD 会被替换，
# 此时补回程序路径。两种情况最终都只启动一次 /app/WeBan。
if [ "$#" -eq 0 ]; then
  set -- /app/WeBan
elif [[ "$1" == -* ]]; then
  set -- /app/WeBan "$@"
fi
exec "$@"
