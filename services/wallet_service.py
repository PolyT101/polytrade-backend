"""
wallet_service.py — v3 עם Alchemy RPC
---------------------------------------
שדרוג: משתמש ב-Alchemy במקום polygon-rpc.com הציבורי
לאמינות גבוהה יותר ולביצועים טובים יותר.
"""

import os
import secrets
import httpx
from eth_account import Account
from cryptography.fernet import Fernet

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").encode()

# Alchemy RPC — מהיר ואמין
ALCHEMY_KEY = os.getenv("ALCHEMY_KEY", "")
POLYGON_RPC  = f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}" if ALCHEMY_KEY else "https://polygon-rpc.com"

USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC Native על Polygon


# ------------------------------------------------------------------ #
#  הצפנה / פענוח                                                       #
# ------------------------------------------------------------------ #

def _get_fernet() -> Fernet:
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY חסר")
    key = ENCRYPTION_KEY
    # Fernet דורש key באורך 32 bytes מקודד ב-base64
    if len(key) != 44:  # 32 bytes in base64 = 44 chars
        import base64, hashlib
        key = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
    return Fernet(key)


def encrypt_private_key(private_key: str) -> str:
    return _get_fernet().encrypt(private_key.encode()).decode()


def decrypt_private_key(encrypted_key: str) -> str:
    return _get_fernet().decrypt(encrypted_key.encode()).decode()


# ------------------------------------------------------------------ #
#  יצירת ארנקים                                                        #
# ------------------------------------------------------------------ #

def create_wallet() -> dict:
    """יוצר ארנק Polygon חדש."""
    account = Account.create(extra_entropy=secrets.token_hex(32))
    encrypted = encrypt_private_key(account.key.hex())
    return {
        "address":               account.address,
        "encrypted_private_key": encrypted,
        "private_key_plaintext": account.key.hex(),  # מוחזר פעם אחת בלבד!
    }


def create_wallet_for_copy(use_default: bool, user_default_wallet: dict | None) -> dict:
    if use_default and user_default_wallet:
        return {
            "address":               user_default_wallet["address"],
            "encrypted_private_key": user_default_wallet["encrypted_private_key"],
            "private_key_plaintext": None,
            "is_new_wallet":         False,
        }
    new = create_wallet()
    return {**new, "is_new_wallet": True}


# ------------------------------------------------------------------ #
#  יתרות — דרך Alchemy                                                  #
# ------------------------------------------------------------------ #

def _rpc_call(method: str, params: list) -> dict:
    """שליחת קריאת JSON-RPC ל-Polygon."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        r = httpx.post(POLYGON_RPC, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_usdc_balance(address: str) -> float:
    """שולף יתרת USDC מהבלוקצ'יין."""
    data = "0x70a08231" + "000000000000000000000000" + address[2:].lower()
    result = _rpc_call("eth_call", [{"to": USDC_ADDRESS, "data": data}, "latest"])
    try:
        return int(result.get("result", "0x0"), 16) / 1_000_000
    except Exception:
        return 0.0


def get_matic_balance(address: str) -> float:
    """שולף יתרת POL/MATIC."""
    result = _rpc_call("eth_getBalance", [address, "latest"])
    try:
        return int(result.get("result", "0x0"), 16) / 1e18
    except Exception:
        return 0.0


def get_all_balances(address: str) -> dict:
    return {
        "address":       address,
        "usdc_balance":  get_usdc_balance(address),
        "matic_balance": round(get_matic_balance(address), 4),
    }


# ------------------------------------------------------------------ #
#  העברת USDC                                                           #
# ------------------------------------------------------------------ #

def transfer_usdc(
    from_encrypted_key: str,
    from_address: str,
    to_address: str,
    amount_usdc: float,
) -> dict:
    """מעביר USDC בין ארנקים דרך Polygon."""
    from web3 import Web3

    private_key = decrypt_private_key(from_encrypted_key)
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))

    ERC20_ABI = [{
        "name": "transfer",
        "type": "function",
        "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    }]

    contract   = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    amount_raw = int(amount_usdc * 1_000_000)
    nonce      = w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))

    tx = contract.functions.transfer(
        Web3.to_checksum_address(to_address), amount_raw
    ).build_transaction({
        "chainId": 137, "nonce": nonce,
        "gas": 100_000, "gasPrice": w3.eth.gas_price,
        "from": Web3.to_checksum_address(from_address),
    })

    signed  = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    return {
        "success": receipt.status == 1,
        "tx_hash": tx_hash.hex(),
        "from": from_address, "to": to_address,
        "amount_usdc": amount_usdc,
        "gas_used": receipt.gasUsed,
        "polygon_scan": f"https://polygonscan.com/tx/{tx_hash.hex()}"
    }
