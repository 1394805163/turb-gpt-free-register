# -*- coding: utf-8 -*-
"""CloakBrowser 自动化注册配置。"""
from config.env_loader import apply_env_overrides

# 是否无头启动：False=显示窗口，True=无头。
CLOAK_HEADLESS: bool = True

# 是否启用 CloakBrowser humanize 行为。
CLOAK_HUMANIZE: bool = True

# 使用当前出口 IP 自动匹配时区/语言/WebRTC IP。
CLOAK_GEOIP: bool = True

# 显式指定 Cloak 语言/时区；留空则在 CLOAK_GEOIP=True 时按出口 IP 自动推断。
# 例如：CLOAK_LOCALE="ja-JP"，CLOAK_TIMEZONE="Asia/Tokyo"。
CLOAK_LOCALE: str = ""
CLOAK_TIMEZONE: str = ""

# 是否把本项目传入/代理池抽取的代理传给 CloakBrowser。
CLOAK_USE_PROXY: bool = True

# Pro license；留空则使用免费 binary。
CLOAK_LICENSE_KEY: str = ""

# 固定指纹 seed；留空则每次 launch 随机生成新指纹。
CLOAK_FINGERPRINT_SEED: str = ""

# 持久化用户目录；留空则临时上下文。若要固定账号画像/缓存，可填如 "./cloak-profiles/default"。
CLOAK_USER_DATA_DIR: str = ""

# 额外 Chromium 参数，例如 ["--fingerprint=12345"]。CLOAK_FINGERPRINT_SEED 会自动追加。
CLOAK_EXTRA_ARGS: list = []

# 页面导航/元素操作的基础上限。注册流程会在此基础上为各阶段设置更明确的上限。
CLOAK_SELENIUM_TIMEOUT: int = 60

# 调试时保留浏览器不自动关闭。
CLOAK_KEEP_BROWSER_OPEN: bool = False

# ChatGPT/Cloudflare 拒绝当前出口时，自动切换下一个代理身份的最大尝试次数。
# 0 表示最多无重复走完当前代理池；正数表示设置一个更小的上限。
CLOAK_PROXY_ROTATION_ATTEMPTS: int = 0

# 透明 Mihomo 路由在启动浏览器前校验 OpenAI 同域真实出口；命中排除国家时，
# 最多重新选择该数量的节点。该重选不会启动浏览器，也不会触发邮箱流程。
CLOAK_MIHOMO_EXIT_ATTEMPTS: int = 3

# 登录页导航上限；超时后关闭当前浏览器并交给上层轮换代理。
CLOAK_LOGIN_PAGE_TIMEOUT: int = 60

# 邮箱提交后进入密码页/验证码页的阶段上限。
CLOAK_EMAIL_STEP_TIMEOUT: int = 60

# OAuth authorize 中间页没有输入框；超过这个窗口仍未进入真实步骤时，
# 关闭当前浏览器并交给上层轮换出口，避免固定占住整个邮箱阶段。
CLOAK_AUTHORIZE_GRACE_TIMEOUT: int = 15

# Cloak 适配层在跨域导航的执行上下文切换期间可能暂时返回空 URL。
CLOAK_NAVIGATION_GRACE_TIMEOUT: int = 12

# 密码页、资料页和最终 session 阶段的独立上限，避免单个阶段拖住整个 Worker。
CLOAK_PASSWORD_PAGE_TIMEOUT: int = 45
CLOAK_PROFILE_TIMEOUT: int = 90
CLOAK_SESSION_TIMEOUT: int = 90

# OTP 提交后等待页面状态变化的上限。
CLOAK_OTP_SUBMIT_TIMEOUT: int = 20

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'CLOAK_HEADLESS': 'bool', 'CLOAK_HUMANIZE': 'bool', 'CLOAK_GEOIP': 'bool', 'CLOAK_LOCALE': 'str', 'CLOAK_TIMEZONE': 'str', 'CLOAK_USE_PROXY': 'bool', 'CLOAK_LICENSE_KEY': 'str', 'CLOAK_FINGERPRINT_SEED': 'str', 'CLOAK_USER_DATA_DIR': 'str', 'CLOAK_SELENIUM_TIMEOUT': 'int', 'CLOAK_KEEP_BROWSER_OPEN': 'bool', 'CLOAK_PROXY_ROTATION_ATTEMPTS': 'int', 'CLOAK_MIHOMO_EXIT_ATTEMPTS': 'int', 'CLOAK_LOGIN_PAGE_TIMEOUT': 'int', 'CLOAK_EMAIL_STEP_TIMEOUT': 'int', 'CLOAK_AUTHORIZE_GRACE_TIMEOUT': 'int', 'CLOAK_NAVIGATION_GRACE_TIMEOUT': 'int', 'CLOAK_PASSWORD_PAGE_TIMEOUT': 'int', 'CLOAK_PROFILE_TIMEOUT': 'int', 'CLOAK_SESSION_TIMEOUT': 'int', 'CLOAK_OTP_SUBMIT_TIMEOUT': 'int'})
