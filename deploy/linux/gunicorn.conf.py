import os

workers = 1
worker_class = "gthread"
threads = 4
preload_app = False
# 注册页面阶段允许等待，但 Worker 不能因 Gunicorn 默认 30 秒超时被误杀。
timeout = 300
# Keep service shutdown bounded when a browser RPC is stuck.
graceful_timeout = 45
bind = f"{os.environ.get('HOST', '127.0.0.1')}:{os.environ.get('PORT', '5000')}"
