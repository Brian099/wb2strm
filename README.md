# 金牌影视 · 本地资源系统

[![Build and Push Docker Image](https://github.com/Brian099/wb2strm/actions/workflows/docker.yml/badge.svg)](https://github.com/Brian099/wb2strm/actions/workflows/docker.yml)
[![Docker Hub](https://img.shields.io/docker/pulls/brian9909/wb2strm?label=docker%20pulls)](https://hub.docker.com/r/brian9909/wb2strm)
[![Image Size](https://img.shields.io/docker/image-size/brian9909/wb2strm/latest?label=image%20size)](https://hub.docker.com/r/brian9909/wb2strm)

> **免责声明**
> 本项目仅为技术学习用途的**客户端工具**：不存储、不分发任何视频文件，也不提供任何内容服务。
> 抓取目标为第三方站点，其内容版权归原权利人所有。请勿用于商业用途、公开对外提供服务或再分发。
> 使用本项目产生的任何后果由使用者自行承担。

把「金牌影视」（jiabaide.cn）的剧集目录抓到本地 SQLite，并由一个本地服务在播放时**现取**最新有效的播放地址，同时支持导出 **STRM** 给 Emby / Jellyfin / Kodi 使用。

## 为什么不是直接存链接

金牌的播放地址是**每次请求现签**的：

```
https://ppvod011.blbtgg.com/.../index.m3u8?auth_key=1793256928-e0552fdf-0-65902ef5...
                                              └─ 签发时刻的 unix 时间戳
```

- 同一集连续取两次，`auth_key` 完全不同
- 去掉 `auth_key` → 403；连 m3u8 里的 `index0.ts` 分片也带同一签名
- 实测同一张签名地址连续 **33 分钟以上**仍返回 200，属于「分钟级～小时级」短时效，绝不是永久

所以本系统只存 **`vod_id` + `nid`（剧集 ID + 分集 ID）**，播放时调一次源站接口换新地址，并做 20 分钟内存缓存（比实测有效期留了足够余量）。

## 目录结构

```
金牌系统/
├─ config.py     配置（站点、DB 路径、对外地址、令牌、缓存时长）
├─ jpapi.py      金牌接口客户端（签名 / 列表 / 详情 / 取流）
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
- 分类胶囊（短剧 / 电视剧 / 电影 …，带数量）+ 年份下拉
- 海报卡片墙，点击弹出详情
- 详情弹窗：海报、标签、年份/地区/集数、简介、**选集按钮**
- 点集数直接在内置播放器播放（hls.js），可切 1080P / 720P / 480P

**运维页**
- 库存统计（剧集 / 已抓分集 / 分集总数 / 取流缓存）
- 三个任务面板：同步目录、同步分集、导出 STRM（可填分类、数量上限、并发、NFO/海报/清理）
- 实时日志控制台（2 秒轮询，任务运行中按钮自动禁用）

> 播放器用 CDN 上的 hls.js；如果浏览器访问不了外网，会自动降级成「直接打开链接」，把链接丢给 Emby / PotPlayer 即可。
> 设了 `JP_TOKEN` 时，界面地址要带上 `?token=xxx`，前端会自动把它附加到所有接口和播放地址上。

## 快速开始

```bash
cd 金牌系统

# 1) 试跑：只抓「短剧」分类 60 部，并补 12 部的分集
python sync.py list --types 88 --limit 60
python sync.py detail --limit 12 --workers 3

# 2) 起服务
python server.py
# 浏览器打开 http://127.0.0.1:8989/

# 3) 导出 STRM（带 NFO 与海报）
python strm.py --nfo --images
```

## Docker 部署（拉取已发布镜像）

镜像发布在 Docker Hub：**[brian9909/wb2strm](https://hub.docker.com/r/brian9909/wb2strm)**，同时提供 `linux/amd64` 与 `linux/arm64`（群晖/威联通 ARM 机型可直接用）。

`docker-compose.yml` 里默认就是 `image: brian9909/wb2strm:latest`，**不需要本地构建**。

```bash
cd 金牌系统

# 1) 改 docker-compose.yml 里的 JP_PUBLIC_BASE（改成播放器能访问到的局域网 IP）

# 2) 拉取并启动
docker compose pull
docker compose up -d

# 3) 看日志
docker compose logs -f
```

打开 `http://<宿主机IP>:8989/` 即可。

**更新到最新版**

```bash
docker compose pull && docker compose up -d
```

**不想用 compose 也可以直接 run**

```bash
docker run -d --name jinpai \
  --restart unless-stopped \
  -p 8989:8989 \
  -e JP_PUBLIC_BASE=http://192.168.1.110:8989 \
  -v $PWD/data:/app/data \
  -v $PWD/strm:/app/strm \
  brian9909/wb2strm:latest
```

**`docker-compose.yml` 里需要改的只有这一行**：

```yaml
environment:
  JP_PUBLIC_BASE: http://192.168.1.110:8989   # ← 换成你的局域网 IP 或域名
```

其余项（`TZ` / `JP_TOKEN` / `JP_CACHE_TTL` / `JP_DEFAULT_QUALITY`）默认值就能用。

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
docker compose ps                        # 状态 / 健康
docker compose logs -f --tail 100        # 实时日志
docker compose exec jinpai python sync.py stats     # 查库存
docker compose exec jinpai python strm.py --nfo --images   # 命令行导出
docker compose restart
docker compose down                      # 停止（数据保留在 ./data）
docker compose pull && docker compose up -d   # 更新镜像
```

**注意事项**

- `JP_PUBLIC_BASE` 是**写进 .strm 文件**的地址，用 `127.0.0.1` 只有宿主机自己能播，电视/手机要填局域网 IP
- 容器默认以 root 运行（NAS 上写宿主机目录最省事）；想用非 root：`chown -R 1000:1000 ./data ./strm` 后取消 compose 里 `user:` 那行注释
- Emby/Jellyfin 也要跑在 Docker 里时，把 `./strm` 再挂给 Emby 容器（例如 `- /volume1/docker/jinpai/strm:/media/jinpai`）
- 端口冲突就改 compose 里的 `ports`（`"8989:8989"` 冒号左边是宿主机端口，容器内固定 8989）

## 全量同步

```bash
python sync.py list            # 全部分类（电影会自动按年份切片）
python sync.py detail --workers 4   # 补分集，约 4 万次详情请求
```

实测规模与耗时：

| 分类 | 数量 | 平均集数 |
|---|---:|---:|
| 电影 | 22,199 | 1.0 |
| 电视剧 | 9,625 | 5.8 |
| 综艺 | 3,045 | 7.2 |
| 动漫 | 4,421 | 4.5 |
| 短剧 | 1,620 | 14.6 |
| **合计** | **40,910** | **≈143,500 集** |

- 详情接口**不支持批量**，只能一集一集问 → 单线程约 9.7 小时，4 并发约 2.4 小时
- 列表接口 `totalCount` 被 **10000 截顶**（Elasticsearch `max_result_window`），`sync.py` 会自动按「年份 → 地区」切片绕开
- 断点续爬：每个列表页成功后会写 `sync_state`，中断后重跑不会重复抓

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端界面 |
| GET | `/play/<vod_id>/<nid>.m3u8` | **302** 跳转到最新有效地址（`.strm` 里写这个） |
| GET | `/proxy/<vod_id>/<nid>.m3u8` | 拉取 m3u8 并把分片地址改成绝对路径（给不跟随 302 的播放器） |
| GET | `/api/play/<vod_id>/<nid>` | JSON：`url / quality / expires_in` |
| GET | `/api/videos?cat=&year=&q=&limit=&offset=` | 本地库列表（返回 total 供分页） |
| GET | `/api/video/<vod_id>` | 详情 + 分集清单（含每集播放地址） |
| GET | `/api/types` | 分类与年份聚合 |
| GET | `/api/stats` | 库存统计 |
| POST | `/api/job/sync` | 触发同步 `{mode:"list"\|"detail", types, limit, workers}` |
| POST | `/api/job/strm` | 触发导出 `{nfo, images, clean, types, limit}` |
| GET | `/api/job` | 任务状态 + 日志 |
| GET | `/health` | 健康检查 |

清晰度：`?q=1080|720|480`，不传则取最高。

## STRM 与媒体库

导出结果：

```
strm/
├─ 短剧/
│  └─ 予苍龙 (2026)/
│     ├─ tvshow.nfo
│     ├─ poster.jpg / fanart.jpg
│     └─ Season 01/
│        ├─ 予苍龙 S01E01.strm      ← 内容是 http://<你的服务>/play/147716/1326079.m3u8
│        ├─ 予苍龙 S01E01.nfo
│        └─ ...
└─ 电影/
   └─ 某电影 (2026)/
      ├─ 某电影.strm
      └─ movie.nfo
```

`.strm` 就是一行文本，里面是**取流接口地址**，不是真实链接——所以链接过期不需要重新导出。

Emby / Jellyfin 建库：

- 电影 → 内容类型选「电影」，目录指向 `strm/电影`
- 电视剧/综艺/动漫/短剧 → 内容类型选「电视节目」，目录分别指向对应子目录（或直接指向 `strm`，它会自动识别 `Season 01` 结构）

Kodi：直接添加 `strm` 目录为视频源即可。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `JP_PUBLIC_BASE` | `http://127.0.0.1:8989` | **写进 .strm 的地址**，必须改成播放器能访问的局域网 IP |
| `JP_LISTEN_PORT` | `8989` | 服务端口 |
| `JP_TOKEN` | 空 | 非空时所有接口要带 `?token=xxx`（会写进 .strm） |
| `JP_DB` | `data/jinpai.db` | 数据库路径 |
| `JP_STRM_ROOT` | `strm` | STRM 导出根目录 |
| `JP_CACHE_TTL` | `1200` | 取流缓存秒数 |
| `JP_DEFAULT_QUALITY` | `1080` | 默认清晰度 |

例：

```powershell
$env:JP_PUBLIC_BASE="http://192.168.1.10:8989"
$env:JP_TOKEN="mysecret"
python server.py
```

## 常见问题

**播放报 403 / 地址失效**
签名过期或源站换了签名算法。服务会自动重取；连续失败说明算法变了，需要更新 `jpapi.py` 里的 `_sign`。

**为什么有 `_manifest.json`**
记录每个 `.strm` 对应的 `vod_id/nid`，导出时只重写有变化的文件。`--clean` 会删掉库里已不存在的 `.strm`。

**源站限流**
`sync.py` 默认每次请求间隔 0.1–0.15 秒，4 并发。被限流就降到 2 并发、加大 `--sleep`。

**定时更新**
Windows 计划任务每天跑一次：

```
python sync.py list
python sync.py detail --workers 4
python strm.py --nfo --images --clean
```

## 注意

- 本项目只做**目录索引 + 实时换签**，不下载、不转存视频文件
- 内容来自第三方聚合站，仅供个人技术研究与自用，请勿对外提供服务或再分发
