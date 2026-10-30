# -*- coding: utf-8 -*-
"""目标站点接口客户端：签名、分类、列表、详情、取流。"""
import hashlib
import threading
import time

import requests
import urllib3

import config
import settings

urllib3.disable_warnings()


class JPError(Exception):
    pass


class JPRateLimited(JPError):
    """被站点限流（code=99600 请求太频繁）。"""


# 站点限流返回码
_RATE_CODES = (99600,)
# 全局冷却时间戳：被限流后所有请求先等一下，避免越撞越狠
_cooldown = [0.0]
_cooldown_lock = threading.Lock()
# 日志钩子（由 sync 模块挂上）
LOG = None


def _log(msg):
    if LOG:
        try:
            LOG(msg)
        except Exception:
            pass


def _is_rate_limit(code, msg):
    if code in _RATE_CODES or str(code) in [str(c) for c in _RATE_CODES]:
        return True
    m = str(msg or "")
    return ("频繁" in m) or ("太快" in m) or ("休息" in m)


def _wait_cooldown():
    wait = _cooldown[0] - time.time()
    if wait > 0:
        time.sleep(min(wait, 120))


def _set_cooldown(seconds):
    with _cooldown_lock:
        _cooldown[0] = max(_cooldown[0], time.time() + seconds)


class JPApi:
    def __init__(self, host=None, timeout=20):
        self.host = (host or settings.source_host()).rstrip("/")
        if not config.APP_KEY:
            raise JPError("未配置环境变量 JP_APP_KEY（接口签名密钥）")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.UA,
            "Referer": self.host + "/",
            "Accept": "application/json, text/plain, */*",
        })
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 基础
    def _sign(self, params):
        clean = {}
        for k, v in (params or {}).items():
            v = "" if v is None else str(v).strip()
            if v:
                clean[k] = v
        qs = "&".join("%s=%s" % (k, clean[k]) for k in sorted(clean))
        t = str(int(time.time() * 1000))
        raw = (qs + "&" if qs else "") + "key=%s&t=%s" % (config.APP_KEY, t)
        sign = hashlib.sha1(hashlib.md5(raw.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()
        return qs, t, sign

    def _api(self, path, params=None, retry=6):
        qs, t, sign = self._sign(params)
        url = self.host + path + (("?" + qs) if qs else "")
        headers = {"t": t, "sign": sign}
        last = ""
        rate_hits = 0
        for i in range(retry):
            _wait_cooldown()
            try:
                r = self.session.get(url, headers=headers, timeout=self.timeout, verify=False)
                if r.status_code != 200:
                    last = "HTTP %s" % r.status_code
                    time.sleep(1.0 * (i + 1))
                    continue
                data = r.json()
                code = data.get("code")
                msg = data.get("msg")

                # 被限流：指数退避 + 全局冷却，重试次数放宽
                if _is_rate_limit(code, msg):
                    rate_hits += 1
                    back = min(120, 5 * (2 ** min(rate_hits, 4)))
                    _set_cooldown(back)
                    _log("[限流] 站点提示「%s」，暂停 %d 秒后重试（第 %d 次）" % (msg or code, back, rate_hits))
                    time.sleep(back)
                    continue

                if code not in (None, 0, "0", 200, "200"):
                    last = "code=%s msg=%s" % (code, msg)
                    time.sleep(1.0)
                    continue
                return data
            except Exception as e:
                last = repr(e)
                time.sleep(1.0 * (i + 1))
        raise JPError("%s 请求失败: %s" % (path, last))

    # ------------------------------------------------------------------ 分类
    def types(self):
        data = self._api("/api/mw-movie/anonymous/get/filer/type")
        out = []
        for it in data.get("data") or []:
            tid = str(it.get("typeId") or "").strip()
            name = str(it.get("typeName") or "").strip()
            if tid and name:
                out.append({"type_id": tid, "type_name": name})
        return out

    def filters(self):
        data = self._api("/api/mw-movie/anonymous/v1/get/filer/list")
        return data.get("data") or {}

    # ------------------------------------------------------------------ 列表
    def video_list(self, type_id, page=1, year="", area="", v_class="", lang="", type_="", sort="1"):
        params = {
            "pageNum": str(page),
            "pageSize": str(config.PAGE_SIZE),
            "sort": str(sort),
            "sortBy": "1",
            "type1": str(type_id),
            "year": year,
            "area": area,
            "v_class": v_class,
            "lang": lang,
            "type": type_,
        }
        data = self._api("/api/mw-movie/anonymous/video/list", params)
        return data.get("data") or {}

    def video_count(self, type_id, **kw):
        d = self.video_list(type_id, page=1, **kw)
        return int(d.get("totalCount") or 0)

    # ------------------------------------------------------------------ 详情
    def video_detail(self, vod_id):
        data = self._api("/api/mw-movie/anonymous/video/detail", {"id": str(vod_id)})
        d = data.get("data")
        if not isinstance(d, dict) or not d:
            raise JPError("详情为空 vod_id=%s" % vod_id)
        return d

    # ------------------------------------------------------------------ 取流
    def episode_urls(self, vod_id, nid):
        """返回按清晰度降序排列的 [{resolution, resolutionName, url, needLogin}]"""
        params = {"clientType": "3", "id": str(vod_id), "nid": str(nid)}
        data = self._api("/api/mw-movie/anonymous/v2/video/episode/url", params)
        lst = (data.get("data") or {}).get("list") or []
        out = []
        for it in lst:
            url = str(it.get("url") or "").strip()
            if not url:
                continue
            out.append({
                "resolution": int(it.get("resolution") or 0),
                "resolutionName": str(it.get("resolutionName") or ""),
                "url": url,
                "needLogin": bool(it.get("needLogin")),
            })
        out.sort(key=lambda x: -x["resolution"])
        return out
