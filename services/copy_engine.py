"""
services/copy_engine.py — Real Copy Trading Engine
===================================================
Polls Polymarket /activity for watched traders,
detects new trades, and mirrors them.
"""

import asyncio
import httpx
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_API  = "https://data-api.polymarket.com"
POLL_SECS = 10

class CopyEngine:
    def __init__(self):
        self.running   = False
        self._task     = None
        self._last_ts  = {}   # trader_addr → last seen timestamp

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("✅ Copy engine started (polling every %ds)", POLL_SECS)

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("Copy engine stopped")

    # ── Main loop ─────────────────────────────────────────────────

    async def _loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Tick error: %s", e)
            await asyncio.sleep(POLL_SECS)

    async def _tick(self):
        # Import DB lazily to avoid startup import errors
        try:
            from db import SessionLocal
        except ImportError:
            return

        db = SessionLocal()
        try:
            # Get active copy settings
            try:
                from models.copy_settings import CopySetting
            except ImportError:
                return  # Model not ready yet

            settings = db.query(CopySetting).filter(
                CopySetting.is_active == True
            ).all()

            if not settings:
                return

            traders = set(s.trader_address for s in settings)

            async with httpx.AsyncClient(timeout=15) as client:
                for addr in traders:
                    await self._check_trader(client, db, addr, settings)
        finally:
            db.close()

    # ── Per-trader check ──────────────────────────────────────────

    async def _check_trader(self, client, db, addr: str, all_settings):
        try:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": addr, "limit": 20},
                headers={"Accept": "application/json"}
            )
            if not r.is_success:
                return

            trades = r.json()
            if not trades or not isinstance(trades, list):
                return

            last_ts   = self._last_ts.get(addr, 0)
            new_trades = [t for t in trades if int(t.get("timestamp", 0)) > last_ts]

            if not new_trades:
                return

            # Update watermark
            self._last_ts[addr] = max(int(t.get("timestamp", 0)) for t in new_trades)
            logger.info("🔔 %d new trades from %s…", len(new_trades), addr[:10])

            relevant = [s for s in all_settings if s.trader_address == addr]
            for trade in new_trades:
                for setting in relevant:
                    await self._mirror(client, db, trade, setting)

        except Exception as e:
            logger.warning("Error checking %s: %s", addr[:10], e)

    # ── Mirror one trade ─────────────────────────────────────────

    async def _mirror(self, client, db, trade: dict, setting):
        try:
            # Parse trade
            condition_id = trade.get("conditionId", "")
            token_id     = trade.get("asset", "")
            side         = (trade.get("side") or "").upper()   # BUY / SELL
            trader_size  = float(trade.get("usdcSize") or trade.get("size") or 0)
            price        = float(trade.get("price") or 0.5)
            market_title = trade.get("title") or trade.get("?title") or ""

            if not condition_id or side not in ("BUY", "SELL") or trader_size <= 0:
                return

            copy_size = self._calc_size(trader_size, setting)
            if copy_size < 1.0:
                return

            # ── DEMO MODE ───────────────────────────────────────
            if setting.is_demo:
                await self._save_trade(db, {
                    "copy_setting_id": setting.id,
                    "user_id":         setting.user_id,
                    "trader_address":  setting.trader_address,
                    "condition_id":    condition_id,
                    "side":            side,
                    "size":            copy_size,
                    "price":           price,
                    "status":          "demo",
                    "is_demo":         True,
                    "trader_tx":       trade.get("transactionHash", ""),
                    "market_title":    market_title,
                })
                logger.info("📝 Demo: %s $%.2f @ %.3f | %s", side, copy_size, price, market_title[:30])
                return

            # ── REAL MODE ────────────────────────────────────────
            try:
                from models.wallet import Wallet
                from services.security import decrypt_key
                wallet = db.query(Wallet).filter(
                    Wallet.id == setting.wallet_id,
                    Wallet.is_active == True
                ).first()
                if not wallet:
                    return

                from services.clob import CLOBClient
                clob   = CLOBClient(wallet)
                result = await clob.place_order(token_id, side, copy_size, price, condition_id)

                status = "executed" if result.get("success") else "failed"
                await self._save_trade(db, {
                    "copy_setting_id": setting.id,
                    "user_id":         setting.user_id,
                    "trader_address":  setting.trader_address,
                    "condition_id":    condition_id,
                    "side":            side,
                    "size":            copy_size,
                    "price":           price,
                    "status":          status,
                    "is_demo":         False,
                    "trader_tx":       trade.get("transactionHash", ""),
                    "our_tx":          result.get("transaction_hash", ""),
                    "market_title":    market_title,
                    "error_msg":       result.get("error") if not result.get("success") else None,
                })
                if result.get("success"):
                    logger.info("✅ Real copy: %s $%.2f | %s", side, copy_size, market_title[:30])
                else:
                    logger.warning("❌ Copy failed: %s", result.get("error"))

            except ImportError as e:
                logger.warning("Missing module for real trading: %s", e)

        except Exception as e:
            logger.error("Mirror error: %s", e)

    # ── Helpers ──────────────────────────────────────────────────

    def _calc_size(self, trader_size: float, setting) -> float:
        mode = getattr(setting, "copy_mode", "fixed")
        if mode == "fixed":
            return float(getattr(setting, "fixed_amount", 10) or 10)
        elif mode == "percent":
            pct = float(getattr(setting, "copy_percent", 10) or 10) / 100
            return trader_size * pct
        elif mode == "mirror":
            ratio = float(getattr(setting, "mirror_ratio", 1) or 1)
            return trader_size * ratio
        return min(float(getattr(setting, "max_trade", 50) or 50), trader_size)

    async def _save_trade(self, db, data: dict):
        try:
            from models.copy_trade import CopyTrade
        except ImportError:
            return  # Table not created yet
        try:
            trade = CopyTrade(**data)
            db.add(trade)
            db.commit()
        except Exception as e:
            logger.error("Save trade error: %s", e)
            db.rollback()

copy_engine = CopyEngine()
