# app.py — Mooncake Payout Backend (Render-ready)
# Flask + Firestore + Solana SPL 发放（安全：私钥仅从环境变量读取）
# 前端 ACTIVITY_ID = "mid-autumn-2025"

import os
import json
import time
from dataclasses import dataclass
from typing import Optional, List

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# Firestore Admin
import firebase_admin
from firebase_admin import credentials, firestore

# Solana
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from solana.publickey import PublicKey
from solana.keypair import Keypair

# SPL Token helpers（该模块随 solana 包提供，无需额外安装 spl-token）
from spl.token.instructions import (
    get_associated_token_address,
    create_associated_token_account,
    transfer_checked,
)

load_dotenv()

# -------------------------
# Flask & CORS
# -------------------------
app = Flask(__name__)
# 如需严格白名单，把 origins 改成 ["https://你的前端域名"]
CORS(app, resources={r"/*": {"origins": "*"}})

# -------------------------
# Required Config (via env)
# -------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-admin-token")  # 管理令牌（保护发放接口）
ACTIVITY_ID = os.getenv("ACTIVITY_ID", "mid-autumn-2025")  # 与前端一致
RPC_ENDPOINT = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# 代币 Mint（上线后填到 Render 环境变量）
MINT_ADDRESS = os.getenv("SPL_MINT", "")        # 例如:  So11111111111111111111111111111111111111112
MINT_DECIMALS = int(os.getenv("SPL_DECIMALS", "6"))

# 金库私钥（Phantom 导出的 Base58 私钥 或 64 字节 JSON 数组）
TREASURY_SECRET = os.getenv("SOLANA_TREASURY_SECRET_KEY", "")

# Firebase Admin（服务账号 JSON 文件）
FIREBASE_CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service-account.json")

# Firestore Collections
PAYOUTS_COLL_PATH = f"activities/{ACTIVITY_ID}/payouts"
PARTICIPANTS_COLL_PATH = f"activities/{ACTIVITY_ID}/participants"

# 批量/速率参数
BATCH_LIMIT = int(os.getenv("PAYOUT_BATCH_LIMIT", "20"))
SLEEP_BETWEEN_TX = float(os.getenv("PAYOUT_SLEEP", "0.5"))

# -------------------------
# Bootstrap Firestore
# -------------------------
if not firebase_admin._apps:
    if not os.path.exists(FIREBASE_CRED_PATH):
        raise RuntimeError(
            "Missing Firebase service-account.json. "
            "Upload it as a Secret File on Render and set GOOGLE_APPLICATION_CREDENTIALS to that path."
        )
    cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# -------------------------
# Bootstrap Solana client & treasury
# -------------------------
client = Client(RPC_ENDPOINT)

def _load_keypair(secret: str) -> Keypair:
    if not secret:
        raise RuntimeError("SOLANA_TREASURY_SECRET_KEY not set")
    # 尝试当作 JSON 数组
    try:
        arr = json.loads(secret)
        if isinstance(arr, list):
            return Keypair.from_secret_key(bytes(arr))
    except json.JSONDecodeError:
        pass
    # 否则当作 Phantom Base58 私钥
    return Keypair.from_base58_string(secret)

treasury_kp = _load_keypair(TREASURY_SECRET)
TREASURY_PUB = treasury_kp.public_key

# -------------------------
# Dataclass
# -------------------------
@dataclass
class PayoutItem:
    id: str
    to_address: str
    amount: float
    status: str = "pending"  # pending | paid | failed
    tx: Optional[str] = None
    note: Optional[str] = None

# -------------------------
# Helpers
# -------------------------
def _require_admin(req) -> bool:
    token = req.headers.get("X-Admin-Token") or req.args.get("token")
    return token == ADMIN_TOKEN

def _ui_to_base(amount_ui: float) -> int:
    return int(round(amount_ui * (10 ** MINT_DECIMALS)))

def _fetch_pending(limit: int) -> List[PayoutItem]:
    qs = (
        db.collection(PAYOUTS_COLL_PATH)
        .where("status", "==", "pending")
        .order_by("createdAt")
        .limit(limit)
        .stream()
    )
    out: List[PayoutItem] = []
    for doc_snap in qs:
        data = doc_snap.to_dict() or {}
        out.append(
            PayoutItem(
                id=doc_snap.id,
                to_address=(data.get("address") or "").strip(),
                amount=float(data.get("amount") or 0),
                status=data.get("status", "pending"),
                tx=data.get("tx"),
                note=data.get("note"),
            )
        )
    return out

