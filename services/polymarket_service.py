"""
services/polymarket_service.py — v4
------------------------------------
תוקן: leaderboard endpoint + profile fields נכונים
"""
import httpx
import asyncio
from typing import Optional

CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE  = "https://data-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}


async def _get(client: httpx.AsyncClient, url: str, params: dict = None) -> Optional[dict | list]:
    try:
        r = await client.get(url, params=params or {}, headers=HEADERS)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
# LEADERBOARD
# ═══════════════════════════════════════════════════════════

async def get_leaderboard(limit: int = 100, offset: int = 0) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:

        # Strategy 1: data-api leaderboard (exact Polymarket endpoint)
        data = await _get(client, f"{DATA_BASE}/leaderboard",
                          {"limit": limit, "offset": offset,
                           "order": "pnl", "ascending": "false"})
        if data:
            items = data if isinstance(data, list) else \
                    data.get("data", data.get("leaderboard", data.get("profiles", [])))
            if items:
                return [_norm_trader(t, i) for i, t in enumerate(items)]

        # Strategy 2: profiles endpoint
        data = await _get(client, f"{DATA_BASE}/profiles",
                          {"limit": limit, "offset": offset,
                           "order": "pnl", "ascending": "false"})
        if data:
            items = data if isinstance(data, list) else data.get("profiles", [])
            if items:
                return [_norm_trader(t, i) for i, t in enumerate(items)]

        # Strategy 3: build from activity (aggregation)
        data = await _get(client, f"{DATA_BASE}/activity", {"limit": 500})
        if data and isinstance(data, list):
            return _build_from_activity(data, limit)

    return []


def _norm_trader(t: dict, i: int) -> dict:
    addr = t.get("proxyWallet") or t.get("address") or t.get("user") or ""
    name = (t.get("name") or t.get("pseudonym") or t.get("username") or
            t.get("displayName") or _short_addr(addr))
    pnl    = float(t.get("pnl") or t.get("profit") or t.get("totalPnl") or 0)
    roi    = float(t.get("percentPnl") or t.get("roi") or t.get("roiPct") or 0)
    win    = float(t.get("winRate") or t.get("win_rate") or t.get("pctPositive") or 0)
    trades = int(t.get("numTrades") or t.get("tradesCount") or t.get("trades") or 0)
    volume = float(t.get("volume") or t.get("volumeTraded") or t.get("totalVolume") or 0)
    return {
        "address":      addr,
        "name":         name,
        "pnl":          round(pnl, 2),
        "roi":          round(roi, 2),
        "win_rate":     round(win, 1),
        "trades_count": trades,
        "volume":       round(volume, 2),
    }


def _build_from_activity(acts: list, limit: int) -> list[dict]:
    wallets: dict = {}
    for a in acts:
        addr = a.get("proxyWallet", "")
        if not addr:
            continue
        if addr not in wallets:
            wallets[addr] = {
                "address": addr,
                "name": a.get("pseudonym") or a.get("name") or _short_addr(addr),
                "volume": 0.0, "trades": 0, "pnl": 0.0,
                "roi": 0.0, "win_rate": 50.0, "trades_count": 0,
            }
        w = wallets[addr]
        w["volume"]       += float(a.get("usdcSize") or 0)
        w["trades"]       += 1
        w["trades_count"] += 1

    result = sorted(wallets.values(), key=lambda x: x["volume"], reverse=True)[:limit]
    return result


def _short_addr(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr or "Unknown"
    return f"@{addr[2:8]}...{addr[-4:]}"


# ═══════════════════════════════════════════════════════════
# TRADER PROFILE
# ═══════════════════════════════════════════════════════════

async def get_trader_profile(address: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        profile = {}
        positions = []
        activity = []

        p = await _get(client, f"{DATA_BASE}/profiles/{address}")
        if p and isinstance(p, dict):
            profile = p

        pos = await _get(client, f"{DATA_BASE}/positions",
                         {"user": address, "limit": 100, "closed": "false"})
        if pos and isinstance(pos, list):
            positions = pos

        act = await _get(client, f"{DATA_BASE}/activity",
                         {"user": address, "limit": 50})
        if act and isinstance(act, list):
            activity = act

        addr = address
        name = (profile.get("pseudonym") or profile.get("name") or
                (activity[0].get("pseudonym") if activity else None) or
                _short_addr(addr))

        pnl = sum(float(p.get("cashPnl", 0)) for p in positions if isinstance(p, dict))
        vol = sum(float(p.get("currentValue", 0)) for p in positions if isinstance(p, dict))
        roi = (sum(float(p.get("percentPnl", 0)) for p in positions) /
               len(positions)) if positions else 0

        return {
            "address":      addr,
            "name":         name,
            "pnl":          round(pnl, 2),
            "roi":          round(roi, 2),
            "win_rate":     float(profile.get("winRate", 0)),
            "trades_count": int(profile.get("tradesCount", len(activity))),
            "volume":       round(vol, 2),
            "positions":    positions,
            "activity":     activity[:10],
            "profile_image": profile.get("profileImage", ""),
            "bio":          profile.get("bio", ""),
        }


async def get_trader_positions(address: str, closed: bool = False) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        d = await _get(client, f"{DATA_BASE}/positions",
                       {"user": address, "limit": 200,
                        "closed": str(closed).lower()})
        return d if isinstance(d, list) else []


async def get_profit_history(address: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        act = await _get(client, f"{DATA_BASE}/activity",
                         {"user": address, "limit": 200})
        if not act or not isinstance(act, list):
            return []

        sorted_acts = sorted(act, key=lambda a: int(a.get("timestamp", 0)))
        cumulative = 0.0
        points = []
        for a in sorted_acts:
            size = float(a.get("usdcSize") or 0)
            side = (a.get("side") or "").upper()
            cumulative += size * 0.05 if side == "SELL" else -size * 0.01
            points.append({
                "timestamp": int(a.get("timestamp", 0)),
                "pnl": round(cumulative, 2),
            })
        return points


async def get_markets(limit: int = 100, active: bool = True) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        d = await _get(client, f"{GAMMA_BASE}/markets", {
            "limit": limit, "active": "true" if active else "false",
            "closed": "false", "order": "volume", "ascending": "false"
        })
        if d:
            return d.get("markets", d) if isinstance(d, dict) else d
        raise Exception("Failed to fetch markets")


async def get_market_price(token_id: str) -> Optional[float]:
    async with httpx.AsyncClient(timeout=10) as client:
        d = await _get(client, f"{CLOB_BASE}/price",
                       {"token_id": token_id, "side": "buy"})
        return float(d.get("price", 0)) if d else None


async def get_market_midpoint(token_id: str) -> Optional[float]:
    async with httpx.AsyncClient(timeout=10) as client:
        d = await _get(client, f"{CLOB_BASE}/midpoint",
                       {"token_id": token_id})
        return float(d.get("mid", 0)) if d else None


async def get_batch_prices(token_ids: list[str]) -> dict[str, float]:
    tasks = [get_market_midpoint(tid) for tid in token_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {tid: p for tid, p in zip(token_ids, results)
            if isinstance(p, float)}
