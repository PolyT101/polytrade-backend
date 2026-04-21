"""
services/market_analyzer.py
----------------------------
לוגיקת ניתוח שוק לשלושת הכלים:

1. sniper()       — הזדמנויות עם ROI פוטנציאלי גבוה + זיהוי כסף חכם
2. safe_profit()  — שאלות עם הסתברות >80% (הימור בטוח יחסית)
3. whales()       — שאלות עם השקעת כסף גדולה של לוויתנים
"""

import asyncio
import httpx
from datetime import datetime, timezone
from typing import Optional

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE  = "https://data-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"


# ------------------------------------------------------------------ #
#  פונקציות עזר                                                        #
# ------------------------------------------------------------------ #

def _days_remaining(end_date_str: Optional[str]) -> Optional[int]:
    if not end_date_str:
        return None
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        delta = end - datetime.now(timezone.utc)
        return max(0, delta.days)
    except Exception:
        return None


def _polymarket_url(slug: str) -> str:
    return f"https://polymarket.com/event/{slug}"


def _format_market(m: dict, smart_money: bool = False) -> dict:
    """ממיר נתוני שוק גולמיים לפורמט אחיד לכל העמודים."""
    tokens   = m.get("tokens", [])
    yes_tok  = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), {})
    no_tok   = next((t for t in tokens if t.get("outcome", "").upper() == "NO"),  {})
    yes_price = float(yes_tok.get("price", 0))
    no_price  = float(no_tok.get("price", 0))
    slug      = m.get("slug", "")

    # חשב ROI פוטנציאלי: אם YES = 0.30, ROI אם נכון = (1-0.30)/0.30 = 233%
    best_price = min(p for p in [yes_price, no_price] if p > 0) if (yes_price or no_price) else 0.5
    roi_pct    = round(((1 - best_price) / best_price) * 100, 1) if best_price > 0 else 0
    best_side  = "YES" if yes_price <= no_price else "NO"

    days = _days_remaining(m.get("endDate") or m.get("end_date_iso"))

    return {
        "condition_id":   m.get("conditionId", m.get("condition_id", "")),
        "slug":           slug,
        "question":       m.get("question", ""),
        "category":       m.get("category", m.get("tags", [None])[0] if m.get("tags") else None),
        "yes_price":      round(yes_price, 3),
        "no_price":       round(no_price, 3),
        "best_side":      best_side,
        "best_price":     round(best_price, 3),
        "market_prob":    round(max(yes_price, no_price) * 100, 1),
        "roi_pct":        roi_pct,
        "volume":         m.get("volume", 0),
        "liquidity":      m.get("liquidity", 0),
        "days_remaining": days,
        "is_smart_money": smart_money,
        "polymarket_url": _polymarket_url(slug),
    }


async def _fetch_markets(params: dict) -> list[dict]:
    """שולף שווקים מה-Gamma API."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{GAMMA_BASE}/markets", params=params)
        r.raise_for_status()
        return r.json()


async def _fetch_big_trades(min_amount: float = 1000) -> list[dict]:
    """
    שולף עסקאות גדולות מה-Data API — לזיהוי לוויתנים.
    min_amount: סכום מינימלי בדולרים לעסקה.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{DATA_BASE}/trades",
            params={"limit": 500, "order": "size", "ascending": "false"},
        )
        if r.status_code != 200:
            return []
        trades = r.json()
        return [t for t in trades if float(t.get("size", 0)) >= min_amount]


# ------------------------------------------------------------------ #
#  1. Sniper — הזדמנויות ROI גבוה                                      #
# ------------------------------------------------------------------ #

