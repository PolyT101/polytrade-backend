"""
polymarket_service.py
---------------------
כל הקריאות ל-Polymarket CLOB API ו-Gamma API מרוכזות כאן.
"""

import httpx
import asyncio
from typing import Optional

CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE  = "https://data-api.polymarket.com"

# ---- Traders / Leaderboard ----

async def get_leaderboard(limit: int = 50, offset: int = 0) -> list[dict]:
    """
    מושך את רשימת הטריידרים הרווחיים מה-Data API של פולימארקט.
    מחזיר רשימה עם: address, pnl, roi, win_rate, trades_count, volume, ...
    """
    url = f"{DATA_BASE}/profiles"
    params = {
        "limit":  limit,
        "offset": offset,
        "order":  "pnl",        # מיון לפי רווח
        "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def get_trader_profile(address: str) -> dict:
    """
    מושך את כל הנתונים של טריידר ספציפי לפי כתובת ארנק.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        profile_url  = f"{DATA_BASE}/profiles/{address}"
        positions_url = f"{DATA_BASE}/positions?user={address}&limit=200"

        profile_res, positions_res = await asyncio.gather(
            client.get(profile_url),
            client.get(positions_url),
        )
        profile_res.raise_for_status()
        positions_res.raise_for_status()

        profile   = profile_res.json()
        positions = positions_res.json()

        return {**profile, "positions": positions}


async def get_trader_positions(address: str, closed: bool = False) -> list[dict]:
    """
    מושך את הפוזיציות הפתוחות / הסגורות של טריידר.
    """
    url = f"{DATA_BASE}/positions"
    params = {
        "user":   address,
        "limit":  200,
        "closed": str(closed).lower(),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def get_profit_history(address: str) -> list[dict]:
    """
    מושך היסטוריית רווח/הפסד לגרף — נקודות לפי זמן.
    """
    url = f"{DATA_BASE}/pnl-history/{address}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


# ---- Markets ----

async def get_markets(limit: int = 100, active: bool = True) -> list[dict]:
    """
    מושך שווקים פעילים מה-Gamma API.
    """
    url = f"{GAMMA_BASE}/markets"
    params = {"limit": limit, "active": str(active).lower(), "closed": "false"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def get_market_price(token_id: str) -> Optional[float]:
    """
    מחזיר את המחיר הנוכחי של טוקן בשוק ספציפי.
    """
    url = f"{CLOB_BASE}/price"
    params = {"token_id": token_id, "side": "buy"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
        return None
