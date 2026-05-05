"""
wallet_service.py — v4
-----------------------
ניהול ארנקים: יצירה, הצפנה, יתרות, העברות, ואישורי חוזי Polymarket.

Polymarket Contract Addresses (Polygon mainnet):
  USDC Native:            0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
  CTF Exchange:           0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
  Neg-Risk CTF Exchange:  0xC5d563A36AE78145C45a50134d48A1215220f80a
  Conditional Tokens:     0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  Neg-Risk Adapter:       0xd91E80cF2Ed09f3De5B5a9d1089b54db7b0B5B88
"""

import os
import secrets
import asyncio
import httpx
from eth_account import Account
from cryptography.fernet import Fernet

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").encode()

# Alchemy RPC — מהיר ואמין
ALCHEMY_KEY = os.getenv("ALCHEMY_KEY", "")
POLYGON_RPC  = f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}" if ALCHEMY_KEY else "https://polygon-rpc.com"

# ── Contract addresses (Polygon mainnet) ──────────────────────────────────────
USDC_ADDRESS         = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
CTF_EXCHANGE         = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE= "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CONDITIONAL_TOKENS   = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER     = "0xd91E80cF2Ed09f3De5B5a9d1089b54db7b0B5B88"

MAX_UINT256 = 2**256 - 1  # unlimited approval

