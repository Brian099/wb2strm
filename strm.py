# -*- coding: utf-8 -*-
"""STRM 导出：每一集生成一个 .strm 文本文件，内容是本机取流接口地址。

.strm 里写的是 <服务地址>/relay/<vod_id>/<nid>.m3u8（或 /play/…，取决于 JP_RELAY），
播放器读取后请求本服务，由本服务去换最新有效的签名地址。

性能要点（几十万集时很关键）：
  * 分集一次性查出（episodes_map），不再逐部查询
  * 目录只创建一次（缓存已建目录），避免每个文件都 makedirs
  * 海报并行下载，只存 poster.jpg（站点没有横版背景图，不做假 fanart）
  * 每集单独的 .nfo 默认关闭（每部剧 1 个 tvshow.nfo 就够 Emby 用）
  * manifest 只存「路径 -> 地址」，几十万条也不会吃掉太多内存

用法：
    python strm.py                        # 导出全部
    python strm.py --nfo --images         # 生成 NFO 与海报
    python strm.py --types 2,88 --limit 20
    python strm.py --clean                # 删除库里已经没有的 .strm
"""
import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3

import config
import db
import settings
import subcat

urllib3.disable_warnings()

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 供 server 的运维任务把日志接到前端
LOG_SINK = None
# 进度回调：PROGRESS(阶段, 已完成, 总数)
PROGRESS = None


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode("gbk", "replace").decode("gbk", "replace"), flush=True)
        except Exception:
            pass
    if LOG_SINK:
        try:
            LOG_SINK(line)
        except Exception:
            pass


def progress(phase, done, total):
    if PROGRESS:
        try:
            PROGRESS(phase, int(done), int(total))
        except Exception:
            pass


def safe_name(name, fallback="未命名"):
    s = _BAD.sub(" ", str(name or "")).strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = fallback
    return s[:120]


def play_url(vod_id, nid):
    # 统一写 /play：内网直连还是外网中继由服务端按客户端 IP 决定，
    # 以后切换模式不需要重新导出 .strm
    u = "%s/play/%s/%s.m3u8" % (settings.public_base(), vod_id, nid)
    if config.ACCESS_TOKEN:
        u += "?token=" + config.ACCESS_TOKEN
    return u


def xml_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def movie_nfo(v):
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>%s</title>
  <originaltitle>%s</originaltitle>
  <year>%s</year>
  <plot>%s</plot>
  <runtime>0</runtime>
  <actor><name>%s</name></actor>
  <director>%s</director>
  <genre>%s</genre>
  <uniqueid type="wb2strm" default="true">%s</uniqueid>
</movie>
""" % (xml_escape(v["name"]), xml_escape(v["name"]), xml_escape(v["year"]),
       xml_escape(v["content"]), xml_escape(v["actor"]), xml_escape(v["director"]),
       xml_escape(v["type_name"]), xml_escape(v["vod_id"]))


def tvshow_nfo(v):
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>%s</title>
  <year>%s</year>
  <plot>%s</plot>
  <actor><name>%s</name></actor>
  <genre>%s</genre>
  <uniqueid type="wb2strm" default="true">%s</uniqueid>
</tvshow>
""" % (xml_escape(v["name"]), xml_escape(v["year"]), xml_escape(v["content"]),
       xml_escape(v["actor"]), xml_escape(v["type_name"]), xml_escape(v["vod_id"]))


def episode_nfo(v, ep):
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>%s</title>
  <season>1</season>
  <episode>%d</episode>
  <plot>%s</plot>
