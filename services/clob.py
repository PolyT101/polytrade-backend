"""
services/clob.py — Polymarket CLOB Executor v2
================================================
Real order execution via py-clob-client (official Polymarket Python SDK).

All py-clob-client calls are SYNCHRONOUS — run them via asyncio.to_thread()
so they don't block the FastAPI / copy-engine event loop.

Public async API:
  execute_buy(encrypted_key, token_id, amount_usdc, price_hint) → dict
  execute_sell(encrypted_key, token_id, shares, price_hint)      → dict
  derive_api_creds(encrypted_key)                                 → dict
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137  # Polygon mainnet


# ─────────────────────────────────────────────────────────────────────────────
#  Internal sync helpers (called via asyncio.to_thread)
# ─────────────────────────────────────────────────────────────────────────────

def _make_client_sync(private_key_hex: str):
    """
    Build a fully-authenticated ClobClient.
    Step 1: L1-only client → derive API key (nonce=0, deterministic).
    Step 2: Return client with full ApiCreds.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    # L1 client — only private key, no API creds yet
    l1 = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, private_key=private_key_hex)

    # Derive API credentials (nonce=0 → always the same key for this wallet)
    try:
        raw = l1.create_api_key(nonce=0)
    except Exception as e:
        # Key may already exist — fetch existing keys instead
        logger.debug("create_api_key raised (%s) — trying get_api_keys", e)
        try:
            keys = l1.get_api_keys()
            raw  = keys[0] if isinstance(keys, list) and keys else {}
        except Exception:
            raw = {}

    creds = ApiCreds(
        api_key        = raw.get("apiKey")      or raw.get("api_key",        ""),
        api_secret     = raw.get("secret")      or raw.get("api_secret",     ""),
        api_passphrase = raw.get("passphrase")  or raw.get("api_passphrase", ""),
    )

    return ClobClient(
        host        = CLOB_HOST,
        chain_id    = CHAIN_ID,
        private_key = private_key_hex,
        creds       = creds,
    )


def _parse_fill_price(resp: dict, side: str, fallback: float = 0.5) -> float:
    """Extract the average fill price from a CLOB order response."""
    # Direct price field
    if resp.get("price"):
        try:
            return float(resp["price"])
        except (TypeError, ValueError):
            pass
    # Derive from maker/taker amounts (both in 10^6 USDC / token units)
    try:
        maker = int(resp.get("makerAmount") or 0)
        taker = int(resp.get("takerAmount") or 0)
        if side == "BUY" and taker > 0:
            # BUY: maker=USDC spent, taker=tokens received
            return (maker / 1e6) / (taker / 1e6)
        if side == "SELL" and maker > 0:
            # SELL: maker=tokens sold, taker=USDC received
            return (taker / 1e6) / (maker / 1e6)
    except Exception:
        pass
    return fallback


def _is_filled(resp: dict) -> bool:
    """Return True if the order response indicates a successful fill."""
    status = (resp.get("status") or "").upper()
    if resp.get("success") is True:
        return True
    if status in ("MATCHED", "FILLED", "MINED"):
        return True
    # Some versions nest result under "orderBook" / "data"
    if resp.get("orderID") or resp.get("order_id"):
        # Order was accepted — count as success even if fillable>0
        return True
    return False


def _execute_buy_sync(private_key_hex: str, token_id: str, amount_usdc: float,
                      price_hint: float = 0.5) -> dict:
    """
    Place a market BUY (FOK) for `amount_usdc` USDC worth of `token_id` tokens.
    Returns {success, fill_price, shares, order_id, error}.
    """
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        client  = _make_client_sync(private_key_hex)
        args    = MarketOrderArgs(token_id=token_id, amount=amount_usdc)
        signed  = client.create_market_order(args)
        resp    = client.post_order(signed, OrderType.FOK)

        logger.debug("BUY response: %s", resp)

        if _is_filled(resp):
            fill_price = _parse_fill_price(resp, "BUY", price_hint)
            shares     = amount_usdc / fill_price if fill_price > 0 else 0
            return {
                "success":    True,
                "fill_price": fill_price,
                "shares":     round(shares, 6),
                "order_id":   resp.get("orderID") or resp.get("order_id") or "",
                "error":      None,
            }

        error_msg = resp.get("errorMsg") or resp.get("error") or str(resp)
        logger.warning("BUY order not filled: %s", error_msg)
        return {"success": False, "fill_price": 0, "shares": 0, "order_id": "", "error": error_msg}

    except Exception as e:
        logger.error("execute_buy_sync exception: %s", e)
        return {"success": False, "fill_price": 0, "shares": 0, "order_id": "", "error": str(e)}


