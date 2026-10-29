# -*- coding: utf-8 -*-
"""后端服务：按需签发播放地址（302 重定向）+ 目录查询 + 前端 UI + 运维任务。

核心接口：
    GET  /play/<vod_id>/<nid>.m3u8        -> 302 跳转到最新有效的签名地址（.strm 里就写这个）
    GET  /proxy/<vod_id>/<nid>.m3u8       -> 拉取 m3u8 并把相对分片地址改写成绝对地址
    GET  /api/play/<vod_id>/<nid>         -> JSON，返回地址与过期时间
    GET  /api/videos?cat=&type_id=&year=&q=&limit=&offset=
    GET  /api/video/<vod_id>              -> 剧集详情 + 分集清单
    GET  /api/types                       -> 分类/年份聚合
    GET  /api/stats                       -> 库存统计
    POST /api/job/sync                    -> 触发同步任务（list / detail）
    POST /api/job/strm                    -> 触发 STRM 导出
    GET  /api/job                         -> 任务状态与日志
    GET  /                                -> 前端界面

启动：python server.py
"""
import collections
import os
import re
import threading
import time
from urllib.parse import urljoin

import requests
import urllib3
from flask import Flask, Response, jsonify, redirect, request, send_from_directory

import config
import db
import strm
import sync
from jpapi import JPApi, JPError

urllib3.disable_warnings()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

api = JPApi()
WEB_DIR = os.path.join(config.BASE_DIR, "web")


# ------------------------------------------------------------------ 取流缓存
class PlayCache:
    """缓存整档清晰度列表，key=(vod_id,nid)。签名有效期实测 30 分钟以上，缓存 20 分钟。"""

    def __init__(self, ttl):
        self.ttl = ttl
        self._data = {}
        self._locks = {}
        self._guard = threading.Lock()

    def get(self, key):
        with self._guard:
            item = self._data.get(key)
        if item and item[1] > time.time():
            return item[0]
        return None

    def put(self, key, value):
        with self._guard:
            self._data[key] = (value, time.time() + self.ttl)
            if len(self._data) > 5000:
                now = time.time()
                for k in [k for k, v in self._data.items() if v[1] < now]:
                    self._data.pop(k, None)

    def drop(self, key):
        with self._guard:
            self._data.pop(key, None)

    def key_lock(self, key):
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = self._locks[key] = threading.Lock()
            return lk

    def __len__(self):
        return len(self._data)


cache = PlayCache(config.PLAY_CACHE_TTL)


def pick_quality(urls, want):
    """从多清晰度里挑一个。want=0 表示要最高。"""
    if not urls:
        return None
    if want:
        exact = [u for u in urls if u["resolution"] == want]
        if exact:
            return exact[0]
        lower = [u for u in urls if 0 < u["resolution"] <= want]
        if lower:
            return max(lower, key=lambda x: x["resolution"])
    return urls[0]


def resolve(vid, nid, want=0):
    """返回 (url, 剩余有效秒数)。带单飞：同一集并发只打一次源站。"""
    key = "%s|%s" % (vid, nid)
    hit = cache.get(key)
    if hit:
        return pick_quality(hit, want), config.PLAY_CACHE_TTL
    with cache.key_lock(key):
        hit = cache.get(key)
        if hit:
            return pick_quality(hit, want), config.PLAY_CACHE_TTL
        urls = api.episode_urls(vid, nid)
        if not urls:
            raise JPError("该集暂无播放地址")
        cache.put(key, urls)
        return pick_quality(urls, want), config.PLAY_CACHE_TTL


# ------------------------------------------------------------------ 运维任务
class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.kind = ""
        self.started = 0.0
        self.finished = 0.0
        self.ok = None
        self.error = ""
        self.logs = collections.deque(maxlen=1000)

    def add(self, line):
        with self.lock:
            self.logs.append(str(line))

    def start(self, kind, fn):
        with self.lock:
            if self.running:
                return False, "已有任务在运行：%s" % self.kind
            self.running = True
            self.kind = kind
            self.started = time.time()
            self.finished = 0.0
            self.ok = None
            self.error = ""
            self.logs.clear()

        def worker():
            try:
                fn()
                with self.lock:
                    self.ok = True
            except Exception as e:
                with self.lock:
                    self.ok = False
                    self.error = repr(e)
                self.add("[ERR] %r" % (e,))
            finally:
                with self.lock:
                    self.running = False
                    self.finished = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return True, "started"

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "kind": self.kind,
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.started, 1) if self.started else 0,
                "ok": self.ok,
                "error": self.error,
                "logs": list(self.logs)[-400:],
            }


