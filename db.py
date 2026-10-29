# -*- coding: utf-8 -*-
"""SQLite 存储层：剧集目录 + 分集清单 + 同步状态。

只存"清单"（vod_id / nid），不存签名播放地址——地址是短时效的，
由 server 在播放时现取并做内存缓存。
"""
import os
import sqlite3
import threading
import time

import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS video (
    vod_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    cat           TEXT DEFAULT '',
    type_id       TEXT DEFAULT '',
    type_name     TEXT DEFAULT '',
    year          TEXT DEFAULT '',
    area          TEXT DEFAULT '',
    lang          TEXT DEFAULT '',
    pic           TEXT DEFAULT '',
    remarks       TEXT DEFAULT '',
    actor         TEXT DEFAULT '',
    director      TEXT DEFAULT '',
    content       TEXT DEFAULT '',
    episode_count INTEGER DEFAULT 0,
    updated_at    INTEGER DEFAULT 0,
    detail_at     INTEGER DEFAULT 0,     -- 详情(分集)最后同步时间
    detail_remark TEXT DEFAULT ''        -- 抓详情时的状态文本，用于判断"集数有没有更新"
);
CREATE INDEX IF NOT EXISTS idx_video_type ON video(type_id);
CREATE INDEX IF NOT EXISTS idx_video_year ON video(year);
CREATE INDEX IF NOT EXISTS idx_video_detail ON video(detail_at);

