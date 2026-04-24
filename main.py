from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from services.security import check_rate_limit
from routers import (
    traders, copy, portfolio, wallets,
    watchlist, market_watchlist,
    analyst, scanner, smart_money,
    dashboard, auth,
)
from routers import markets

app = FastAPI(title="Polymarket CopyTrade API", version="3.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host
    try:
        check_rate_limit(key=client_ip, max_requests=120, window_seconds=60)
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=429, content={"detail": str(e)})
    return await call_next(request)

# ── Routers ──────────────────────────────────────────────────────
app.include_router(auth.router,             prefix="/api/auth",             tags=["Auth"])
app.include_router(markets.router,          prefix="/api/pm",               tags=["Polymarket Proxy"])
app.include_router(dashboard.router,        prefix="/api/dashboard",        tags=["Dashboard"])
app.include_router(traders.router,          prefix="/api/traders",          tags=["Traders"])
app.include_router(copy.router,             prefix="/api/copy",             tags=["Copy Trading"])
app.include_router(portfolio.router,        prefix="/api/portfolio",        tags=["Portfolio"])
app.include_router(wallets.router,          prefix="/api/wallets",          tags=["Wallets"])
app.include_router(watchlist.router,        prefix="/api/watchlist",        tags=["Watchlist - Traders"])
app.include_router(market_watchlist.router, prefix="/api/market-watchlist", tags=["Watchlist - Markets"])
app.include_router(analyst.router,          prefix="/api/analyst",          tags=["Market Analyst"])
app.include_router(scanner.router,          prefix="/api/scanner",          tags=["Market Scanner"])
app.include_router(smart_money.router,      prefix="/api/smart-money",      tags=["Smart Money"])

@app.on_event("startup")
async def startup():
    from db import create_tables
    create_tables()
    from services.copy_engine import copy_engine
    await copy_engine.start()

@app.on_event("shutdown")
async def shutdown():
    from services.copy_engine import copy_engine
    await copy_engine.stop()

@app.get("/")
def root():
    return {"status": "ok", "version": "3.2.1"}
