import os
import uuid

import requests
from bitcoinlib.wallets import Wallet, wallet_delete_if_exists, wallet_exists
from flask import Flask, jsonify, render_template, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")

MEMPOOL_API = "https://mempool.space/testnet/api"
MEMPOOL_TX_URL = "https://mempool.space/testnet/tx"
REQUIRED_CONFIRMATIONS = 10


def get_wallet_name():
    if "wallet_name" not in session:
        session["wallet_name"] = f"web_sender_{uuid.uuid4().hex[:12]}"
    return session["wallet_name"]


def fetch_address_utxos(address):
    response = requests.get(f"{MEMPOOL_API}/address/{address}/utxo", timeout=15)
    response.raise_for_status()
    return response.json()


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
    wallet.scan()

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(debug=True, host="127.0.0.1", port=port)