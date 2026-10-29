# 金牌影视 · 本地资源系统
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    JP_DATA_DIR=/app/data \
    JP_STRM_ROOT=/app/strm \
    JP_LISTEN_HOST=0.0.0.0 \
    JP_LISTEN_PORT=8989

# 时区数据（日志与签名时间戳对齐本地时间）
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# 国内网络慢就换成镜像源：
# RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 再拷代码
COPY config.py jpapi.py db.py sync.py strm.py server.py run.py ./
COPY web ./web

RUN mkdir -p /app/data /app/strm

EXPOSE 8989
VOLUME ["/app/data", "/app/strm"]

# 健康检查用 python 自带库，避免装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8989/health',timeout=3).status==200 else 1)"

CMD ["python", "run.py"]
