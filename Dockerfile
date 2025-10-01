# 固定 Python 3.11，避免 solana/solders 在 3.13 出问题
FROM python:3.11-slim

# 基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装依赖
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir --root-user-action=ignore --force-reinstall \
      solana==0.30.2 solders==0.18.1 base58==2.1.1 && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# 复制代码
COPY . /app

# 环境变量
ENV PYTHONUNBUFFERED=1

# Render 默认映射 8080
EXPOSE 8080

# 启动命令（直接监听 8080，不用 $PORT）
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "-w", "2", "--timeout", "180"]

