# app.py — Mooncake Lottery + Payout Backend (legacy-solana API, no solders)

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

# Solana (legacy-style API from solana==0.25.x)
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from solana.publickey import PublicKey
from solana.keypair import Keypair

# SPL Token helpers
from spl.token.instructions import (
    get_associated_token_address,
    create_associated_token_account,
    transfer_checked,
)
from spl.token.constants import TOKEN_PROGRAM_ID

import base58

load_dotenv()

# -------------------------
# Flask & CORS
# -------------------------
app = Flask(__name__)
CORS(
    app,
    resources={r"/*": {"origins": os.getenv("CORS_ORIGINS", "*").split(",")}},
    supports_credentials=True,
)

# -------------------------
# Env helpers
# -------------------------
def _clean_mint(raw: str) -> str:
    """Fix accidental 'SPL_MINT=SPL_MINT=...' values."""
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith("SPL_MINT="):
        s = s.split("=", 1)[1].strip()
    return s

def _read_text_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None

def _load_treasury_secret_from_str(secret: str) -> Keypair:
    """
    Accepts: JSON array (Phantom export) or base58 string.
    """
    if not secret:
        raise RuntimeError("SOLANA_TREASURY_SECRET_KEY not set and no file provided")
    # JSON array
    try:
        arr = json.loads(secret)
        if isinstance(arr, list):
            b = bytes(arr)
            if len(b) != 64:
                raise ValueError("Invalid JSON secret key length (expect 64 bytes)")
            return Keypair.from_secret_key(b)
    except json.JSONDecodeError:
        pass
    # base58
    raw = base58.b58decode(secret)
    if len(raw) != 64:
        raise ValueError("Invalid base58 secret key length (expect 64 bytes)")
    return Keypair.from_secret_key(raw)

def _load_treasury_keypair() -> Keypair:
    """
    Load from file first (SOLANA_TREASURY_SECRET_FILE), else from env (SOLANA_TREASURY_SECRET_KEY).
    File content can be JSON array or base58.
    """
    secret_file = (os.getenv("SOLANA_TREASURY_SECRET_FILE") or "").strip()
    if secret_file:
        content = _read_text_file(secret_file)
        if not content:
            raise RuntimeError(f"Failed to read SOLANA_TREASURY_SECRET_FILE: {secret_file}")
        return _load_treasury_secret_from_str(content)

    secret_env = (os.getenv("SOLANA_TREASURY_SECRET_KEY") or "").strip()
    if not secret_env:
        raise RuntimeError("Missing SOLANA_TREASURY_SECRET_KEY (or set SOLANA_TREASURY_SECRET_FILE)")
    return _load_treasury_secret_from_str(secret_env)

# -------------------------
# Required Config
# -------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "dev-admin-token")
ACTIVITY_ID = os.getenv("ACTIVITY_ID", "mid-autumn-2025")

RPC_ENDPOINT = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()

MINT_ADDRESS = _clean_mint(os.getenv("SPL_MINT", ""))
MINT_DECIMALS = int(os.getenv("SPL_DECIMALS", "6"))

FIREBASE_CRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/secrets/service-account.json")

BASE_ACTIVITY_PATH = f"activities/{ACTIVITY_ID}"
PAYOUTS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/payouts"
PARTICIPANTS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/participants"
DRAWS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/draws"
CONFIG_DOC_PATH = f"{BASE_ACTIVITY_PATH}/config"
PAYMENTS_COLL_PATH = f"{BASE_ACTIVITY_PATH}/payments"  # record used paySig to prevent replay

BATCH_LIMIT = int(os.getenv("PAYOUT_BATCH_LIMIT", "20"))
SLEEP_BETWEEN_TX = float(os.getenv("PAYOUT_SLEEP", "0.5"))
MAX_DRAWS_PER_WALLET = int(os.getenv("MAX_DRAWS_PER_WALLET", "0"))

# paid draw cost (UI units)
DRAW_COST_UI = float(os.getenv("DRAW_COST_UI", "10000"))

