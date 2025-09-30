# app.py — Mooncake Lottery + Payout Backend (Render-ready)
# 功能：/config（奖池配置） /draw（抽奖） /payout/enqueue（手动入队） /payout/run（批量打币）
# 说明：仅当奖品 type="SPL" 时才会写入 payouts 等待打币；其余（NONE/OFFCHAIN）仅落库抽奖记录。

import os
import json
import time
import random
import hashlib
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

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

# SPL Token helpers（随 solana 包提供）
from spl.token.instructions import (
    get_associated_token_address,
    create_associated_token_account,
    transfer_checked,
)

# Base58（兜底）
import base58

load_dotenv()

# -------------------------
# Flask & CORS
# -------------------------
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": os.getenv("CORS_ORIGINS", "*").split(",")}})

# -------------------------
# Required Config (via env)
# -------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-admin-token")              # 管理令牌
ACTIVITY_ID = os.getenv("ACTIVITY_ID", "mid-autumn-2025")
RPC_ENDPOINT = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# 代币 Mint（上线后填）
MINT_ADDRESS = os.getenv("SPL_MINT", "")        # 例如 So11111111111111111111111111111111111111112
MINT_DECIMALS = int(os.getenv("SPL_DECIMALS", "6"))

# 金库私钥（Base58 或 Phantom 导出的 JSON 数组）
TREASURY_SECRET = os.getenv("SOLANA_TREASURY_SECRET_KEY", "")

# Firebase Admin（服务账号 JSON 文件）
FIREBASE_CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service-account.json")

# Firestore Collections
BASE_ACTIVITY_PATH = f"activities/{ACTIVITY_ID}"
PAYOUTS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/payouts"
PARTICIPANTS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/participants"  # 预留
DRAWS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/draws"                # 抽奖记录
CONFIG_DOC_PATH = f"{BASE_ACTIVITY_PATH}/config"               # 配置快照（可选）

# 批量/速率参数
BATCH_LIMIT = int(os.getenv("PAYOUT_BATCH_LIMIT", "20"))
SLEEP_BETWEEN_TX = float(os.getenv("PAYOUT_SLEEP", "0.5"))

# -------------------------
# Lottery Prize Table & Profile
#   两套奖池：real（真实） / fake（演示）
#   你也可以用 PRIZE_TABLE_JSON 覆盖整张表（JSON 字符串）。
#   字段含义：
#     id, label, type: "NONE" | "OFFCHAIN" | "SPL", amount(仅 SPL 用 UI 单位), weight(整数权重)
# -------------------------
LOTTERY_PROFILE = os.getenv("LOTTERY_PROFILE", "real").lower()  # "real" | "fake"
PRIZE_TABLE_JSON = os.getenv("PRIZE_TABLE_JSON", "")

def _get_prize_table() -> List[Dict[str, Any]]:
    # 优先用环境变量提供的 JSON
    if PRIZE_TABLE_JSON:
        try:
            data = json.loads(PRIZE_TABLE_JSON)
            if isinstance(data, list):
                return data
        except Exception:
            pass

    if LOTTERY_PROFILE == "fake":
        # 假（演示）奖池（总和 100%）
        # 你在“假”里多了一项 Random 大奖（不自动上链，线下登记）
        return [
            {"id":"mooncake","label":"Mooncake","type":"OFFCHAIN","weight":5},
            {"id":"better-luck","label":"下次更好运","type":"NONE","weight":69},  # 69%（保证总和=100）
            {"id":"moon-10k","label":"10,000 $MOON","type":"SPL","amount":10000,"weight":20},
            {"id":"moon-50k","label":"50,000 $MOON","type":"SPL","amount":50000,"weight":3},
            {"id":"moon-100k","label":"100,000 $MOON","type":"SPL","amount":100000,"weight":2},
            {"id":"random-big","label":"Random: 1 SOL / 1 BTC / 1 ETH（线下登记）","type":"OFFCHAIN","weight":1},
        ]
    else:
        # 真实奖池（总和 100%）
        return [
            {"id":"mooncake","label":"Mooncake","type":"OFFCHAIN","weight":5},
            {"id":"better-luck","label":"下次更好运","type":"NONE","weight":70},
            {"id":"moon-10k","label":"10,000 $MOON","type":"SPL","amount":10000,"weight":20},
            {"id":"moon-50k","label":"50,000 $MOON","type":"SPL","amount":50000,"weight":3},
            {"id":"moon-100k","label":"100,000 $MOON","type":"SPL","amount":100000,"weight":2},
        ]

PRIZE_TABLE: List[Dict[str, Any]] = _get_prize_table()

# 每个钱包最多抽奖次数（0 表示不限制）
MAX_DRAWS_PER_WALLET = int(os.getenv("MAX_DRAWS_PER_WALLET", "0"))

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

def _keypair_from_secret_bytes(secret_bytes: bytes) -> Keypair:
    try:
        return Keypair.from_secret_key(secret_bytes)
    except Exception as e:
        raise RuntimeError(f"Invalid secret key bytes: {e}")

def _load_keypair(secret: str) -> Keypair:
    if not secret:
        raise RuntimeError("SOLANA_TREASURY_SECRET_KEY not set")
    # 尝试 JSON 数组（Phantom 导出为 keypair 数组）
    try:
        arr = json.loads(secret)
        if isinstance(arr, list):
            return _keypair_from_secret_bytes(bytes(arr))
    except json.JSONDecodeError:
        pass
    # 否则 Base58
    raw = base58.b58decode(secret)
    return _keypair_from_secret_bytes(raw)

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
        # 参数：payer_pubkey（支付租金）、owner（目标账户拥有者）、mint
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
            owner=TREASURY_PUB,    # ✅ 修复：兼容所有 Python 版本（不使用海象运算符）
            amount=amount,
            decimals=MINT_DECIMALS,
            signers=[treasury_kp],
        )
    )
    res = client.send_transaction(tx, treasury_kp, opts=TxOpts(skip_preflight=False))
    sig = res.get("result")
    client.confirm_transaction(sig)
    return sig

