# -*- coding: utf-8 -*-
"""苹果CMS V10 兼容的 Provide API。

把本地库以标准「资源站接口」的形式暴露出去，任何支持苹果CMS V10 的客户端
（TVBox 的「添加接口」、各类聚合/采集器）都能直接订阅本服务。

接口对照（与 maccms-go 的实现保持一致）：

    GET|POST /api.php/provide/vod            （可带末尾斜杠）
        ac=list        -> {code,msg,page,pagecount,limit,total,list:[],class:[...]}
        ac=videolist   -> {code,msg,page,pagecount,limit,total,list:[VodItem...]}
        ac=detail      -> 同 videolist
        参数: t=分类, pg=页码, limit=每页(默认20,上限500), ids=1,2,3, id=单个, wd=搜索
        at=xml         -> 输出 XML

    GET /pic?url=<原始地址>   或  /pic/<base64url>    图片代理

鉴权：配置了 JP_API_KEY 时，请求需带 &key=<JP_API_KEY>。
"""
import base64
import io
import time

import requests
from flask import Blueprint, Response, request

import config
import db
import settings
import strm

bp = Blueprint("maccms", __name__)

_MAX_LIMIT = 500


# ---------------------------------------------------------------- 工具
def _text(v):
    return str("" if v is None else v).strip()


def _int(v, d=0):
    try:
        return int(str(v).strip())
    except Exception:
        return d


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return ""


def _xml_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _image_mime(blob, upstream=None):
    """按文件头判断图片类型，上游常返回 application/octet-stream。"""
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    up = (upstream or "").lower()
    if up.startswith("image/"):
        return upstream
    return "image/jpeg"


def _key_ok():
    need = _text(getattr(config, "API_KEY", ""))
    if not need:
        return True
    got = _text(request.values.get("key") or request.headers.get("X-Api-Key") or "")
    return got == need


def _base_url():
    """本服务的对外地址，用于拼图片代理与播放地址。"""
    return settings.public_base() or request.host_url.rstrip("/")


# ---------------------------------------------------------------- 分类
def _classes():
    """分类列表（type_id 用库里的一级分类 id，名称用分类名）。"""
    rows = db._conn().execute(
        "SELECT type_id AS tid, "
        "       COALESCE(NULLIF(MAX(cat),''), MAX(type_name), '未分类') AS nm, "
        "       COUNT(*) AS n "
        "FROM video GROUP BY type_id ORDER BY CAST(type_id AS INTEGER)"
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "type_id": _text(r["tid"]),
            "type_name": _text(r["nm"]),
            "type_en": _text(r["nm"]),
            "type_pid": "0",
            "type_count": str(r["n"]),
        })
    return out


# ---------------------------------------------------------------- 视频条目
def _episodes_for(vod_ids):
    """一次取出这些剧的分集，按 vod_id 分组。"""
    if not vod_ids:
        return {}
    marks = ",".join("?" * len(vod_ids))
    out = {}
    for r in db._conn().execute(
            "SELECT vod_id,nid,seq,name FROM episode WHERE vod_id IN (%s) "
            "ORDER BY vod_id, seq, nid" % marks, list(vod_ids)).fetchall():
        out.setdefault(r["vod_id"], []).append(
            {"nid": r["nid"], "seq": r["seq"], "name": r["name"]})
    return out


def _play_url(vid, eps):
    """拼成苹果CMS格式：第1集$地址#第2集$地址"""
    parts = []
    for i, ep in enumerate(eps):
        seq = _int(ep.get("seq"), 0) or (i + 1)
        nm = _text(ep.get("name"))
        if not nm or nm.isdigit():
            nm = "第%d集" % seq
        parts.append("%s$%s" % (nm, strm.play_url(vid, ep["nid"])))
    return "#".join(parts)


def _vod_item(v, eps):
    play_from = _text(config.API_PLAY_FROM) or "wb2strm"
    return {
        "vod_id": _int(v["vod_id"]),
        "type_id": _text(v["type_id"]),
        "type_name": _text(v.get("cat") or v.get("type_name")),
        "vod_name": _text(v.get("name")),
        "vod_en": "",
        "vod_pic": _text(v.get("pic")),
        "vod_remarks": _text(v.get("remarks")),
        "vod_time": _fmt_time(v.get("updated_at")),
        "vod_actor": _text(v.get("actor")),
        "vod_director": _text(v.get("director")),
        "vod_content": _text(v.get("content")),
        "vod_year": _text(v.get("year")),
        "vod_area": _text(v.get("area")),
        "vod_lang": _text(v.get("lang")),
        "vod_play_from": play_from,
        "vod_play_url": _play_url(_text(v["vod_id"]), eps),
        "vod_douban_id": "",
        "vod_douban_score": "",
        "vod_score": "",
        "vod_score_num": "",
    }