CREATE TABLE IF NOT EXISTS episode (
    vod_id  TEXT NOT NULL,
    nid     TEXT NOT NULL,
    seq     INTEGER DEFAULT 0,
    name    TEXT DEFAULT '',
    PRIMARY KEY (vod_id, nid)
);
CREATE INDEX IF NOT EXISTS idx_episode_vod ON episode(vod_id, seq);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
"""


def _conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def init_db():
    c = _conn()
    c.executescript(SCHEMA)
    # 老库补列
    cols = [r["name"] for r in c.execute("PRAGMA table_info(video)").fetchall()]
    if "cat" not in cols:
        c.execute("ALTER TABLE video ADD COLUMN cat TEXT DEFAULT ''")
    if "detail_remark" not in cols:
        c.execute("ALTER TABLE video ADD COLUMN detail_remark TEXT DEFAULT ''")
        # 老数据回填：把当前 remarks 当成"已抓过的状态"，避免升级后第一次增量把全库重抓一遍
        c.execute("UPDATE video SET detail_remark = COALESCE(remarks,'') WHERE detail_at > 0")
    c.commit()


# ---------------------------------------------------------------- 写入
def upsert_video(v, detail=False):
    now = int(time.time())
    c = _conn()
    c.execute(
        """INSERT INTO video (vod_id,name,cat,type_id,type_name,year,area,lang,pic,remarks,actor,director,content,episode_count,updated_at,detail_at,detail_remark)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(vod_id) DO UPDATE SET
             name=excluded.name,
             cat=CASE WHEN excluded.cat<>'' THEN excluded.cat ELSE video.cat END,
             type_id=CASE WHEN excluded.type_id<>'' THEN excluded.type_id ELSE video.type_id END,
             type_name=CASE WHEN excluded.type_name<>'' THEN excluded.type_name ELSE video.type_name END,
             year=CASE WHEN excluded.year<>'' THEN excluded.year ELSE video.year END,
             area=CASE WHEN excluded.area<>'' THEN excluded.area ELSE video.area END,
             lang=CASE WHEN excluded.lang<>'' THEN excluded.lang ELSE video.lang END,
             pic=CASE WHEN excluded.pic<>'' THEN excluded.pic ELSE video.pic END,
             remarks=CASE WHEN excluded.remarks<>'' THEN excluded.remarks ELSE video.remarks END,
             actor=CASE WHEN excluded.actor<>'' THEN excluded.actor ELSE video.actor END,
             director=CASE WHEN excluded.director<>'' THEN excluded.director ELSE video.director END,
             content=CASE WHEN excluded.content<>'' THEN excluded.content ELSE video.content END,
             episode_count=CASE WHEN excluded.episode_count>0 THEN excluded.episode_count ELSE video.episode_count END,
             updated_at=excluded.updated_at,
             detail_at=CASE WHEN excluded.detail_at>0 THEN excluded.detail_at ELSE video.detail_at END,
             detail_remark=CASE WHEN excluded.detail_remark<>'' THEN excluded.detail_remark ELSE video.detail_remark END""",
        (
            str(v.get("vod_id") or ""),
            str(v.get("name") or ""),
            str(v.get("cat") or ""),
            str(v.get("type_id") or ""),
            str(v.get("type_name") or ""),
            str(v.get("year") or ""),
            str(v.get("area") or ""),
            str(v.get("lang") or ""),
            str(v.get("pic") or ""),
            str(v.get("remarks") or ""),
            str(v.get("actor") or ""),
            str(v.get("director") or ""),
            str(v.get("content") or ""),
            int(v.get("episode_count") or 0),
            now,
            now if detail else 0,
            str(v.get("remarks") or "") if detail else "",
        ),
    )


def replace_episodes(vod_id, episodes):
    """episodes: [{nid, seq, name}]"""
    c = _conn()
    vod_id = str(vod_id)
    keep = [str(e["nid"]) for e in episodes if e.get("nid")]
    if keep:
        marks = ",".join("?" * len(keep))
        c.execute("DELETE FROM episode WHERE vod_id=? AND nid NOT IN (%s)" % marks, [vod_id] + keep)
    else:
        c.execute("DELETE FROM episode WHERE vod_id=?", (vod_id,))
    c.executemany(
        "INSERT INTO episode (vod_id,nid,seq,name) VALUES (?,?,?,?) "
        "ON CONFLICT(vod_id,nid) DO UPDATE SET seq=excluded.seq, name=excluded.name",
        [(vod_id, str(e["nid"]), int(e.get("seq") or 0), str(e.get("name") or "")) for e in episodes if e.get("nid")],
    )


def commit():
    _conn().commit()


def set_state(key, value):
    c = _conn()
    c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (str(key), str(value)))
    c.commit()


def get_state(key, default=""):
    row = _conn().execute("SELECT value FROM sync_state WHERE key=?", (str(key),)).fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------------- 读取
def get_video(vod_id):
    row = _conn().execute("SELECT * FROM video WHERE vod_id=?", (str(vod_id),)).fetchone()
    return dict(row) if row else None


def get_episodes(vod_id):
    rows = _conn().execute(
        "SELECT nid,seq,name FROM episode WHERE vod_id=? ORDER BY seq, nid", (str(vod_id),)
    ).fetchall()
    return [dict(r) for r in rows]


def _where(type_id="", year="", keyword="", cat="", need_detail=None, changed=False):
    sql = " WHERE 1=1"
    args = []
    if cat:
        sql += " AND COALESCE(NULLIF(cat,''), type_name)=?"
        args.append(str(cat))
    if type_id:
        sql += " AND type_id=?"
        args.append(str(type_id))
    if year:
        sql += " AND year=?"
        args.append(str(year))
    if keyword:
        sql += " AND (name LIKE ? OR actor LIKE ? OR type_name LIKE ?)"
        kw = "%" + keyword + "%"
        args += [kw, kw, kw]
    if changed:
        # 从未抓过分集，或列表里的状态文本（如"更新至71"）和上次抓详情时不一样
        sql += " AND (detail_at=0 OR COALESCE(remarks,'')<>COALESCE(detail_remark,''))"
    elif need_detail is True:
        sql += " AND detail_at=0"
    elif need_detail is False:
        sql += " AND detail_at>0"
    return sql, args


def list_videos(type_id="", year="", keyword="", cat="", need_detail=None, changed=False, limit=100, offset=0):
    sql, args = _where(type_id, year, keyword, cat, need_detail, changed)
    sql = "SELECT * FROM video" + sql + " ORDER BY vod_id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    return [dict(r) for r in _conn().execute(sql, args).fetchall()]


def count_videos(type_id="", year="", keyword="", cat="", need_detail=None, changed=False):
    sql, args = _where(type_id, year, keyword, cat, need_detail, changed)
    row = _conn().execute("SELECT COUNT(*) n FROM video" + sql, args).fetchone()
    return int(row["n"])


def cats():
    """一级分类及数量，供前端筛选栏使用。"""
    rows = _conn().execute(
        "SELECT COALESCE(NULLIF(cat,''), type_name, '未分类') AS cat_name, COUNT(*) AS n "
        "FROM video GROUP BY cat_name ORDER BY n DESC"
    ).fetchall()
    return [{"name": r["cat_name"], "count": r["n"]} for r in rows]


def years():
    rows = _conn().execute(
        "SELECT year, COUNT(*) n FROM video WHERE year<>'' GROUP BY year ORDER BY year DESC"
    ).fetchall()
    return [{"year": r["year"], "count": r["n"]} for r in rows]


def stats():
    c = _conn()
    v = c.execute("SELECT COUNT(*) n FROM video").fetchone()["n"]
    d = c.execute("SELECT COUNT(*) n FROM video WHERE detail_at>0").fetchone()["n"]
    e = c.execute("SELECT COUNT(*) n FROM episode").fetchone()["n"]
    return {"videos": v, "videos_with_episodes": d, "episodes": e}