# ---- New: payout mode switches ----
IMMEDIATE_PAYOUT = os.getenv("IMMEDIATE_PAYOUT", "").lower() in ("1", "true", "yes")
PAYOUT_MODE = os.getenv("PAYOUT_MODE", "queue").strip().lower()  # "queue" | "immediate" | "hybrid"
if IMMEDIATE_PAYOUT and PAYOUT_MODE == "queue":
    # Keep backward-compat convenience switch
    PAYOUT_MODE = "immediate"

# -------------------------
# Lottery Prize Table
# -------------------------
PRIZE_TABLE: List[Dict[str, Any]] = [
    {"id": "mooncake", "label": "MOONcake", "type": "OFFCHAIN", "weight": 5},
    {"id": "better-luck", "label": "Better luck next time", "type": "NONE", "weight": 70},
    {"id": "moon-10k", "label": "10,000 $MOON", "type": "SPL", "amount": 10000, "weight": 20},
    {"id": "moon-50k", "label": "50,000 $MOON", "type": "SPL", "amount": 50000, "weight": 3},
    {"id": "moon-100k", "label": "100,000 $MOON", "type": "SPL", "amount": 100000, "weight": 2},
]

# -------------------------
# Bootstrap Firestore (fail-safe)
# -------------------------
def _init_firestore_or_log():
    try:
        if not os.path.exists(FIREBASE_CRED_PATH):
            raise FileNotFoundError(f"GAC not found at {FIREBASE_CRED_PATH}")
        with open(FIREBASE_CRED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        app.logger.error(f"[FIREBASE] credentials load failed: {e}")
        return None

db = _init_firestore_or_log()

def _require_db():
    if db is None:
        return jsonify({"ok": False, "error": "firestore_not_ready", "hint": "Check service-account.json"}), 503
    return None

# -------------------------
# Bootstrap Solana client & treasury
# -------------------------
client = Client(RPC_ENDPOINT)

try:
    treasury_kp = _load_treasury_keypair()
except Exception as e:
    # Fail fast with clear error (so you see it immediately in logs)
    raise RuntimeError(f"Load treasury keypair failed: {e}")

TREASURY_PUB: PublicKey = treasury_kp.public_key

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
    err = _require_db()
    if err:  # 503
        raise RuntimeError("firestore_not_ready")
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
    if db is None: return
    db.document(f"{PAYOUTS_COLL_PATH}/{item.id}").set(
        {"status": "paid", "tx": sig, "paidAt": firestore.SERVER_TIMESTAMP}, merge=True
    )

def _mark_failed(item: PayoutItem, note: str):
    if db is None: return
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
        client.confirm_transaction(sig)
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
            source=source_ata,
            mint=mint_pk,
            dest=dest_ata,
            owner=TREASURY_PUB,
            amount=amount,
            decimals=MINT_DECIMALS,
            program_id=TOKEN_PROGRAM_ID,
            signers=None,
        )
    )
    res = client.send_transaction(tx, treasury_kp, opts=TxOpts(skip_preflight=False))
    sig = res.get("result")
    client.confirm_transaction(sig)
    return sig

# ---- New: safe wrapper for immediate payout ----
def _send_spl_safe(to_addr: str, ui_amount: float):
    try:
        sig = _send_spl(to_addr, ui_amount)
        return True, sig, None
    except Exception as e:
        return False, None, str(e)

def _weighted_choice(items: List[Dict[str, Any]], rnd: random.Random) -> Dict[str, Any]:
    weights = [max(0, int(it.get("weight", 0))) for it in items]
    total = sum(weights) or 1
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
    err = _require_db()
    if err: return 0
    qs = db.collection(DRAWS_COLL_PATH).where("wallet", "==", wallet).stream()
    return sum(1 for _ in qs)

def _enqueue_payout(address: str, amount: float, note: Optional[str]):
    err = _require_db()
    if err: raise RuntimeError("firestore_not_ready")
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