job = JobState()
sync.LOG_SINK = job.add
strm.LOG_SINK = job.add


def _job_sync_list(types, limit, sleep):
    a = JPApi()
    all_types = a.types()
    want = [x.strip() for x in str(types or "").split(",") if x.strip()]
    sel = [t for t in all_types if not want or t["type_id"] in want]
    filters = a.filters()
    years = sync._filter_values(filters, "yearList")
    areas = sync._filter_values(filters, "districtList")
    sync.log("分类：%s" % ", ".join("%s(%s)" % (t["type_name"], t["type_id"]) for t in sel))
    sync.log("年份切片 %d 个，地区切片 %d 个" % (len(years), len(areas)))
    sync.sync_list(a, sel, years, areas, limit=int(limit or 0), sleep=float(sleep or 0.15))
    s = db.stats()
    sync.log("列表同步结束：剧集 %d 部" % s["videos"])


def _job_sync_detail(limit, workers, sleep):
    a = JPApi()
    sync.sync_detail(a, limit=int(limit or 0), workers=int(workers or 4), sleep=float(sleep or 0.1))
    s = db.stats()
    sync.log("详情同步结束：%d 部有分集，共 %d 条分集" % (s["videos_with_episodes"], s["episodes"]))


def _job_strm(nfo, images, clean, types, limit):
    strm.export(None, types or "", int(limit or 0), bool(nfo), bool(images), bool(clean))


# ------------------------------------------------------------------ 鉴权
# 静态资源不需要令牌，否则设了 JP_TOKEN 后界面加载不出来
_OPEN_PREFIX = ("/ui", "/app.js", "/style.css", "/favicon.ico")


def _authed():
    if not config.ACCESS_TOKEN:
        return True
    token = request.args.get("token") or request.headers.get("X-Token") or ""
    return token == config.ACCESS_TOKEN


@app.before_request
def _guard():
    p = request.path
    if p == "/health" or p.startswith(_OPEN_PREFIX) or p == "/":
        return None
    if not _authed():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    return None


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Range, X-Token"
    return resp


# ------------------------------------------------------------------ 前端
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/ui/")
def ui_index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/ui/<path:name>")
def ui_static(name):
    return send_from_directory(WEB_DIR, name)


@app.route("/app.js")
def ui_appjs():
    return send_from_directory(WEB_DIR, "app.js")


@app.route("/style.css")
def ui_css():
    return send_from_directory(WEB_DIR, "style.css")


