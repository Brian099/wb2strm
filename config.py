# -*- coding: utf-8 -*-
"""全局配置。所有项都可以用环境变量覆盖，方便部署到别的机器。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 站点（必填，来自环境变量）----------------
# 不在代码里固化任何域名/密钥：换站点只改 compose，镜像本身保持通用
HOST = os.environ.get("JP_HOST", "").strip().rstrip("/")
APP_KEY = os.environ.get("JP_APP_KEY", "").strip()


def missing_required():
    """返回缺失的必填环境变量，供启动自检使用。"""
    miss = []
    if not HOST:
        miss.append("JP_HOST")
    if not APP_KEY:
        miss.append("JP_APP_KEY")
    return miss


def check_required():
    """启动自检：缺配置直接退出，避免带着错误域名默默跑。"""
    miss = missing_required()
    if not miss:
        return True
    print("=" * 60)
    print("[错误] 缺少必填环境变量：%s" % "、".join(miss))
    print("")
    print("请在 docker-compose.yml 的 environment 里配置：")
    print('  JP_HOST: "https://<站点域名>"')
    print('  JP_APP_KEY: "<站点前端 bundle 里的签名密钥>"')
    print("")
    print("说明：")
    print("  JP_HOST     被抓取的站点地址（不是本服务的地址）")
    print("  JP_APP_KEY  接口签名密钥，站点改版后可能变化")
    print("=" * 60)
    return False
UA = os.environ.get(
    "JP_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

# ---------------- 路径 ----------------
DATA_DIR = os.environ.get("JP_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("JP_DB", os.path.join(DATA_DIR, "wb2strm.db"))
STRM_ROOT = os.environ.get("JP_STRM_ROOT", os.path.join(BASE_DIR, "strm"))

# ---------------- 对外服务 ----------------
# 写进 .strm 文件的地址，必须是播放器能访问到的地址（局域网就填局域网 IP）
PUBLIC_BASE = os.environ.get("JP_PUBLIC_BASE", "http://127.0.0.1:8989").rstrip("/")
LISTEN_HOST = os.environ.get("JP_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("JP_LISTEN_PORT", "8989"))
# 访问令牌：非空时所有接口都要带 ?token=xxx 或 X-Token 头
ACCESS_TOKEN = os.environ.get("JP_TOKEN", "")

# ---------------- 取流缓存 ----------------
# 实测同一张签名地址连续 46 分钟仍返回 200，这里缓存 30 分钟，留足余量
PLAY_CACHE_TTL = int(os.environ.get("JP_CACHE_TTL", "1800"))
DEFAULT_QUALITY = int(os.environ.get("JP_DEFAULT_QUALITY", "1080"))

# 播放方式：1=中继（所有流量经本服务转发，兼容性最好）
#           0=直连（302 跳转，省带宽；但 CDN 签名与请求 IP 绑定，
#             只有和本服务同一出口 IP 的播放器能用）
RELAY = int(os.environ.get("JP_RELAY", "1"))
# WSGI 工作线程数：中继模式下每个分片转发会占用一个线程，
# 线程太少会互相排队（多路同时播放时会卡）
THREADS = int(os.environ.get("JP_THREADS", "24"))

# ---------------- 苹果CMS 兼容接口 ----------------
# 非空时，/api.php/provide/vod 需要带 &key=<JP_API_KEY>；留空则公开
API_KEY = os.environ.get("JP_API_KEY", "").strip()
# vod_play_from 里显示的线路名
API_PLAY_FROM = os.environ.get("JP_API_PLAY_FROM", "wb2strm").strip()

# ---------------- 导出参数 ----------------
# 自动导出只写 .strm；NFO / 海报只在手动「重新生成 STRM」时按需勾选
# 图片下载并发（手动勾选海报时生效）
IMAGE_WORKERS = int(os.environ.get("JP_IMAGE_WORKERS", "8"))

# ---------------- 同步参数（全部走环境变量，写进 compose 即可） ----------------
# 列表接口 totalCount 被 10000 截顶（ES max_result_window），超过就按年份切片
TOTAL_CAP = 10000
PAGE_SIZE = 30

# 增量时每个切片扫几页（0=全部；首次空库会自动用全部）
PAGES = int(os.environ.get("JP_PAGES", "5"))
# 详情并发
WORKERS = int(os.environ.get("JP_WORKERS", "2"))
# 请求间隔（秒）：列表页 / 详情页。站点会限流（code=99600），慢一点更稳
LIST_SLEEP = float(os.environ.get("JP_LIST_SLEEP", "0.5"))
DETAIL_SLEEP = float(os.environ.get("JP_DETAIL_SLEEP", "0.3"))
# 自动更新间隔（小时），从上一次「跑完」开始计时
INTERVAL_HOURS = float(os.environ.get("JP_INTERVAL_HOURS", "24"))
# 启动后是否立刻跑一次（默认开启；设为 0 则等第一个间隔到期）
BOOTSTRAP = int(os.environ.get("JP_BOOTSTRAP", "1"))
# 强制按「全量」跑一轮（怀疑目录不全时用；全量是断点续爬，抓过的页会跳过，
# 跑完会自动打上「已扫全」标记，之后即便保留该开关也几乎不耗时）
FORCE_FULL = int(os.environ.get("JP_FORCE_FULL", "0"))
