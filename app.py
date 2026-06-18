import os
import time
import uuid

import requests
from bitcoinlib.wallets import Wallet, wallet_delete_if_exists, wallet_exists
from flask import Flask, jsonify, render_template, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")

MEMPOOL_API = "https://mempool.space/testnet/api"
MEMPOOL_TX_URL = "https://mempool.space/testnet/tx"
REQUIRED_CONFIRMATIONS = 5

# Shop demo settings (testnet only)
SHOP_MERCHANT_ADDRESS = "tb1ql7y0k5xct73ksaq03wrtn6pfvu6fktrxm8p4qj"
SHOP_SNACK_PRICE_SAT = 10_000  # All snacks cost exactly this for simplicity
MERCHANT_WIF = "cQaKzNNXuXUeAzbRrg6QWatqGZddLVeeC6Sgb1cTwTiCVrA4y19g"  # for merchant monitoring demo (read-only view)
INVOICE_TTL_SECONDS = 15 * 60


def get_wallet_name():
    if "wallet_name" not in session:
        session["wallet_name"] = f"web_sender_{uuid.uuid4().hex[:12]}"
    return session["wallet_name"]


def fetch_address_utxos(address):
    response = requests.get(f"{MEMPOOL_API}/address/{address}/utxo", timeout=15)
    response.raise_for_status()
    return response.json()


def fetch_address_txs(address, count=15):
    """Fetch recent transactions involving this address (newest first)."""
    response = requests.get(f"{MEMPOOL_API}/address/{address}/txs", timeout=15)
    response.raise_for_status()
    txs = response.json()
    return txs[:count]


def sync_wallet_utxos(wallet, address):
    utxos = fetch_address_utxos(address)
    for utxo in utxos:
        wallet.utxo_add(
            address,
            utxo["value"],
            utxo["txid"],
            utxo["vout"],
            confirmations=utxo.get("status", {}).get("confirmed", False) and 1 or 0,
        )
    return utxos


def load_or_create_wallet(private_key, derivation_path):
    wallet_name = get_wallet_name()

    if wallet_exists(wallet_name):
        wallet_delete_if_exists(wallet_name, force=True)

    wallet = Wallet.create(
        name=wallet_name,
        keys=private_key,
        network="testnet",
        witness_type="segwit",
        scheme="single",
        key_path=derivation_path,
    )
    # Note: intentionally skip wallet.scan() here. For single-key imported WIFs the scan
    # is unreliable (depth/key list mismatches) and can fail due to upstream provider issues.
    # We populate UTXOs explicitly via mempool.space sync below, which is all the demo needs.

    address = wallet.get_key().address
    mempool_utxos = sync_wallet_utxos(wallet, address)

    balance_sat = sum(utxo["value"] for utxo in mempool_utxos)
    session["wallet_loaded"] = True
    session["wallet_address"] = address
    session["derivation_path"] = derivation_path

    formatted_utxos = [
        {
            "txid": utxo["txid"],
            "vout": utxo["vout"],
            "value_sat": utxo["value"],
            "value_btc": utxo["value"] / 100_000_000,
            "confirmed": utxo.get("status", {}).get("confirmed", False),
            "block_height": utxo.get("status", {}).get("block_height"),
        }
        for utxo in mempool_utxos
    ]

    return {
        "address": address,
        "derivation_path": derivation_path,
        "network": "testnet",
        "balance_sat": balance_sat,
        "balance_btc": balance_sat / 100_000_000,
        "utxo_count": len(formatted_utxos),
        "utxos": formatted_utxos,
    }


def get_loaded_wallet():
    if not session.get("wallet_loaded"):
        raise ValueError("No wallet loaded. Please load a wallet first.")
    return Wallet(get_wallet_name())


def get_wallet_info():
    """Return current wallet snapshot (requires wallet_loaded in session)."""
    if not session.get("wallet_loaded"):
        raise ValueError("No wallet loaded. Please load a wallet first.")

    wallet = get_loaded_wallet()
    address = wallet.get_key().address
    mempool_utxos = sync_wallet_utxos(wallet, address)
    balance_sat = sum(utxo["value"] for utxo in mempool_utxos)

    formatted_utxos = [
        {
            "txid": utxo["txid"],
            "vout": utxo["vout"],
            "value_sat": utxo["value"],
            "value_btc": utxo["value"] / 100_000_000,
            "confirmed": utxo.get("status", {}).get("confirmed", False),
            "block_height": utxo.get("status", {}).get("block_height"),
        }
        for utxo in mempool_utxos
    ]

    return {
        "address": address,
        "derivation_path": session.get("derivation_path", "m/84'/1'/0'/0/0"),
        "network": "testnet",
        "balance_sat": balance_sat,
        "balance_btc": balance_sat / 100_000_000,
        "utxo_count": len(formatted_utxos),
        "utxos": formatted_utxos,
    }


