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
import base64
import collections
import ipaddress
import hashlib
import hmac
import os
import re
import threading
import time
from urllib.parse import urljoin

import requests
import urllib3
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, stream_with_context

import config
import db
import maccms
import settings
import strm
import sync
from jpapi import JPApi, JPError

urllib3.disable_warnings()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
maccms.register(app)   # 苹果CMS 兼容接口 /api.php/provide/vod

_api_lock = threading.Lock()
_api_host = ""
api = None      # 延迟创建：模块导入时不碰环境变量，缺配置由启动自检统一报错


def get_api():
    """按当前配置创建/复用接口客户端（站点地址变了会自动重建）。"""
    global api, _api_host
    host = settings.source_host()
    if host != _api_host:
        with _api_lock:
            if host != _api_host:
                api = JPApi(host)
                _api_host = host
    return api


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
        urls = get_api().episode_urls(vid, nid)
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
        self.progress = {"phase": "", "done": 0, "total": 0}

    def add(self, line):
        with self.lock:
            self.logs.append(str(line))

    def set_progress(self, phase, done, total):
        with self.lock:
            self.progress = {"phase": str(phase), "done": int(done), "total": int(total)}

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
            self.progress = {"phase": "", "done": 0, "total": 0}

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
                "progress": dict(self.progress),
            }


job = JobState()
sync.LOG_SINK = job.add
strm.LOG_SINK = job.add
sync.PROGRESS = job.set_progress
strm.PROGRESS = job.set_progress


def _job_sync_list(types, limit, sleep, force=False, pages=0):
    a = get_api()
    all_types = a.types()
    want = [x.strip() for x in str(types or "").split(",") if x.strip()]
    sel = [t for t in all_types if not want or t["type_id"] in want]
    filters = a.filters()
    years = sync._filter_values(filters, "yearList")
    areas = sync._filter_values(filters, "districtList")
    sync.log("分类：%s" % ", ".join("%s(%s)" % (t["type_name"], t["type_id"]) for t in sel))
    sync.log("年份切片 %d 个，地区切片 %d 个" % (len(years), len(areas)))
    if force:
        sync.log("增量模式：忽略断点重扫%s" % ("，每个切片最多 %d 页" % int(pages) if pages else ""))
    sync.sync_list(a, sel, years, areas, limit=int(limit or 0), sleep=float(sleep or 0.15),
                   force=bool(force), max_pages=int(pages or 0))
    s = db.stats()
    sync.log("列表同步结束：剧集 %d 部" % s["videos"])


def _job_sync_detail(limit, workers, sleep, changed=False):
    a = get_api()
    sync.sync_detail(a, limit=int(limit or 0), workers=int(workers or 4), sleep=float(sleep or 0.1),
                     changed=bool(changed))
    s = db.stats()
    sync.log("详情同步结束：%d 部有分集，共 %d 条分集" % (s["videos_with_episodes"], s["episodes"]))


def _job_strm(nfo, images, clean, types, limit, ep_nfo=False):
    strm.export(None, types or "", int(limit or 0), bool(nfo), bool(images), bool(clean), bool(ep_nfo))


