"""
copy_engine.py — Real Copy Trading Engine
=========================================
Polls Polymarket activity for watched traders,
detects new trades, and mirrors them to user wallets.

Flow:
1. Every 10 seconds: fetch /activity?user={trader_addr}&limit=20
2. Compare with last seen → find NEW trades
3. For each new trade: calculate copy size per user settings
4. Execute trade via Polymarket CLOB API
5. Record in DB
"""

import asyncio
import httpx
import json
import time
import logging
from datetime import datetime
from typing import Optional
from db import SessionLocal, engine
from models import CopySetting, Wallet, Trade, CopyTrade
from services.clob import CLOBClient

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
POLL_INTERVAL = 10  # seconds

class CopyEngine:
    def __init__(self):
        self.running = False
        self._task: Optional[asyncio.Task] = None
        # Track last seen trade timestamp per trader
        self._last_seen: dict[str, int] = {}

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Copy engine started")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("Copy engine stopped")

    async def _loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Copy engine tick error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self):
        """One polling cycle."""
        db = SessionLocal()
        try:
            # Get all active copy settings
            settings = db.query(CopySetting).filter(
                CopySetting.is_active == True
            ).all()

            if not settings:
                return

            # Get unique trader addresses to watch
            traders = set(s.trader_address for s in settings)

            async with httpx.AsyncClient(timeout=15) as client:
                for trader_addr in traders:
                    await self._check_trader(client, db, trader_addr, settings)
        finally:
            db.close()

    async def _check_trader(self, client, db, trader_addr: str, all_settings):
        """Check a specific trader for new activity."""
        try:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": trader_addr, "limit": 20},
                headers={"Accept": "application/json"}
            )
            if not r.is_success:
                return

            trades = r.json()
            if not trades or not isinstance(trades, list):
                return

            last_seen = self._last_seen.get(trader_addr, 0)
            new_trades = []

            for trade in trades:
                ts = int(trade.get("timestamp", 0))
                if ts > last_seen:
                    new_trades.append(trade)

            if not new_trades:
                return

            # Update last seen
            self._last_seen[trader_addr] = max(
                int(t.get("timestamp", 0)) for t in new_trades
            )

            logger.info(f"Found {len(new_trades)} new trades from {trader_addr[:10]}...")

            # Find copy settings that watch this trader
            relevant = [s for s in all_settings if s.trader_address == trader_addr]

            for trade in new_trades:
                for setting in relevant:
                    await self._mirror_trade(client, db, trade, setting)

        except Exception as e:
            logger.warning(f"Error checking trader {trader_addr[:10]}: {e}")

    async def _mirror_trade(self, client, db, trade: dict, setting):
        """Mirror a single trade according to copy settings."""
        try:
            # Get user's wallet
            db_temp = SessionLocal()
            wallet = db_temp.query(Wallet).filter(
                Wallet.id == setting.wallet_id,
                Wallet.is_active == True
            ).first()
            db_temp.close()

            if not wallet or not wallet.private_key_encrypted:
                return

            # Parse trade details
            condition_id = trade.get("conditionId", "")
            side = trade.get("side", "").upper()  # BUY or SELL
            token_id = trade.get("asset", "")
            trader_size = float(trade.get("usdcSize", trade.get("size", 0)))
            trader_price = float(trade.get("price", 0.5))

            if not condition_id or not side or trader_size <= 0:
                return

            # Calculate copy size
            copy_size = self._calc_size(trader_size, trader_price, setting)
            if copy_size < 1.0:  # Min $1
                logger.info(f"Copy size ${copy_size:.2f} too small, skipping")
                return

            # Check demo mode
            if setting.is_demo:
                await self._record_demo_trade(db, trade, setting, copy_size)
                return

            # Execute real trade via CLOB
            clob = CLOBClient(wallet)
            result = await clob.place_order(
                token_id=token_id,
                side=side,
                size=copy_size,
                price=trader_price,
                condition_id=condition_id
            )

            if result.get("success"):
                logger.info(f"✅ Copy trade executed: {side} ${copy_size:.2f} on {condition_id[:10]}")
                await self._record_trade(db, trade, setting, copy_size, result)
            else:
                logger.warning(f"❌ Copy trade failed: {result.get('error')}")

        except Exception as e:
            logger.error(f"Mirror trade error: {e}")

    def _calc_size(self, trader_size: float, price: float, setting) -> float:
        """Calculate copy trade size based on settings."""
        mode = setting.copy_mode  # 'mirror', 'fixed', 'percent'

        if mode == "fixed":
            return float(setting.fixed_amount or 10)

        elif mode == "percent":
            # % of trader's trade
            pct = float(setting.copy_percent or 10) / 100
            return trader_size * pct

        elif mode == "mirror":
            # Mirror exact ratio (trader_size / trader_portfolio * our_portfolio)
            ratio = float(setting.mirror_ratio or 1.0)
            return trader_size * ratio

        return min(float(setting.max_trade or 50), trader_size)

    async def _record_demo_trade(self, db, trade: dict, setting, size: float):
        """Record a simulated copy trade for demo mode."""
        try:
            demo_trade = CopyTrade(
                copy_setting_id=setting.id,
                user_id=setting.user_id,
                trader_address=setting.trader_address,
                condition_id=trade.get("conditionId", ""),
                side=trade.get("side", "").upper(),
                size=size,
                price=float(trade.get("price", 0)),
                status="demo",
                is_demo=True,
                trader_tx=trade.get("transactionHash", ""),
                market_title=trade.get("title", ""),
                created_at=datetime.utcnow()
            )
            db.add(demo_trade)
            db.commit()
            logger.info(f"📝 Demo trade recorded: {size:.2f} USDC")
        except Exception as e:
            logger.error(f"Demo record error: {e}")
            db.rollback()

    async def _record_trade(self, db, trade: dict, setting, size: float, result: dict):
        """Record a real executed copy trade."""
        try:
            copy_trade = CopyTrade(
                copy_setting_id=setting.id,
                user_id=setting.user_id,
                trader_address=setting.trader_address,
                condition_id=trade.get("conditionId", ""),
                side=trade.get("side", "").upper(),
                size=size,
                price=float(trade.get("price", 0)),
                status="executed",
                is_demo=False,
                trader_tx=trade.get("transactionHash", ""),
                our_tx=result.get("transaction_hash", ""),
                market_title=trade.get("title", ""),
                created_at=datetime.utcnow()
            )
            db.add(copy_trade)
            db.commit()
        except Exception as e:
            logger.error(f"Trade record error: {e}")
            db.rollback()

copy_engine = CopyEngine()