# ---------- Payment validation helpers ----------
def _get_tx_json(signature: str) -> Optional[Dict[str, Any]]:
    """Try get_transaction first, fall back to get_confirmed_transaction."""
    try:
        resp = client.get_transaction(signature, commitment="confirmed", encoding="jsonParsed")
        if resp and resp.get("result"):
            return resp["result"]
    except Exception:
        pass
    try:
        resp = client.get_confirmed_transaction(signature, commitment="confirmed")
        if resp and resp.get("result"):
            return resp["result"]
    except Exception:
        pass
    return None

def _is_pay_sig_used(sig: str) -> bool:
    if db is None:
        return False
    doc = db.document(f"{PAYMENTS_COLL_PATH}/{sig}").get()
    return doc.exists

def _mark_pay_sig_used(sig: str, wallet: str, amount_ui: float):
    if db is None:
        return
    db.document(f"{PAYMENTS_COLL_PATH}/{sig}").set(
        {
            "wallet": wallet,
            "amount": float(amount_ui),
            "mint": MINT_ADDRESS,
            "decimals": MINT_DECIMALS,
            "usedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

def _validate_payment(pay_sig: str, wallet: str) -> Optional[str]:
    """
    Returns None if valid; otherwise returns error string.
    Validation:
      - tx exists & meta.err is None
      - contains a Token Program 'transferChecked' instruction
      - authority == wallet
      - source == ATA(wallet, mint); destination == ATA(treasury, mint)
      - mint == MINT_ADDRESS
      - tokenAmount.decimals == MINT_DECIMALS and uiAmount == DRAW_COST_UI
    """
    if not pay_sig:
        return "missing paySig"
    if _is_pay_sig_used(pay_sig):
        return "paySig already used"
    if not MINT_ADDRESS:
        return "server_mint_not_set"

    tx = _get_tx_json(pay_sig)
    if not tx:
        return "tx_not_found"

    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return "tx_failed"

    tx_msg = (tx.get("transaction") or {}).get("message") or {}
    instrs = tx_msg.get("instructions") or []

    try:
        mint_pk = PublicKey(MINT_ADDRESS)
    except Exception:
        return "server_mint_invalid"

    try:
        wallet_pk = PublicKey(wallet)
    except Exception:
        return "wallet_invalid"

    source_ata = str(get_associated_token_address(wallet_pk, mint_pk))
    dest_ata   = str(get_associated_token_address(TREASURY_PUB, mint_pk))

    found_ok = False
    for ix in instrs:
        parsed = ix.get("parsed")
        if not parsed:
            continue
        if parsed.get("type") != "transferChecked":
            continue
        info = parsed.get("info") or {}
        program = ix.get("program")
        program_id = ix.get("programId")
        if not (program == "spl-token" or str(program_id) == str(TOKEN_PROGRAM_ID)):
            continue

        authority = info.get("authority")
        src = info.get("source")
        dst = info.get("destination")
        ix_mint = info.get("mint")
        token_amount = info.get("tokenAmount") or {}
        ui_amount = float(token_amount.get("uiAmount") or 0)
        decimals = int(token_amount.get("decimals") or 0)

        if (
            str(authority) == wallet
            and str(src) == source_ata
            and str(dst) == dest_ata
            and str(ix_mint) == MINT_ADDRESS
            and decimals == MINT_DECIMALS
            and abs(ui_amount - float(DRAW_COST_UI)) < 1e-9
        ):
            found_ok = True
            break

    if not found_ok:
        return "tx_not_match_expected_payment"

    return None

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
        "profile": "real",
    })

