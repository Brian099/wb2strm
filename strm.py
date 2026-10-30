# -*- coding: utf-8 -*-
"""STRM 导出：每一集生成一个 .strm 文本文件，内容是本机取流接口地址。

.strm 里写的是 http://<你的服务>/play/<vod_id>/<nid>.m3u8，
播放器（Emby / Jellyfin / Kodi / 芝杜等）读取后会请求它，服务端 302 到最新有效的签名地址。
这样文件永远有效，不用因为链接过期而重新导出。

用法：
    python strm.py                        # 导出全部（不含 NFO/图片）
    python strm.py --nfo --images         # 同时生成 NFO 元数据和海报
    python strm.py --types 2,88 --limit 20
    python strm.py --clean                # 删除库里已经没有的 .strm
"""
import argparse
import json
import os
import re
import time

import requests
import urllib3

import config
import db
import settings

urllib3.disable_warnings()

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 供 server 的运维任务把日志接到前端
LOG_SINK = None
# 进度回调：PROGRESS(阶段, 已完成, 总数)
PROGRESS = None


def progress(phase, done, total):
    if PROGRESS:
        try:
            PROGRESS(phase, int(done), int(total))
        except Exception:
            pass


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        # Windows 控制台是 GBK 时，个别字符可能编不进去，降级处理
        try:
            print(line.encode("gbk", "replace").decode("gbk", "replace"), flush=True)
        except Exception:
            pass
    if LOG_SINK:
        try:
            LOG_SINK(line)
        except Exception:
            pass


def safe_name(name, fallback="未命名"):
    s = _BAD.sub(" ", str(name or "")).strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = fallback
    return s[:120]


def play_url(vod_id, nid):
    u = "%s/play/%s/%s.m3u8" % (settings.public_base(), vod_id, nid)
    if config.ACCESS_TOKEN:
        u += "?token=" + config.ACCESS_TOKEN
    return u


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


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


def download_image(url, path):
    if not url or os.path.exists(path):
        return False
    try:
        r = requests.get(url, timeout=30, verify=False, headers={"User-Agent": config.UA})
        if r.status_code == 200 and r.content:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except Exception:
        pass
    return False


def _check_public_base():
    """服务地址是本机回环时给出提示：写进 .strm 后别的设备播不了。"""
    base = settings.public_base()
    if not base or "127.0.0.1" in base or "localhost" in base:
        log("[警告] 服务地址是 %s，只有「本机原生运行的播放器」能访问" % (base or "(空)"))
        log("  如果 Emby / Jellyfin 也跑在 Docker 里，127.0.0.1 指向它自己的容器，会连不上。")
        log("  请到运维页改成宿主机局域网 IP（如 http://192.168.1.10:8989），"
            "再点「保存并重新生成 STRM」")


def export(root=None, types="", limit=0, nfo=False, images=False, clean=False):
    """导出 STRM。types 为逗号分隔的一级分类 id。返回统计字典。"""
    db.init_db()
    _check_public_base()
    root = os.path.abspath(root or config.STRM_ROOT)
    os.makedirs(root, exist_ok=True)
    manifest_path = os.path.join(root, "_manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            manifest = json.load(open(manifest_path, encoding="utf-8"))
        except Exception:
            manifest = {}

    want_types = [x.strip() for x in str(types or "").split(",") if x.strip()]
    videos = db.list_videos(need_detail=False, limit=limit or 10 ** 9)
    if want_types:
        videos = [v for v in videos if v["type_id"] in want_types]

    jobs = []
    for v in videos:
        eps = db.get_episodes(v["vod_id"])
        if eps:
            jobs.append((v, eps))

    total_files = written = 0
    now_manifest = {}
    used_dirs = {}
    total_jobs = len(jobs)
    progress("导出 STRM", 0, total_jobs)

    for idx, (v, eps) in enumerate(jobs, 1):
        if idx % 20 == 0 or idx == total_jobs:
            progress("导出 STRM", idx, total_jobs)
        type_dir = safe_name(v.get("cat") or v.get("type_name") or "未分类")
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

        is_movie = len(eps) <= 1
        if is_movie:
            ep = eps[0]
            url = play_url(v["vod_id"], ep["nid"])
            path = os.path.join(series_dir, title + ".strm")
            total_files += 1
            old = manifest.get(os.path.normcase(path))
            if not old or old.get("url") != url:
                write_text(path, url + "\n")
                written += 1
            now_manifest[os.path.normcase(path)] = {"vod_id": v["vod_id"], "nid": ep["nid"], "url": url}
            if nfo:
                write_text(os.path.join(series_dir, "movie.nfo"), movie_nfo(v))
            if images and v["pic"]:
                download_image(v["pic"], os.path.join(series_dir, "poster.jpg"))
                download_image(v["pic"], os.path.join(series_dir, "fanart.jpg"))
        else:
            season_dir = os.path.join(series_dir, "Season 01")
            for ep in eps:
                seq = int(ep["seq"] or 0) or 1
                url = play_url(v["vod_id"], ep["nid"])
                path = os.path.join(season_dir, "%s S01E%02d.strm" % (title, seq))
                total_files += 1
                old = manifest.get(os.path.normcase(path))
                if not old or old.get("url") != url:
                    write_text(path, url + "\n")
                    written += 1
                now_manifest[os.path.normcase(path)] = {"vod_id": v["vod_id"], "nid": ep["nid"], "url": url}
                if nfo:
                    write_text(os.path.join(season_dir, "%s S01E%02d.nfo" % (title, seq)), episode_nfo(v, ep))
            if nfo:
                write_text(os.path.join(series_dir, "tvshow.nfo"), tvshow_nfo(v))
            if images and v["pic"]:
                download_image(v["pic"], os.path.join(series_dir, "poster.jpg"))
                download_image(v["pic"], os.path.join(series_dir, "fanart.jpg"))

    removed = 0
    if clean:
        for old_path in list(manifest.keys()):
            if old_path in now_manifest:
                continue
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
                    removed += 1
            except Exception:
                pass

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(now_manifest, f, ensure_ascii=False)

    settings.set_strm_count(total_files)
    progress("导出 STRM", total_jobs, total_jobs)

    result = {"files": total_files, "written": written, "removed": removed, "root": root}
    log("导出完成：共 %d 个 .strm（新写/更新 %d 个），删除 %d 个 -> %s"
        % (total_files, written, removed, root))
    return result


def main():
    ap = argparse.ArgumentParser(description="导出 STRM")
    ap.add_argument("--root", default=config.STRM_ROOT, help="导出根目录")
    ap.add_argument("--types", default="", help="只导出指定分类 id，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="最多导出多少部")
    ap.add_argument("--nfo", action="store_true", help="生成 NFO 元数据")
    ap.add_argument("--images", action="store_true", help="下载海报/背景图")
    ap.add_argument("--clean", action="store_true", help="删除库里已不存在的 .strm")
    args = ap.parse_args()
    export(args.root, args.types, args.limit, args.nfo, args.images, args.clean)


if __name__ == "__main__":
    main()
