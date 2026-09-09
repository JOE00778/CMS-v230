# 一元管理系统 V2.3 · Streamlit 镜像
# 多阶段构建：缩小最终镜像
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libxcb1 \
        libgl1 \
        libglib2.0-0 \
        curl \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt /app/requirements.txt
# 加 psycopg2 + python-lark-oapi（飞书 OAuth）
RUN pip install -r /app/requirements.txt \
    && pip install psycopg2-binary>=2.9 \
    && pip install lark-oapi>=1.4 || true

# 应用代码
COPY . /app

# 数据目录
RUN mkdir -p /app/data/inputs /app/data/outputs /app/data_warehouse

# Streamlit 配置
ENV STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -fsSL http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "cms.py", "--server.port=8501", "--server.address=0.0.0.0"]
