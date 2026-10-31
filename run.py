# -*- coding: utf-8 -*-
"""生产入口：用 waitress 起 WSGI 服务（容器里默认走这个）。

本地没有 waitress 时会自动退回 Flask 内置服务器，方便直接调试。
本地开发也可以继续用 python server.py。
"""
import config
import db
import settings
from server import app, scheduler


def main():
    if not config.check_required():
        raise SystemExit(1)
    db.init_db()
    scheduler.start()
    port = config.LISTEN_PORT
    print("=" * 56, flush=True)
    print("界面      http://127.0.0.1:%d/" % port, flush=True)
    print("对外地址  %s   <- .strm 里写这个，必须是播放器能访问的地址" % config.PUBLIC_BASE, flush=True)
    print("抓取站点  %s" % settings.source_host(), flush=True)
    print("数据库    %s" % config.DB_PATH, flush=True)
    print("STRM 目录 %s" % config.STRM_ROOT, flush=True)
    print("取流缓存  %d 秒" % config.PLAY_CACHE_TTL, flush=True)
    print("界面鉴权  %s" % ("开启" if config.ACCESS_TOKEN else "关闭"), flush=True)
    print("采集接口  /api.php/provide/vod  %s"
          % ("密钥 " + ("已设置" if config.API_KEY else "未设置")), flush=True)
    print("=" * 56, flush=True)

    try:
        from waitress import serve
        print("WSGI      waitress（threads=8）", flush=True)
        serve(app, host=config.LISTEN_HOST, port=port, threads=8, ident="wb2strm")
    except ImportError:
        print("WSGI      Flask 内置服务器（未安装 waitress）", flush=True)
        app.run(host=config.LISTEN_HOST, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
