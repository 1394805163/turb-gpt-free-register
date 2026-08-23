# -*- coding: utf-8 -*-
"""
代理池配置

每次注册按打乱后的无重复轮换顺序抽取代理，保证一轮内不重复使用身份。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
from config.env_loader import apply_env_overrides
import base64
import logging
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse


logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def _normalize_proxy_line(raw: str) -> str | None:
    """Normalize one proxy-list line; return None for comments or invalid rows."""
    text = str(raw or "").strip().lstrip("\ufeff")
    if not text or text.startswith(("#", ";")):
        return None
    if "://" not in text:
        if ":" not in text:
            return None
        text = f"http://{text}"
    try:
        parsed = urlparse(text)
        if parsed.scheme.lower() not in _SUPPORTED_PROXY_SCHEMES:
            return None
        if not parsed.hostname or not parsed.port:
            return None
    except (TypeError, ValueError):
        return None
    return text


def load_proxy_pool_file(file_path: str | Path) -> list[str]:
    """Read a proxy file without modifying it, preserving order and removing duplicates."""
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    if not path.is_file():
        return []

    proxies: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        proxy = _normalize_proxy_line(raw)
        if proxy and proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


def resolve_proxy_pool(configured_pool: list[str], file_path: str | Path | None) -> tuple[list[str], Path | None]:
    """Prefer a non-empty verified proxy file; otherwise keep configured proxies."""
    if not str(file_path or "").strip():
        return list(configured_pool or []), None
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    path = path.resolve()
    file_pool = load_proxy_pool_file(path)
    if file_pool:
        return file_pool, path
    return list(configured_pool or []), None


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# Resin sticky-account proxy output. A non-empty file takes priority over PROXY_POOL.
# Missing/empty files fall back to PROXY_POOL so WebUI can start independently.
PROXY_POOL_FILE: str = "../data/register-proxies.txt"

# True 时注册任务必须拿到一个 Resin/代理池地址；代理为空时后端直接阻止任务，
# 避免 CloakBrowser 悄悄回退到 VPS 直连出口。
REGISTRATION_PROXY_REQUIRED = False

# 公共注册出口来源：resin / mihomo。留空时兼容旧配置。
REGISTRATION_PROXY_SOURCE = ""

# 只用于 WebUI 显示和跳转管理面板，不作为浏览器 forward proxy 地址。
RESIN_MANAGEMENT_URL = ""

# Resin 门禁关闭后的注册回退：通过 Mihomo Controller 切换指定代理组
# 中的合格节点，再把浏览器指向本地 Mihomo 代理端口。该路径不允许直连兜底。
MIHOMO_US_FALLBACK_ENABLED = True
MIHOMO_CONTROLLER_URL = "http://127.0.0.1:9090"
MIHOMO_CONTROLLER_SECRET = ""
MIHOMO_CONTROLLER_USERNAME = ""
MIHOMO_CONTROLLER_PASSWORD = ""
MIHOMO_US_GROUP = "chatgpt us"
MIHOMO_REGISTRATION_GROUP = ""
MIHOMO_PROXY_URL = "socks5h://127.0.0.1:7897"
MIHOMO_TRANSPARENT_ROUTING = False
MIHOMO_CONTROLLER_TIMEOUT = 5.0

# exclude 模式默认允许全部地区，只排除香港；allowed 非空时才额外限定地区。
MIHOMO_REGISTRATION_ROUTE = "exclude"
MIHOMO_REGISTRATION_EXCLUDED_COUNTRIES = ["HK"]
MIHOMO_REGISTRATION_ALLOWED_COUNTRIES = []
MIHOMO_REGISTRATION_EXCLUDED_MULTIPLIERS = ["0.2"]

# 所有注册出口共同使用的国家策略；空列表表示除固定排除国家外均可用。
REGISTRATION_PROXY_EXCLUDED_COUNTRIES = ["HK"]
REGISTRATION_PROXY_ALLOWED_COUNTRIES = []

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 2
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3


_PROXY_ROTATION_LOCK = threading.Lock()
_PROXY_ROTATION_SIGNATURE: tuple[str, ...] = ()
_PROXY_ROTATION_QUEUE: list[str] = []
_PROXY_ROTATION_INDEX = 0
_REGISTRATION_PROXY_LEASE_LOCK = threading.Lock()
_REGISTRATION_PROXY_LEASES: set[str] = set()
_PROXY_FILE_CACHE_LOCK = threading.Lock()
_PROXY_FILE_CACHE_SIGNATURE: tuple[str, int, int] | None = None
_PROXY_FILE_CACHE: list[str] = []
_CONFIGURED_PROXY_POOL: list[str] = []


def get_proxy_pool() -> list[str]:
    """Return the latest proxy file contents without restarting the WebUI.

    Resin replaces its export atomically.  Cache by path/mtime/size so normal
    requests do not repeatedly parse the file, while the next registration
    immediately sees a refreshed pool.  When the Resin gate is required, a
    missing/empty file deliberately returns an empty list instead of silently
    falling back to the host's local/direct route.
    """
    configured_path = str(PROXY_POOL_FILE or "").strip()
    if not configured_path:
        return list(_CONFIGURED_PROXY_POOL or PROXY_POOL or [])
    path = Path(configured_path).expanduser()
    if not path.is_absolute():
        path = (_PROJECT_ROOT / path).resolve()
    try:
        stat = path.stat()
        signature = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        signature = (str(path), -1, -1)

    global _PROXY_FILE_CACHE_SIGNATURE, _PROXY_FILE_CACHE
    with _PROXY_FILE_CACHE_LOCK:
        if signature != _PROXY_FILE_CACHE_SIGNATURE:
            _PROXY_FILE_CACHE = load_proxy_pool_file(path) if signature[1] >= 0 else []
            _PROXY_FILE_CACHE_SIGNATURE = signature
        file_pool = list(_PROXY_FILE_CACHE)
    if file_pool:
        return file_pool
    if bool(REGISTRATION_PROXY_REQUIRED):
        return []
    return list(_CONFIGURED_PROXY_POOL or PROXY_POOL or [])


def pick_proxy() -> str:
    """打乱后逐个轮换代理；一轮耗尽前不重复，池为空时返回空串。"""
    global _PROXY_ROTATION_SIGNATURE, _PROXY_ROTATION_QUEUE, _PROXY_ROTATION_INDEX
    pool = tuple(get_proxy_pool())
    if not pool:
        return ""
    with _PROXY_ROTATION_LOCK:
        if pool != _PROXY_ROTATION_SIGNATURE:
            _PROXY_ROTATION_SIGNATURE = pool
            _PROXY_ROTATION_QUEUE = list(pool)
            random.shuffle(_PROXY_ROTATION_QUEUE)
            _PROXY_ROTATION_INDEX = 0
        selected = _PROXY_ROTATION_QUEUE[_PROXY_ROTATION_INDEX]
        _PROXY_ROTATION_INDEX += 1
        if _PROXY_ROTATION_INDEX >= len(_PROXY_ROTATION_QUEUE):
            random.shuffle(_PROXY_ROTATION_QUEUE)
            if len(_PROXY_ROTATION_QUEUE) > 1 and _PROXY_ROTATION_QUEUE[0] == selected:
                _PROXY_ROTATION_QUEUE[0], _PROXY_ROTATION_QUEUE[1] = (
                    _PROXY_ROTATION_QUEUE[1],
                    _PROXY_ROTATION_QUEUE[0],
                )
            _PROXY_ROTATION_INDEX = 0
        return selected


def acquire_registration_proxy(*, excluded: set[str] | None = None, preferred: str | None = None) -> str:
    """Lease one proxy identity for a registration attempt.

    Active registrations never share the same identity, and one registration
    can exclude identities it has already tried during the current run.
    """
    blocked = set(excluded or ())
    pool = get_proxy_pool()
    candidates = len(pool) + (1 if preferred and preferred not in pool else 0)
    with _REGISTRATION_PROXY_LEASE_LOCK:
        if preferred and preferred not in blocked and preferred not in _REGISTRATION_PROXY_LEASES:
            _REGISTRATION_PROXY_LEASES.add(preferred)
            return preferred
        for _ in range(candidates):
            candidate = pick_proxy()
            if not candidate:
                break
            if candidate in blocked or candidate in _REGISTRATION_PROXY_LEASES:
                continue
            _REGISTRATION_PROXY_LEASES.add(candidate)
            return candidate
    return ""


def release_registration_proxy(proxy: str | None) -> None:
    value = str(proxy or "").strip()
    if not value:
        return
    with _REGISTRATION_PROXY_LEASE_LOCK:
        _REGISTRATION_PROXY_LEASES.discard(value)


def is_us_node_name(node_name: str) -> bool:
    """按节点名识别美国节点，并显式排除 DIRECT/REJECT。"""
    name = str(node_name or "").strip()
    upper = name.upper()
    if not name or upper in {"DIRECT", "REJECT", "GLOBAL", "COMPATIBLE"}:
        return False
    return bool(
        "🇺🇸" in name
        or "美国" in name
        or "UNITED STATES" in upper
        or re.search(r"(?:^|[\s\-_|/])(US|USA)(?:$|[\s\-_|/0-9])", upper)
    )


_NODE_COUNTRY_MARKERS = {
    "HK": ("🇭🇰", "香港", "HONG KONG"),
    "JP": ("🇯🇵", "日本", "JAPAN"),
    "TW": ("🇹🇼", "台湾", "TAIWAN"),
    "SG": ("🇸🇬", "新加坡", "SINGAPORE"),
    "US": ("🇺🇸", "美国", "UNITED STATES"),
    "KR": ("🇰🇷", "韩国", "SOUTH KOREA", "KOREA"),
    "CA": ("🇨🇦", "加拿大", "CANADA"),
    "GB": ("🇬🇧", "英国", "UNITED KINGDOM"),
    "DE": ("🇩🇪", "德国", "GERMANY"),
}

_REGISTRATION_COMMON_COUNTRIES = {"US", "JP", "SG", "KR", "TW"}

# Mihomo 目标组的 `all` 同时包含叶子代理和动态代理组。组内当前节点会随
# URLTest/自动选择漂移，不能把 Selector/URLTest 等类型提交回目标组。
_MIHOMO_GROUP_TYPES = {
    "SELECTOR",
    "URLTEST",
    "FALLBACK",
    "LOADBALANCE",
    "RELAY",
    "COMPATIBLE",
}
_MIHOMO_GROUP_NAME_MARKERS = (
    "节点",
    "选择",
    "其他地区",
    "家宽",
    "低倍率",
    "全球直连",
)


def _normalize_country_codes(values: object) -> set[str]:
    if isinstance(values, str):
        values = re.split(r"[,;\n\s]+", values)
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {
        item.strip().upper()
        for value in values
        for item in re.split(r"[,;\n\s]+", str(value))
        if item.strip()
    }


def node_matches_country(node_name: str, country_code: str) -> bool:
    name = str(node_name or "").strip()
    upper = name.upper()
    code = str(country_code or "").strip().upper()
    if not name or upper in {"DIRECT", "REJECT", "GLOBAL", "COMPATIBLE"}:
        return False
    for marker in _NODE_COUNTRY_MARKERS.get(code, ()):
        if marker.isascii() and marker in upper:
            return True
        if not marker.isascii() and marker in name:
            return True
    return bool(re.search(rf"(?:^|[\s\-_|/]){re.escape(code)}(?:$|[\s\-_|/0-9])", upper))


def node_matches_registration_region(
    node_name: str,
    region_code: str,
    proxy_types: dict[str, str] | None = None,
) -> bool:
    """匹配注册 UI 的常用地区或 OTHER 冷门地区。"""
    region = str(region_code or "").strip().upper()
    if not _is_mihomo_leaf_node(node_name, proxy_types):
        return False
    if region == "OTHER":
        if node_matches_country(node_name, "HK"):
            return False
        return not any(node_matches_country(node_name, code) for code in _REGISTRATION_COMMON_COUNTRIES)
    return node_matches_country(node_name, region)


def _mihomo_proxy_type_map(client, base: str, headers: dict[str, str], timeout: float) -> dict[str, str]:
    """读取 Mihomo provider 元数据，建立代理名到协议/分组类型的索引。"""
    try:
        response = client.get(f"{base}/providers/proxies", headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        # 兼容旧版控制器或只实现 /proxies/{group} 的测试桩；调用方会使用名称兜底。
        return {}

    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, dict):
        return {}
    result: dict[str, str] = {}
    for provider in providers.values():
        items = provider.get("proxies") if isinstance(provider, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            proxy_type = str(item.get("type") or "").strip().upper()
            if name and proxy_type:
                result[name] = proxy_type
    return result


def _is_mihomo_leaf_node(node_name: str, proxy_types: dict[str, str] | None = None) -> bool:
    """只允许真实单节点，拒绝动态分组和直连/拒绝保留项。"""
    name = str(node_name or "").strip()
    upper = name.upper()
    if not name or upper in {"DIRECT", "REJECT", "GLOBAL", "COMPATIBLE"}:
        return False
    known_type = str((proxy_types or {}).get(name) or "").strip().upper()
    if known_type:
        return known_type not in _MIHOMO_GROUP_TYPES and known_type not in {"DIRECT", "REJECT"}
    return not any(marker in name for marker in _MIHOMO_GROUP_NAME_MARKERS)


def node_matches_multiplier(node_name: str, multiplier: object) -> bool:
    name = str(node_name or "")
    value = str(multiplier or "").strip().replace(",", ".")
    if not name or not value:
        return False
    if value == "0.2" and any(marker in name for marker in ("低倍率", "低倍", "低速倍率")):
        return True
    return bool(re.search(
        rf"(?<![0-9.]){re.escape(value)}(?:\s*[xX×倍]|(?=$|[\s\-_|/]))",
        name,
    ))


def _normalize_multipliers(values: object) -> set[str]:
    if isinstance(values, str):
        values = re.split(r"[,;\n\s]+", values)
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {str(value).strip().replace(",", ".") for value in values if str(value).strip()}


def _mihomo_headers(secret: str, username: str = "", password: str = "") -> dict[str, str]:
    headers = {"Accept": "application/json"}
    user = str(username or "").strip()
    if user:
        raw = f"{user}:{str(password or '')}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    elif str(secret or "").strip():
        headers["Authorization"] = f"Bearer {str(secret).strip()}"
    return headers


def select_mihomo_proxy(
    *,
    controller_url: str,
    secret: str,
    group: str,
    proxy_url: str,
    excluded_countries: object = None,
    allowed_countries: object = None,
    excluded_multipliers: object = None,
    allow_transparent: bool = False,
    mode: str = "mihomo_excluded",
    username: str = "",
    password: str = "",
    session=None,
) -> dict:
    """通用选择器：默认仅排除 HK/0.2，其他地区均可轮换。"""
    import requests

    base = str(controller_url or "").strip().rstrip("/")
    group_name = str(group or "").strip()
    excluded = _normalize_country_codes(
        MIHOMO_REGISTRATION_EXCLUDED_COUNTRIES if excluded_countries is None else excluded_countries
    ) | {"HK"}
    allowed = _normalize_country_codes(allowed_countries)
    multipliers = _normalize_multipliers(
        MIHOMO_REGISTRATION_EXCLUDED_MULTIPLIERS if excluded_multipliers is None else excluded_multipliers
    ) | {"0.2"}
    transparent = bool(allow_transparent)
    local_proxy = None if transparent else _normalize_proxy_line(proxy_url)
    if not base or not group_name or (not local_proxy and not transparent):
        raise RuntimeError("Mihomo 代理配置不完整")
    client = session or requests
    headers = _mihomo_headers(secret, username=username, password=password)
    timeout = max(0.2, float(globals().get("MIHOMO_CONTROLLER_TIMEOUT", 5.0) or 5.0))
    endpoint = f"{base}/proxies/{quote(group_name, safe='')}"
    started = time.monotonic()
    response = client.get(endpoint, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    names = payload.get("all") if isinstance(payload, dict) else []
    proxy_types = _mihomo_proxy_type_map(client, base, headers, timeout)
    candidates = [
        str(name) for name in (names or [])
        if _is_mihomo_leaf_node(str(name), proxy_types)
        and (not allowed or any(node_matches_registration_region(str(name), code, proxy_types) for code in allowed))
        and not any(node_matches_country(str(name), code) for code in excluded)
        and not any(node_matches_multiplier(str(name), value) for value in multipliers)
    ]
    if not candidates:
        raise RuntimeError(f"Mihomo 组 {group_name!r} 中没有符合筛选条件的节点；已阻止直连")
    current = str(payload.get("now") or "") if isinstance(payload, dict) else ""
    alternatives = [name for name in candidates if name != current]
    if alternatives:
        candidates = alternatives
    node_name = random.choice(candidates)
    switched = client.put(
        endpoint,
        headers={**headers, "Content-Type": "application/json"},
        json={"name": node_name},
        timeout=timeout,
    )
    switched.raise_for_status()
    return {
        "mode": mode,
        "group": group_name,
        "node_name": node_name,
        "proxy_url": local_proxy or "",
        "transparent": transparent,
        "excluded_countries": sorted(excluded),
        "allowed_countries": sorted(allowed),
        "excluded_multipliers": sorted(multipliers),
        "node_type": proxy_types.get(node_name, ""),
        "selection_ms": round((time.monotonic() - started) * 1000),
    }


def select_mihomo_us_proxy(
    *,
    controller_url: str,
    secret: str,
    group: str,
    proxy_url: str,
    allow_transparent: bool = False,
    username: str = "",
    password: str = "",
    session=None,
) -> dict:
    """兼容入口：从指定 Mihomo 组选择美国节点。"""
    return select_mihomo_proxy(
        controller_url=controller_url,
        secret=secret,
        group=group,
        proxy_url=proxy_url,
        allowed_countries={"US"},
        excluded_countries={"HK"},
        excluded_multipliers={"0.2"},
        allow_transparent=allow_transparent,
        mode="mihomo_us",
        username=username,
        password=password,
        session=session,
    )


def pick_registration_proxy() -> dict:
    """选择注册代理；Resin 和 Mihomo 均不允许失败时回退直连。"""
    source = str(REGISTRATION_PROXY_SOURCE or "").strip().lower()
    explicit_source = source
    if bool(REGISTRATION_PROXY_REQUIRED):
        # 旧版开关仍是硬门禁：只要开启就必须走 Resin，避免历史 .env
        # 与新版来源字段不一致时意外切到 Mihomo。
        source = "resin"
    elif source not in {"resin", "mihomo"}:
        source = "resin" if bool(REGISTRATION_PROXY_REQUIRED) else "mihomo"

    allowed = _normalize_country_codes(REGISTRATION_PROXY_ALLOWED_COUNTRIES)
    excluded = _normalize_country_codes(REGISTRATION_PROXY_EXCLUDED_COUNTRIES) or {"HK"}

    if source == "resin":
        pool = get_proxy_pool()
        if not pool:
            raise RuntimeError("Resin 注册代理池为空；已阻止直连")
        from core.registration_preflight import preflight_proxy

        attempted: set[str] = set()
        for _ in range(max(1, len(pool))):
            selected = pick_proxy()
            if not selected or selected in attempted:
                continue
            attempted.add(selected)
            result = preflight_proxy(
                selected,
                require_country="",
                allowed_countries=allowed,
                excluded_countries=excluded,
            )
            if result.get("ok"):
                return {
                    "mode": "resin",
                    "proxy_url": selected,
                    "node_name": "",
                    "selection_ms": 0,
                    "preflight": result,
                    "exit_country": result.get("country") or "",
                    "allowed_countries": sorted(allowed),
                    "excluded_countries": sorted(excluded),
                }
        raise RuntimeError(f"Resin 代理池没有通过 OpenAI 出口预检（已尝试 {len(attempted)} 个）")

    if not bool(MIHOMO_US_FALLBACK_ENABLED) and explicit_source != "mihomo":
        raise RuntimeError("Mihomo 美国代理回退未启用；已阻止直连")
    route = str(MIHOMO_REGISTRATION_ROUTE or "exclude").strip().lower()
    group = str(MIHOMO_REGISTRATION_GROUP or MIHOMO_US_GROUP or "").strip()
    if not group:
        raise RuntimeError("Mihomo 代理组配置不完整；已阻止直连")
    try:
        if route in {"us", "usa"}:
            return select_mihomo_us_proxy(
                controller_url=MIHOMO_CONTROLLER_URL,
                secret=MIHOMO_CONTROLLER_SECRET,
                group=group,
                proxy_url=MIHOMO_PROXY_URL,
                allow_transparent=MIHOMO_TRANSPARENT_ROUTING,
                username=MIHOMO_CONTROLLER_USERNAME,
                password=MIHOMO_CONTROLLER_PASSWORD,
            )
        mihomo_allowed = allowed or _normalize_country_codes(MIHOMO_REGISTRATION_ALLOWED_COUNTRIES)
        mihomo_excluded = excluded | {"HK"}
        return select_mihomo_proxy(
            controller_url=MIHOMO_CONTROLLER_URL,
            secret=MIHOMO_CONTROLLER_SECRET,
            group=group,
            proxy_url=MIHOMO_PROXY_URL,
            excluded_countries=mihomo_excluded,
            allowed_countries=mihomo_allowed,
            excluded_multipliers={"0.2"},
            allow_transparent=MIHOMO_TRANSPARENT_ROUTING,
            mode="mihomo_excluded",
            username=MIHOMO_CONTROLLER_USERNAME,
            password=MIHOMO_CONTROLLER_PASSWORD,
        )
    except Exception as exc:
        raise RuntimeError(f"Mihomo 代理选择失败；已阻止直连: {type(exc).__name__}") from exc


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = PROXY_POOL[0] if PROXY_POOL else ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_POOL_FILE': 'str',
    'REGISTRATION_PROXY_REQUIRED': 'bool',
    'REGISTRATION_PROXY_SOURCE': 'str',
    'RESIN_MANAGEMENT_URL': 'str',
    'MIHOMO_US_FALLBACK_ENABLED': 'bool',
    'MIHOMO_CONTROLLER_URL': 'str',
    'MIHOMO_CONTROLLER_SECRET': 'str',
    'MIHOMO_CONTROLLER_USERNAME': 'str',
    'MIHOMO_CONTROLLER_PASSWORD': 'str',
    'MIHOMO_US_GROUP': 'str',
    'MIHOMO_REGISTRATION_GROUP': 'str',
    'MIHOMO_PROXY_URL': 'str',
    'MIHOMO_TRANSPARENT_ROUTING': 'bool',
    'MIHOMO_CONTROLLER_TIMEOUT': 'float',
    'MIHOMO_REGISTRATION_ROUTE': 'str',
    'MIHOMO_REGISTRATION_EXCLUDED_COUNTRIES': 'list_str_delimited',
    'MIHOMO_REGISTRATION_ALLOWED_COUNTRIES': 'list_str_delimited',
    'MIHOMO_REGISTRATION_EXCLUDED_MULTIPLIERS': 'list_str_delimited',
    'REGISTRATION_PROXY_EXCLUDED_COUNTRIES': 'list_str_delimited',
    'REGISTRATION_PROXY_ALLOWED_COUNTRIES': 'list_str_delimited',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
_CONFIGURED_PROXY_POOL = list(PROXY_POOL or [])
PROXY_POOL, PROXY_POOL_LOADED_FROM = resolve_proxy_pool(PROXY_POOL, PROXY_POOL_FILE)
if PROXY_POOL_LOADED_FROM:
    logger.info("已从验证结果文件加载 %s 个代理: %s", len(PROXY_POOL), PROXY_POOL_LOADED_FROM)
PROXY = PROXY_POOL[0] if PROXY_POOL else ""