def _weighted_choice(items: List[Dict[str, Any]], rnd: random.Random) -> Dict[str, Any]:
    weights = [max(0, int(it.get("weight", 0))) for it in items]
    total = sum(weights)
    if total <= 0:
        return items[0]
    pick = rnd.randint(1, total)
    acc = 0
    for it, w in zip(items, weights):
        acc += w
        if pick <= acc:
            return it
    return items[-1]

def _safe_pubkey_str(s: str) -> Optional[str]:
    try:
        _ = PublicKey(s)
        return s
    except Exception:
        return None

def _count_wallet_draws(wallet: str) -> int:
    qs = (
        db.collection(DRAWS_COLL_PATH)
        .where("wallet", "==", wallet)
        .stream()
    )
    return sum(1 for _ in qs)

def _enqueue_payout(address: str, amount: float, note: Optional[str]):
    doc_ref = db.collection(PAYOUTS_COLL_PATH).document()
    doc_ref.set({
        "address": address,
        "amount": float(amount),
        "status": "pending",
        "createdAt": firestore.SERVER_TIMESTAMP,
        "note": note,
    })
    return doc_ref.id

# -------------------------
# Endpoints - 基本
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
        "profile": LOTTERY_PROFILE,
    })

@app.get("/health")
def health():
    return jsonify(status="ok")

# -------------------------
# Endpoints - Lottery
# -------------------------
@app.get("/config")
def get_config():
    """给前端读取活动配置：奖池、Mint、限制等。"""
    cfg = {
        "activity": ACTIVITY_ID,
        "mint": MINT_ADDRESS,
        "mintDecimals": MINT_DECIMALS,
        "maxDrawsPerWallet": MAX_DRAWS_PER_WALLET,
        "profile": LOTTERY_PROFILE,
        "prizes": PRIZE_TABLE,  # label / type / amount / weight
    }
    # 同步快照到 Firestore（可选）
    db.document(CONFIG_DOC_PATH).set({**cfg, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
    return jsonify({"ok": True, "config": cfg})

@app.post("/draw")
def draw_once():
    """
    真正抽一次。Body: { "wallet": "<SolanaPubkey>", "clientSeed": "<optional string>" }
    - 校验钱包格式
    - 限制抽奖次数（可选）
    - 权重抽奖
    - 记录到 Firestore /draws
    - 若奖励为 "SPL"，自动写入 payouts pending 队列
    返回: { ok, prize, drawId, maybe: {payoutId} }
    """
    body = request.get_json(force=True) if request.data else {}
    wallet_raw = (body.get("wallet") or "").strip()
    client_seed = (body.get("clientSeed") or "").strip()

    wallet = _safe_pubkey_str(wallet_raw)
    if not wallet:
        return jsonify({"ok": False, "error": "invalid wallet"}), 400

    # 限制抽奖次数
    if MAX_DRAWS_PER_WALLET > 0:
        cnt = _count_wallet_draws(wallet)
        if cnt >= MAX_DRAWS_PER_WALLET:
            return jsonify({"ok": False, "error": "draw limit reached", "count": cnt}), 403

    # 伪随机：活动ID + 钱包 + 时间 + clientSeed
    now_ms = int(time.time() * 1000)
    salt = f"{ACTIVITY_ID}|{wallet}|{now_ms}|{client_seed}".encode("utf-8")
    seed_int = int(hashlib.sha256(salt).hexdigest(), 16)
    rnd = random.Random(seed_int)

    prize = _weighted_choice(PRIZE_TABLE, rnd)

    draw_doc = {
        "wallet": wallet,
        "prizeId": prize.get("id"),
        "prizeLabel": prize.get("label"),
        "type": prize.get("type"),
        "amount": float(prize.get("amount", 0)) if prize.get("type") == "SPL" else None,
        "clientSeed": client_seed or None,
        "profile": LOTTERY_PROFILE,
        "timestamp": firestore.SERVER_TIMESTAMP,
    }

    payout_id = None
    # 若是 SPL 上链奖励 -> 自动入队 payouts
    if prize.get("type") == "SPL":
        if not MINT_ADDRESS:
            draw_doc["payoutEnqueued"] = False
            draw_doc["payoutError"] = "SPL_MINT not set"
        else:
            try:
                payout_id = _enqueue_payout(address=wallet, amount=float(prize["amount"]), note=f"draw:{prize['id']}")
                draw_doc["payoutEnqueued"] = True
                draw_doc["payoutId"] = payout_id
            except Exception as e:
                draw_doc["payoutEnqueued"] = False
                draw_doc["payoutError"] = str(e)

    # 落库抽奖记录
    doc_ref = db.collection(DRAWS_COLL_PATH).document()
    doc_ref.set(draw_doc)
    draw_id = doc_ref.id

    return jsonify({
        "ok": True,
        "drawId": draw_id,
        "prize": {
            "id": prize.get("id"),
            "label": prize.get("label"),
            "type": prize.get("type"),
            "amount": prize.get("amount"),
        },
        "payoutId": payout_id,
        "serverTime": now_ms,
    })

# -------------------------
# Endpoints - Payout（原有）
# -------------------------
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))