def _job_full(types="", pages=None, workers=None, limit=0, export=True, clean=False):
    """全流程：① 同步目录 -> ② 同步分集 -> ③ 导出 STRM

    空库时自动全量（扫全部页），有数据时走增量（每片 config.PAGES 页）。
    """
    st0 = db.stats()
    full_mode = bool(config.FORCE_FULL) or not settings.catalog_complete()   # 未扫全 或 强制全量
    if pages is None:
        pages = 0 if full_mode else config.PAGES
    if workers is None:
        workers = config.WORKERS

    try:
        a = get_api()
        all_types = a.types()
        want = [x.strip() for x in str(types or "").split(",") if x.strip()]
        sel = [t for t in all_types if not want or t["type_id"] in want]
        filters = a.filters()
        years = sync._filter_values(filters, "yearList")
        areas = sync._filter_values(filters, "districtList")

        sync.log("站点：%s" % settings.source_host())
        if full_mode:
            why = "JP_FORCE_FULL=1 强制全量" if config.FORCE_FULL else "目录尚未完整扫过"
            sync.log("① 同步目录：%s，全量抓取（断点续爬，已有的页会跳过）" % why)
        else:
            sync.log("① 同步目录：增量，每片最多 %s 页" % (pages or "全部"))
        res = sync.sync_list(a, sel, years, areas, limit=int(limit or 0),
                             sleep=config.LIST_SLEEP,
                             force=not full_mode, max_pages=int(pages or 0))
        if full_mode:
            if res.get("complete"):
                settings.set_catalog_complete(True)
                sync.log("   目录首次全量完成（共 %d 页），之后转入增量模式" % res.get("pages", 0))
            else:
                sync.log("   目录仍未扫全：失败 %d 页，下次启动会继续补" % res.get("failed", 0))

        sync.log("② 同步分集：新增 + 状态变化，并发 %s" % workers)
        sync.sync_detail(a, limit=0, workers=int(workers or config.WORKERS),
                         sleep=config.DETAIL_SLEEP, changed=True)

        if export:
            sync.log("③ 导出 STRM + NFO + 海报（已存在的文件会跳过）")
            # 自动流程一并生成 NFO 与海报：交给 Emby 自己刮削更慢、还不一定准
            # clean 清掉库里已不存在的 .strm，保证磁盘数量与库一致
            strm.export(None, types or "", 0, True, True, True)

        st = db.stats()
        sync.log("全流程完成：剧集 %d 部（%d 部有分集），分集 %d 条"
                 % (st["videos"], st["videos_with_episodes"], st["episodes"]))
        job.set_progress("空闲", 0, 0)
        settings.mark_finished()
    except Exception as e:
        sync.log("[错误] 全流程失败：%r" % (e,))
        # 失败后 30 分钟再重试（而不是等满一个间隔）
        db.set_setting("last_finish",
                       str(int(time.time() - config.INTERVAL_HOURS * 3600 + 1800)))
        settings.invalidate()
        raise


class Scheduler(threading.Thread):
    """开机跑一次，之后每隔 config.INTERVAL_HOURS 小时（从上一次跑完算起）再跑一次。"""

    def __init__(self):
        super().__init__(daemon=True, name="auto-sync")
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def next_run_at(self):
        last = settings.last_finish()
        if not last:
            return int(time.time()) + 15 if config.BOOTSTRAP else 0
        return int(last + config.INTERVAL_HOURS * 3600)

    def run(self):
        self._stop.wait(10)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                sync.log("[自动更新] 调度异常：%r" % (e,))
            self._stop.wait(60)

    def _tick(self):
        if job.running:
            return
        last = settings.last_finish()
        if not last and not config.BOOTSTRAP:
            return
        if last and time.time() - last < config.INTERVAL_HOURS * 3600:
            return
        if not last:
            sync.log("[自动更新] 首次启动，开始抓取")
        else:
            sync.log("[自动更新] 距上次完成已满 %g 小时，开始增量更新" % config.INTERVAL_HOURS)
        job.start("自动更新", lambda: _job_full())


scheduler = Scheduler()


