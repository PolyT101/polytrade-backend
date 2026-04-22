"""
routers/markets.py
------------------
Proxy endpoints for Polymarket APIs — solves CORS issue.
All frontend calls go through our Railway backend.
"""

from fastapi import APIRouter, Query, HTTPException
import httpx

router = APIRouter()

GAMMA = "https://gamma-api.polymarket.com"
DATA  = "https://data-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PolyTrade/1.0)",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/"
}

async def pm_get(url: str, params: dict = None):
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=HEADERS, params=params or {})
        r.raise_for_status()
        return r.json()

# ── Markets ──────────────────────────────────────────────────────
@router.get("/markets")
async def get_markets(
    limit:      int  = Query(100, ge=1, le=500),
    offset:     int  = Query(0,   ge=0),
    active:     bool = Query(True),
    order:      str  = Query("volume"),
    ascending:  bool = Query(False),
    category:   str  = Query(None),
):
    params = {
        "limit": limit, "offset": offset,
        "active": str(active).lower(), "closed": "false",
        "order": order, "ascending": str(ascending).lower(),
    }
    if category:
        params["category"] = category
    try:
        data = await pm_get(f"{GAMMA}/markets", params)
        markets = data.get("markets", data) if isinstance(data, dict) else data
        return {"markets": markets, "count": len(markets)}
    except Exception as e:
        raise HTTPException(502, f"Polymarket unavailable: {e}")

# ── Market by slug ────────────────────────────────────────────────
@router.get("/markets/{slug}")
async def get_market(slug: str):
    try:
        data = await pm_get(f"{GAMMA}/markets", {"slug": slug})
        markets = data.get("markets", data) if isinstance(data, dict) else data
        if markets:
            return markets[0]
        raise HTTPException(404, "Market not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))

# ── Leaderboard ───────────────────────────────────────────────────
@router.get("/leaderboard")
async def get_leaderboard(
    limit:  int = Query(50,  ge=1, le=200),
    offset: int = Query(0,   ge=0),
    order:  str = Query("pnl"),
):
    endpoints = [
        (f"{DATA}/leaderboard",  {"limit": limit, "offset": offset, "order": order, "ascending": "false"}),
        (f"{DATA}/profiles",     {"limit": limit, "offset": offset, "order": order, "ascending": "false"}),
        (f"{GAMMA}/leaderboard", {"limit": limit, "offset": offset}),
    ]
    last_err = None
    for url, params in endpoints:
        try:
            data = await pm_get(url, params)
            arr = data if isinstance(data, list) else \
                  data.get("data") or data.get("profiles") or \
                  data.get("leaderboard") or data.get("results") or []
            if arr:
                return {"traders": arr, "count": len(arr)}
        except Exception as e:
            last_err = e
            continue
    raise HTTPException(502, f"Leaderboard unavailable: {last_err}")

# ── Trader profile ────────────────────────────────────────────────
@router.get("/trader/{address}")
async def get_trader(address: str):
    try:
        data = await pm_get(f"{DATA}/profiles/{address}")
        return data
    except Exception as e:
        raise HTTPException(502, f"Trader not found: {e}")

# ── Trader positions ──────────────────────────────────────────────
@router.get("/trader/{address}/positions")
async def get_trader_positions(
    address: str,
    limit:   int  = Query(50,  ge=1, le=200),
    closed:  bool = Query(False),
):
    try:
        data = await pm_get(f"{DATA}/positions", {
            "user": address, "limit": limit,
            "closed": str(closed).lower()
        })
        return data if isinstance(data, list) else data.get("positions", [])
    except Exception as e:
        raise HTTPException(502, str(e))

# ── Smart money / large trades ────────────────────────────────────
@router.get("/smart-money")
async def get_smart_money(
    limit:     int = Query(50, ge=1, le=200),
    min_amount: int = Query(10000),
):
    try:
        data = await pm_get(f"{DATA}/activity", {
            "limit": limit, "minAmount": min_amount
        })
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        # Fallback: return top markets as "smart money"
        try:
            mdata = await pm_get(f"{GAMMA}/markets", {
                "limit": limit, "active": "true",
                "closed": "false", "order": "volume", "ascending": "false"
            })
            markets = mdata.get("markets", mdata) if isinstance(mdata, dict) else mdata
            return [{"market": m.get("question",""), "volume": m.get("volume",0),
                     "type": "whale" if float(m.get("volume",0)) > 500000 else "smart",
                     "slug": m.get("slug",""), "id": m.get("id","")} for m in markets]
        except:
            raise HTTPException(502, str(e))
