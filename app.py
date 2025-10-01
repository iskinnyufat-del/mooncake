# app.py — Mooncake Lottery + Payout Backend (Render-ready)

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

# SPL Token helpers (solana==0.30.x)
from spl.token.instructions import (
    get_associated_token_address,
    create_associated_token_account,
    transfer_checked,
    TransferCheckedParams,
)
from spl.token.constants import TOKEN_PROGRAM_ID

import base58

load_dotenv()

# -------------------------
# Flask & CORS
# -------------------------
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": os.getenv("CORS_ORIGINS", "*").split(",")}})

# -------------------------
# Required Config
# -------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-admin-token")
ACTIVITY_ID = os.getenv("ACTIVITY_ID", "mid-autumn-2025")
RPC_ENDPOINT = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

MINT_ADDRESS = os.getenv("SPL_MINT", "")
MINT_DECIMALS = int(os.getenv("SPL_DECIMALS", "6"))

TREASURY_SECRET = os.getenv("SOLANA_TREASURY_SECRET_KEY", "")

FIREBASE_CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service-account.json")

BASE_ACTIVITY_PATH = f"activities/{ACTIVITY_ID}"
PAYOUTS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/payouts"
PARTICIPANTS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/participants"
DRAWS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/draws"
CONFIG_DOC_PATH = f"{BASE_ACTIVITY_PATH}/config"

BATCH_LIMIT = int(os.getenv("PAYOUT_BATCH_LIMIT", "20"))
SLEEP_BETWEEN_TX = float(os.getenv("PAYOUT_SLEEP", "0.5"))
MAX_DRAWS_PER_WALLET = int(os.getenv("MAX_DRAWS_PER_WALLET", "0"))

# -------------------------
# Lottery Prize Table (Fixed, real logic only)
# -------------------------
PRIZE_TABLE: List[Dict[str, Any]] = [
    {"id": "mooncake", "label": "MOONcake", "type": "OFFCHAIN", "weight": 5},
    {"id": "better-luck", "label": "Better luck next time", "type": "NONE", "weight": 70},
    {"id": "moon-10k", "label": "10,000 $MOON", "type": "SPL", "amount": 10000, "weight": 20},
    {"id": "moon-50k", "label": "50,000 $MOON", "type": "SPL", "amount": 50000, "weight": 3},
    {"id": "moon-100k", "label": "100,000 $MOON", "type": "SPL", "amount": 100000, "weight": 2},
]

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
    return Keypair.from_secret_key(secret_bytes)

def _load_keypair(secret: str) -> Keypair:
    if not secret:
        raise RuntimeError("SOLANA_TREASURY_SECRET_KEY not set")
    # JSON 数组（Phantom 导出）
    try:
        arr = json.loads(secret)
        if isinstance(arr, list):
            return _keypair_from_secret_bytes(bytes(arr))
    except json.JSONDecodeError:
        pass
    # base58 字符串
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
    status: str = "pending"
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
        {"status": "paid", "tx": sig, "paidAt": firestore.SERVER_TIMESTAMP}, merge=True
    )

def _mark_failed(item: PayoutItem, note: str):
    db.document(f"{PAYOUTS_COLL_PATH}/{item.id}").set(
        {"status": "failed", "note": note, "updatedAt": firestore.SERVER_TIMESTAMP},
        merge=True,
    )

def _ensure_ata(owner: PublicKey, mint: PublicKey, payer: Keypair) -> PublicKey:
    ata = get_associated_token_address(owner, mint)
    resp = client.get_account_info(ata)
    if resp.get("result", {}).get("value") is None:
        tx = Transaction()
        tx.add(create_associated_token_account(payer.public_key, owner, mint))
        res = client.send_transaction(tx, payer, opts=TxOpts(skip_preflight=False))
        sig = res.get("result")
        # 显式确认级别
        client.confirm_transaction(sig, commitment="confirmed")
    return ata