def _query(ac, t, pg, limit, ids, wd, one_id):
    """返回 (total, [rows])。"""
    where = []
    args = []
    if ids:
        where.append("vod_id IN (%s)" % ",".join("?" * len(ids)))
        args += [str(x) for x in ids]
    elif one_id:
        where.append("vod_id = ?")
        args.append(str(one_id))
    if t:
        where.append("type_id = ?")
        args.append(str(t))
    if wd:
        where.append("(name LIKE ? OR actor LIKE ? OR type_name LIKE ?)")
        kw = "%" + wd + "%"
        args += [kw, kw, kw]
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""

    conn = db._conn()
    total = conn.execute("SELECT COUNT(*) n FROM video" + sql_where, args).fetchone()["n"]

    if ids or one_id:
        # 指定 id 时不翻页
        sql = "SELECT * FROM video" + sql_where + " ORDER BY CAST(vod_id AS INTEGER) DESC"
        rows = conn.execute(sql, args).fetchall()
    else:
        sql = ("SELECT * FROM video" + sql_where +
               " ORDER BY CAST(vod_id AS INTEGER) DESC LIMIT ? OFFSET ?")
        rows = conn.execute(sql, args + [limit, (pg - 1) * limit]).fetchall()
    return total, [dict(r) for r in rows]


# ---------------------------------------------------------------- XML
def _xml_list(classes):
    body = io.StringIO()
    body.write('<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0">')
    body.write('<list page="1" pagecount="1" pagesize="20" recordcount="0"></list>')
    body.write("<class>")
    for c in classes:
        body.write('<ty id="%s">%s</ty>' % (_xml_escape(c["type_id"]), _xml_escape(c["type_name"])))
    body.write("</class></rss>")
    return body.getvalue()


def _xml_vods(items, pg, page_count, total):
    body = io.StringIO()
    body.write('<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0">')
    body.write('<list page="%d" pagecount="%d" pagesize="%d" recordcount="%d">'
               % (pg, page_count, len(items), total))
    for it in items:
        body.write("<video>")
        for k in ("vod_id", "type_id", "type_name", "vod_name", "vod_en", "vod_pic",
                  "vod_remarks", "vod_time", "vod_actor", "vod_director", "vod_content",
                  "vod_year", "vod_area", "vod_lang", "vod_play_from", "vod_play_url"):
            body.write("<%s>%s</%s>" % (k, _xml_escape(it.get(k)), k))
        body.write("</video>")
    body.write("</list><class></class></rss>")
    return body.getvalue()


# ---------------------------------------------------------------- 路由
@bp.route("/api.php/provide/vod", methods=["GET", "POST"])
@bp.route("/api.php/provide/vod/", methods=["GET", "POST"])
def provide():
    if not _key_ok():
        return {"code": 0, "msg": "密钥错误", "page": 1, "pagecount": 1,
                "limit": "20", "total": "0", "list": [], "class": []}

    ac = _text(request.values.get("ac")) or "list"
    want_xml = _text(request.values.get("at")).lower() == "xml"

    if ac == "list":
        classes = _classes()
        if want_xml:
            return Response(_xml_list(classes), mimetype="text/xml")
        return {"code": 1, "msg": "数据列表", "page": 1, "pagecount": 1,
                "limit": "20", "total": "0", "list": [], "class": classes}

    # videolist / detail
    pg = max(1, _int(request.values.get("pg"), 1))
    limit = _int(request.values.get("limit"), 20)
    if limit < 1:
        limit = 20
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT
    t = _text(request.values.get("t"))
    wd = _text(request.values.get("wd"))
    one_id = _text(request.values.get("id"))
    ids = []
    raw_ids = _text(request.values.get("ids"))
    if raw_ids:
        parts = [x.strip() for x in raw_ids.split(",") if x.strip()]
        if all(x.isdigit() for x in parts):
            ids = parts
        else:
            wd = wd or raw_ids

    total, rows = _query(ac, t, pg, limit, ids, wd, one_id)
    page_count = max(1, (total + limit - 1) // limit) if not ids and not one_id else 1
    ep_map = _episodes_for([_text(r["vod_id"]) for r in rows])
    items = [_vod_item(r, ep_map.get(_text(r["vod_id"]), [])) for r in rows]

    if want_xml:
        return Response(_xml_vods(items, pg, page_count, total), mimetype="text/xml")
    return {"code": 1, "msg": "数据列表", "page": pg, "pagecount": page_count,
            "limit": str(limit), "total": str(total), "list": items}


@bp.route("/pic")
@bp.route("/pic/<path:b64>")
def pic(b64=None):
    """图片代理：TVBox 访问不到图床时走后端转发。"""
    url = ""
    b64 = b64 or ""
    if b64:
        try:
            url = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
        except Exception:
            return Response("invalid b64", status=400)
    else:
        url = _text(request.args.get("url"))
    if not url.lower().startswith("http"):
        return Response("bad url", status=400)
    try:
        r = requests.get(url, timeout=20, verify=False,
                         headers={"User-Agent": config.UA, "Referer": config.HOST + "/"})
    except Exception as e:
        return Response(str(e), status=502)
    if r.status_code != 200 or not r.content:
        return Response("upstream %s" % r.status_code, status=502)
    resp = Response(r.content, mimetype=_image_mime(r.content, r.headers.get("Content-Type")))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def register(app):
    app.register_blueprint(bp)
