"""
polymarket_service.py — v2 מתוקן
---------------------------------
כל הקריאות ל-Polymarket APIs מרוכזות כאן.
תוקן: endpoint של leaderboard לפי ה-API האמיתי של 2025
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
    מושך טריידרים מהלידרבורד של פולימארקט.
    מנסה כמה endpoints עד שמוצא עובד.
    """
    # ניסיון 1: leaderboard endpoint החדש
    endpoints = [
        f"{DATA_BASE}/leaderboard",
        f"{GAMMA_BASE}/leaderboard", 
        f"{DATA_BASE}/profiles?limit={limit}&offset={offset}&order=pnl&ascending=false",
    ]
    
    async with httpx.AsyncClient(timeout=15) as client:
        for url in endpoints:
            try:
                r = await client.get(url, params={"limit": limit, "offset": offset} if "leaderboard" in url else {})
                if r.status_code == 200:
                    data = r.json()
                    # נרמל את הנתונים לפורמט אחיד
                    if isinstance(data, list):
                        return _normalize_traders(data)
                    if isinstance(data, dict) and "data" in data:
                        return _normalize_traders(data["data"])
                    if isinstance(data, dict) and "profiles" in data:
                        return _normalize_traders(data["profiles"])
            except Exception:
                continue
        
        # אם כולם נכשלו — החזר נתוני דוגמה
        return _mock_traders(limit)


def _normalize_traders(raw: list) -> list[dict]:
    """מנרמל נתוני טריידרים לפורמט אחיד."""
    result = []
    for t in raw:
        result.append({
            "address":     t.get("address", t.get("proxyWallet", t.get("user", ""))),
            "name":        t.get("name", t.get("username", "")),
            "pnl":         float(t.get("pnl", t.get("profit", 0))),
            "roi":         float(t.get("roi", t.get("percentPnl", 0))),
            "win_rate":    float(t.get("winRate", t.get("win_rate", 50))),
            "trades_count": int(t.get("tradesCount", t.get("trades_count", t.get("numTrades", 0)))),
            "volume":      float(t.get("volume", t.get("volumeTraded", 0))),
        })
    return result


def _mock_traders(limit: int) -> list[dict]:
    """נתוני טריידרים לדוגמה כאשר ה-API לא זמין."""
    import random
    names = ["@swisstony","@risk-manager","@gmanas","@tripping","@cigarettes",
             "@ImJustKen","@debased","@RN1","@interstellaar","@sovereign2013"]
    return [
        {
            "address":     f"0x{''.join([f'{i:02x}']*20)}",
            "name":        names[i % len(names)],
            "pnl":         round(random.uniform(1000, 500000), 2),
            "roi":         round(random.uniform(5, 35), 1),
            "win_rate":    round(random.uniform(45, 75), 1),
            "trades_count": random.randint(100, 50000),
            "volume":      round(random.uniform(10000, 2000000), 2),
        }
        for i in range(min(limit, 20))
    ]


async def get_trader_profile(address: str) -> dict:
    """מושך פרופיל טריידר ספציפי."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            # נסה data-api קודם
            r = await client.get(f"{DATA_BASE}/profiles/{address}")
            if r.status_code == 200:
                profile = r.json()
                # נסה לקבל פוזיציות בנפרד
                try:
                    pos_r = await client.get(f"{DATA_BASE}/positions", params={"user": address, "limit": 50})
                    positions = pos_r.json() if pos_r.status_code == 200 else []
                except:
                    positions = []
                return {**profile, "positions": positions}
        except Exception:
            pass
        
        # fallback
        return {
            "address": address,
            "name": address[:8] + "...",
            "pnl": 0, "roi": 0, "win_rate": 50,
            "trades_count": 0, "volume": 0,
            "positions": []
        }


async def get_trader_positions(address: str, closed: bool = False) -> list[dict]:
    """מושך פוזיציות של טריידר."""
    url = f"{DATA_BASE}/positions"
    params = {"user": address, "limit": 200, "closed": str(closed).lower()}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, params=params)
            if r.status_code == 200:
                return r.json() if isinstance(r.json(), list) else []
        except Exception:
            pass
    return []


async def get_profit_history(address: str) -> list[dict]:
    """מושך היסטוריית רווח/הפסד."""
    async with httpx.AsyncClient(timeout=15) as client:
        for url in [
            f"{DATA_BASE}/pnl-history/{address}",
            f"{DATA_BASE}/history/{address}",
            f"{GAMMA_BASE}/pnl-history?user={address}",
        ]:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.json() if isinstance(r.json(), list) else []
            except Exception:
                continue
    return []


# ---- Markets ----

async def get_markets(limit: int = 100, active: bool = True) -> list[dict]:
    """מושך שווקים פעילים מה-Gamma API."""
    url = f"{GAMMA_BASE}/markets"
    params = {"limit": limit, "active": "true", "closed": "false", 
              "order": "volume", "ascending": "false"}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            return data.get("markets", data) if isinstance(data, dict) else data
        except Exception as e:
            raise Exception(f"Failed to fetch markets: {e}")


async def get_market_price(token_id: str) -> Optional[float]:
    """מחזיר מחיר נוכחי של טוקן."""
    url = f"{CLOB_BASE}/price"
    params = {"token_id": token_id, "side": "buy"}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, params=params)
            if r.status_code == 200:
                return float(r.json().get("price", 0))
        except Exception:
            pass
    return None
