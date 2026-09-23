# -*- coding: utf-8 -*-
"""测活成功后推送到 chatgpt2api 的配置。"""
from config.env_loader import apply_env_overrides


CHATGPT2API_PUSH_ENABLED = False
CHATGPT2API_BASE_URL = ""
CHATGPT2API_ADMIN_KEY = ""
CHATGPT2API_TIMEOUT = 10.0
CHATGPT2API_MAX_RETRIES = 3
CHATGPT2API_BACKOFF_BASE = 1.0
# AT-only：只把 access_token 下发给下游（不带 refresh_token/id_token）。
# 下游不持有 RT；本地用「账号+密码+2FA 协议登录」续 AT 后再复推。
CHATGPT2API_PUSH_AT_ONLY = False


apply_env_overrides(globals(), {
    "CHATGPT2API_PUSH_ENABLED": "bool",
    "CHATGPT2API_BASE_URL": "str",
    "CHATGPT2API_ADMIN_KEY": "str",
    "CHATGPT2API_TIMEOUT": "float",
    "CHATGPT2API_MAX_RETRIES": "int",
    "CHATGPT2API_BACKOFF_BASE": "float",
    "CHATGPT2API_PUSH_AT_ONLY": "bool",
})
