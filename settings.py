# -*- coding: utf-8 -*-
"""运行时设置。

只保留一项需要经常改的东西：**服务地址**（写进 .strm 的地址）。
其他参数（页数、并发、间隔）全部走环境变量 / compose，不在这里。

源站域名固定在 config.HOST —— 这个程序是给单一站点用的，不做多站适配。
"""
import threading

import config
import db

_DEFAULTS = {
    "public_base": config.PUBLIC_BASE,
    "last_finish": "0",      # 上一次全流程跑完的时间戳，用于算下次更新
    "strm_count": "0",       # 最近一次导出后的 .strm 数量
    "catalog_complete": "0", # 目录是否已完整扫过一遍（0=还没，需继续全量）
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
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in _load().items() if v != ""})
    return out


# ---------------------------------------------------------------- 便捷读取
def public_base():
    return str(get("public_base") or "").rstrip("/")


def source_host():
    """被抓取的站点地址，来自环境变量 JP_HOST（换域名只改 compose）。"""
    return config.HOST.rstrip("/")


def last_finish():
    return get_int("last_finish", 0)


def mark_finished():
    db.set_setting("last_finish", str(__import__("time").time().__int__()))
    invalidate()


def catalog_complete():
    return get_int("catalog_complete", 0) == 1


def set_catalog_complete(done=True):
    db.set_setting("catalog_complete", "1" if done else "0")
    invalidate()


def strm_count():
    return get_int("strm_count", 0)


def set_strm_count(n):
    db.set_setting("strm_count", str(int(n)))
    invalidate()
