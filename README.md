# wb2strm

[![Build and Push Docker Image](https://github.com/Brian099/wb2strm/actions/workflows/docker.yml/badge.svg)](https://github.com/Brian099/wb2strm/actions/workflows/docker.yml)
[![Docker Hub](https://img.shields.io/docker/pulls/brian9909/wb2strm?label=docker%20pulls)](https://hub.docker.com/r/brian9909/wb2strm)
[![Image Size](https://img.shields.io/docker/image-size/brian9909/wb2strm/latest?label=image%20size)](https://hub.docker.com/r/brian9909/wb2strm)

> **免责声明**
> 本项目仅为技术学习用途的**客户端工具**：不存储、不分发任何视频文件，也不提供任何内容服务。
> 抓取目标为第三方站点，其内容版权归原权利人所有。请勿用于商业用途、公开对外提供服务或再分发。
> 使用本项目产生的任何后果由使用者自行承担。

把目标站点的剧集目录抓到本地 SQLite，并由一个本地服务在播放时**现取**最新有效的播放地址，同时支持导出 **STRM** 给 Emby / Jellyfin / Kodi 使用。

## 为什么不是直接存链接

目标站点的播放地址是**每次请求现签**的：

```
https://cdn.example.com/.../index.m3u8?auth_key=1700000000-e0552fdf-0-65902ef5...
                                         └─ 签发时刻的 unix 时间戳
```

- 同一集连续取两次，`auth_key` 完全不同
- 去掉 `auth_key` → 403；连 m3u8 里的 `index0.ts` 分片也带同一签名
- 实测同一张签名地址连续 **30 分钟以上**仍返回 200，属于「分钟级～小时级」短时效，绝不是永久

所以本系统只存 **`vod_id` + `nid`（剧集 ID + 分集 ID）**，播放时调一次源站接口换新地址，并做 30 分钟内存缓存（比实测有效期留了余量）。

## 工作流程

整个流程分三步，服务会**自动**串起来跑（启动时一次，之后按间隔循环）：

```
① 同步目录   sync.py list
   一级分类 → 条目 ≥10000 的分类按「年份」再按「地区」切片
   → 每个切片翻页（30 条/页）→ 批量 UPSERT 进 video 表
   只拿列表元数据（ID / 标题 / 封面 / 状态），不碰分集

② 同步分集   sync.py detail
   对「没抓过分集」和「状态文本变了」的剧，逐部调详情接口
   → 拿到 episodeList → 写 episode 表（nid / 集号 / 集名）
   接口不支持批量，只能一部一部来，所以这步最慢

③ 导出 STRM  strm.py
   读 video + episode → 每集写一个 .strm（内容是取流地址）+ 可选 NFO / 海报
```

- **自动**：容器一启动就开始跑（空库全量、有库增量），跑完按间隔循环，全程无需人工干预
- **全量**：① 扫全部页 → ② 处理全部 → ③ 导出，首次大约几小时（看站点规模）
- **增量**：① 只扫每个分类前几页 → ② 只处理新片和状态变化的 → ③ 只重写有变化的文件，通常几分钟
- **换地址**：改完「服务地址」点「保存并重新导出」，只重写 `.strm` 内容，不动数据库

## 目录结构

```
wb2strm/
├─ config.py     配置（站点、DB 路径、对外地址、令牌、缓存时长）
├─ jpapi.py      站点接口客户端（签名 / 列表 / 详情 / 取流）
├─ db.py         SQLite 存储层
├─ sync.py       同步任务（抓目录 + 抓分集）
├─ server.py     后端服务（302 取流 + 目录查询 + 运维接口）
├─ strm.py       STRM / NFO / 海报导出
├─ run.py        生产入口（waitress，容器里用这个）
├─ web/          前端界面
│  ├─ index.html
│  ├─ app.js
│  └─ style.css
├─ Dockerfile / docker-compose.yml
├─ data/         SQLite 数据库（自动创建）
└─ strm/         导出的 STRM 目录
```

## 前端界面

启动服务后浏览器打开 `http://127.0.0.1:8989/`：

**浏览页**
- 顶部搜索框（片名 / 演员 / 类型，防抖 350ms）
- 分类胶囊（按一级分类聚合，带数量）+ 年份下拉
- 海报卡片墙，点击弹出详情
- 详情弹窗：海报、标签、年份/地区/集数、简介、**选集按钮**
- 点集数直接在内置播放器播放（hls.js），可切 1080P / 720P / 480P

**运维页**（只做两件事：看状态 + 改服务地址）
- 三张状态卡：**入库剧集 / 分集 / STRM 文件数**
- **获取进度**：当前阶段 + 进度条 + `已完成 / 总数`，运行中每 2 秒刷新
- 下次更新时间（从上一次跑完开始算）
- **服务地址**：改完点「保存并重新生成 STRM」即可，不用改 compose、不用重启
- 实时日志控制台

抓取、更新、导出全部自动进行，界面上没有「开始」按钮。

> 播放器用 CDN 上的 hls.js；如果浏览器访问不了外网，会自动降级成「直接打开链接」，把链接丢给 Emby / PotPlayer 即可。
> 设了 `JP_TOKEN` 时，界面地址要带上 `?token=xxx`，前端会自动把它附加到所有接口和播放地址上。

## 快速开始

```bash
cd wb2strm

# 1) 试跑：只抓某个分类 60 部，并补 12 部的分集
python sync.py list --types 88 --limit 60
python sync.py detail --limit 12 --workers 3

# 2) 起服务（必须先设 JP_HOST / JP_APP_KEY，否则会提示缺项并退出）
$env:JP_HOST="https://www.jiabaide.cn"
$env:JP_APP_KEY="cb808529bae6b6be45ecfab29a4889bc"
python server.py
# 浏览器打开 http://127.0.0.1:8989/

# 3) 导出 STRM（带 NFO 与海报）
python strm.py --nfo --images
```

## Docker 部署（拉取已发布镜像）

镜像发布在 Docker Hub：**[brian9909/wb2strm](https://hub.docker.com/r/brian9909/wb2strm)**，同时提供 `linux/amd64` 与 `linux/arm64`（ARM 架构的 NAS 可直接用）。

`docker-compose.yml` 里默认就是 `image: brian9909/wb2strm:latest`，**不需要本地构建**，容器名固定为 `wb2strm`。

```bash
cd wb2strm

# 1) 先改 docker-compose.yml 里的 JP_PUBLIC_BASE 为宿主机局域网 IP
#    JP_PUBLIC_BASE: "http://192.168.1.147:8989"

# 2) 拉取并启动
docker compose pull
docker compose up -d

# 3) 看日志
docker compose logs -f
```

打开 `http://<宿主机IP>:8989/` 即可，抓取会自动开始。

> `JP_PUBLIC_BASE` 是**首次启动的默认值**，之后在运维页改会覆盖它（存数据库）。
> 为什么要有这个默认值：容器一开机就会自动抓取 + 导出，默认值不对的话第一次导出的
> 上万条 `.strm` 全是错的，还得再改一次重新生成。

**更新到最新版**

```bash
docker compose pull && docker compose up -d
```

**不想用 compose 也可以直接 run**

```bash
docker run -d --name wb2strm \
  --restart unless-stopped \
  -p 8989:8989 \
  -v $PWD/data:/app/data \
  -v $PWD/strm:/app/strm \
  brian9909/wb2strm:latest
```

**`docker-compose.yml` 里要改的**：`JP_PUBLIC_BASE`（宿主机局域网 IP）和抓取参数。

| 变量 | 默认 | 说明 |
|---|---|---|
| `JP_PUBLIC_BASE` | `http://192.168.1.147:8989` | **写进 .strm 的地址**，改成你的宿主机 IP |
| `JP_PAGES` | `5` | 增量时每个切片扫几页 |
| `JP_WORKERS` | `3` | 分集并发 |
| `JP_INTERVAL_HOURS` | `24` | 自动更新间隔（小时） |
| `JP_BOOTSTRAP` | `1` | 启动后立刻跑一次 |

> 填宿主机局域网 IP 的好处：内网不管影视软件（Emby/Jellyfin）装在宿主机还是容器里都能访问。
> 填 `127.0.0.1` 只对「影视软件原生装在宿主机」有效——影视软件也在 Docker 里时，
> `127.0.0.1` 指向它自己的容器，会连不上。

**挂载**（`docker-compose.yml` 里已配好）：

| 宿主机 | 容器 | 用途 |
|---|---|---|
| `./data` | `/app/data` | SQLite 数据库，**必须持久化** |
| `./strm` | `/app/strm` | STRM 输出，给 Emby/Jellyfin 挂载 |

## 本地构建镜像（可选）

改过代码、想自己打包时，把 `docker-compose.yml` 里的 `image:` 那行注释掉、改用下面注释着的 `build: .`，然后：

```bash
docker compose up -d --build
```

镜像本身基于 `python:3.12-slim`，用 waitress（8 线程）跑 WSGI，自带健康检查。
所有配置都**直接写在 `docker-compose.yml` 里**，不需要 `.env`。

**常用命令**

```bash
docker compose ps                              # 状态 / 健康
docker compose logs -f --tail 100              # 实时日志
docker compose exec wb2strm python sync.py stats            # 查库存
docker compose exec wb2strm python strm.py --nfo --images   # 命令行导出
docker compose restart
docker compose down                            # 停止（数据保留在 ./data）
docker compose pull && docker compose up -d    # 更新镜像
```

**注意事项**

- `JP_PUBLIC_BASE` 是**写进 .strm 文件**的地址，用 `127.0.0.1` 只有宿主机自己能播，电视/手机要填局域网 IP
- 容器默认以 root 运行（NAS 上写宿主机目录最省事）；想用非 root：`chown -R 1000:1000 ./data ./strm` 后取消 compose 里 `user:` 那行注释
- Emby/Jellyfin 也要跑在 Docker 里时，把 `./strm` 再挂给 Emby 容器（例如 `- /volume1/docker/wb2strm/strm:/media/wb2strm`）
- 端口冲突就改 compose 里的 `ports`（`"8989:8989"` 冒号左边是宿主机端口，容器内固定 8989）

## 全量同步

```bash
python sync.py list                 # 全部分类（条目多的分类会自动按年份切片）
python sync.py detail --workers 4   # 补分集
```

实测规模与耗时（示例站点）：

| 分类 | 数量 | 平均集数 |
|---|---:|---:|
| 电影 | 22,199 | 1.0 |
| 电视剧 | 9,625 | 5.8 |
| 综艺 | 3,045 | 7.2 |
| 动漫 | 4,421 | 4.5 |
| 短剧 | 1,620 | 14.6 |
| **合计** | **40,910** | **≈143,500 集** |

- 详情接口**不支持批量**，只能一条一条问 → 单线程约 9.7 小时，4 并发约 2.4 小时
- 列表接口的 `totalCount` 有 **10000 截顶**（Elasticsearch `max_result_window`），`sync.py` 会自动按「年份 → 地区」切片绕开
- 断点续爬：每个列表页成功后会写 `sync_state`，中断后重跑不会重复抓

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端界面 |
| GET | `/play/<vod_id>/<nid>.m3u8` | **302** 跳转到最新有效地址（`.strm` 里写这个） |
| GET | `/relay/<vod_id>/<nid>.m3u8` | **中继播放（默认）**：m3u8 与分片全部经本服务转发 |
| GET | `/proxy/<vod_id>/<nid>.m3u8` | 只代理 m3u8 播放列表（分片仍由客户端直连 CDN） |
| GET | `/api/play/<vod_id>/<nid>` | JSON：`url / quality / expires_in` |
| GET | `/api/videos?cat=&year=&q=&limit=&offset=` | 本地库列表（返回 total 供分页） |
| GET | `/api/video/<vod_id>` | 详情 + 分集清单（含每集播放地址） |
| GET | `/api/types` | 分类与年份聚合 |
| GET | `/api/stats` | 库存统计 |
| POST | `/api/job/all` | **一键全流程** `{types, pages, workers, export, nfo, images, clean}` |
| POST | `/api/job/sync` | 触发同步 `{mode:"list"\|"detail", types, limit, workers, force, pages, changed}` |
| POST | `/api/job/strm` | 触发导出 `{nfo, images, clean, types, limit}` |
| GET | `/api/job` | 任务状态 + 日志 |
| GET | `/api/settings` | 读取运行时设置 + 下次自动更新时间 |
| POST | `/api/settings` | 保存设置 `{public_base, source_host, cache_ttl, auto_*, regen_strm}` |
| GET | `/health` | 健康检查 |

清晰度：`?q=1080|720|480`，不传则取最高。

## 两种播放方式

`JP_RELAY` 控制 `.strm` 里写哪种地址：

| 模式 | `.strm` 里的地址 | 流量走向 | 适用 |
|---|---|---|---|
| **中继（默认，`JP_RELAY=1`）** | `/relay/<vid>/<nid>.m3u8` | 客户端 → 本服务 → CDN | **都能用**，但视频流量走本服务转发 |
| 直连（`JP_RELAY=0`） | `/play/<vid>/<nid>.m3u8` | 客户端 302 到 CDN 直连 | 省带宽，但见下面的限制 |

### 为什么默认用中继

CDN 的 `auth_key` 签名**与请求方 IP 绑定**：

```
本服务（NAS）→ 调接口拿签名 → 签名绑定 NAS 的出口 IP
播放器（电视/手机）→ 拿这个签名去 CDN → IP 不匹配 → 403 Forbidden
```

实测同一张签名地址：NAS 自己拉 → `200`，从别的 IP 拉 → `403`。

所以只要播放器的出口 IP 和本服务不一致（远程播放、NAS 走代理/VPN、多 WAN 等），
直连模式必然失败。中继模式由本服务统一取流，客户端只跟本服务打交道，就不受这个限制。

**代价**：视频流量要经过本服务（下行 CDN + 上行客户端）。局域网播放无感，
外网播放会占用本服务的上行带宽。

想省带宽、且确认播放器与本服务同出口 IP 时，可以设 `JP_RELAY: "0"`。

## 外网播放

`.strm` 里的地址是**给取流的一方**用的，谁是取流方取决于 Emby 的处理方式：

```
① 服务端取流（转封装/转码后推给客户端）  → 地址只需 Emby 服务器能访问 = 局域网 IP 就够
② 客户端直连（Emby 把 URL 丢给播放器）    → 地址必须是客户端能访问 = 外网就得是公网域名
```

| 方案 | `.strm` 里填什么 | 前提 | 代价 |
|---|---|---|---|
| **A. 让 Emby 转码** | 局域网 IP | Emby 本身已能外网访问 + 有转码性能 | 吃 CPU 与外网上行；部分客户端强制直连会失败 |
| **B. 子域名反代** | `https://wb2strm.域名.com` | DDNS + 反代 + 证书 | 服务暴露公网，**必须开 token** |
| **C. 内网穿透** | 隧道给的公网地址 | Cloudflare Tunnel / frp | 同样要 token，延迟略高 |

**先试方案 A**：`.strm` 保持局域网 IP，看外网客户端能不能播。能播就什么都不用动。

播不了再走方案 B。Caddy 一行搞定证书：

```caddyfile
wb2strm.你的域名.com {
    reverse_proxy 127.0.0.1:8989
}
```

然后运维页把服务地址改成 `https://wb2strm.你的域名.com`，点「保存并重新生成 STRM」。

> ⚠️ **必须用子域名，不能用路径前缀**：前端接口是根路径绝对地址（`/api/...`），挂在 `/wb2strm/` 下会 404。
>
> ⚠️ **暴露公网必须开 `JP_TOKEN`**：这个服务会给任何请求者签发播放地址，不设令牌等于开了个免费取流接口。
> 加了之后界面地址要带 `?token=xxx`，生成的 `.strm` 里也会自动带上。
>
> ⚠️ **切到域名后注意 NAT 回流（发夹 NAT）**：部分路由器不支持从内网访问自己的公网域名，
> 会导致局域网内反而播不了。解决办法：路由器开 NAT loopback，或用内网 DNS 把域名解析到内网 IP。

## STRM 与媒体库

导出结果：

```
strm/
├─ 剧集分类 A/
│  └─ 示例剧集 (2026)/
│     ├─ tvshow.nfo
│     ├─ poster.jpg / fanart.jpg
│     └─ Season 01/
│        ├─ 示例剧集 S01E01.strm      ← 内容是 http://<你的服务>/play/<vod_id>/<nid>.m3u8
│        ├─ 示例剧集 S01E01.nfo
│        └─ ...
└─ 剧集分类 B/
   └─ 示例电影 (2026)/
      ├─ 示例电影.strm
      └─ movie.nfo
```

`.strm` 就是一行文本，里面是**取流接口地址**，不是真实链接——所以链接过期不需要重新导出。

Emby / Jellyfin 建库：

- 电影类 → 内容类型选「电影」，目录指向对应的电影分类目录
- 剧集类 → 内容类型选「电视节目」，目录指向对应分类目录（或直接指向 `strm`，它会自动识别 `Season 01` 结构）

Kodi：直接添加 `strm` 目录为视频源即可。

## 配置（环境变量）

**抓取相关**（写在 compose 里，改完 `docker compose up -d` 生效）

| 变量 | 默认 | 说明 |
|---|---|---|
| `JP_PAGES` | `5` | 增量时每个切片扫几页（0=全部） |
| `JP_WORKERS` | `3` | 分集详情并发 |
| `JP_INTERVAL_HOURS` | `24` | 自动更新间隔（小时），从上次跑完算起 |
| `JP_BOOTSTRAP` | `1` | 启动后是否立刻跑一次 |

**服务相关**

| 变量 | 默认 | 说明 |
|---|---|---|
| `JP_LISTEN_HOST` | `0.0.0.0` | 监听地址 |
| `JP_LISTEN_PORT` | `8989` | 服务端口 |
| `JP_TOKEN` | 空 | 非空时所有接口要带 `?token=xxx`（会写进 .strm） |
| `JP_DB` | `data/wb2strm.db` | 数据库路径 |
| `JP_STRM_ROOT` | `strm` | STRM 导出根目录 |
| `JP_CACHE_TTL` | `1800` | 取流缓存秒数 |
| `JP_RELAY` | `1` | 播放方式：1=中继（推荐），0=302 直连 |
| `JP_EXPORT_NFO` | `1` | 生成 tvshow.nfo / movie.nfo（每部剧 1 个，很便宜） |
| `JP_EXPORT_EP_NFO` | `0` | 每集单独生成 .nfo（几十万集时很慢，默认关） |
| `JP_EXPORT_IMAGES` | `1` | 下载海报/背景图（每部剧 1 次请求，并行） |
| `JP_IMAGE_WORKERS` | `8` | 图片下载并发 |
| `JP_DEFAULT_QUALITY` | `1080` | 默认清晰度 |
| `JP_PUBLIC_BASE` | `http://127.0.0.1:8989` | **本服务自己的地址**（写进 .strm），首次默认值，之后以界面保存的为准 |
| `JP_HOST` | **必填** | **被抓取的站点地址**，换域名只改这里 |
| `JP_APP_KEY` | **必填** | 接口签名密钥，报「应用签名失败」时更新 |

> 代码里**不固化任何域名和密钥**。这两项缺失时容器会直接报错退出，不会带着旧域名默默跑。

> 注意区分两个地址：`JP_HOST` 是**去哪抓**，`JP_PUBLIC_BASE` 是**本服务在哪**（写进 .strm 供播放器访问）。
>
> 站点换域名时：改 `JP_HOST` → `docker compose up -d` 重启即可，数据库和 strm 都不用动。
> 如果换域名后日志里出现 `应用签名失败`，说明密钥也换了，需要从新站前端 bundle 里找新的 `APP_KEY` 填到 `JP_APP_KEY`。

## 常见问题

**播放报 403 / 地址失效**
签名过期或目标站点换了签名算法。服务会自动重取；连续失败说明算法变了，需要更新 `jpapi.py` 里的 `_sign`。

**为什么有 `_manifest.json`**
记录每个 `.strm` 对应的 `vod_id/nid`，导出时只重写有变化的文件。`--clean` 会删掉库里已不存在的 `.strm`。

**站点限流**
`sync.py` 默认每次请求间隔 0.1–0.15 秒，4 并发。被限流就降到 2 并发、加大 `--sleep`。

**定时更新**
Windows 计划任务每天跑一次（增量，几分钟）：

```
python sync.py list --force --pages 5
python sync.py detail --changed --workers 4
python strm.py --nfo --images --clean
```

## 导出性能

几十万集时，导出做了这些优化（实测本地 1.08 万集：全量写盘 3.9 秒，无变化复跑 1.4 秒）：

| 优化点 | 说明 |
|---|---|
| 分集流式读取 | 按 `vod_id` 顺序流式产出，不再一次性把几十万集读进内存 |
| 目录只建一次 | 缓存已创建的目录，避免每个文件都 `makedirs` |
| 海报只下一次 | 原来 `poster.jpg` 和 `fanart.jpg` 各下载一次，现在下一张本地复制 |
| 海报并行下载 | `JP_IMAGE_WORKERS` 并发（默认 8） |
| 每集 NFO 默认关 | 原来每集写一个 `.nfo`，45 万集就是多 45 万个文件 |
| manifest 精简 | 只存「路径 → 地址」，几十万条也不占多少内存 |
| 只写变化的文件 | 地址没变就跳过，日志里会显示「新写/更新 N 个」 |

日志会分阶段报耗时，方便定位瓶颈：

```
读取完成：3180 部剧（分集按需流式读取），耗时 0.0s
写入 .strm：10832 个（新写/更新 10832 个，未变的跳过），耗时 3.9s
海报完成：新下载 0 张，耗时 0.1s
manifest：10831 条，耗时 0.0s
导出完成：共 10832 个 .strm，总耗时 4.1s
```

> 如果卡在「下载海报」，说明是首次导出（每部剧要下 1 张图），调大 `JP_IMAGE_WORKERS` 或临时 `JP_EXPORT_IMAGES: "0"`。

## 增量更新

日常增量是**自动完成**的，不用管。手工排查时可以用命令行：

```bash
# 1) 重扫每个分类的前几页（新内容都在最前面，列表按"最近更新"排序）
python sync.py list --force --pages 5

# 2) 只补"新增的剧" + "集数/状态有变化的剧"
python sync.py detail --changed
```

原理：

| 参数 | 作用 |
|---|---|
| `--force` | 列表页默认按 `sync_state` 断点跳过抓过的页；`--force` 忽略断点重扫（UPSERT 幂等，不会产生重复） |
| `--pages N` | 每个查询切片最多扫 N 页，避免增量时把全库扫一遍 |
| `--changed` | 详情阶段除了补 `detail_at=0`（新片），还挑 `remarks <> detail_remark` 的——抓详情时会把当时的 `remarks`（如 `更新至71`）存进 `detail_remark`，站点加集后这个值会变，就能识别出来 |

自动更新就是把上面两步 + 导出串起来跑，参数取 `JP_PAGES` / `JP_WORKERS`。

## 自动更新

不需要 cron、不需要手动点按钮，服务自己会跑：

```
容器启动
  └─ 10 秒后自动执行一次全流程
       ├─ 目录还没完整扫过一遍 → 继续全量（断点续爬，抓过的页自动跳过）
       │    扫全后才打上「完成」标记，转入增量
       └─ 目录已完整 → 增量（每个切片扫 JP_PAGES 页）
     跑完记录完成时间
  └─ 之后每隔 JP_INTERVAL_HOURS 小时（从上一次**跑完**算起）再跑一次
```

> **首次全量可能跨多次运行**：全站几万部、详情接口又不支持批量，一次跑不完很正常。
> 没扫全之前，每次重启/每次到点都会**接着上次的进度继续抓**（已抓过的页跳过），
> 不会因为重启就从头来，也不会因为「库里有数据了」就误判成已抓全。
> 运维页「目录：首次全量抓取中」就是在提示这个状态。

参数都在 `docker-compose.yml` 的 `environment` 里：

| 变量 | 默认 | 说明 |
|---|---|---|
| `JP_PAGES` | `5` | 增量时每个切片扫几页（0=全部；空库首次自动全量） |
| `JP_WORKERS` | `3` | 分集详情并发，NAS 上建议 2~3 |
| `JP_INTERVAL_HOURS` | `24` | 自动更新间隔（小时），从上一次跑完开始计时 |
| `JP_BOOTSTRAP` | `1` | 启动后是否立刻跑一次（0=等第一个间隔到期） |

- 改完这些参数需要 `docker compose up -d` 重建容器才生效
- 任务互斥：正在跑的时候不会重复启动
- 如果某次失败，会在 30 分钟后重试，而不是等满一个间隔
- `JP_PAGES=0` + `JP_BOOTSTRAP=1` 就是「每次启动都全量扫一遍」

## 注意

- 本项目只做**目录索引 + 实时换签**，不下载、不转存视频文件
- 内容来自第三方站点，仅供个人技术研究与自用，请勿对外提供服务或再分发
