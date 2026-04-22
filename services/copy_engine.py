"""
services/copy_engine.py — Real Copy Trading Engine
מותאם למבנה המודלים הקיים:
  CopySettings (לא CopySetting)
  CopyTrade עם שדות: user_id, copy_settings_id, trader_address,
                      market_id, market_question, side, amount_usdc,
                      price_entry, status, tx_hash
"""
import asyncio
import httpx
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DATA_API  = "https://data-api.polymarket.com"
POLL_SECS = 10


class CopyEngine:
    def __init__(self):
        self.running  = False
        self._task    = None
        self._last_ts = {}  # trader_addr → last seen timestamp

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("✅ Copy engine started (poll every %ds)", POLL_SECS)

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Tick error: %s", e)
            await asyncio.sleep(POLL_SECS)

    async def _tick(self):
        try:
            from db import SessionLocal
            from models.copy_settings import CopySettings  # ← שם נכון
        except ImportError as e:
            logger.warning("DB/model not ready: %s", e)
            return

        db = SessionLocal()
        try:
            settings = db.query(CopySettings).filter(
                CopySettings.is_active == True
            ).all()

            if not settings:
                return

            traders = set(s.trader_address for s in settings)
            logger.info("👁 Watching %d traders", len(traders))

            async with httpx.AsyncClient(timeout=15) as client:
                for addr in traders:
                    await self._check_trader(client, db, addr, settings)
        except Exception as e:
            logger.error("Tick DB error: %s", e)
        finally:
            db.close()

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

            last_ts    = self._last_ts.get(addr, 0)
            new_trades = [t for t in trades
                          if int(t.get("timestamp", 0)) > last_ts]

            if not new_trades:
                return

            self._last_ts[addr] = max(
                int(t.get("timestamp", 0)) for t in new_trades
            )
            logger.info("🔔 %d new trades from %s…", len(new_trades), addr[:10])

            relevant = [s for s in all_settings if s.trader_address == addr]
            for trade in new_trades:
                for setting in relevant:
                    await self._mirror(client, db, trade, setting)

        except Exception as e:
            logger.warning("Error checking %s: %s", addr[:10], e)

    async def _mirror(self, client, db, trade: dict, setting):
        try:
            # Parse trade from Polymarket activity endpoint
            condition_id  = trade.get("conditionId", "")
            token_id      = trade.get("asset", "")
            side_raw      = (trade.get("side") or "").upper()  # BUY/SELL
            trader_size   = float(trade.get("usdcSize") or trade.get("size") or 0)
            price         = float(trade.get("price") or 0.5)
            market_title  = trade.get("title") or trade.get("?title") or trade.get("market", "")
            outcome_index = int(trade.get("outcomeIndex", 0))

            # Convert BUY/SELL + outcomeIndex → YES/NO
            # outcomeIndex 0 = YES, 1 = NO (Polymarket convention)
            if side_raw == "BUY":
                side = "YES" if outcome_index == 0 else "NO"
            else:
                side = "NO" if outcome_index == 0 else "YES"

            if not condition_id or trader_size <= 0:
                return

            # Calculate copy size based on settings
            copy_size = self._calc_size(trader_size, setting)
            if copy_size < 1.0:
                logger.info("Copy size $%.2f too small, skip", copy_size)
                return

            logger.info("📋 Mirror: %s $%.2f on %s [demo=%s]",
                        side, copy_size, market_title[:40], setting.is_active)

            # Save as demo trade (always first)
            await self._save_trade(db, {
                "user_id":          setting.user_id,
                "copy_settings_id": setting.id,
                "trader_address":   setting.trader_address,
                "market_id":        condition_id,
                "market_question":  market_title,
                "side":             side,
                "amount_usdc":      copy_size,
                "price_entry":      price,
                "status":           "demo",
                "tx_hash":          trade.get("transactionHash", ""),
            })

            # TODO: Real execution via CLOB when wallet key is configured
            # Uncomment when ready:
            # wallet_addr = setting.wallet_address
            # wallet_key  = setting.encrypted_wallet_key
            # if wallet_key and not setting_is_demo(setting):
            #     from services.clob import CLOBClient
            #     clob = CLOBClient(wallet_addr, wallet_key)
            #     result = await clob.place_order(token_id, side, copy_size, price)

        except Exception as e:
            logger.error("Mirror error: %s", e)

    def _calc_size(self, trader_size: float, setting) -> float:
        """Calculate copy trade size based on CopySettings fields."""
        mode = getattr(setting, "entry_mode", "fixed")
        if mode == "percent":
            pct = float(getattr(setting, "entry_amount", 10) or 10) / 100
            return trader_size * pct
        # default: fixed
        amount = float(getattr(setting, "entry_amount", 10) or 10)
        # Respect max_daily_loss as max per trade if set
        max_trade = getattr(setting, "max_daily_loss_usd", None)
        if max_trade:
            amount = min(amount, float(max_trade))
        return amount

    async def _save_trade(self, db, data: dict):
        try:
            from models.copy_settings import CopyTrade
            trade = CopyTrade(**data)
            db.add(trade)
            db.commit()
            logger.info("💾 Saved trade: %s $%.2f [%s]",
                        data.get("side"), data.get("amount_usdc", 0),
                        data.get("status"))
        except Exception as e:
            logger.error("Save trade error: %s", e)
            db.rollback()


copy_engine = CopyEngine()
