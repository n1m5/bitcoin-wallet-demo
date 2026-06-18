import json
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
ACCOUNT_PATH_DEFAULT = "m/84'/1'/0'"
XPUB_GAP_LIMIT = 20
FEE_RATE_SAT_VB = 2
BIP329_LABEL_TYPES = {"tx", "addr", "pubkey", "input", "output", "xpub", "spscan"}
BIP329_MAX_LABEL_LENGTH = 255
MERCHANT_LABEL = "Satoshi Snacks (merchant)"


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
    """Replace wallet UTXOs with the live mempool.space set (drops spent/stale coins)."""
    utxos = fetch_address_utxos(address)
    formatted = [
        {
            "address": address,
            "script": "",
            "confirmations": 1 if utxo.get("status", {}).get("confirmed", False) else 0,
            "output_n": utxo["vout"],
            "txid": utxo["txid"],
            "value": utxo["value"],
        }
        for utxo in utxos
    ]
    wallet.utxos_update(utxos=formatted, rescan_all=True, networks="testnet")
    return utxos


def verify_utxo_unspent(txid, vout):
    """Return True if the UTXO still exists on testnet (mempool.space)."""
    try:
        response = requests.get(f"{MEMPOOL_API}/tx/{txid}/outspend/{vout}", timeout=15)
        response.raise_for_status()
        return not response.json().get("spent", True)
    except Exception:
        return False


def load_or_create_wallet(private_key, derivation_path):
    wallet_name = get_wallet_name()

    if wallet_exists(wallet_name):
        wallet_delete_if_exists(wallet_name, force=True)

    clear_wallet_labels()

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
    session["wallet_mode"] = "private"
    session.pop("watchonly_xpub", None)
    session.pop("watchonly_account_path", None)
    session.pop("watchonly_fingerprint", None)
    session.pop("watchonly_has_master_fp", None)
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
        "wallet_mode": "private",
        "balance_sat": balance_sat,
        "balance_btc": balance_sat / 100_000_000,
        "utxo_count": len(formatted_utxos),
        "utxos": formatted_utxos,
        "descriptors": build_private_descriptors(wallet, derivation_path, address),
    }


def get_loaded_wallet():
    if not session.get("wallet_loaded"):
        raise ValueError("No wallet loaded. Please load a wallet first.")
    if session.get("wallet_mode") == "watchonly":
        raise ValueError("Watch-only wallet loaded. Native signing is not available — use PSBT or BIP21.")
    return Wallet(get_wallet_name())


def is_watchonly_wallet():
    return session.get("wallet_loaded") and session.get("wallet_mode") == "watchonly"


def txid_to_embit_bytes(txid_hex):
    """Convert mempool.space display txid hex to embit TransactionInput.txid storage.

    embit stores the human-readable (display) byte order; serialization reverses
    it for the on-wire prevout hash. Reversing here was causing invalid inputs.
    """
    return bytes.fromhex(txid_hex)