def _send_spl(to_addr: str, ui_amount: float) -> str:
    if not MINT_ADDRESS:
        raise RuntimeError("SPL_MINT not set.")
    mint_pk = PublicKey(MINT_ADDRESS)
    dest_owner = PublicKey(to_addr)
    dest_ata = _ensure_ata(dest_owner, mint_pk, treasury_kp)
    source_ata = get_associated_token_address(TREASURY_PUB, mint_pk)
    amount = _ui_to_base(ui_amount)

    tx = Transaction()
    tx.add(
        transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_ata,
                mint=mint_pk,
                dest=dest_ata,
                owner=TREASURY_PUB,
                amount=amount,
                decimals=MINT_DECIMALS,
                signers=None,  # 若使用多签，这里传 [<owner-pubkey-2>, ...]
            )
        )
    )
    res = client.send_transaction(tx, treasury_kp, opts=TxOpts(skip_preflight=False))
    sig = res.get("result")
    client.confirm_transaction(sig, commitment="confirmed")
    return sig

def _weighted_choice(items: List[Dict[str, Any]], rnd: random.Random) -> Dict[str, Any]:
    weights = [max(0, int(it.get("weight", 0))) for it in items]
    total = sum(weights)
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
    qs = db.collection(DRAWS_COLL_PATH).where("wallet", "==", wallet).stream()
    return sum(1 for _ in qs)

def _enqueue_payout(address: str, amount: float, note: Optional[str]):
    doc_ref = db.collection(PAYOUTS_COLL_PATH).document()
    doc_ref.set(
        {
            "address": address,
            "amount": float(amount),
            "status": "pending",
            "createdAt": firestore.SERVER_TIMESTAMP,
            "note": note,
        }
    )
    return doc_ref.id

# -------------------------
# Endpoints
# -------------------------
@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "activity": ACTIVITY_ID,
        "treasury": str(TREASURY_PUB),
        "mint": MINT_ADDRESS,
        "rpc": RPC_ENDPOINT,
        "profile": "real",  # always real
    })

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.get("/config")
def get_config():
    cfg = {
        "activity": ACTIVITY_ID,
        "mint": MINT_ADDRESS,
        "mintDecimals": MINT_DECIMALS,
        "maxDrawsPerWallet": MAX_DRAWS_PER_WALLET,
        "profile": "real",
        "prizes": PRIZE_TABLE,
    }
    try:
        db.document(CONFIG_DOC_PATH).set(
            {**cfg, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True
        )
    except Exception as e:
        app.logger.warning(f"[CONFIG] Firestore snapshot failed: {e}")
    return jsonify({"ok": True, "config": cfg})

@app.post("/draw")
def draw_once():
    body = request.get_json(force=True) if request.data else {}
    wallet_raw = (body.get("wallet") or "").strip()
    client_seed = (body.get("clientSeed") or "").strip()

    wallet = _safe_pubkey_str(wallet_raw)
    if not wallet:
        return jsonify({"ok": False, "error": "invalid wallet"}), 400

    if MAX_DRAWS_PER_WALLET > 0:
        cnt = _count_wallet_draws(wallet)
        if cnt >= MAX_DRAWS_PER_WALLET:
            return jsonify({"ok": False, "error": "draw limit reached", "count": cnt}), 403

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
        "profile": "real",
        "timestamp": firestore.SERVER_TIMESTAMP,
    }

    payout_id = None
    if prize.get("type") == "SPL":
        if not MINT_ADDRESS:
            draw_doc["payoutEnqueued"] = False
            draw_doc["payoutError"] = "SPL_MINT not set"
        else:
            try:
                payout_id = _enqueue_payout(wallet, float(prize["amount"]), f"draw:{prize['id']}")
                draw_doc["payoutEnqueued"] = True
                draw_doc["payoutId"] = payout_id
            except Exception as e:
                draw_doc["payoutEnqueued"] = False
                draw_doc["payoutError"] = str(e)

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

@app.post("/payout/enqueue")
def enqueue():
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








