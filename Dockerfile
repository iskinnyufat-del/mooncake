# 固定 Python 3.11（避免 3.13 引发的 solders/solana 问题）
FROM python:3.11-slim

# 基础系统依赖（可选，但对部分加速/SSL有帮助）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制 requirements 加速分层缓存
COPY requirements.txt /app/requirements.txt

# 升级 pip 并安装依赖（强制装对 solana 生态）
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir --force-reinstall \
      solana==0.30.2 solders==0.18.1 base58==2.1.1 && \
    pip install --no-cache-dir -r requirements.txt

# 再复制项目源码
COPY . /app

# 环境变量（按需）
ENV PYTHONUNBUFFERED=1

# 暴露端口（Render 会自动映射）
EXPOSE 8080

# 启动命令
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:${PORT}", "-w", "2", "--timeout", "180"]
