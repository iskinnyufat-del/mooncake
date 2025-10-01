FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# 升级 pip
RUN python -m pip install --upgrade pip

# 先卸载可能残留的同名包（忽略错误）
RUN pip uninstall -y solana || true

# 强制安装 solana 生态 + 你的依赖（root 用户警告忽略）
RUN pip install --no-cache-dir --root-user-action=ignore --force-reinstall \
      solana==0.30.2 solders==0.18.1 base58==2.1.1 && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# 仅打印诊断信息（不失败）
RUN python - <<'PY'
import pkgutil
try:
    import solana
    print(">>> solana __version__:", getattr(solana, "__version__", None))
except Exception as e:
    print(">>> import solana FAILED:", e)

for mod in ["solana.publickey","solana.keypair","solana.transaction","spl.token.instructions"]:
    print(f">>> has {mod}:", bool(pkgutil.find_loader(mod)))

try:
    import solders
    print(">>> solders __version__:", getattr(solders, "__version__", None))
except Exception as e:
    print(">>> import solders FAILED:", e)
PY

COPY . /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "-w", "2", "--timeout", "180"]


