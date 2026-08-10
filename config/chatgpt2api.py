# -*- coding: utf-8 -*-
"""测活成功后推送到 chatgpt2api 的配置。"""
from config.env_loader import apply_env_overrides


CHATGPT2API_PUSH_ENABLED = False
CHATGPT2API_BASE_URL = ""
CHATGPT2API_ADMIN_KEY = ""
CHATGPT2API_TIMEOUT = 10.0
CHATGPT2API_MAX_RETRIES = 3
CHATGPT2API_BACKOFF_BASE = 1.0


apply_env_overrides(globals(), {
    "CHATGPT2API_PUSH_ENABLED": "bool",
    "CHATGPT2API_BASE_URL": "str",
    "CHATGPT2API_ADMIN_KEY": "str",
    "CHATGPT2API_TIMEOUT": "float",
    "CHATGPT2API_MAX_RETRIES": "int",
    "CHATGPT2API_BACKOFF_BASE": "float",
})