@app.get("/status")
def status():
    return jsonify({
        "ok": True,
        "activity": ACTIVITY_ID,
        "treasury": str(TREASURY_PUB),
        "mint": MINT_ADDRESS,
        "decimals": MINT_DECIMALS,
        "rpc": RPC_ENDPOINT,
        "costUi": float(DRAW_COST_UI),
    })

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.get("/config")
def get_config():
    """
    前端期望的扁平结构（关键对齐）：
    {
      ok, activity, mint, decimals, treasury, rpc, price, prizes, maxDrawsPerWallet, payoutMode, immediatePayout
    }
    同时保留 {"config": {...}} 兼容旧前端。
    """
    flat = {
        "activity": ACTIVITY_ID,
        "mint": MINT_ADDRESS,
        "decimals": MINT_DECIMALS,
        "treasury": str(TREASURY_PUB),
        "rpc": RPC_ENDPOINT,
        "price": float(DRAW_COST_UI),            # 前端 PRICE_UI
        "prizes": PRIZE_TABLE,
        "maxDrawsPerWallet": MAX_DRAWS_PER_WALLET,
        "profile": "real",
        "payoutMode": PAYOUT_MODE,
        "immediatePayout": (PAYOUT_MODE == "immediate"),
    }
    legacy = {
        "activity": ACTIVITY_ID,
        "mint": MINT_ADDRESS,
        "mintDecimals": MINT_DECIMALS,
        "maxDrawsPerWallet": MAX_DRAWS_PER_WALLET,
        "profile": "real",
        "prizes": PRIZE_TABLE,
        "drawCostUi": float(DRAW_COST_UI),
    }

    # 快照到 Firestore（可选）
    if db is not None:
        try:
            db.document(CONFIG_DOC_PATH).set(
                {**flat, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True
            )
        except Exception as e:
            app.logger.warning(f"[CONFIG] Firestore snapshot failed: {e}")

    return jsonify({"ok": True, **flat, "config": legacy})

@app.post("/draw")
def draw_once():
    body = request.get_json(force=True) if request.data else {}
    wallet_raw = (body.get("wallet") or "").strip()
    client_seed = (body.get("clientSeed") or "").strip()
    pay_sig = (body.get("paySig") or "").strip() or None

    wallet = _safe_pubkey_str(wallet_raw)
    if not wallet:
        return jsonify({"ok": False, "error": "invalid wallet"}), 400

    # Free for first draw, paid afterwards
    current_cnt = _count_wallet_draws(wallet)
    need_pay = current_cnt >= 1

    if need_pay:
        err = _validate_payment(pay_sig or "", wallet)
        if err is not None:
            return jsonify({"ok": False, "error": f"payment_invalid:{err}"}), 400
        _mark_pay_sig_used(pay_sig, wallet, float(DRAW_COST_UI))

    if MAX_DRAWS_PER_WALLET > 0:
        if current_cnt >= MAX_DRAWS_PER_WALLET:
            return jsonify({"ok": False, "error": "draw limit reached", "count": current_cnt}), 403

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
        "timestamp": firestore.SERVER_TIMESTAMP if db is not None else None,
        "paySig": pay_sig if need_pay else None,
    }

    payout_id = None
    payout_tx = None  # 新增：即时发放的交易哈希

    if prize.get("type") == "SPL":
        if not MINT_ADDRESS:
            draw_doc["payoutEnqueued"] = False
            draw_doc["payoutError"] = "SPL_MINT not set"
        else:
            # 根据模式决定如何发放
            mode = PAYOUT_MODE  # "queue" | "immediate" | "hybrid"
            if mode == "immediate":
                ok, sig, err = _send_spl_safe(wallet, float(prize["amount"]))
                if ok and sig:
                    payout_tx = sig
                    draw_doc["payoutImmediate"] = True
                    draw_doc["payoutTx"] = sig
                    draw_doc["payoutEnqueued"] = False
                else:
                    draw_doc["payoutImmediate"] = False
                    draw_doc["payoutError"] = err or "immediate payout failed"

            elif mode == "hybrid":
                ok, sig, err = _send_spl_safe(wallet, float(prize["amount"]))
                if ok and sig:
                    payout_tx = sig
                    draw_doc["payoutImmediate"] = True
                    draw_doc["payoutTx"] = sig
                    draw_doc["payoutEnqueued"] = False
                else:
                    draw_doc["payoutImmediate"] = False
                    draw_doc["payoutError"] = err or "immediate payout failed"
                    if db is not None:
                        try:
                            payout_id = _enqueue_payout(wallet, float(prize["amount"]), f"draw:{prize['id']}")
                            draw_doc["payoutEnqueued"] = True
                            draw_doc["payoutId"] = payout_id
                        except Exception as e:
                            draw_doc["payoutEnqueued"] = False
                            draw_doc["payoutError"] = f"{draw_doc.get('payoutError','')}; enqueue_failed:{e}"

            else:  # "queue"（保持原有逻辑）
                if db is None:
                    draw_doc["payoutEnqueued"] = False
                    draw_doc["payoutError"] = "firestore_not_ready"
                else:
                    try:
                        payout_id = _enqueue_payout(wallet, float(prize["amount"]), f"draw:{prize['id']}")
                        draw_doc["payoutEnqueued"] = True
                        draw_doc["payoutId"] = payout_id
                    except Exception as e:
                        draw_doc["payoutEnqueued"] = False
                        draw_doc["payoutError"] = str(e)

    if db is not None:
        try:
            doc_ref = db.collection(DRAWS_COLL_PATH).document()
            doc_ref.set(draw_doc)
            draw_id = doc_ref.id
        except Exception as e:
            app.logger.warning(f"[DRAW] write failed: {e}")
            draw_id = None
    else:
        draw_id = None

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
        "payoutTx": payout_tx,   # 新增：即时发放成功时返回
        "serverTime": now_ms,
        "needPay": need_pay,
    })

