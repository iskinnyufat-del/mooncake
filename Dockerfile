# 固定 Python 3.11，避免运行时差异
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制 requirements 加速缓存
COPY requirements.txt /app/requirements.txt

# 升级 pip
RUN python -m pip install --upgrade pip

# 先把可能残留的冲突包卸掉（忽略失败）
RUN pip uninstall -y solders || true && pip uninstall -y solana || true

# 关键：安装“老而稳”的 solana 版本（含 solana.publickey）
RUN pip install --no-cache-dir --root-user-action=ignore \
      solana==0.25.2 base58==2.1.1

# 再装你的其他依赖
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# 打印下诊断信息（非必须）
RUN python - <<'PY'
import pkgutil
try:
  import solana
  print(">>> solana __version__:", getattr(solana, "__version__", None))
except Exception as e:
  print(">>> import solana FAILED:", e)
for m in ["solana.publickey","solana.keypair","solana.transaction","spl.token.instructions"]:
  print(f">>> has {m}:", bool(pkgutil.find_loader(m)))
PY

# 复制代码
COPY . /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# 直接监听 8080（Render 会映射外部端口）
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "-w", "2", "--timeout", "180"]