def get_open_invoice():
    """Return the session invoice, marking it expired when past exp."""
    invoice = session.get("open_invoice")
    if not invoice:
        return None
    if invoice.get("status") == "open" and int(time.time()) > invoice.get("expires_at", 0):
        invoice = {**invoice, "status": "expired"}
        session["open_invoice"] = invoice
    return invoice


def evaluate_invoice_match(received_sat, block_time, invoice):
    """
    Compare an incoming merchant payment against the open invoice.
    Returns: None | 'match' | 'amount_match_late'
    """
    if not invoice or invoice.get("status") != "open":
        return None
    if received_sat != invoice.get("amount_sat"):
        return None

    created_at = invoice.get("created_at", 0)
    expires_at = invoice.get("expires_at", 0)
    now = int(time.time())

    if block_time is not None:
        if block_time < created_at:
            return None
        if block_time > expires_at:
            return "amount_match_late"
        return "match"

    if now > expires_at:
        return "amount_match_late"
    return "match"


def format_txid(raw_txid):
    if isinstance(raw_txid, bytes):
        return raw_txid[::-1].hex()
    if hasattr(raw_txid, "hex"):
        return raw_txid.hex()
    return str(raw_txid)


def transaction_details(tx, wallet):
    inputs = []
    for inp in tx.inputs:
        inputs.append(
            {
                "txid": format_txid(inp.prev_txid),
                "vout": inp.output_n_int,
                "value_sat": inp.value,
                "value_btc": inp.value / 100_000_000,
            }
        )

    outputs = []
    for out in tx.outputs:
        outputs.append(
            {
                "address": out.address,
                "value_sat": out.value,
                "value_btc": out.value / 100_000_000,
                "is_change": out.address == wallet.get_key().address,
            }
        )

    return {
        "txid": tx.txid,
        "fee_sat": tx.fee,
        "fee_btc": tx.fee / 100_000_000,
        "size_bytes": tx.size,
        "vsize": getattr(tx, "vsize", None),
        "weight_units": getattr(tx, "weight_units", None),
        "raw_hex_preview": tx.raw_hex()[:120] + "...",
        "inputs": inputs,
        "outputs": outputs,
        "input_count": len(inputs),
        "output_count": len(outputs),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/wallet/load", methods=["POST"])
def load_wallet():
    data = request.json or {}
    private_key = (data.get("private_key") or "").strip()
    derivation_path = (data.get("derivation_path") or "m/84'/1'/0'/0/0").strip()

    if not private_key:
        return jsonify({"success": False, "error": "Private key is required."}), 400

    try:
        wallet_info = load_or_create_wallet(private_key, derivation_path)
        return jsonify({"success": True, "wallet": wallet_info})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/wallet/info", methods=["GET"])
def get_wallet_info_route():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "No wallet loaded."}), 400
    try:
        info = get_wallet_info()
        return jsonify({"success": True, "wallet": info})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/transaction/send", methods=["POST"])
def send_transaction():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "Load a wallet before sending."}), 400

    data = request.json or {}
    to_address = (data.get("to_address") or "").strip()
    amount_btc = data.get("amount_btc")

    if not to_address:
        return jsonify({"success": False, "error": "Destination address is required."}), 400

    try:
        amount_btc = float(amount_btc)
        if amount_btc <= 0:
            raise ValueError("Amount must be greater than zero.")
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": f"Invalid amount: {exc}"}), 400

    try:
        wallet = get_loaded_wallet()
        from_address = wallet.get_key().address
        sync_wallet_utxos(wallet, from_address)

        amount_sat = int(amount_btc * 100_000_000)
        tx = wallet.send_to(
            to_address=to_address,
            amount=amount_sat,
            fee="normal",
            broadcast=False,
        )

        details = transaction_details(tx, wallet)
        details["from_address"] = from_address
        details["to_address"] = to_address
        details["amount_sat"] = amount_sat
        details["amount_btc"] = amount_btc
        details["network"] = "testnet"
        details["fee_rate"] = "normal"
        details["actions"] = [
            "Selected UTXOs to cover the send amount plus network fee",
            "Built a SegWit (P2WPKH) transaction",
            "Signed inputs with your private key",
            "Broadcasting raw transaction to Bitcoin testnet nodes",
        ]

        tx.send()
        txid = tx.txid
        details["txid"] = txid
        details["mempool_url"] = f"{MEMPOOL_TX_URL}/{txid}"

        return jsonify({"success": True, "transaction": details})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/shop/checkout", methods=["POST"])
