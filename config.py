# -*- coding: utf-8 -*-
"""全局配置。所有项都可以用环境变量覆盖，方便部署到别的机器。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 站点 ----------------
HOST = os.environ.get("JP_HOST", "https://www.jiabaide.cn").rstrip("/")
APP_KEY = os.environ.get("JP_APP_KEY", "cb808529bae6b6be45ecfab29a4889bc")
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
