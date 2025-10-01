# 固定 Python 3.11，避免 3.13 带来的兼容坑
FROM python:3.11-slim

# 基础系统依赖（证书等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷 requirements 以利用构建缓存
COPY requirements.txt /app/requirements.txt

# 升级 pip
RUN python -m pip install --upgrade pip

# 清掉可能残留的冲突包（忽略失败）
RUN pip uninstall -y solders || true && pip uninstall -y solana || true

# 先装你的常规依赖
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# 关键：最后强制覆盖安装“老而稳”的 solana（自带 solana.publickey/solana.keypair）
RUN pip install --no-cache-dir --root-user-action=ignore --force-reinstall \
    solana==0.25.1 base58==2.1.1

# 打一点构建期诊断（可留可删）
RUN python - <<'PY'
import pkgutil
import solana
print(">>> solana __version__ =", getattr(solana, "__version__", None))
for m in ["solana.publickey","solana.keypair","solana.transaction","spl.token.instructions"]:
    print(f">>> has {m}:", bool(pkgutil.find_loader(m)))
PY

# 再拷贝源码
COPY . /app

# 运行时环境
ENV PYTHONUNBUFFERED=1

# Render 会把外部端口映射到容器 8080
EXPOSE 8080

# 直接监听 8080（避免 ${PORT} 在 JSON CMD 里不展开的问题）
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "-w", "2", "--timeout", "180"]