def parse_xpub_descriptor(value):
    """Parse Electrum-style `[fingerprint/m/84'/1'/0']vpub...` or plain vpub."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Extended public key (xpub/vpub) is required.")

    fingerprint = None
    account_path = ACCOUNT_PATH_DEFAULT
    xpub = value

    if value.startswith("["):
        end = value.index("]")
        bracket = value[1:end]
        xpub = value[end + 1 :].strip()
        if "/" in bracket:
            fingerprint, path_suffix = bracket.split("/", 1)
            fingerprint = fingerprint.strip()
            path_suffix = path_suffix.strip()
            account_path = path_suffix if path_suffix.startswith("m/") else "m/" + path_suffix
        else:
            fingerprint = bracket.strip()

    if not xpub:
        raise ValueError("Missing xpub/vpub after descriptor prefix.")

    return {
        "xpub": xpub,
        "fingerprint": fingerprint,
        "account_path": account_path,
    }


def account_hdkey_from_xpub(xpub_str):
    from embit import bip32

    return bip32.HDKey.from_base58(xpub_str.strip())


def derive_address_from_account(account_hd, change, index):
    from embit import script
    from embit.networks import NETWORKS

    child = account_hd.child(change).child(index)
    pub = child.key
    return child, pub, script.p2wpkh(pub).address(NETWORKS["test"])


def full_derivation_path(account_path, change, index):
    return parse_derivation_path(account_path) + [change, index]


def relative_account_path(change, index):
    """Derivation path relative to a BIP84 account xpub (external/change + index)."""
    return [change, index]


def xpub_parent_fingerprint_bytes(xpub_str):
    """Master/parent fingerprint embedded in the BIP32 serialized xpub."""
    from embit import base58

    raw = base58.decode_check(xpub_str.strip())
    return raw[5:9]


def psbt_signing_origin(account_hd, xpub_str, account_path, change, index, has_master_fingerprint, fingerprint_hex):
    """
    Return (fingerprint, derivation path) for PSBT bip32_derivations.

    Electrum expects the master fingerprint plus the full path from the master key.
    Shallow vpubs (e.g. Electrum m/0') must include the hardened account level: m/0'/0/0.
    """
    from binascii import unhexlify

    if has_master_fingerprint and fingerprint_hex:
        return unhexlify(fingerprint_hex), full_derivation_path(account_path, change, index)

    if account_hd.depth == 1:
        # Electrum-style vpub at m/0' — signing key is m/0'/change/index
        return xpub_parent_fingerprint_bytes(xpub_str), [
            account_hd.child_number,
            change,
            index,
        ]

    if account_hd.depth == 3:
        # Standard BIP84 account xpub — path relative to account
        return account_hd.fingerprint, relative_account_path(change, index)

    return xpub_parent_fingerprint_bytes(xpub_str), relative_account_path(change, index)


def psbt_key_origin(account_path, change, index, has_master_fingerprint):
    """Backward-compatible helper for callers that already resolved origin elsewhere."""
    if has_master_fingerprint:
        return full_derivation_path(account_path, change, index)
    return relative_account_path(change, index)


def psbt_fingerprint_bytes(account_hd, fingerprint_hex, has_master_fingerprint):
    """Master fingerprint from descriptor, or account-node fingerprint for plain vpub."""
    from binascii import unhexlify

    if has_master_fingerprint and fingerprint_hex:
        return unhexlify(fingerprint_hex)
    return account_hd.fingerprint


def origin_path_from_account_path(account_path):
    """BIP380 key-origin path inside brackets (no leading m/)."""
    path = (account_path or ACCOUNT_PATH_DEFAULT).strip()
    return path[2:] if path.startswith("m/") else path


def build_descriptor_key_expression(xpub, account_path, account_hd, has_master_fingerprint, fingerprint_hex):
    """Build the xpub key expression used inside wpkh(...)."""
    if has_master_fingerprint and fingerprint_hex:
        origin = origin_path_from_account_path(account_path)
        return f"[{fingerprint_hex.lower()}/{origin}]{xpub}"
    if account_hd.depth == 1:
        origin = origin_path_from_account_path(account_path)
        fp = xpub_parent_fingerprint_bytes(xpub).hex()
        return f"[{fp}/{origin}]{xpub}"
    return xpub


def format_descriptor_wpkh(key_expression, chain_suffix):
    """Return a checksummed BIP380 wpkh descriptor for testnet BIP84."""
    from embit.descriptor.checksum import add_checksum

    return add_checksum(f"wpkh({key_expression}{chain_suffix})")


def derive_descriptor_address(descriptor_str, index=0):
    """Derive a testnet address from a parsed output descriptor."""
    from embit.descriptor import Descriptor
    from embit.networks import NETWORKS

    descriptor = Descriptor.from_string(descriptor_str)
    return descriptor.derive(index).address(network=NETWORKS["test"])


def build_watchonly_descriptors(xpub, account_path, has_master_fingerprint, fingerprint_hex):
    """Export receive / change / account wpkh descriptors for a watch-only wallet."""
    account_hd = account_hdkey_from_xpub(xpub)
    key_expression = build_descriptor_key_expression(
        xpub, account_path, account_hd, has_master_fingerprint, fingerprint_hex
    )
    receive = format_descriptor_wpkh(key_expression, "/0/*")
    change = format_descriptor_wpkh(key_expression, "/1/*")
    account = format_descriptor_wpkh(key_expression, "/<0;1>/*")
    primary_address = derive_address_from_account(account_hd, 0, 0)[2]
    validated = derive_descriptor_address(receive, 0) == primary_address
    return {
        "type": "account",
        "receive": receive,
        "change": change,
        "account": account,
        "account_path": account_path,
        "validated": validated,
        "validated_address": primary_address,
    }


def clear_wallet_labels():
    session.pop("wallet_labels", None)


def get_wallet_labels():
    return list(session.get("wallet_labels") or [])


def label_record_key(record):
    return f"{record['type']}:{record['ref']}"


def normalize_label_text(label):
    label = str(label or "").strip()
    if not label:
        raise ValueError("Label text is required.")
    if len(label) > BIP329_MAX_LABEL_LENGTH:
        return label[:BIP329_MAX_LABEL_LENGTH]
    return label


def validate_bip329_record(record):
    if not isinstance(record, dict):
        raise ValueError("Each label must be a JSON object.")

    record_type = record.get("type")
    ref = str(record.get("ref") or "").strip()
    if record_type not in BIP329_LABEL_TYPES:
        raise ValueError(f"Unsupported label type: {record_type}")
    if not ref:
        raise ValueError("Label ref is required.")

    normalized = {"type": record_type, "ref": ref}
    if "label" in record and record.get("label") is not None:
        normalized["label"] = normalize_label_text(record["label"])
    if record.get("origin"):
        normalized["origin"] = str(record["origin"]).strip()
    if "spendable" in record and record.get("spendable") is not None:
        normalized["spendable"] = bool(record["spendable"])
    for optional_field in ("height", "fee", "value", "time", "keypath", "heights"):
        if optional_field in record and record.get(optional_field) is not None:
            normalized[optional_field] = record[optional_field]
    if "rate" in record and isinstance(record.get("rate"), dict):
        normalized["rate"] = record["rate"]
    if "fmv" in record and isinstance(record.get("fmv"), dict):
        normalized["fmv"] = record["fmv"]
    return normalized


def upsert_wallet_label(record):
    normalized = validate_bip329_record(record)
    labels = get_wallet_labels()
    key = label_record_key(normalized)
    labels = [item for item in labels if label_record_key(item) != key]
    labels.append(normalized)
    session["wallet_labels"] = labels
    return normalized


def export_wallet_labels_jsonl():
    lines = []
    for record in get_wallet_labels():
        clean = {key: value for key, value in record.items() if value is not None}
        lines.append(json.dumps(clean, separators=(",", ":"), ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def import_wallet_labels_jsonl(content):
    imported = 0
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            upsert_wallet_label(record)
            imported += 1
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid label on line {line_number}: {exc}") from exc
    return imported


def build_bip329_origin():
    """Abbreviated BIP380 origin descriptor for the loaded wallet."""
    if not session.get("wallet_loaded"):
        return None
    if not is_watchonly_wallet():
        return None

    account_path = session.get("watchonly_account_path", ACCOUNT_PATH_DEFAULT)
    origin_path = origin_path_from_account_path(account_path)
    fingerprint = session.get("watchonly_fingerprint")
    has_master_fp = session.get("watchonly_has_master_fp", False)
    if has_master_fp and fingerprint:
        return f"wpkh([{fingerprint.lower()}/{origin_path}])"

    account_hd = account_hdkey_from_xpub(session["watchonly_xpub"])
    if account_hd.depth == 1:
        fp = xpub_parent_fingerprint_bytes(session["watchonly_xpub"]).hex()
        return f"wpkh([{fp}/{origin_path}])"
    return None


def format_order_label(items):
    if not items:
        return "Satoshi Snacks order"
    parts = [f"{item.get('qty', 1)}× {item.get('name', 'item')}" for item in items]
    return "Satoshi Snacks — " + ", ".join(parts)


def fetch_tx_details_for_labeling(txid):
    """Build input/output metadata for BIP329 labels from mempool.space."""
    response = requests.get(f"{MEMPOOL_API}/tx/{txid}", timeout=15)
    response.raise_for_status()
    tx = response.json()
    wallet_address = session.get("wallet_address")

    inputs = []
    for vin in tx.get("vin", []):
        if vin.get("is_coinbase"):
            continue
        prevout = vin.get("prevout") or {}
        if not vin.get("txid"):
            continue
        inputs.append(
            {
                "txid": vin["txid"],
                "vout": vin.get("vout", 0),
                "value_sat": prevout.get("value", 0),
            }
        )

    outputs = []
    merchant_total = 0
    for index, vout in enumerate(tx.get("vout", [])):
        address = vout.get("scriptpubkey_address")
        value_sat = vout.get("value", 0)
        is_change = address == wallet_address
        if address == SHOP_MERCHANT_ADDRESS:
            merchant_total += value_sat
        outputs.append(
            {
                "vout": index,
                "address": address,
                "value_sat": value_sat,
                "is_change": is_change,
            }
        )

    return {
        "txid": txid,
        "fee_sat": tx.get("fee", 0),
        "amount_sat": merchant_total,
        "order_total_sat": merchant_total,
        "inputs": inputs,
        "outputs": outputs,
    }


def apply_checkout_labels(txid, items, tx_details=None):
    """Create BIP329 labels for a snack shop payment."""
    details = dict(tx_details or {})
    if not details.get("inputs") or not details.get("outputs"):
        try:
            fetched = fetch_tx_details_for_labeling(txid)
            details = {**fetched, **{k: v for k, v in details.items() if v is not None}}
        except Exception:
            details.setdefault("inputs", [])
            details.setdefault("outputs", [])

    origin = build_bip329_origin()
    order_label = format_order_label(items)

    tx_record = {"type": "tx", "ref": txid, "label": order_label}
    if origin:
        tx_record["origin"] = origin
    if details.get("fee_sat") is not None:
        tx_record["fee"] = int(details["fee_sat"])
    order_total = details.get("order_total_sat") or details.get("amount_sat")
    if order_total is not None:
        tx_record["value"] = -int(order_total)
    upsert_wallet_label(tx_record)

    upsert_wallet_label({"type": "addr", "ref": SHOP_MERCHANT_ADDRESS, "label": MERCHANT_LABEL})

    for inp in details.get("inputs", []):
        input_record = {
            "type": "input",
            "ref": f"{inp['txid']}:{inp['vout']}",
            "label": "Payment source",
        }
        if origin:
            input_record["origin"] = origin
        if inp.get("value_sat") is not None:
            input_record["value"] = int(inp["value_sat"])
        upsert_wallet_label(input_record)

    for out in details.get("outputs", []):
        vout = out.get("vout", 0)
        output_record = {
            "type": "output",
            "ref": f"{txid}:{vout}",
            "label": "Change" if out.get("is_change") else order_label,
        }
        if origin:
            output_record["origin"] = origin
        if out.get("value_sat") is not None:
            output_record["value"] = int(out["value_sat"])
        if out.get("is_change"):
            output_record["spendable"] = True
        elif out.get("address") == SHOP_MERCHANT_ADDRESS:
            output_record["spendable"] = False
        upsert_wallet_label(output_record)

    if is_watchonly_wallet() and session.get("watchonly_xpub"):
        xpub_record = {
            "type": "xpub",
            "ref": session["watchonly_xpub"],
            "label": "Satoshi Snacks watch-only account",
        }
        if origin:
            xpub_record["origin"] = origin
        upsert_wallet_label(xpub_record)

    return get_wallet_labels()


def build_private_descriptors(wallet, derivation_path, address):
    """Export the address-level wpkh descriptor for an imported WIF wallet."""
    pubkey = wallet.get_key().key_public
    if isinstance(pubkey, bytes):
        pubkey = pubkey.hex()
    pubkey = str(pubkey).strip()
    if not pubkey:
        raise ValueError("Could not read public key for descriptor export.")

    address_descriptor = format_descriptor_wpkh(pubkey, "")
    validated = derive_descriptor_address(address_descriptor, 0) == address
    return {
        "type": "address",
        "address_descriptor": address_descriptor,
        "derivation_path": derivation_path,
        "validated": validated,
        "validated_address": address,
    }


def address_has_history(address):
    """True if the address appears in any transaction on testnet."""
    try:
        return bool(fetch_address_txs(address, count=1))
    except Exception:
        return False


def find_next_change_index(account_hd, account_path, gap_limit=XPUB_GAP_LIMIT):
    """Pick the next unused change-chain index (Electrum-style gap scan)."""
    last_used = -1
    for index in range(gap_limit):
        _, _, address = derive_address_from_account(account_hd, 1, index)
        if address_has_history(address):
            last_used = index
    return max(last_used + 1, 0)


def format_xpub_utxos_for_api(utxos, limit=10):
    """Strip internal embit objects before returning UTXOs in JSON responses."""
    return [
        {
            "txid": utxo["txid"],
            "vout": utxo["vout"],
            "value_sat": utxo["value_sat"],
            "value_btc": utxo["value_btc"],
            "address": utxo["address"],
            "change": utxo["change"],
            "index": utxo["index"],
            "confirmed": utxo.get("confirmed"),
            "block_height": utxo.get("block_height"),
        }
        for utxo in utxos[:limit]
    ]


def scan_xpub_utxos(account_hd, account_path, gap_limit=XPUB_GAP_LIMIT):
    """Scan external + change chains for UTXOs (BIP84 account xpub)."""
    utxos = []
    for change in (0, 1):
        empty_streak = 0
        for index in range(gap_limit):
            _, pub, address = derive_address_from_account(account_hd, change, index)
            addr_utxos = fetch_address_utxos(address)
            if not addr_utxos:
                empty_streak += 1
                if change == 0 and empty_streak >= 6:
                    break
                continue
            empty_streak = 0
            for utxo in addr_utxos:
                utxos.append(
                    {
                        "txid": utxo["txid"],
                        "vout": utxo["vout"],
                        "value_sat": utxo["value"],
                        "value_btc": utxo["value"] / 100_000_000,
                        "address": address,
                        "change": change,
                        "index": index,
                        "pubkey": pub,
                        "derivation": full_derivation_path(account_path, change, index),
                        "confirmed": utxo.get("status", {}).get("confirmed", False),
                        "block_height": utxo.get("status", {}).get("block_height"),
                    }
                )
    return utxos


def estimate_segwit_fee(num_inputs, num_outputs, fee_rate=FEE_RATE_SAT_VB):
    vsize = 10 + (68 * num_inputs) + (31 * num_outputs)
    return max(vsize * fee_rate, 141)


def select_xpub_utxos(utxos, amount_sat):
    if not utxos:
        raise ValueError("No unspent coins found for this xpub.")

    ordered = sorted(utxos, key=lambda item: item["value_sat"], reverse=True)
    selected = []
    total = 0
    for utxo in ordered:
        selected.append(utxo)
        total += utxo["value_sat"]
        fee_sat = estimate_segwit_fee(len(selected), 2)
        if total >= amount_sat + fee_sat:
            return selected, fee_sat

    raise ValueError(
        "Insufficient funds for this order. Fund your wallet via a testnet faucet and refresh."
    )


def build_xpub_checkout_psbt(
    amount_sat,
    to_address,
    xpub_str,
    account_path,
    fingerprint_hex=None,
    has_master_fingerprint=False,
):
    """Build unsigned PSBT from a BIP84 account xpub — no private key on the server."""
    from embit import script
    from embit.psbt import DerivationPath, PSBT
    from embit.transaction import Transaction as EmbitTx, TransactionInput, TransactionOutput

    parsed = parse_xpub_descriptor(xpub_str)
    xpub_value = parsed["xpub"]
    account_hd = account_hdkey_from_xpub(xpub_value)
    account_path = account_path or parsed["account_path"]
    has_master_fingerprint = has_master_fingerprint or bool(parsed.get("fingerprint"))
    fingerprint_hex = fingerprint_hex or parsed.get("fingerprint") or account_hd.fingerprint.hex()

    utxos = scan_xpub_utxos(account_hd, account_path)
    if not utxos:
        raise ValueError("No unspent coins found. Fund your testnet wallet and try again.")

    selected, fee_sat = select_xpub_utxos(utxos, amount_sat)
    spent_inputs = []
    for utxo in selected:
        if not verify_utxo_unspent(utxo["txid"], utxo["vout"]):
            spent_inputs.append(f"{utxo['txid']}:{utxo['vout']}")
    if spent_inputs:
        raise ValueError(
            "UTXO(s) already spent on testnet: "
            + ", ".join(spent_inputs)
            + ". Refresh the PSBT and try again."
        )

    total_in = sum(item["value_sat"] for item in selected)
    change_sat = total_in - amount_sat - fee_sat
    if change_sat < 0:
        raise ValueError("Insufficient funds after fees.")

    change_index = find_next_change_index(account_hd, account_path)
    _, change_pub, change_address = derive_address_from_account(account_hd, 1, change_index)
    change_fingerprint, change_derivation = psbt_signing_origin(
        account_hd, xpub_value, account_path, 1, change_index, has_master_fingerprint, fingerprint_hex
    )

    vin = [
        TransactionInput(txid_to_embit_bytes(item["txid"]), item["vout"], sequence=0xFFFFFFFD)
        for item in selected
    ]
    vout = [
        TransactionOutput(amount_sat, script.address_to_scriptpubkey(to_address)),
    ]
    if change_sat > 0:
        vout.append(TransactionOutput(change_sat, script.p2wpkh(change_pub)))

    embit_tx = EmbitTx(version=2, vin=vin, vout=vout, locktime=0)
    psbt = PSBT(tx=embit_tx)

    if has_master_fingerprint:
        account_prefix = parse_derivation_path(account_path)
        master_fp = psbt_fingerprint_bytes(account_hd, fingerprint_hex, True)
        # Electrum rejects global xpub when derivation prefix length != xpub depth.
        if len(account_prefix) == account_hd.depth:
            psbt.xpubs[account_hd] = DerivationPath(master_fp, account_prefix)

    for i, utxo in enumerate(selected):
        pub = utxo["pubkey"]
        input_fingerprint, input_derivation = psbt_signing_origin(
            account_hd,
            xpub_value,
            account_path,
            utxo["change"],
            utxo["index"],
            has_master_fingerprint,
            fingerprint_hex,
        )
        psbt.inputs[i].witness_utxo = TransactionOutput(
            utxo["value_sat"], script.p2wpkh(pub)
        )
        psbt.inputs[i].bip32_derivations[pub] = DerivationPath(
            input_fingerprint,
            input_derivation,
        )

    if change_sat > 0:
        psbt.outputs[1].bip32_derivations[change_pub] = DerivationPath(
            change_fingerprint, change_derivation
        )

    primary_address = selected[0]["address"]
    outputs = [
        {
            "address": to_address,
            "value_sat": amount_sat,
            "value_btc": amount_sat / 100_000_000,
            "is_change": False,
        }
    ]
    if change_sat > 0:
        outputs.append(
            {
                "address": change_address,
                "value_sat": change_sat,
                "value_btc": change_sat / 100_000_000,
                "is_change": True,
            }
        )

    return {
        "psbt_base64": psbt.to_base64(),
        "fee_sat": fee_sat,
        "fee_btc": fee_sat / 100_000_000,
        "amount_sat": amount_sat,
        "amount_btc": amount_sat / 100_000_000,
        "from_address": primary_address,
        "to_address": to_address,
        "account_path": account_path,
        "fingerprint": fingerprint_hex,
        "has_master_fingerprint": has_master_fingerprint,
        "change_index": change_index,
        "xpub_preview": parsed["xpub"][:18] + "…",
        "inputs": [
            {
                "txid": item["txid"],
                "vout": item["vout"],
                "value_sat": item["value_sat"],
                "value_btc": item["value_sat"] / 100_000_000,
                "address": item["address"],
                "derivation": psbt_signing_origin(
                    account_hd,
                    xpub_value,
                    account_path,
                    item["change"],
                    item["index"],
                    has_master_fingerprint,
                    fingerprint_hex,
                )[1],
            }
            for item in selected
        ],
        "outputs": outputs,
        "input_count": len(selected),
        "output_count": len(outputs),
        "watchonly": True,
    }


def load_watchonly_xpub(xpub_input):
    parsed = parse_xpub_descriptor(xpub_input)
    account_hd = account_hdkey_from_xpub(parsed["xpub"])
    account_path = parsed["account_path"]
    has_master_fp = bool(parsed.get("fingerprint"))
    fingerprint = parsed.get("fingerprint") or account_hd.fingerprint.hex()

    wallet_name = get_wallet_name()
    if wallet_exists(wallet_name):
        wallet_delete_if_exists(wallet_name, force=True)

    clear_wallet_labels()

    utxos = scan_xpub_utxos(account_hd, account_path)
    balance_sat = sum(utxo["value_sat"] for utxo in utxos)
    primary_address = utxos[0]["address"] if utxos else derive_address_from_account(account_hd, 0, 0)[2]

    session["wallet_loaded"] = True
    session["wallet_mode"] = "watchonly"
    session["watchonly_xpub"] = parsed["xpub"]
    session["watchonly_account_path"] = account_path
    session["watchonly_fingerprint"] = fingerprint
    session["watchonly_has_master_fp"] = has_master_fp
    session["wallet_address"] = primary_address
    session["derivation_path"] = f"{account_path}/0/0"

    descriptors = build_watchonly_descriptors(
        parsed["xpub"], account_path, has_master_fp, fingerprint
    )

    return {
        "address": primary_address,
        "derivation_path": session["derivation_path"],
        "account_path": account_path,
        "fingerprint": fingerprint,
        "has_master_fingerprint": has_master_fp,
        "xpub_preview": parsed["xpub"][:20] + "…",
        "network": "testnet",
        "wallet_mode": "watchonly",
        "balance_sat": balance_sat,
        "balance_btc": balance_sat / 100_000_000,
        "utxo_count": len(utxos),
        "utxos": format_xpub_utxos_for_api(utxos),
        "descriptors": descriptors,
    }


def get_watchonly_wallet_info():
    if not is_watchonly_wallet():
        raise ValueError("No watch-only wallet loaded.")

    account_hd = account_hdkey_from_xpub(session["watchonly_xpub"])
    account_path = session["watchonly_account_path"]
    utxos = scan_xpub_utxos(account_hd, account_path)
    balance_sat = sum(utxo["value_sat"] for utxo in utxos)
    primary_address = utxos[0]["address"] if utxos else session.get("wallet_address")

    descriptors = build_watchonly_descriptors(
        session["watchonly_xpub"],
        account_path,
        session.get("watchonly_has_master_fp", False),
        session.get("watchonly_fingerprint"),
    )

    return {
        "address": primary_address,
        "derivation_path": session.get("derivation_path", f"{account_path}/0/0"),
        "account_path": account_path,
        "fingerprint": session.get("watchonly_fingerprint"),
        "xpub_preview": session["watchonly_xpub"][:20] + "…",
        "network": "testnet",
        "wallet_mode": "watchonly",
        "balance_sat": balance_sat,
        "balance_btc": balance_sat / 100_000_000,
        "utxo_count": len(utxos),
        "utxos": format_xpub_utxos_for_api(utxos),
        "descriptors": descriptors,
    }


def get_wallet_info():
    """Return current wallet snapshot (requires wallet_loaded in session)."""
    if not session.get("wallet_loaded"):
        raise ValueError("No wallet loaded. Please load a wallet first.")

    if is_watchonly_wallet():
        return get_watchonly_wallet_info()

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

    derivation_path = session.get("derivation_path", "m/84'/1'/0'/0/0")

    return {
        "address": address,
        "derivation_path": derivation_path,
        "network": "testnet",
        "wallet_mode": "private",
        "balance_sat": balance_sat,
        "balance_btc": balance_sat / 100_000_000,
        "utxo_count": len(formatted_utxos),
        "utxos": formatted_utxos,
        "descriptors": build_private_descriptors(wallet, derivation_path, address),
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


def parse_derivation_path(path_str):
    """Convert BIP32 path string to embit derivation index list."""
    deriv = []
    for part in path_str.replace("m/", "").split("/"):
        if not part:
            continue
        hardened = part.endswith("'") or part.endswith("h")
        idx = int(part.rstrip("'hH"))
        if hardened:
            idx |= 0x80000000
        deriv.append(idx)
    return deriv


def build_checkout_psbt(wallet, amount_sat, to_address, derivation_path):
    """Build an unsigned BIP174 PSBT for external signing (BIP84 P2WPKH)."""
    from binascii import unhexlify

    from embit import ec, hashes, script
    from embit.psbt import DerivationPath, PSBT
    from embit.transaction import Transaction as EmbitTx, TransactionInput, TransactionOutput

    from_address = wallet.get_key().address
    mempool_utxos = sync_wallet_utxos(wallet, from_address)
    if not mempool_utxos:
        raise ValueError("No unspent coins available. Refresh your wallet balance or fund via a testnet faucet.")

    tx = wallet.transaction_create([(to_address, amount_sat)], fee="normal")

    for inp in tx.inputs:
        input_txid = format_txid(inp.prev_txid)
        if not verify_utxo_unspent(input_txid, inp.output_n_int):
            raise ValueError(
                f"UTXO {input_txid}:{inp.output_n_int} is already spent. "
                "Refresh the PSBT — do not reuse an old export after paying another way."
            )
    key = wallet.get_key()
    pub_bytes = key.key_public
    if isinstance(pub_bytes, str):
        pub_bytes = unhexlify(pub_bytes)
    pub = ec.PublicKey.parse(pub_bytes)
    deriv = parse_derivation_path(derivation_path)
    fingerprint = hashes.hash160(pub.sec())[:4]

    vin = []
    for inp in tx.inputs:
        prev_hash = txid_to_embit_bytes(format_txid(inp.prev_txid))
        sequence = getattr(inp, "sequence", 0xFFFFFFFD)
        vin.append(TransactionInput(prev_hash, inp.output_n_int, sequence))

    vout = []
    for out in tx.outputs:
        spk = script.address_to_scriptpubkey(out.address)
        vout.append(TransactionOutput(out.value, spk))

    embit_tx = EmbitTx(version=2, vin=vin, vout=vout, locktime=getattr(tx, "locktime", 0) or 0)
    psbt = PSBT(tx=embit_tx)

    for i, inp in enumerate(tx.inputs):
        witness_spk = script.p2wpkh(pub)
        psbt.inputs[i].witness_utxo = TransactionOutput(inp.value, witness_spk)
        psbt.inputs[i].bip32_derivations[pub] = DerivationPath(fingerprint, deriv)

    for i, out in enumerate(tx.outputs):
        if out.change:
            psbt.outputs[i].bip32_derivations[pub] = DerivationPath(fingerprint, deriv)

    inputs = [
        {
            "txid": format_txid(inp.prev_txid),
            "vout": inp.output_n_int,
            "value_sat": inp.value,
            "value_btc": inp.value / 100_000_000,
        }
        for inp in tx.inputs
    ]
    outputs = [
        {
            "address": out.address,
            "value_sat": out.value,
            "value_btc": out.value / 100_000_000,
            "is_change": bool(out.change),
        }
        for out in tx.outputs
    ]

    return {
        "psbt_base64": psbt.to_base64(),
        "fee_sat": tx.fee,
        "fee_btc": tx.fee / 100_000_000,
        "amount_sat": amount_sat,
        "amount_btc": amount_sat / 100_000_000,
        "from_address": from_address,
        "to_address": to_address,
        "derivation_path": derivation_path,
        "inputs": inputs,
        "outputs": outputs,
        "input_count": len(inputs),
        "output_count": len(outputs),
    }


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
    for index, out in enumerate(tx.outputs):
        outputs.append(
            {
                "vout": getattr(out, "output_n_int", index),
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


@app.route("/api/wallet/load-xpub", methods=["POST"])
def load_xpub_wallet():
    data = request.json or {}
    xpub_input = (data.get("xpub") or "").strip()

    if not xpub_input:
        return jsonify({"success": False, "error": "Extended public key (xpub/vpub) is required."}), 400

    try:
        wallet_info = load_watchonly_xpub(xpub_input)
        return jsonify({"success": True, "wallet": wallet_info})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


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


@app.route("/api/wallet/labels", methods=["GET"])
def get_wallet_labels_route():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "No wallet loaded."}), 400
    labels = get_wallet_labels()
    return jsonify({"success": True, "labels": labels, "count": len(labels)})


@app.route("/api/wallet/labels", methods=["POST"])
def upsert_wallet_label_route():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "No wallet loaded."}), 400

    data = request.json or {}
    try:
        record = upsert_wallet_label(data)
        return jsonify({"success": True, "label": record, "labels": get_wallet_labels()})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/wallet/labels/from-payment", methods=["POST"])
def label_payment_route():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "No wallet loaded."}), 400

    data = request.json or {}
    txid = (data.get("txid") or "").strip()
    items = data.get("items") or []
    if not txid:
        return jsonify({"success": False, "error": "Transaction id is required."}), 400

    try:
        labels = apply_checkout_labels(txid, items)
        return jsonify({"success": True, "labels": labels, "count": len(labels)})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/wallet/labels/export", methods=["GET"])
def export_wallet_labels_route():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "No wallet loaded."}), 400
    content = export_wallet_labels_jsonl()
    return jsonify({"success": True, "format": "jsonl", "content": content, "count": len(get_wallet_labels())})


@app.route("/api/wallet/labels/import", methods=["POST"])
def import_wallet_labels_route():
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "No wallet loaded."}), 400

    data = request.json or {}
    content = data.get("content")
    if not content or not str(content).strip():
        return jsonify({"success": False, "error": "JSON Lines content is required."}), 400

    try:
        imported = import_wallet_labels_jsonl(str(content))
        return jsonify(
            {
                "success": True,
                "imported": imported,
                "labels": get_wallet_labels(),
                "count": len(get_wallet_labels()),
            }
        )
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/wallet/labels", methods=["DELETE"])
def clear_wallet_labels_route():
    clear_wallet_labels()
    return jsonify({"success": True, "labels": [], "count": 0})


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


@app.route("/api/shop/psbt", methods=["POST"])
def shop_psbt():
    """Build an unsigned PSBT for checkout — sign and broadcast in an external wallet."""
    if not session.get("wallet_loaded"):
        return jsonify({"success": False, "error": "Load a wallet or xpub before exporting a PSBT."}), 400

    data = request.json or {}
    total_sats = data.get("total_sats")

    try:
        total_sats = int(total_sats)
        if total_sats <= 0:
            raise ValueError("Total must be greater than zero.")
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": f"Invalid total: {exc}"}), 400

    try:
        if is_watchonly_wallet():
            psbt_info = build_xpub_checkout_psbt(
                total_sats,
                SHOP_MERCHANT_ADDRESS,
                session["watchonly_xpub"],
                session["watchonly_account_path"],
                session.get("watchonly_fingerprint"),
                session.get("watchonly_has_master_fp", False),
            )
        else:
            wallet = get_loaded_wallet()
            derivation_path = session.get("derivation_path", "m/84'/1'/0'/0/0")
            psbt_info = build_checkout_psbt(
                wallet,
                total_sats,
                SHOP_MERCHANT_ADDRESS,
                derivation_path,
            )
        psbt_info["network"] = "testnet"
        psbt_info["merchant_address"] = SHOP_MERCHANT_ADDRESS
        return jsonify({"success": True, "psbt": psbt_info})
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

        labels = apply_checkout_labels(txid, items, details)

        return jsonify({"success": True, "transaction": details, "labels": labels})
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
    return jsonify({
        "success": True,
        "invoice": {
            **invoice,
            "seconds_remaining": INVOICE_TTL_SECONDS,
        },
    })


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
        if is_watchonly_wallet():
            account_hd = account_hdkey_from_xpub(session["watchonly_xpub"])
            account_path = session["watchonly_account_path"]
            utxos = scan_xpub_utxos(account_hd, account_path)
            selected, fee_sat = select_xpub_utxos(utxos, amount_sat)
            from_address = selected[0]["address"] if selected else session.get("wallet_address")
            vsize = 10 + (68 * len(selected)) + (31 * 2)
            return jsonify({
                "success": True,
                "from_address": from_address,
                "to_address": to_address,
                "amount_sat": amount_sat,
                "fee_sat": fee_sat,
                "fee_btc": fee_sat / 100_000_000,
                "size_bytes": None,
                "vsize": vsize,
                "wallet_mode": "watchonly",
            })

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
            "wallet_mode": "private",
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(debug=True, host="127.0.0.1", port=port)