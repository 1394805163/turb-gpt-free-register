import os

workers = 1
worker_class = "gthread"
threads = 4
preload_app = False
graceful_timeout = 45
bind = f"{os.environ.get('HOST', '127.0.0.1')}:{os.environ.get('PORT', '5000')}"
