# -*- coding: utf-8 -*-
"""运行时设置：存在数据库里，可在维护界面随时改，优先级高于环境变量。

这样换服务地址（写进 .strm 的地址）或换源站域名时，不用改 compose / 重启容器，
在界面上保存后重新导出 STRM 即可。
"""
import threading

import config
import db

# 默认值（来自 config / 环境变量），数据库里没有对应 key 时用它
_DEFAULTS = {
    "public_base": config.PUBLIC_BASE,
    "source_host": config.HOST,
    "cache_ttl": str(config.PLAY_CACHE_TTL),
    # 自动更新
    "auto_enabled": "0",
    "auto_hours": "24",          # 每 N 小时跑一次
    "auto_pages": "5",           # 增量：每个切片扫几页
    "auto_workers": "3",         # 详情并发
    "auto_export": "1",          # 跑完自动导出 STRM
    "auto_types": "",            # 留空=全部分类
    "last_auto_run": "0",
}

_lock = threading.Lock()
_cache = {}


def _load():
    with _lock:
        if not _cache:
            try:
                _cache.update(db.all_settings())
            except Exception:
                pass
        return _cache


def invalidate():
    with _lock:
        _cache.clear()


def get(key, default=None):
    data = _load()
    if key in data and data[key] != "":
        return data[key]
    if default is not None:
        return default
    return _DEFAULTS.get(key, "")


def get_int(key, default=0):
    try:
        return int(get(key))
    except Exception:
        return default


def set_many(pairs):
    for k, v in (pairs or {}).items():
        if k not in _DEFAULTS:
            continue
        db.set_setting(k, "" if v is None else str(v))
    invalidate()


def all_values():
    """返回完整设置（含默认值），供界面展示。"""
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in _load().items() if v != ""})
    return out


# ---------------------------------------------------------------- 便捷读取
def public_base():
    return str(get("public_base") or "").rstrip("/")


def source_host():
    return str(get("source_host") or "").rstrip("/")


def cache_ttl():
    return max(60, get_int("cache_ttl", config.PLAY_CACHE_TTL))
