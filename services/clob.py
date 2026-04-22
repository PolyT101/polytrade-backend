"""
services/clob.py — Polymarket CLOB API Client
==============================================
Handles order placement, cancellation, and status checks.
Uses EIP-712 signatures with user's private key.
"""

import httpx
import json
import time
import hashlib
import hmac
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

CLOB_URL = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

class CLOBClient:
    def __init__(self, wallet):
        """wallet: Wallet DB model with private_key_encrypted and address"""
        from services.security import decrypt_key
        self.private_key = decrypt_key(wallet.private_key_encrypted)
        self.address = wallet.address
        self.account = Account.from_key(self.private_key)
        self._api_creds = None

    async def get_api_credentials(self) -> dict:
        """Get or create CLOB API credentials."""
        if self._api_creds:
            return self._api_creds

        # Create API key via signature
        timestamp = str(int(time.time()))
        nonce = timestamp

        # Sign the authentication message
        msg = f"This message attests that I control the given wallet\n\nTimestamp: {timestamp}\nNonce: {nonce}"
        signed = self.account.sign_message(encode_defunct(text=msg))

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CLOB_URL}/auth/api-key",
                json={
                    "address": self.address,
                    "signature": signed.signature.hex(),
                    "timestamp": timestamp,
                    "nonce": nonce
                }
            )
            if r.is_success:
                creds = r.json()
                self._api_creds = creds
                return creds
            else:
                raise Exception(f"Auth failed: {r.text}")

    async def place_order(self, token_id: str, side: str, size: float, 
                          price: float, condition_id: str) -> dict:
        """
        Place a limit order on Polymarket CLOB.
        
        token_id: The outcome token to buy/sell
        side: BUY or SELL
        size: Amount in USDC
        price: Price in cents (0.0 to 1.0)
        """
        try:
            creds = await self.get_api_credentials()

            # Build order
            order = {
                "tokenID": token_id,
                "side": side,
                "price": str(round(price, 4)),
                "size": str(round(size, 2)),
                "orderType": "GTC",  # Good Till Cancelled
                "feeRateBps": "0",
                "nonce": str(int(time.time() * 1000)),
                "maker": self.address,
                "taker": "0x0000000000000000000000000000000000000000",
                "expiration": str(int(time.time()) + 3600),  # 1 hour expiry
            }

            # Sign the order hash
            order_hash = self._hash_order(order)
            signed = self.account.sign_message(encode_defunct(hexstr=order_hash))
            order["signature"] = signed.signature.hex()

            # Submit
            headers = {
                "POLY_ADDRESS": self.address,
                "POLY_SIGNATURE": creds.get("signature", ""),
                "POLY_TIMESTAMP": creds.get("timestamp", ""),
                "POLY_NONCE": creds.get("nonce", ""),
                "Content-Type": "application/json"
            }

            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    f"{CLOB_URL}/order",
                    json={"order": order, "owner": self.address, "orderType": "GTC"},
                    headers=headers
                )

                if r.is_success:
                    data = r.json()
                    return {
                        "success": True,
                        "order_id": data.get("orderID"),
                        "transaction_hash": data.get("transactionHash", ""),
                        "status": data.get("status", "pending")
                    }
                else:
                    return {
                        "success": False,
                        "error": r.text,
                        "status_code": r.status_code
                    }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _hash_order(self, order: dict) -> str:
        """Create EIP-712 order hash for signing."""
        # Simplified hash - production should use full EIP-712
        order_str = json.dumps(order, sort_keys=True)
        return "0x" + hashlib.sha256(order_str.encode()).hexdigest()

    async def get_positions(self) -> list:
        """Get current open positions."""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://data-api.polymarket.com/positions",
                params={"user": self.address, "limit": 100, "closed": "false"}
            )
            return r.json() if r.is_success else []

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            creds = await self.get_api_credentials()
            headers = {
                "POLY_ADDRESS": self.address,
                "POLY_SIGNATURE": creds.get("signature", ""),
                "POLY_TIMESTAMP": creds.get("timestamp", ""),
                "POLY_NONCE": creds.get("nonce", ""),
            }
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.delete(
                    f"{CLOB_URL}/order/{order_id}",
                    headers=headers
                )
                return r.is_success
        except Exception:
            return False