# Minimal ABIs
_ERC20_ABI = [
    {"name": "approve",   "type": "function",
     "inputs":  [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
    {"name": "allowance", "type": "function",
     "inputs":  [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]
_ERC1155_ABI = [
    {"name": "setApprovalForAll", "type": "function",
     "inputs":  [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "outputs": [], "stateMutability": "nonpayable"},
    {"name": "isApprovedForAll", "type": "function",
     "inputs":  [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view"},
]


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

# ------------------------------------------------------------------ #
#  Polymarket Approvals                                                #
# ------------------------------------------------------------------ #

def check_polymarket_approval_sync(address: str) -> dict:
    """
    Read-only: check current approval status for all Polymarket contracts.
    Returns dict with True/False per contract — no gas needed.
    """
    from web3 import Web3
    w3  = Web3(Web3.HTTPProvider(POLYGON_RPC))
    cs  = Web3.to_checksum_address

    try:
        usdc = w3.eth.contract(address=cs(USDC_ADDRESS), abi=_ERC20_ABI)
        ctf  = w3.eth.contract(address=cs(CONDITIONAL_TOKENS), abi=_ERC1155_ABI)
        addr = cs(address)

        usdc_exchange     = usdc.functions.allowance(addr, cs(CTF_EXCHANGE)).call()
        usdc_neg_exchange = usdc.functions.allowance(addr, cs(NEG_RISK_CTF_EXCHANGE)).call()
        ctf_exchange      = ctf.functions.isApprovedForAll(addr, cs(CTF_EXCHANGE)).call()
        ctf_neg_exchange  = ctf.functions.isApprovedForAll(addr, cs(NEG_RISK_CTF_EXCHANGE)).call()
        ctf_neg_adapter   = ctf.functions.isApprovedForAll(addr, cs(NEG_RISK_ADAPTER)).call()

        all_approved = (
            usdc_exchange > 0
            and usdc_neg_exchange > 0
            and ctf_exchange
            and ctf_neg_exchange
            and ctf_neg_adapter
        )

        return {
            "ready":                   all_approved,
            "usdc_exchange":           usdc_exchange > 0,
            "usdc_neg_risk_exchange":  usdc_neg_exchange > 0,
            "ctf_exchange":            ctf_exchange,
            "ctf_neg_risk_exchange":   ctf_neg_exchange,
            "ctf_neg_risk_adapter":    ctf_neg_adapter,
            "usdc_allowance_raw":      str(usdc_exchange),
        }
    except Exception as e:
        return {"ready": False, "error": str(e)}


def approve_polymarket_contracts_sync(address: str, encrypted_key: str) -> dict:
    """
    One-time on-chain setup: approve all 5 Polymarket contracts.
    Skips contracts already approved to save gas.
    Returns list of tx hashes submitted.
    """
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware

    private_key = decrypt_private_key(encrypted_key)
    w3          = Web3(Web3.HTTPProvider(POLYGON_RPC))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)  # Polygon PoA

    cs   = Web3.to_checksum_address
    addr = cs(address)

    usdc = w3.eth.contract(address=cs(USDC_ADDRESS),       abi=_ERC20_ABI)
    ctf  = w3.eth.contract(address=cs(CONDITIONAL_TOKENS), abi=_ERC1155_ABI)

    gas_price = w3.eth.gas_price
    nonce     = w3.eth.get_transaction_count(addr)
    submitted = []
    skipped   = []

    def _send(fn, label: str, check_fn=None):
        nonlocal nonce
        try:
            # Check if already approved (skip to save gas)
            if check_fn and check_fn():
                skipped.append(label)
                return
            tx = fn.build_transaction({
                "chainId": 137, "from": addr, "nonce": nonce,
                "gas": 100_000, "gasPrice": gas_price,
            })
            signed  = w3.eth.account.sign_transaction(tx, private_key=private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            nonce  += 1
            submitted.append({"label": label, "tx_hash": tx_hash})
        except Exception as e:
            submitted.append({"label": label, "error": str(e)})

    # 1. USDC → CTF Exchange
    _send(usdc.functions.approve(cs(CTF_EXCHANGE), MAX_UINT256),
          "usdc→ctf_exchange",
          check_fn=lambda: usdc.functions.allowance(addr, cs(CTF_EXCHANGE)).call() > 0)

    # 2. USDC → Neg-Risk CTF Exchange
    _send(usdc.functions.approve(cs(NEG_RISK_CTF_EXCHANGE), MAX_UINT256),
          "usdc→neg_risk_ctf_exchange",
          check_fn=lambda: usdc.functions.allowance(addr, cs(NEG_RISK_CTF_EXCHANGE)).call() > 0)

    # 3. ConditionalTokens → CTF Exchange
    _send(ctf.functions.setApprovalForAll(cs(CTF_EXCHANGE), True),
          "ctf→ctf_exchange",
          check_fn=lambda: ctf.functions.isApprovedForAll(addr, cs(CTF_EXCHANGE)).call())

    # 4. ConditionalTokens → Neg-Risk CTF Exchange
    _send(ctf.functions.setApprovalForAll(cs(NEG_RISK_CTF_EXCHANGE), True),
          "ctf→neg_risk_ctf_exchange",
          check_fn=lambda: ctf.functions.isApprovedForAll(addr, cs(NEG_RISK_CTF_EXCHANGE)).call())

    # 5. ConditionalTokens → Neg-Risk Adapter
    _send(ctf.functions.setApprovalForAll(cs(NEG_RISK_ADAPTER), True),
          "ctf→neg_risk_adapter",
          check_fn=lambda: ctf.functions.isApprovedForAll(addr, cs(NEG_RISK_ADAPTER)).call())

    # Wait for receipts (up to 60s per tx)
    receipts = []
    for item in submitted:
        if "tx_hash" in item:
            try:
                receipt = w3.eth.wait_for_transaction_receipt(
                    bytes.fromhex(item["tx_hash"].lstrip("0x")), timeout=60
                )
                item["confirmed"] = receipt.status == 1
                item["gas_used"]  = receipt.gasUsed
            except Exception as e:
                item["receipt_error"] = str(e)
        receipts.append(item)

    all_ok = all(
        r.get("confirmed", False) or r.get("error") is None
        for r in receipts if "tx_hash" in r
    )

    return {
        "success":   all_ok or not any("tx_hash" in r for r in receipts),
        "submitted": receipts,
        "skipped":   skipped,
        "message":   "ארנק מוכן לטריידינג אמיתי!" if (all_ok or not receipts) else "חלק מהאישורים נכשלו",
    }


async def approve_polymarket_contracts(address: str, encrypted_key: str) -> dict:
    """Async wrapper for approve_polymarket_contracts_sync."""
    return await asyncio.to_thread(approve_polymarket_contracts_sync, address, encrypted_key)


async def check_polymarket_approval(address: str) -> dict:
    """Async wrapper for check_polymarket_approval_sync."""
    return await asyncio.to_thread(check_polymarket_approval_sync, address)


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