def _execute_sell_sync(private_key_hex: str, token_id: str, shares: float,
                       price_hint: float = 0.5) -> dict:
    """
    Place a market SELL (FOK) for `shares` tokens of `token_id`.
    Returns {success, fill_price, proceeds_usdc, order_id, error}.
    """
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        client = _make_client_sync(private_key_hex)
        args   = MarketOrderArgs(token_id=token_id, amount=shares, side=SELL)
        signed = client.create_market_order(args)
        resp   = client.post_order(signed, OrderType.FOK)

        logger.debug("SELL response: %s", resp)

        if _is_filled(resp):
            fill_price   = _parse_fill_price(resp, "SELL", price_hint)
            proceeds     = shares * fill_price
            return {
                "success":      True,
                "fill_price":   fill_price,
                "proceeds_usdc": round(proceeds, 4),
                "order_id":     resp.get("orderID") or resp.get("order_id") or "",
                "error":        None,
            }

        error_msg = resp.get("errorMsg") or resp.get("error") or str(resp)
        logger.warning("SELL order not filled: %s", error_msg)
        return {"success": False, "fill_price": 0, "proceeds_usdc": 0, "order_id": "", "error": error_msg}

    except Exception as e:
        logger.error("execute_sell_sync exception: %s", e)
        return {"success": False, "fill_price": 0, "proceeds_usdc": 0, "order_id": "", "error": str(e)}


def _derive_creds_sync(private_key_hex: str) -> dict:
    """Derive and return raw CLOB API credentials for this wallet."""
    try:
        from py_clob_client.client import ClobClient
        l1  = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, private_key=private_key_hex)
        raw = l1.create_api_key(nonce=0)
        return {"success": True, "creds": raw}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  Public async interface
# ─────────────────────────────────────────────────────────────────────────────

async def execute_buy(encrypted_key: str, token_id: str,
                      amount_usdc: float, price_hint: float = 0.5) -> dict:
    """
    Async wrapper — executes a real market BUY on Polymarket CLOB.
    Decrypts the wallet key and runs the blocking CLOB call in a thread pool.
    """
    from services.wallet_service import decrypt_private_key
    try:
        pk = decrypt_private_key(encrypted_key)
    except Exception as e:
        return {"success": False, "error": f"Key decrypt failed: {e}",
                "fill_price": 0, "shares": 0, "order_id": ""}
    return await asyncio.to_thread(_execute_buy_sync, pk, token_id, amount_usdc, price_hint)


async def execute_sell(encrypted_key: str, token_id: str,
                       shares: float, price_hint: float = 0.5) -> dict:
    """
    Async wrapper — executes a real market SELL on Polymarket CLOB.
    """
    from services.wallet_service import decrypt_private_key
    try:
        pk = decrypt_private_key(encrypted_key)
    except Exception as e:
        return {"success": False, "error": f"Key decrypt failed: {e}",
                "fill_price": 0, "proceeds_usdc": 0, "order_id": ""}
    return await asyncio.to_thread(_execute_sell_sync, pk, token_id, shares, price_hint)


async def derive_api_creds(encrypted_key: str) -> dict:
    """Derive CLOB API credentials for a wallet (health-check / setup)."""
    from services.wallet_service import decrypt_private_key
    try:
        pk = decrypt_private_key(encrypted_key)
    except Exception as e:
        return {"success": False, "error": str(e)}
    return await asyncio.to_thread(_derive_creds_sync, pk)
