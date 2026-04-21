"""
services/market_scanner.py
--------------------------
חיפוש וסינון שאלות מפולימארקט לפי:
- נזילות (min/max)
- טווח מחירים (yes_price)
- נושא / קטגוריה
- זמן שנותר (ימים)
- כמות כסף בשאלה (volume)
"""

import httpx
from datetime import datetime, timezone
from typing import Optional

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _days_remaining(end_str: Optional[str]) -> Optional[int]:
    if not end_str:
        return None
    try:
        end   = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        delta = end - datetime.now(timezone.utc)
        return max(0, delta.days)
    except Exception:
        return None


async def scan_markets(
    min_liquidity:    Optional[float] = None,
    max_liquidity:    Optional[float] = None,
    min_volume:       Optional[float] = None,
    max_volume:       Optional[float] = None,
    min_price:        Optional[float] = None,   # מחיר YES
    max_price:        Optional[float] = None,
    category:         Optional[str]   = None,
    min_days:         Optional[int]   = None,
    max_days:         Optional[int]   = None,
    query:            Optional[str]   = None,   # חיפוש חופשי בטקסט השאלה
    limit:            int             = 100,
) -> list[dict]:
    """
    מחזיר רשימת שאלות לפי פילטרים.
    כל שאלה מוחזרת עם נתונים לתצוגת שורה.
    """
    params: dict = {"active": "true", "closed": "false", "limit": 500}
    if category:
        params["tag"] = category

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{GAMMA_BASE}/markets", params=params)
        r.raise_for_status()
        markets = r.json()

    results = []
    for m in markets:
        tokens    = m.get("tokens", [])
        yes_price = float(next((t.get("price", 0) for t in tokens if t.get("outcome","").upper()=="YES"), 0))
        no_price  = float(next((t.get("price", 0) for t in tokens if t.get("outcome","").upper()=="NO"),  0))
        liquidity = float(m.get("liquidity", 0))
        volume    = float(m.get("volume",    0))
        slug      = m.get("slug", "")
        question  = m.get("question", "")
        days      = _days_remaining(m.get("endDate") or m.get("end_date_iso"))

        # ---- פילטרים ----
        if min_liquidity  and liquidity < min_liquidity:  continue
        if max_liquidity  and liquidity > max_liquidity:  continue
        if min_volume     and volume    < min_volume:     continue
        if max_volume     and volume    > max_volume:     continue
        if min_price      and yes_price < min_price:      continue
        if max_price      and yes_price > max_price:      continue
        if min_days       and (days is None or days < min_days): continue
        if max_days       and (days is None or days > max_days): continue
        if query          and query.lower() not in question.lower(): continue

        # ---- פורמט ----
        results.append({
            "condition_id":   m.get("conditionId", ""),
            "slug":           slug,
            "question":       question,
            "category":       (m.get("tags") or [None])[0],
            "yes_price":      round(yes_price, 3),
            "no_price":       round(no_price,  3),
            "yes_prob":       round(yes_price * 100, 1),
            "liquidity":      round(liquidity, 0),
            "volume":         round(volume,    0),
            "days_remaining": days,
            "polymarket_url": f"https://polymarket.com/event/{slug}",
        })

    # מיון לפי נזילות (ברירת מחדל)
    results.sort(key=lambda x: x["liquidity"], reverse=True)
    return results[:limit]