def shop_checkout():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "Load a wallet before checking out."}), 400

    data = request.json or {}
    total_sats = data.get("total_sats")
    items = data.get("items") or []  # [{id, name, qty, price_sat}, ...] for display only

    try:
        total_sats = int(total_sats)
        if total_sats <= 0:
            raise ValueError("Total must be greater than zero.")
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": f"Invalid total: {exc}"}), 400

    try:
        wallet = get_loaded_wallet()
        from_address = wallet.get_key().address
        sync_wallet_utxos(wallet, from_address)

        amount_btc = total_sats / 100_000_000
        tx = wallet.send_to(
            to_address=SHOP_MERCHANT_ADDRESS,
            amount=total_sats,
            fee="normal",
            broadcast=False,
        )

        details = transaction_details(tx, wallet)
        details["from_address"] = from_address
        details["to_address"] = SHOP_MERCHANT_ADDRESS
        details["amount_sat"] = total_sats
        details["amount_btc"] = amount_btc
        details["network"] = "testnet"
        details["fee_rate"] = "normal"
        details["items"] = items
        details["order_total_sat"] = total_sats

        # Shop-themed action narrative for the demo journey
        details["actions"] = [
            f"Snack order created — {len(items)} item type(s), total {total_sats:,} sats",
            "Selected UTXOs covering purchase amount + miner fee",
            "Constructed SegWit (P2WPKH) transaction",
            "Signed inputs using your imported private key (server-side for demo)",
            "Broadcast raw transaction to Bitcoin testnet",
        ]

        tx.send()
        txid = tx.txid
        details["txid"] = txid
        details["mempool_url"] = f"{MEMPOOL_TX_URL}/{txid}"

        return jsonify({"success": True, "transaction": details})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/transaction/<txid>/status", methods=["GET"])
def transaction_status(txid):
    try:
        response = requests.get(f"{MEMPOOL_API}/tx/{txid}", timeout=15)
        response.raise_for_status()
        status = response.json()

        confirmed = status.get("status", {}).get("confirmed", False)
        block_height = status.get("status", {}).get("block_height")
        block_hash = status.get("status", {}).get("block_hash")

        confirmations = 0
        if confirmed and block_height:
            tip_response = requests.get(f"{MEMPOOL_API}/blocks/tip/height", timeout=15)
            tip_response.raise_for_status()
            tip_height = tip_response.json()
            confirmations = max(0, tip_height - block_height + 1)

        return jsonify(
            {
                "success": True,
                "txid": txid,
                "confirmed": confirmed,
                "confirmations": confirmations,
                "required_confirmations": REQUIRED_CONFIRMATIONS,
                "fully_confirmed": confirmations >= REQUIRED_CONFIRMATIONS,
                "block_height": block_height,
                "block_hash": block_hash,
                "mempool_url": f"{MEMPOOL_TX_URL}/{txid}",
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/shop/invoice", methods=["POST"])
def create_shop_invoice():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "Load a wallet before creating an invoice."}), 400

    data = request.json or {}
    items = data.get("items") or []

    try:
        total_sats = int(data.get("total_sats"))
        if total_sats <= 0:
            raise ValueError("Total must be greater than zero.")
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": f"Invalid total: {exc}"}), 400

    now = int(time.time())
    invoice = {
        "id": uuid.uuid4().hex[:12],
        "amount_sat": total_sats,
        "address": SHOP_MERCHANT_ADDRESS,
        "created_at": now,
        "expires_at": now + INVOICE_TTL_SECONDS,
        "items": items,
        "status": "open",
        "paid_txid": None,
    }
    session["open_invoice"] = invoice
    return jsonify({"success": True, "invoice": invoice})


@app.route("/api/shop/invoice", methods=["GET"])
def get_shop_invoice():
    return jsonify({"success": True, "invoice": get_open_invoice()})