</episodedetails>
""" % (xml_escape(ep["name"] or ("第%d集" % ep["seq"])), int(ep["seq"] or 0), xml_escape(v["content"]))


def _check_public_base():
    base = settings.public_base()
    if not base or "127.0.0.1" in base or "localhost" in base:
        log("[警告] 服务地址是 %s，只有「本机原生运行的播放器」能访问" % (base or "(空)"))
        log("  如果 Emby / Jellyfin 也跑在 Docker 里，127.0.0.1 指向它自己的容器，会连不上。")
        log("  请到运维页改成宿主机局域网 IP（如 http://192.168.1.10:8989），"
            "再点「保存并重新生成 STRM」")


class _Writer:
    """小工具：目录只建一次，文件写入统一走这里。"""

    def __init__(self):
        self._dirs = set()
        self.files = 0

    def ensure_dir(self, path):
        d = os.path.dirname(path)
        if d and d not in self._dirs:
            os.makedirs(d, exist_ok=True)
            self._dirs.add(d)

    def write(self, path, text):
        """写文件。内容一模一样就不写 —— 避免改 mtime 让 Emby 以为元数据变了。

        NFO 是每次都重新生成的（没做 manifest 判断），如果每次都覆盖，
        Emby 会把整库几千个条目重新扫一遍，白忙一场。
        """
        self.ensure_dir(path)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    self.files += 1
                    return False
        except FileNotFoundError:
            pass
        except Exception:
            pass
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.files += 1
        return True


def _download_images(items, workers=8):
    """items: [(url, series_dir)]。只下载竖版封面 poster.jpg，并行执行。"""
    if not items:
        return 0

    def one(item):
        url, d = item
        p1 = os.path.join(d, "poster.jpg")
        # 站点只提供一张竖版封面（270×360），没有横版背景图。
        # 硬把竖图当 fanart 会被 Emby 拉伸裁切成 16:9，很难看，所以只存 poster，
        # 背景交给 Emby/Jellyfin 自己用主图生成。
        if os.path.exists(p1):
            return 0
        try:
            r = requests.get(url, timeout=20, verify=False,
                             headers={"User-Agent": config.UA, "Referer": config.HOST + "/"})
            if r.status_code != 200 or not r.content:
                return 0
            os.makedirs(d, exist_ok=True)
            with open(p1, "wb") as f:
                f.write(r.content)
            return 1
        except Exception:
            return 0

    done = 0
    total = len(items)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, n in enumerate(pool.map(one, items), 1):
            done += n
            if i % 200 == 0 or i == total:
                progress("下载海报", i, total)
    return done


def export(root=None, types="", limit=0, nfo=None, images=None, clean=False, ep_nfo=None):
    """导出 STRM。返回统计字典。"""
    t_start = time.time()
    db.init_db()
    _check_public_base()

    nfo = bool(nfo)
    ep_nfo = bool(ep_nfo)
    images = bool(images)

    root = os.path.abspath(root or config.STRM_ROOT)
    os.makedirs(root, exist_ok=True)
    manifest_path = os.path.join(root, "_manifest.json")

    # ---------- 1. 读库（一次性） ----------
    t = time.time()
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                raw = json.load(f)
            for k, val in raw.items():
                # 兼容旧格式（值曾经是 dict）
                manifest[k] = val.get("url", "") if isinstance(val, dict) else val
        except Exception:
            manifest = {}

    want_types = [x.strip() for x in str(types or "").split(",") if x.strip()]
    videos = db.list_videos(need_detail=False, limit=limit or 10 ** 9)
    if want_types:
        videos = [v for v in videos if v["type_id"] in want_types]
    have_eps = db.vod_ids_with_episodes()
    videos = [v for v in videos if v["vod_id"] in have_eps]
    # 与 iter_episodes 保持同样的排序（TEXT 字典序），便于流式合并
    videos.sort(key=lambda x: str(x["vod_id"]))
    total_jobs = len(videos)
    log("读取完成：%d 部剧（分集按需流式读取），耗时 %.1fs" % (total_jobs, time.time() - t))

    # ---------- 2. 写文件 ----------
    t = time.time()
    w = _Writer()
    now_manifest = {}
    used_dirs = {}
    pending_img = []
    total_files = written = 0
    progress("导出 STRM", 0, total_jobs)

    ep_iter = db.iter_episodes()
    cur_vid, cur_eps = next(ep_iter, (None, []))

    for idx, v in enumerate(videos, 1):
        if idx % 50 == 0 or idx == total_jobs:
            progress("导出 STRM", idx, total_jobs)

        # 流式对齐：跳过比当前剧更小的分集组
        while cur_vid is not None and cur_vid < str(v["vod_id"]):
            cur_vid, cur_eps = next(ep_iter, (None, []))
        eps = cur_eps if cur_vid == str(v["vod_id"]) else []
        if not eps:
            continue

        type_dir = safe_name(v.get("cat") or v.get("type_name") or "未分类")
        # 电视剧按地区分（国产/欧美/港台/日韩/其他剧）、动漫按语言分（国语/日语/英语/其他），
        # 电影、短剧、综艺不分
        sub_name = subcat.for_video(v.get("cat"), v.get("area"), v.get("lang"))
        if sub_name:
            type_dir = os.path.join(type_dir, safe_name(sub_name))
        title = safe_name(v["name"] or v["vod_id"])
        year = safe_name(v["year"] or "")
        folder = "%s (%s)" % (title, year) if year else title
        series_dir = os.path.join(root, type_dir, folder)

        # 同名同年的不同资源，用 vod_id 区分，避免互相覆盖
        key = os.path.normcase(series_dir)
        if key in used_dirs and used_dirs[key] != v["vod_id"]:
            series_dir = "%s [%s]" % (series_dir, v["vod_id"])
            key = os.path.normcase(series_dir)
        used_dirs[key] = v["vod_id"]

        if len(eps) <= 1:
            ep = eps[0]
            url = play_url(v["vod_id"], ep["nid"])
            path = os.path.join(series_dir, title + ".strm")
            total_files += 1
            # 不能只比对地址：文件被手动删掉 / 目录被清空时，manifest 里还留着旧记录，
            # 只比地址会误判成「没变化」而跳过，结果磁盘上什么都没有。
            if manifest.get(os.path.normcase(path)) != url or not os.path.exists(path):
                w.write(path, url + "\n")
                written += 1
            now_manifest[os.path.normcase(path)] = url
            if nfo:
                w.write(os.path.join(series_dir, "movie.nfo"), movie_nfo(v))
        else:
            season_dir = os.path.join(series_dir, "Season 01")
            for ep in eps:
                seq = int(ep["seq"] or 0) or 1
                url = play_url(v["vod_id"], ep["nid"])
                path = os.path.join(season_dir, "%s S01E%02d.strm" % (title, seq))
                total_files += 1
                if manifest.get(os.path.normcase(path)) != url or not os.path.exists(path):
                    w.write(path, url + "\n")
                    written += 1
                now_manifest[os.path.normcase(path)] = url
                if ep_nfo:
                    w.write(os.path.join(season_dir, "%s S01E%02d.nfo" % (title, seq)),
                            episode_nfo(v, ep))
            if nfo:
                w.write(os.path.join(series_dir, "tvshow.nfo"), tvshow_nfo(v))

        if images and v["pic"]:
            pending_img.append((v["pic"], series_dir))

    log("写入 .strm：%d 个（新写/更新 %d 个，未变的跳过），耗时 %.1fs"
        % (total_files, written, time.time() - t))

    # ---------- 3. 下载海报（并行） ----------
    if pending_img:
        t = time.time()
        log("下载海报：%d 部剧，并发 %d" % (len(pending_img), config.IMAGE_WORKERS))
        got = _download_images(pending_img, workers=config.IMAGE_WORKERS)
        log("海报完成：新下载 %d 张，耗时 %.1fs" % (got, time.time() - t))

    # ---------- 4. 清理孤儿 ----------
    removed = 0
    pruned = 0
    if clean:
        t = time.time()
        # 全量对账：磁盘上凡是不在本次清单里的 .strm 一律删掉。
        # 比「只删 manifest 里记过的」可靠 —— manifest 被覆盖过 / 手工拷进来的
        # 旧文件也能清掉（换目录结构后这类残留最多）。
        for d, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(".strm"):
                    continue
                fp = os.path.join(d, f)
                if os.path.normcase(fp) in now_manifest:
                    continue
                try:
                    os.remove(fp)
                    removed += 1
                except Exception:
                    continue
                # 同名的 .nfo 一并删掉，别留下孤立元数据
                nfo_path = os.path.splitext(fp)[0] + ".nfo"
                try:
                    if os.path.exists(nfo_path):
                        os.remove(nfo_path)
                except Exception:
                    pass

        # 删掉「整棵子树里没有任何 .strm」的目录（自底向上）。
        # 换过目录结构（比如加了二级分类）之后，旧目录里往往只剩
        # poster.jpg / tvshow.nfo / 每集 .nfo，不是空目录但也已经没用了，
        # 一并清掉，否则 Emby 里会出现重复条目。
        for d, _dirs, files in os.walk(root, topdown=False):
            if os.path.abspath(d) == os.path.abspath(root):
                continue
            if any(f.lower().endswith(".strm") for f in files):
                continue
            try:
                rest = os.listdir(d)
            except Exception:
                continue
            if any(os.path.isdir(os.path.join(d, x)) for x in rest):
                continue          # 还有子目录（子树里有 strm），保留
            try:
                for x in rest:
                    fp = os.path.join(d, x)
                    if os.path.isfile(fp):
                        os.remove(fp)
                os.rmdir(d)
                pruned += 1
            except Exception:
                pass

        if removed or pruned:
            log("清理孤儿：%d 个 .strm、%d 个废弃目录，耗时 %.1fs"
                % (removed, pruned, time.time() - t))

    # ---------- 5. 写 manifest ----------
    t = time.time()
    try:
        tmp = manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(now_manifest, f, ensure_ascii=False)
        os.replace(tmp, manifest_path)
    except Exception as e:
        log("[警告] manifest 写入失败：%r" % (e,))
    log("manifest：%d 条，耗时 %.1fs" % (len(now_manifest), time.time() - t))

    settings.set_strm_count(total_files)
    progress("导出 STRM", total_jobs, total_jobs)

    result = {"files": total_files, "written": written, "removed": removed, "root": root}
    log("导出完成：共 %d 个 .strm（新写/更新 %d 个），删除 %d 个，总耗时 %.1fs -> %s"
        % (total_files, written, removed, time.time() - t_start, root))
    return result


def main():
    ap = argparse.ArgumentParser(description="导出 STRM")
    ap.add_argument("--root", default=config.STRM_ROOT, help="导出根目录")
    ap.add_argument("--types", default="", help="只导出指定分类 id，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="最多导出多少部")
    ap.add_argument("--nfo", action="store_true", help="生成 tvshow/movie NFO")
    ap.add_argument("--ep-nfo", action="store_true", help="每集也生成 .nfo（很慢）")
    ap.add_argument("--images", action="store_true", help="下载海报/背景图")
    ap.add_argument("--clean", action="store_true", help="删除库里已不存在的 .strm")
    args = ap.parse_args()
    export(args.root, args.types, args.limit, args.nfo, args.images, args.clean, args.ep_nfo)


if __name__ == "__main__":
    main()
