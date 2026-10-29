# -*- coding: utf-8 -*-
"""金牌影视接口客户端：签名、分类、列表、详情、取流。"""
import hashlib
import threading
import time

import requests
import urllib3

import config

urllib3.disable_warnings()


class JPError(Exception):
    pass


class JPApi:
    def __init__(self, host=None, timeout=20):
        self.host = (host or config.HOST).rstrip("/")
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

    def _api(self, path, params=None, retry=3):
        qs, t, sign = self._sign(params)
        url = self.host + path + (("?" + qs) if qs else "")
        headers = {"t": t, "sign": sign}
        last = ""
        for i in range(retry):
            try:
                r = self.session.get(url, headers=headers, timeout=self.timeout, verify=False)
                if r.status_code != 200:
                    last = "HTTP %s" % r.status_code
                    time.sleep(0.5 * (i + 1))
                    continue
                data = r.json()
                code = data.get("code")
                if code not in (None, 0, "0", 200, "200"):
                    last = "code=%s msg=%s" % (code, data.get("msg"))
                    # 签名类错误重试无意义，但时间戳抖动可能自愈，仍重试一次
                    time.sleep(0.5)
                    continue
                return data
            except Exception as e:
                last = repr(e)
                time.sleep(0.5 * (i + 1))
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