# ------------------------------------------------------------------ 播放
@app.route("/play/<vid>/<nid>.m3u8")
@app.route("/play/<vid>/<nid>")
def play(vid, nid):
    want = int(request.args.get("q") or 0)
    try:
        item, ttl = resolve(vid, nid, want)
    except JPError as e:
        return jsonify({"ok": False, "error": str(e), "vod_id": vid, "nid": nid}), 502
    if not item:
        return jsonify({"ok": False, "error": "no url"}), 502
    resp = redirect(item["url"], code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Quality"] = str(item["resolution"])
    resp.headers["X-Expires-In"] = str(ttl)
    return resp


@app.route("/proxy/<vid>/<nid>.m3u8")
@app.route("/proxy/<vid>/<nid>")
def proxy(vid, nid):
    """部分播放器不跟随 302，这里直接代理 m3u8 并把相对路径改成绝对路径。
    分片本身已带签名，不需要再代理，省带宽。"""
    want = int(request.args.get("q") or 0)
    try:
        item, _ = resolve(vid, nid, want)
    except JPError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not item:
        return jsonify({"ok": False, "error": "no url"}), 502
    url = item["url"]
    try:
        r = requests.get(url, timeout=20, verify=False,
                         headers={"User-Agent": config.UA, "Referer": config.HOST + "/"})
        if r.status_code != 200:
            cache.drop("%s|%s" % (vid, nid))
            return jsonify({"ok": False, "error": "upstream %s" % r.status_code}), 502
    except Exception as e:
        cache.drop("%s|%s" % (vid, nid))
        return jsonify({"ok": False, "error": repr(e)}), 502

    base = url.rsplit("/", 1)[0] + "/"
    out = []
    for line in r.text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if 'URI="' in s:
                s = re.sub(r'URI="([^"]+)"', lambda m: 'URI="%s"' % urljoin(base, m.group(1)), s)
            out.append(s)
        else:
            out.append(urljoin(base, s))
    resp = Response("\n".join(out) + "\n", mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ 数据接口
@app.route("/api/play/<vid>/<nid>")
def api_play(vid, nid):
    want = int(request.args.get("q") or 0)
    try:
        item, ttl = resolve(vid, nid, want)
    except JPError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({
        "ok": True,
        "vod_id": vid,
        "nid": nid,
        "url": item["url"],
        "quality": item["resolution"],
        "quality_name": item["resolutionName"],
        "expires_in": ttl,
        "expires_at": int(time.time()) + ttl,
    })


@app.route("/api/videos")
def api_videos():
    cat = request.args.get("cat", "")
    type_id = request.args.get("type_id", "")
    year = request.args.get("year", "")
    q = request.args.get("q", "")
    limit = min(int(request.args.get("limit", 60)), 300)
    offset = int(request.args.get("offset", 0))
    rows = db.list_videos(type_id=type_id, year=year, keyword=q, cat=cat, limit=limit, offset=offset)
    total = db.count_videos(type_id=type_id, year=year, keyword=q, cat=cat)
    return jsonify({"ok": True, "total": total, "count": len(rows), "list": rows})


@app.route("/api/video/<vid>")
def api_video(vid):
    v = db.get_video(vid)
    if not v:
        return jsonify({"ok": False, "error": "not found"}), 404
    eps = db.get_episodes(vid)
    v["episodes"] = [
        {
            "nid": e["nid"],
            "seq": e["seq"],
            "name": e["name"],
            "play": "%s/play/%s/%s.m3u8" % (config.PUBLIC_BASE, vid, e["nid"]),
        }
        for e in eps
    ]
    return jsonify({"ok": True, "video": v})


@app.route("/api/types")
def api_types():
    return jsonify({
        "ok": True,
        "cats": db.cats(),
        "years": db.years(),
    })


@app.route("/api/stats")
def api_stats():
    s = db.stats()
    s["play_cache"] = len(cache)
    s["public_base"] = config.PUBLIC_BASE
    s["job"] = job.snapshot()
    return jsonify({"ok": True, **s})


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": int(time.time())})


# ------------------------------------------------------------------ 运维
@app.route("/api/job")
def api_job():
    return jsonify({"ok": True, "job": job.snapshot()})


@app.route("/api/job/sync", methods=["POST"])
def api_job_sync():
    d = request.get_json(silent=True) or {}
    mode = str(d.get("mode") or "list")
    limit = int(d.get("limit") or 0)
    workers = int(d.get("workers") or 4)
    sleep = float(d.get("sleep") or 0.15)
    types = str(d.get("types") or "")
    if mode == "detail":
        ok, msg = job.start("同步分集", lambda: _job_sync_detail(limit, workers, sleep))
    else:
        ok, msg = job.start("同步目录", lambda: _job_sync_list(types, limit, sleep))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


@app.route("/api/job/strm", methods=["POST"])
def api_job_strm():
    d = request.get_json(silent=True) or {}
    ok, msg = job.start("导出 STRM", lambda: _job_strm(
        d.get("nfo"), d.get("images"), d.get("clean"), d.get("types"), d.get("limit")))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


if __name__ == "__main__":
    db.init_db()
    print("界面      http://127.0.0.1:%d/" % config.LISTEN_PORT)
    print("对外地址  %s（.strm 里写这个）" % config.PUBLIC_BASE)
    print("取流缓存  %d 秒，鉴权 %s" % (config.PLAY_CACHE_TTL, "开启" if config.ACCESS_TOKEN else "关闭"))
    app.run(host=config.LISTEN_HOST, port=config.LISTEN_PORT, threaded=True, debug=False)