@app.post("/payout/enqueue")
def enqueue():
    if not _require_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if db is None:
        return jsonify({"ok": False, "error": "firestore_not_ready"}), 503
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
    if db is None:
        return jsonify({"ok": False, "error": "firestore_not_ready"}), 503
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

# -------------------------
# Diagnostics
# -------------------------
@app.get("/versions")
def versions():
    import sys, platform, pkgutil
    info = {
        "python": sys.version,
        "platform": platform.platform(),
        "env": {
            "SOLANA_RPC": os.getenv("SOLANA_RPC"),
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        },
        "checks": {},
    }
    for mod in [
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
        import solders  # optional
        info["solders_version"] = getattr(solders, "__version__", "not_installed")
    except Exception:
        info["solders_version"] = "not_installed"
    info["gac_exists"] = os.path.exists(FIREBASE_CRED_PATH)
    info["mint"] = MINT_ADDRESS
    info["decimals"] = MINT_DECIMALS
    info["treasury_pub"] = str(TREASURY_PUB)
    return jsonify(info)

@app.get("/import-debug")
def import_debug():
    import importlib, inspect, sys
    data = {"paths": sys.path[:15], "modules": {}}
    for mod in ["solana", "solana.publickey", "spl", "spl.token.instructions"]:
        try:
            m = importlib.import_module(mod)
            data["modules"][mod] = {"file": inspect.getfile(m)}
        except Exception as e:
            data["modules"][mod] = {"error": str(e)}
    return jsonify(data)

# 最近中奖名单
@app.get("/draws/latest")
def draws_latest():
    if db is None:
        return jsonify({"ok": False, "error": "firestore_not_ready"}), 503
    try:
        try:
            lim = int(request.args.get("limit", "5"))
        except Exception:
            lim = 5
        lim = max(1, min(lim, 50))

        qs = (
            db.collection(DRAWS_COLL_PATH)
              .order_by("timestamp", direction=firestore.Query.DESCENDING)
              .limit(lim * 3)
              .stream()
        )

        out = []
        for docu in qs:
            d = docu.to_dict() or {}
            typ = str(d.get("type") or "").upper()
            if typ == "NONE":
                continue
            wallet = d.get("wallet") or d.get("address")
            prize_label = d.get("prizeLabel") or (d.get("prize") or {}).get("label")
            ts = d.get("timestamp")
            out.append({
                "wallet": wallet,
                "prizeLabel": pr
                "type": typ,
                "timestamp": ts.isoformat() if ts else None,
            })
            if len(out) >= lim:
                break

        return jsonify({"ok": True, "draws": out})
    except Exception as e:
        app.logger.error(f"/draws/latest error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))