@app.route("/api/shop/invoice", methods=["DELETE"])
def clear_shop_invoice():
    session.pop("open_invoice", None)
    return jsonify({"success": True})


@app.route("/api/shop/invoice/match", methods=["POST"])
def mark_shop_invoice_matched():
    data = request.json or {}
    txid = (data.get("txid") or "").strip()
    invoice = session.get("open_invoice")
    if not invoice or invoice.get("status") != "open":
        return jsonify({"success": False, "error": "No open invoice to match."}), 400

    invoice = {
        **invoice,
        "status": "paid",
        "paid_txid": txid or None,
        "paid_at": int(time.time()),
    }
    session["open_invoice"] = invoice
    return jsonify({"success": True, "invoice": invoice})


@app.route("/api/merchant/info", methods=["GET"])
def merchant_info():
    """Read-only merchant monitor. Returns balance + recent incoming payments to the shop address."""
    address = SHOP_MERCHANT_ADDRESS
    try:
        # Balance via UTXOs (consistent with customer side)
        utxos = fetch_address_utxos(address)
        balance_sat = sum(utxo["value"] for utxo in utxos)

        # Recent tx history (newest first)
        raw_txs = fetch_address_txs(address, count=50)

        # Get current tip for confirmation counts
        tip_height = None
        try:
            tip_resp = requests.get(f"{MEMPOOL_API}/blocks/tip/height", timeout=10)
            tip_resp.raise_for_status()
            tip_height = tip_resp.json()
        except Exception:
            pass

        open_invoice = get_open_invoice()
        recent_received = []
        for tx in raw_txs:
            received_sat = 0
            for vout in tx.get("vout", []):
                if vout.get("scriptpubkey_address") == address:
                    received_sat += vout.get("value", 0)

            if received_sat <= 0:
                continue

            status = tx.get("status", {}) or {}
            confirmed = status.get("confirmed", False)
            block_height = status.get("block_height")
            confirmations = 0
            if confirmed and block_height and tip_height:
                confirmations = max(0, tip_height - block_height + 1)

            block_time = status.get("block_time")
            invoice_match = evaluate_invoice_match(received_sat, block_time, open_invoice)

            recent_received.append({
                "txid": tx["txid"],
                "received_sat": received_sat,
                "received_btc": received_sat / 100_000_000,
                "fee": tx.get("fee", 0),
                "fee_btc": (tx.get("fee") or 0) / 100_000_000,
                "confirmed": confirmed,
                "confirmations": confirmations,
                "block_height": block_height,
                "block_time": block_time,
                "invoice_match": invoice_match,
                "mempool_url": f"{MEMPOOL_TX_URL}/{tx['txid']}",
            })

        # Keep a reasonable number for the UI (more for denser view)
        recent_received = recent_received[:25]

        if open_invoice:
            open_invoice = {
                **open_invoice,
                "seconds_remaining": max(0, open_invoice.get("expires_at", 0) - int(time.time())),
            }

        return jsonify({
            "success": True,
            "address": address,
            "balance_sat": balance_sat,
            "balance_btc": balance_sat / 100_000_000,
            "open_invoice": open_invoice,
            "recent_received": recent_received,
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/estimate-fee", methods=["POST"])
def estimate_fee():
    """Dry-run build of the tx to return estimated fee without broadcasting."""
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "Load a wallet before estimating fees."}), 400

    data = request.json or {}
    to_address = (data.get("to_address") or SHOP_MERCHANT_ADDRESS).strip()
    amount_sat = data.get("amount_sat")

    try:
        amount_sat = int(amount_sat)
        if amount_sat <= 0:
            raise ValueError("Amount must be greater than zero.")
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": f"Invalid amount: {exc}"}), 400

    try:
        wallet = get_loaded_wallet()
        from_address = wallet.get_key().address
        sync_wallet_utxos(wallet, from_address)

        tx = wallet.send_to(
            to_address=to_address,
            amount=amount_sat,
            fee="normal",
            broadcast=False,
        )

        return jsonify({
            "success": True,
            "from_address": from_address,
            "to_address": to_address,
            "amount_sat": amount_sat,
            "fee_sat": tx.fee,
            "fee_btc": tx.fee / 100_000_000,
            "size_bytes": tx.size,
            "vsize": getattr(tx, "vsize", None),
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(debug=True, host="127.0.0.1", port=port)