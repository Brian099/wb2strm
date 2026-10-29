# -*- coding: utf-8 -*-
"""同步任务：把金牌影视的「剧集目录 + 分集清单」抓进本地 SQLite。

只存 vod_id / nid，不存播放地址（地址是短时效签名，由 server 现取）。

用法：
    python sync.py stats
    python sync.py list                          # 抓全部分类的列表（自动按年份切片绕开 10000 上限）
    python sync.py list --types 2,88 --limit 60  # 只抓两个分类各 60 条，试跑用
    python sync.py detail --workers 4            # 为所有缺分集的剧补详情（慢，约 2.4h / 4 并发）
    python sync.py detail --limit 100            # 只补 100 部
    python sync.py all --workers 4               # list + detail 一条龙
"""
import argparse
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
from jpapi import JPApi, JPError

_lock = threading.Lock()
_stat = {"list_ok": 0, "detail_ok": 0, "fail": 0, "episodes": 0}

_TAG = re.compile(r"<[^>]*>")

# 供 server 的运维任务把日志接到前端
LOG_SINK = None


def clean(text):
    """去掉简介里的 HTML 标签。"""
    return re.sub(r"\s+", " ", _TAG.sub(" ", str(text or ""))).strip()


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if LOG_SINK:
        try:
            LOG_SINK(line)
        except Exception:
            pass


# ------------------------------------------------------------------ 列表
def iter_slices(api, tid, years, areas):
    """把一个分类切成若干"不超过 10000 条"的查询切片。"""
    total = api.video_count(tid)
    if total < config.TOTAL_CAP:
        yield {"year": "", "area": ""}, total
        return
    for y in years:
        n = api.video_count(tid, year=y)
        if n < config.TOTAL_CAP:
            yield {"year": y, "area": ""}, n
        else:
            for a in areas:
                n2 = api.video_count(tid, year=y, area=a)
                yield {"year": y, "area": a}, n2


def to_video(item, tid, type_name):
    pubdate = str(item.get("vodPubdate") or "")
    return {
        "vod_id": str(item.get("vodId") or ""),
        "name": str(item.get("vodName") or "").strip(),
        "cat": str(type_name or "").strip(),
        "type_id": str(item.get("typeId1") or tid),
        "type_name": str(item.get("vodClass") or type_name or "").strip(),
        "year": pubdate[:4],
        "area": str(item.get("vodArea") or "").strip(),
        "lang": "",
        "pic": str(item.get("vodPic") or "").strip(),
        "remarks": str(item.get("vodRemarks") or "").strip(),
        "actor": str(item.get("vodActor") or "").strip(),
        "director": "",
        "content": clean(item.get("vodBlurb")),
        "episode_count": int(item.get("vodTotal") or 0),
    }


def sync_list(api, types, years, areas, limit=0, sleep=0.15, force=False, max_pages=0):
    done = 0
    for t in types:
        tid, tname = t["type_id"], t["type_name"]
        try:
            slices = list(iter_slices(api, tid, years, areas))
        except JPError as e:
            log("分类 %s(%s) 取数量失败: %s" % (tname, tid, e))
            continue
        log("分类 %s(%s) 切成 %d 个查询切片" % (tname, tid, len(slices)))
        for kw, total in slices:
            pages = int(math.ceil(total / float(config.PAGE_SIZE))) if total else 0
            if max_pages and max_pages > 0:
                pages = min(pages, max_pages)
            for pg in range(1, pages + 1):
                if limit and done >= limit:
                    log("达到 --limit %d，停止列表同步" % limit)
                    return done
                key = "list:%s:%s:%s:%s" % (tid, kw.get("year"), kw.get("area"), pg)
                if not force and db.get_state(key) == "1":
                    continue
                try:
                    d = api.video_list(tid, page=pg, **kw)
                except JPError as e:
                    log("  列表失败 %s p%d: %s" % (tid, pg, e))
                    _stat["fail"] += 1
                    continue
                rows = [to_video(it, tid, tname) for it in (d.get("list") or []) if it.get("vodId")]
                for r in rows:
                    db.upsert_video(r, detail=False)
                db.commit()
                db.set_state(key, "1")
                done += len(rows)
                _stat["list_ok"] += len(rows)
                if rows:
                    log("  %s(%s) %s p%d -> %d 条" % (tname, tid, kw or "全部", pg, len(rows)))
                time.sleep(sleep)
    return done


# ------------------------------------------------------------------ 详情
def fetch_detail(api, vod_id, sleep=0.15):
    try:
        d = api.video_detail(vod_id)
    except JPError as e:
        return vod_id, None, str(e)
    eps = []
    for i, e in enumerate(d.get("episodeList") or []):
        nid = str(e.get("nid") or "").strip()
        if not nid:
            continue
        seq = int(e.get("sort") or 0) or (i + 1)
        eps.append({"nid": nid, "seq": seq, "name": str(e.get("name") or "").strip()})
    video = {
        "vod_id": str(d.get("vodId") or vod_id),
        "name": str(d.get("vodName") or "").strip(),
        "type_id": str(d.get("typeId1") or ""),
        "type_name": str(d.get("typeName") or "").strip(),
        "year": str(d.get("vodYear") or "")[:4] or str(d.get("vodPubdate") or "")[:4],
        "area": str(d.get("vodArea") or "").strip(),
        "lang": str(d.get("vodLang") or "").strip(),
        "pic": str(d.get("vodPic") or "").strip(),
        "remarks": str(d.get("vodRemarks") or "").strip(),
        "actor": str(d.get("vodActor") or "").strip(),
        "director": str(d.get("vodDirector") or "").strip(),
        "content": clean(d.get("vodContent")),
        "episode_count": len(eps) or int(d.get("vodTotal") or 0),
    }
    if sleep:
        time.sleep(sleep)
    return vod_id, (video, eps), ""