def _mark_paid(item: PayoutItem, sig: str):
    db.document(f"{PAYOUTS_COLL_PATH}/{item.id}").set(
        {
            "status": "paid",
            "tx": sig,
            "paidAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

def _mark_failed(item: PayoutItem, note: str):
    db.document(f"{PAYOUTS_COLL_PATH}/{item.id}").set(
        {
            "status": "failed",
            "note": note,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

def _ensure_ata(owner: PublicKey, mint: PublicKey, payer: Keypair) -> PublicKey:
    """确保 owner 的 ATA 存在；若无则由 payer(=treasury) 代付租金创建。"""
    ata = get_associated_token_address(owner, mint)
    resp = client.get_account_info(ata)
    if resp.get("result", {}).get("value") is None:
        tx = Transaction()
        # ✅ 修正参数顺序：payer.pubkey 作为费用支付者；owner 是目标账户的拥有者；第三参才是 mint
        tx.add(create_associated_token_account(payer.public_key, owner, mint))
        res = client.send_transaction(tx, payer, opts=TxOpts(skip_preflight=False))
        sig = res.get("result")
        client.confirm_transaction(sig)
    return ata

def _send_spl(to_addr: str, ui_amount: float) -> str:
    if not MINT_ADDRESS:
        raise RuntimeError("SPL_MINT not set. 请在 Render 环境变量中设置你的代币 Mint 地址。")
    mint_pk = PublicKey(MINT_ADDRESS)
    dest_owner = PublicKey(to_addr)

    # 确保对方 ATA 存在
    dest_ata = _ensure_ata(dest_owner, mint_pk, treasury_kp)

    # 金库源 ATA（建议上线前确认已存在并有余额）
    source_ata = get_associated_token_address(TREASURY_PUB, mint_pk)

    amount = _ui_to_base(ui_amount)

    tx = Transaction()
    tx.add(
        transfer_checked(
            source=source_ata,
            mint=mint_pk,
            dest=dest_ata,
            owner=TREASURY_PUB,
            amount=amount,
            decimals=MINT_DECIMALS,
            signers=[treasury_kp],
        )
    )
    res = client.send_transaction(tx, treasury_kp, opts=TxOpts(skip_preflight=False))
    sig = res.get("result")
    client.confirm_transaction(sig)
    return sig

# -------------------------
# Endpoints
# -------------------------
@app.get("/")
def root():
    # 提供给前端显示透明度（公钥/address），不会泄露私钥
    return jsonify({
        "ok": True,
        "activity": ACTIVITY_ID,
        "treasury": str(TREASURY_PUB),
        "mint": MINT_ADDRESS,
        "rpc": RPC_ENDPOINT,
    })

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.post("/payout/enqueue")
def enqueue():
    """手动入队一条发放记录（便于测试）。需要 X-Admin-Token。Body: {address, amount, note?}"""
    if not _require_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(force=True)
    address = (body.get("address") or "").strip()
    amount = float(body.get("amount") or 0)
    note = body.get("note")

    if not address or amount <= 0:
        return jsonify({"ok": False, "error": "address/amount invalid"}), 400

    doc_ref = db.collection(PAYOUTS_COLL_PATH).document()
    doc_ref.set({
        "address": address,
        "amount": amount,
        "status": "pending",
        "createdAt": firestore.SERVER_TIMESTAMP,
        "note": note,
    })
    return jsonify({"ok": True, "id": doc_ref.id})

@app.post("/payout/run")
def run_batch():
    """批量处理 pending 发放（最多 BATCH_LIMIT 条）。需要 X-Admin-Token。"""
    if not _require_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    items = _fetch_pending(BATCH_LIMIT)
    results = []
    for it in items:
        try:
            sig = _send_spl(it.to_address, it.amount)
            _mark_paid(it, sig)
            results.append({"id": it.id, "status": "paid", "tx": sig})
            time.sleep(SLEEP_BETWEEN_TX)
        except Exception as e:
            _mark_failed(it, str(e))
            results.append({"id": it.id, "status": "failed", "error": str(e)})

    return jsonify({"ok": True, "processed": len(items), "results": results})

if __name__ == "__main__":
    # 在 Render 会注入 PORT 环境变量；本地可直接 python app.py 运行
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


