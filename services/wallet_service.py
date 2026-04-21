"""
wallet_service.py — גרסה 2
---------------------------
ניהול ארנקי Polygon של המשתמשים.

מה יש כאן:
- יצירת ארנקים חדשים
- ארנק "ברירת מחדל" לכל משתמש
- העברת USDC בין ארנקים בתוך המערכת
- שליפת יתרות מהבלוקצ'יין
- הצפנת private keys
"""

import os
import secrets
import httpx
from eth_account import Account
from cryptography.fernet import Fernet

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").encode()
POLYGON_RPC    = "https://polygon-rpc.com"
USDC_ADDRESS   = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC Native על Polygon


# ------------------------------------------------------------------ #
#  הצפנה / פענוח                                                       #
# ------------------------------------------------------------------ #

def _get_fernet() -> Fernet:
    if not ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY חסר — הגדר אותו ב-.env")
    return Fernet(ENCRYPTION_KEY)


def encrypt_private_key(private_key: str) -> str:
    return _get_fernet().encrypt(private_key.encode()).decode()


def decrypt_private_key(encrypted_key: str) -> str:
    """פענוח private key — רק בזיכרון, לעולם לא נכתב ללוג."""
    return _get_fernet().decrypt(encrypted_key.encode()).decode()


# ------------------------------------------------------------------ #
#  יצירת ארנקים                                                        #
# ------------------------------------------------------------------ #

def create_wallet() -> dict:
    """
    יוצר ארנק Polygon חדש.

    ⚠️  private_key_plaintext מוחזר רק פעם אחת כדי שהמשתמש יוכל לגבות אותו.
        לאחר מכן הוא לא נשמר בשום מקום בטקסט פתוח.
    """
    account = Account.create(extra_entropy=secrets.token_hex(32))
    encrypted = encrypt_private_key(account.key.hex())

    return {
        "address":               account.address,
        "encrypted_private_key": encrypted,
        "private_key_plaintext": account.key.hex(),   # מוחזר פעם אחת בלבד!
    }


def create_wallet_for_copy(use_default: bool, user_default_wallet: dict | None) -> dict:
    """
    מחליט אם ליצור ארנק חדש או להשתמש בארנק ברירת המחדל.

    פרמטרים:
        use_default           — True = השתמש בארנק ברירת המחדל הקיים
        user_default_wallet   — dict עם address + encrypted_private_key (מה-DB)

    מחזיר:
        dict עם address + encrypted_private_key + is_new_wallet
    """
    if use_default and user_default_wallet:
        return {
            "address":               user_default_wallet["address"],
            "encrypted_private_key": user_default_wallet["encrypted_private_key"],
            "private_key_plaintext": None,   # לא מחזירים שוב — המשתמש כבר שמר
            "is_new_wallet":         False,
        }

    # ארנק חדש
    new = create_wallet()
    return {**new, "is_new_wallet": True}


# ------------------------------------------------------------------ #
#  יתרות                                                               #
# ------------------------------------------------------------------ #

def get_usdc_balance(address: str) -> float:
    """שולף יתרת USDC (Native) מהבלוקצ'יין של Polygon."""
    data = (
        "0x70a08231"
        + "000000000000000000000000"
        + address[2:].lower()
    )
    payload = {
        "jsonrpc": "2.0",
        "method":  "eth_call",
        "params":  [{"to": USDC_ADDRESS, "data": data}, "latest"],
        "id":      1,
    }
    try:
        r = httpx.post(POLYGON_RPC, json=payload, timeout=10)
        result = r.json().get("result", "0x0")
        return int(result, 16) / 1_000_000   # USDC = 6 decimals
    except Exception:
        return 0.0


def get_matic_balance(address: str) -> float:
    """שולף יתרת MATIC (לתשלום עמלות גז)."""
    payload = {
        "jsonrpc": "2.0",
        "method":  "eth_getBalance",
        "params":  [address, "latest"],
        "id":      1,
    }
    try:
        r = httpx.post(POLYGON_RPC, json=payload, timeout=10)
        result = r.json().get("result", "0x0")
        return int(result, 16) / 1e18   # wei → MATIC
    except Exception:
        return 0.0


def get_all_balances(address: str) -> dict:
    """מחזיר יתרת USDC + MATIC של ארנק."""
    return {
        "address":      address,
        "usdc_balance": get_usdc_balance(address),
        "matic_balance": round(get_matic_balance(address), 4),
    }


# ------------------------------------------------------------------ #
#  העברת USDC בין ארנקים                                               #
# ------------------------------------------------------------------ #

def transfer_usdc(
    from_encrypted_key: str,
    from_address:       str,
    to_address:         str,
    amount_usdc:        float,
) -> dict:
    """
    מעביר USDC מארנק אחד לאחר בתוך המערכת.

    שלבים:
    1. פענוח ה-private key של השולח
    2. בניית טרנזקציית ERC-20 transfer
    3. חתימה ושליחה ל-Polygon

    ⚠️  דורש שלארנק השולח יש מספיק MATIC לגז (~0.01 MATIC מספיק).
    """
    from web3 import Web3
    from web3.middleware import geth_poa_middleware

    private_key  = decrypt_private_key(from_encrypted_key)
    w3           = Web3(Web3.HTTPProvider(POLYGON_RPC))
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

    # ABI מינימלי ל-ERC-20 transfer
    ERC20_ABI = [
        {
            "name": "transfer",
            "type": "function",
            "inputs": [
                {"name": "to",     "type": "address"},
                {"name": "value",  "type": "uint256"},
            ],
            "outputs": [{"name": "", "type": "bool"}],
            "stateMutability": "nonpayable",
        }
    ]

    contract   = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    amount_raw = int(amount_usdc * 1_000_000)   # USDC = 6 decimals
    nonce      = w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
    gas_price  = w3.eth.gas_price

    tx = contract.functions.transfer(
        Web3.to_checksum_address(to_address),
        amount_raw,
    ).build_transaction({
        "chainId":  137,
        "nonce":    nonce,
        "gas":      100_000,
        "gasPrice": gas_price,
        "from":     Web3.to_checksum_address(from_address),
    })

    signed    = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash   = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt   = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    return {
        "success":    receipt.status == 1,
        "tx_hash":    tx_hash.hex(),
        "from":       from_address,
        "to":         to_address,
        "amount_usdc": amount_usdc,
        "gas_used":   receipt.gasUsed,
    }