def sync_detail(api, limit=0, workers=4, sleep=0.1, changed=False):
    if changed:
        todo = db.list_videos(changed=True, limit=limit or 10 ** 9)
        log("待更新详情的剧集（新增 + 状态变化）：%d 部，并发 %d" % (len(todo), workers))
    else:
        todo = db.list_videos(need_detail=True, limit=limit or 10 ** 9)
        log("待补详情的剧集（仅从未抓过的）：%d 部，并发 %d" % (len(todo), workers))
    if not todo:
        return 0
    n = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(fetch_detail, api, v["vod_id"], sleep): v for v in todo}
        for fut in as_completed(futs):
            vod_id, data, err = fut.result()
            n += 1
            if err or not data:
                _stat["fail"] += 1
                log("  详情失败 %s: %s" % (vod_id, err))
                continue
            video, eps = data
            db.upsert_video(video, detail=True)
            db.replace_episodes(vod_id, eps)
            db.commit()
            _stat["detail_ok"] += 1
            _stat["episodes"] += len(eps)
            if n % 20 == 0 or n == len(todo):
                log("  进度 %d/%d，已入库分集 %d" % (n, len(todo), _stat["episodes"]))
    return n


# ------------------------------------------------------------------ 入口
def _filter_values(filters, key):
    """从 /v1/get/filer/list 的返回里取某一组筛选项的取值（接口用 itemText 作参数值）。"""
    for tid, groups in (filters or {}).items():
        if not isinstance(groups, dict):
            continue
        vals = [str(i.get("itemText") or "").strip() for i in (groups.get(key) or []) if i.get("itemText")]
        if vals:
            return vals
    return []


def main():
    ap = argparse.ArgumentParser(description="金牌影视目录同步")
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="同步剧集目录")
    p_list.add_argument("--types", default="", help="分类 id，逗号分隔，默认全部")
    p_list.add_argument("--limit", type=int, default=0, help="最多入库多少条（试跑用）")
    p_list.add_argument("--sleep", type=float, default=0.15)
    p_list.add_argument("--force", action="store_true", help="忽略断点重扫（增量更新用）")
    p_list.add_argument("--pages", type=int, default=0, help="每个切片最多扫几页（增量更新用，0=全部）")

    p_det = sub.add_parser("detail", help="同步分集清单")
    p_det.add_argument("--limit", type=int, default=0)
    p_det.add_argument("--workers", type=int, default=4)
    p_det.add_argument("--sleep", type=float, default=0.1)
    p_det.add_argument("--changed", action="store_true", help="同时更新状态有变化（如新增集数）的剧")

    p_all = sub.add_parser("all", help="列表 + 详情")
    p_all.add_argument("--types", default="")
    p_all.add_argument("--limit", type=int, default=0)
    p_all.add_argument("--workers", type=int, default=4)
    p_all.add_argument("--force", action="store_true")
    p_all.add_argument("--pages", type=int, default=0)
    p_all.add_argument("--changed", action="store_true")

    sub.add_parser("stats", help="查看库存")

    args = ap.parse_args()
    db.init_db()
    api = JPApi()

    if args.cmd == "stats" or not args.cmd:
        s = db.stats()
        print("剧集: %d 部（其中 %d 部已抓分集），分集: %d 条" % (s["videos"], s["videos_with_episodes"], s["episodes"]))
        return

    if args.cmd in ("list", "all"):
        all_types = api.types()
        want = [x.strip() for x in (args.types or "").split(",") if x.strip()]
        types = [t for t in all_types if not want or t["type_id"] in want]
        log("分类：%s" % ", ".join("%s(%s)" % (t["type_name"], t["type_id"]) for t in types))
        filters = api.filters()
        years = _filter_values(filters, "yearList")
        areas = _filter_values(filters, "districtList")
        log("年份切片 %d 个，地区切片 %d 个" % (len(years), len(areas)))
        sync_list(api, types, years, areas, limit=args.limit,
                  force=getattr(args, "force", False), max_pages=getattr(args, "pages", 0))

    if args.cmd in ("detail", "all"):
        sync_detail(api, limit=getattr(args, "limit", 0), workers=getattr(args, "workers", 4),
                    changed=getattr(args, "changed", False))

    s = db.stats()
    log("完成。剧集 %d 部（%d 部有分集），分集 %d 条；本次失败 %d 次"
        % (s["videos"], s["videos_with_episodes"], s["episodes"], _stat["fail"]))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
