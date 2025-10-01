# app.py — minimal debug server to verify routes are live

import os, sys, platform, pkgutil, importlib, inspect
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": os.getenv("CORS_ORIGINS", "*").split(",")}})

@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "note": "Minimal debug server is running.",
        "tips": ["open /health", "open /versions", "open /import-debug"]
    })

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.get("/versions")
def versions():
    info = {
        "python": sys.version,
        "platform": platform.platform(),
        "runtime_note": "Expect python-3.11.9 via runtime.txt",
        "env": {
            "SOLANA_RPC": os.getenv("SOLANA_RPC"),
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        },
        "checks": {}
    }
    # 检查几个关键模块是否可见
    for mod in [
        "solana",
        "solana.publickey",
        "solana.keypair",
        "solana.transaction",
        "spl.token.instructions",
    ]:
        info["checks"][mod] = bool(pkgutil.find_loader(mod))
    try:
        import solana
        info["solana_version"] = getattr(solana, "__version__", "unknown")
    except Exception as e:
        info["solana_version_error"] = str(e)
    try:
        import solders
        info["solders_version"] = getattr(solders, "__version__", "unknown")
    except Exception as e:
        info["solders_version_error"] = str(e)
    gac = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or ""
    info["gac_exists"] = os.path.exists(gac) if gac else False
    return jsonify(info)

@app.get("/import-debug")
def import_debug():
    data = {"paths": sys.path[:15], "modules": {}}
    for mod in ["solana", "spl", "base58"]:
        try:
            m = importlib.import_module(mod)
            data["modules"][mod] = {"file": inspect.getfile(m)}
        except Exception as e:
            data["modules"][mod] = {"error": str(e)}
    return jsonify(data)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))












