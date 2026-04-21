"""
services/smart_money.py
-----------------------
זיהוי סוגי משקיעים:

1. "כסף חכם"   — ROI גבוה + היסטוריה עשירה
2. "לוויתן"     — נפח מסחר גדול מאוד
3. "מידע פנים"  — מעט עסקאות אבל רווח חריג (>25% לעסקה, >$100)
"""

import asyncio
import httpx
from typing import Optional

DATA_BASE  = "https://data-api.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


def _trader_type(profile: dict) -> str:
    trades     = int(profile.get("tradesCount",  0))
    volume     = float(profile.get("volume",     0))
    pnl        = float(profile.get("pnl",        0))
    roi        = float(profile.get("roi",        0))

    # מידע פנים: מעט עסקאות אבל רווח גבוה
    if trades <= 10 and pnl > 100 and roi > 25:
        return "insider"

    # לוויתן: נפח מסחר גדול
    if volume > 50_000:
        return "whale"

    # כסף חכם: ROI טוב + היסטוריה עשירה
    if roi > 15 and trades > 20:
        return "smart_money"

    return "smart_money"   # default


async def _fetch_trader_profile(address: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{DATA_BASE}/profiles/{address}")
        if r.status_code == 200:
            return r.json()
        return None


async def get_smart_money_activity(limit: int = 60) -> list[dict]:
    """
    מחזיר רשימת פעילויות של משקיעים חריגים — לכל אחד:
    - פרטי השאלה שהשקיע בה
    - סוג המשקיע (כסף חכם / לוויתן / מידע פנים)
    - נתוני הטריידר (שם, PnL, כמות עסקאות, כמה השקיע)
    """
    # שלוף עסקאות גדולות אחרונות
    async with httpx.AsyncClient(timeout=20) as client:
        trades_r = await client.get(
            f"{DATA_BASE}/trades",
            params={"limit": 300, "order": "size", "ascending": "false"},
        )
        if trades_r.status_code != 200:
            return []
        all_trades = trades_r.json()

    # סנן — השקעה >$100 בלבד
    notable = [t for t in all_trades if float(t.get("size", 0)) > 100]

    # קבץ לפי כתובת טריידר — קח את הגדולה מכל טריידר
    seen_traders: dict[str, dict] = {}
    for t in notable:
        addr = t.get("maker", "") or t.get("trader", "")
        if not addr:
            continue
        if addr not in seen_traders or float(t.get("size", 0)) > float(seen_traders[addr].get("size", 0)):
            seen_traders[addr] = t

    # שלוף פרופילים של הטריידרים (concurrent)
    addresses = list(seen_traders.keys())[:limit]
    profiles  = await asyncio.gather(*[_fetch_trader_profile(a) for a in addresses])
    profile_map = {a: p for a, p in zip(addresses, profiles) if p}

    # שלוף פרטי שאלות
    condition_ids = list({t.get("conditionId", "") for t in seen_traders.values() if t.get("conditionId")})
    market_map: dict[str, dict] = {}
    if condition_ids:
        async with httpx.AsyncClient(timeout=20) as client:
            markets_r = await client.get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "limit": 300},
            )
            if markets_r.status_code == 200:
                for m in markets_r.json():
                    cid = m.get("conditionId", "")
                    if cid:
                        market_map[cid] = m

    results = []
    for addr, trade in seen_traders.items():
        profile = profile_map.get(addr)
        if not profile:
            continue

        cid     = trade.get("conditionId", "")
        market  = market_map.get(cid, {})
        slug    = market.get("slug", "")
        size    = float(trade.get("size", 0))
        outcome = trade.get("outcome", trade.get("side", ""))
        entry   = float(trade.get("price", 0))

        ttype = _trader_type(profile)

        results.append({
            # שאלה
            "condition_id":   cid,
            "question":       market.get("question", ""),
            "category":       (market.get("tags") or [None])[0],
            "polymarket_url": f"https://polymarket.com/event/{slug}" if slug else "",
            "slug":           slug,

            # פרטי ההשקעה
            "invested_usd":   round(size, 2),
            "entry_price":    round(entry, 3),
            "side":           outcome.upper() if outcome else "",

            # פרטי טריידר
            "trader_address": addr,
            "trader_name":    profile.get("name") or addr[:10] + "...",
            "trader_pnl":     profile.get("pnl", 0),
            "trader_roi":     profile.get("roi", 0),
            "trader_trades":  profile.get("tradesCount", 0),
            "trader_volume":  profile.get("volume", 0),
            "trader_type":    ttype,
            # smart_money | whale | insider
        })

    # מיין: מידע פנים ראשון (הכי מעניין), אחר כך לוויתנים, אחר כך כסף חכם
    order = {"insider": 0, "whale": 1, "smart_money": 2}
    results.sort(key=lambda x: (order.get(x["trader_type"], 3), -x["invested_usd"]))

    return results[:limit]
