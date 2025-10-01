FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# 1) 升级 pip
RUN python -m pip install --upgrade pip

# 2) 先卸载任何已存在的同名包，避免覆盖/冲突
RUN pip uninstall -y solana || true

# 3) 安装 solana 生态（强制） + 你的依赖
RUN pip install --no-cache-dir --root-user-action=ignore --force-reinstall \
      solana==0.30.2 solders==0.18.1 base58==2.1.1 && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# 4) 构建时自检：版本 & 子模块都要OK；不OK直接失败，避免上线后才发现
RUN python - <<'PY'
import pkgutil, sys, importlib
try:
    import solana
    v = getattr(solana, "__version__", None)
    print("solana __version__:", v)
    assert v and v.startswith("0.30."), f"unexpected solana version: {v}"
    for mod in ["solana.publickey", "solana.keypair", "solana.transaction", "spl.token.instructions"]:
        assert pkgutil.find_loader(mod), f"missing module: {mod}"
    import solders
    print("solders __version__:", getattr(solders, "__version__", "unknown"))
    print("OK: solana stack ready")
except Exception as e:
    print("BUILD CHECK FAILED:", e)
    sys.exit(1)
PY

COPY . /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# 直接监听 8080，避免 $PORT 展不开的问题
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "-w", "2", "--timeout", "180"]