async def get_sniper_opportunities(limit: int = 50) -> list[dict]:
    """
    מחזיר שאלות עם:
    - ROI פוטנציאלי גבוה (מחיר נמוך = תשלום גבוה אם נכון)
    - זיהוי כסף חכם: עסקאות גדולות על אותה שאלה
    ממוינות לפי ROI פוטנציאלי מהגבוה לנמוך.
    """
    markets_task    = _fetch_markets({"active": "true", "closed": "false", "limit": 200})
    big_trades_task = _fetch_big_trades(min_amount=500)

    markets, big_trades = await asyncio.gather(markets_task, big_trades_task)

    # בנה מפה של condition_id → סכום כסף חכם
    smart_money_ids: set[str] = {t.get("conditionId", "") for t in big_trades}

    results = []
    for m in markets:
        tokens    = m.get("tokens", [])
        yes_price = float(next((t.get("price", 0.5) for t in tokens if t.get("outcome","").upper()=="YES"), 0.5))
        no_price  = float(next((t.get("price", 0.5) for t in tokens if t.get("outcome","").upper()=="NO"),  0.5))
        best      = min(yes_price, no_price)

        if best <= 0 or best >= 0.95:
            continue   # דלג על שאלות כמעט סגורות

        roi = ((1 - best) / best) * 100
        if roi < 10:
            continue   # רק ROI > 10%

        is_smart = m.get("conditionId", "") in smart_money_ids
        results.append(_format_market(m, smart_money=is_smart))

    # מיין לפי ROI
    results.sort(key=lambda x: x["roi_pct"], reverse=True)
    return results[:limit]


# ------------------------------------------------------------------ #
#  2. Safe Profit — הסתברות >80%                                       #
# ------------------------------------------------------------------ #

async def get_safe_profit_markets(min_prob: float = 80.0, limit: int = 50) -> list[dict]:
    """
    מחזיר שאלות שיש בהן תשובה עם הסתברות גבוהה (>min_prob%).
    אלו שאלות "בטוחות יחסית" — השוק מצביע על תוצאה ברורה.
    """
    markets = await _fetch_markets({"active": "true", "closed": "false", "limit": 300})

    results = []
    for m in markets:
        tokens    = m.get("tokens", [])
        yes_price = float(next((t.get("price", 0) for t in tokens if t.get("outcome","").upper()=="YES"), 0))
        no_price  = float(next((t.get("price", 0) for t in tokens if t.get("outcome","").upper()=="NO"),  0))

        prob = max(yes_price, no_price) * 100
        if prob < min_prob:
            continue

        fmt = _format_market(m)
        fmt["confidence_pct"] = round(prob, 1)
        results.append(fmt)

    results.sort(key=lambda x: x["market_prob"], reverse=True)
    return results[:limit]


# ------------------------------------------------------------------ #
#  3. Whales — לוויתנים                                                #
# ------------------------------------------------------------------ #

async def get_whale_markets(min_trade_size: float = 5000, limit: int = 50) -> list[dict]:
    """
    מחזיר שאלות שבהן זוהו השקעות גדולות של לוויתנים.
    """
    big_trades = await _fetch_big_trades(min_amount=min_trade_size)

    # קבץ לפי condition_id
    whale_by_market: dict[str, dict] = {}
    for t in big_trades:
        cid = t.get("conditionId", "")
        if not cid:
            continue
        if cid not in whale_by_market:
            whale_by_market[cid] = {
                "total_invested": 0,
                "biggest_trade":  0,
                "trader":         t.get("maker", ""),
                "side":           t.get("outcome", ""),
            }
        size = float(t.get("size", 0))
        whale_by_market[cid]["total_invested"] += size
        whale_by_market[cid]["biggest_trade"]   = max(whale_by_market[cid]["biggest_trade"], size)

    if not whale_by_market:
        return []

    # שלוף פרטי שאלות
    markets = await _fetch_markets({"active": "true", "closed": "false", "limit": 300})
    market_map = {m.get("conditionId", ""): m for m in markets}

    results = []
    for cid, whale_data in whale_by_market.items():
        m = market_map.get(cid)
        if not m:
            continue
        fmt = _format_market(m, smart_money=True)
        fmt["whale_total_invested"] = round(whale_data["total_invested"], 0)
        fmt["whale_biggest_trade"]  = round(whale_data["biggest_trade"], 0)
        fmt["whale_trader"]         = whale_data["trader"]
        fmt["whale_side"]           = whale_data["side"]
        results.append(fmt)

    results.sort(key=lambda x: x["whale_total_invested"], reverse=True)
    return results[:limit]