# ------------------------------------------------------------------ 客户端识别
def _client_ip():
    """取真实客户端 IP：优先反代头，其次 remote_addr。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        ip = xff.split(",")[0].strip()
        if ip:
            return ip
    ip = request.headers.get("X-Real-IP", "").strip()
    if ip:
        return ip
    return (request.remote_addr or "").strip()


def _is_lan_ip(ip):
    """内网/回环/链路本地地址 -> True。"""
    if not ip:
        return False
    raw = ip.split("%")[0]
    try:
        a = ipaddress.ip_address(raw)
    except Exception:
        return False
    return bool(a.is_private or a.is_loopback or a.is_link_local)


def _use_direct():
    """当前请求该走直连（302）还是中继。"""
    mode = config.PLAY_MODE
    if mode == "direct":
        return True
    if mode == "relay":
        return False
    return _is_lan_ip(_client_ip())      # auto


# ------------------------------------------------------------------ 鉴权
# 静态资源不需要令牌，否则设了 JP_TOKEN 后界面加载不出来
# /api.php 与 /pic 有各自的 key 校验（JP_API_KEY），不走界面令牌
_OPEN_PREFIX = ("/ui", "/app.js", "/style.css", "/hls.min.js", "/favicon.ico", "/api.php", "/pic")


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
    # 统计/任务接口必须实时，禁止浏览器缓存
    if request.path.startswith("/api/") or request.path == "/health":
        resp.headers["Cache-Control"] = "no-store, max-age=0"
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


@app.route("/hls.min.js")
def ui_hls():
    return send_from_directory(WEB_DIR, "hls.min.js")


# ------------------------------------------------------------------ 播放
@app.route("/play/<vid>/<nid>.m3u8")
@app.route("/play/<vid>/<nid>")
def play(vid, nid):
    """内网客户端 302 直连 CDN（省带宽），外网客户端走中继。"""
    if not _use_direct():
        return _relay_playlist(vid, nid, int(request.args.get("q") or 0))
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


# ------------------------------------------------------------------ 中继（推荐播放方式）
# CDN 的 auth_key 签名与「请求方 IP」绑定：签名是本服务去要的，所以只有本服务
# 自己（同一出口 IP）能拉。别的设备直连必然 403，必须由本服务转发。


def _seg_key():
    return (config.APP_KEY or "wb2strm").encode("utf-8")


def _seg_token(url):
    raw = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_seg_key(), url.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return raw + "." + sig


def _seg_url(token):
    try:
        raw, sig = token.rsplit(".", 1)
        url = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        want = hmac.new(_seg_key(), url.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, want):
            return ""
        if not url.lower().startswith("http"):
            return ""
        return url
    except Exception:
        return ""


def _relay_link(url):
    return "/relay/seg?u=" + _seg_token(url)


def _relay_playlist(vid, nid, want=0):
    """把 m3u8 里的分片地址改写成本服务的中继地址，客户端全程只跟本服务打交道。"""
    try:
        item, _ = resolve(vid, nid, want)
    except JPError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not item:
        return jsonify({"ok": False, "error": "no url"}), 502

    src = item["url"]
    try:
        r = requests.get(src, timeout=25, verify=False,
                         headers={"User-Agent": config.UA, "Referer": config.HOST + "/"})
        if r.status_code != 200:
            cache.drop("%s|%s" % (vid, nid))
            return jsonify({"ok": False, "error": "upstream %s" % r.status_code}), 502
    except Exception as e:
        cache.drop("%s|%s" % (vid, nid))
        return jsonify({"ok": False, "error": repr(e)}), 502

    base = src.rsplit("/", 1)[0] + "/"
    out = []
    for line in r.text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if 'URI="' in s:
                s = re.sub(r'URI="([^"]+)"',
                           lambda m: 'URI="%s"' % _relay_link(urljoin(base, m.group(1))), s)
            out.append(s)
        else:
            out.append(_relay_link(urljoin(base, s)))
    resp = Response("\n".join(out) + "\n", mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/relay/<vid>/<nid>.m3u8")
@app.route("/relay/<vid>/<nid>")
def relay_playlist(vid, nid):
    """强制中继（老 .strm 里的地址仍可用）。"""
    return _relay_playlist(vid, nid, int(request.args.get("q") or 0))


@app.route("/relay/seg")
def relay_segment():
    """分片中继：按签名取回原始分片地址，由本服务转发。支持 Range。"""
    url = _seg_url(request.args.get("u", ""))
    if not url:
        return jsonify({"ok": False, "error": "bad token"}), 400
    headers = {"User-Agent": config.UA, "Referer": config.HOST + "/"}
    rng = request.headers.get("Range")
    if rng:
        headers["Range"] = rng
    try:
        r = requests.get(url, timeout=40, verify=False, headers=headers, stream=True)
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 502
    if r.status_code not in (200, 206):
        return jsonify({"ok": False, "error": "upstream %s" % r.status_code}), 502

    resp = Response(stream_with_context(r.iter_content(65536)), status=r.status_code)
    resp.headers["Content-Type"] = r.headers.get("Content-Type", "video/mp2t")
    for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if h in r.headers:
            resp.headers[h] = r.headers[h]
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
            "play": "%s/play/%s/%s.m3u8" % (settings.public_base(), vid, e["nid"]),
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


@app.route("/api/whoami")
def api_whoami():
    """自检：服务端看到的你的 IP、是否内网、当前会走直连还是中继。"""
    ip = _client_ip()
    lan = _is_lan_ip(ip)
    direct = _use_direct()
    return jsonify({
        "ok": True,
        "client_ip": ip,
        "is_lan": lan,
        "play_mode": config.PLAY_MODE,
        "decision": "direct(302直连)" if direct else "relay(中继转发)",
        "x_forwarded_for": request.headers.get("X-Forwarded-For", ""),
        "x_real_ip": request.headers.get("X-Real-IP", ""),
        "remote_addr": request.remote_addr,
    })


@app.route("/api/stats")
def api_stats():
    s = db.stats()
    s["play_cache"] = len(cache)
    s["public_base"] = settings.public_base()
    s["source_host"] = settings.source_host()
    s["job"] = job.snapshot()
    s["strm_count"] = settings.strm_count()
    s["catalog_complete"] = settings.catalog_complete()
    s["relay"] = bool(config.RELAY)
    s["play_mode"] = config.PLAY_MODE
    s["last_finish"] = settings.last_finish()
    s["next_run"] = scheduler.next_run_at()
    s["interval_hours"] = config.INTERVAL_HOURS
    s["pages"] = config.PAGES
    s["workers"] = config.WORKERS
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
        changed = bool(d.get("changed"))
        ok, msg = job.start("同步分集", lambda: _job_sync_detail(limit, workers, sleep, changed))
    else:
        force = bool(d.get("force"))
        pages = int(d.get("pages") or 0)
        ok, msg = job.start("同步目录", lambda: _job_sync_list(types, limit, sleep, force, pages))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


@app.route("/api/job/all", methods=["POST"])
def api_job_all():
    d = request.get_json(silent=True) or {}
    ok, msg = job.start("一键全流程", lambda: _job_full(
        types=str(d.get("types") or ""),
        pages=int(d.get("pages") or 0),
        workers=int(d.get("workers") or 3),
        limit=int(d.get("limit") or 0),
        export=bool(d.get("export", True)),
        clean=bool(d.get("clean", False)),
    ))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


def _cms_url():
    u = "%s/api.php/provide/vod" % settings.public_base()
    k = settings.api_key()
    return (u + "?key=" + k) if k else u


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({
        "ok": True,
        "settings": {"public_base": settings.public_base(), "api_key": settings.api_key()},
        "cms_url": _cms_url(),
        "play_mode": config.PLAY_MODE,
        "next_run": scheduler.next_run_at(),
        "interval_hours": config.INTERVAL_HOURS,
        "last_finish": settings.last_finish(),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    d = request.get_json(silent=True) or {}
    pairs = {}
    if "public_base" in d:
        u = str(d.get("public_base") or "").strip().rstrip("/")
        if u and not u.startswith("http"):
            u = "http://" + u
        pairs["public_base"] = u
    if "api_key" in d:
        pairs["api_key"] = str(d.get("api_key") or "").strip()
    settings.set_many(pairs)
    cache.ttl = config.PLAY_CACHE_TTL

    regen = bool(d.get("regen_strm"))
    if regen:
        nfo = bool(d.get("nfo"))
        images = bool(d.get("images"))
        ep_nfo = bool(d.get("ep_nfo"))
        job.start("重新导出 STRM", lambda: _job_strm(nfo, images, False, "", 0, ep_nfo))
    return jsonify({
        "ok": True,
        "settings": {"public_base": settings.public_base(), "api_key": settings.api_key()},
        "cms_url": _cms_url(),
        "regen": regen,
        "msg": "已保存" + ("，正在重新导出 STRM" if regen else ""),
    })


@app.route("/api/job/strm", methods=["POST"])
def api_job_strm():
    d = request.get_json(silent=True) or {}
    ok, msg = job.start("导出 STRM", lambda: _job_strm(
        d.get("nfo"), d.get("images"), d.get("clean"), d.get("types"), d.get("limit")))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)


if __name__ == "__main__":
    if not config.check_required():
        raise SystemExit(1)
    db.init_db()
    scheduler.start()
    print("界面      http://127.0.0.1:%d/" % config.LISTEN_PORT)
    print("对外地址  %s（.strm 里写这个）" % config.PUBLIC_BASE)
    print("取流缓存  %d 秒，鉴权 %s" % (config.PLAY_CACHE_TTL, "开启" if config.ACCESS_TOKEN else "关闭"))
    app.run(host=config.LISTEN_HOST, port=config.LISTEN_PORT, threaded=True, debug=False)
