"""
services/copy_engine.py — Real Copy Trading Engine
NO module-level model imports — everything lazy inside functions
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
        # ALL imports inside function — never at module level
        try:
            from db import SessionLocal
            from models.copy_settings import CopySetting
        except ImportError as e:
            logger.warning("DB/model not ready: %s", e)
            return

        db = SessionLocal()
        try:
            settings = db.query(CopySetting).filter(
                CopySetting.is_active == True
            ).all()

            if not settings:
                return

            traders = set(s.trader_address for s in settings)

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
            logger.info("🔔 %d new trades from %s", len(new_trades), addr[:10])

            relevant = [s for s in all_settings if s.trader_address == addr]
            for trade in new_trades:
                for setting in relevant:
                    await self._mirror(client, db, trade, setting)

        except Exception as e:
            logger.warning("Error checking %s: %s", addr[:10], e)

    async def _mirror(self, client, db, trade: dict, setting):
        try:
            condition_id = trade.get("conditionId", "")
            token_id     = trade.get("asset", "")
            side         = (trade.get("side") or "").upper()
            trader_size  = float(trade.get("usdcSize") or trade.get("size") or 0)
            price        = float(trade.get("price") or 0.5)
            market_title = trade.get("title") or trade.get("?title") or ""

            if not condition_id or side not in ("BUY", "SELL") or trader_size <= 0:
                return

            copy_size = self._calc_size(trader_size, setting)
            if copy_size < 1.0:
                return

            if setting.is_demo:
                await self._save_demo(db, trade, setting,
                                      copy_size, price, market_title)
                return

            # Real trade via CLOB
            try:
                from models.wallet import Wallet
                from services.clob import CLOBClient

                wallet = db.query(Wallet).filter(
                    Wallet.id == setting.wallet_id,
                    Wallet.is_active == True
                ).first()
                if not wallet:
                    return

                clob   = CLOBClient(wallet)
                result = await clob.place_order(
                    token_id, side, copy_size, price, condition_id
                )
                await self._save_real(db, trade, setting,
                                      copy_size, price, market_title, result)
            except ImportError as e:
                logger.warning("CLOB not available: %s", e)

        except Exception as e:
            logger.error("Mirror error: %s", e)

    def _calc_size(self, trader_size: float, setting) -> float:
        mode = getattr(setting, "copy_mode", "fixed")
        if mode == "percent":
            pct = float(getattr(setting, "copy_percent", 10) or 10) / 100
            return trader_size * pct
        elif mode == "mirror":
            ratio = float(getattr(setting, "mirror_ratio", 1) or 1)
            return trader_size * ratio
        # default: fixed
        return float(getattr(setting, "fixed_amount", 10) or 10)

    async def _save_demo(self, db, trade, setting,
                         size, price, title):
        try:
            from models.copy_trade import CopyTrade
            db.add(CopyTrade(
                copy_setting_id=setting.id,
                user_id=setting.user_id,
                trader_address=setting.trader_address,
                condition_id=trade.get("conditionId", ""),
                side=(trade.get("side") or "").upper(),
                size=size, price=price,
                status="demo", is_demo=True,
                trader_tx=trade.get("transactionHash", ""),
                market_title=title,
            ))
            db.commit()
            logger.info("📝 Demo: %s $%.2f | %s",
                        (trade.get("side") or "").upper(), size, title[:30])
        except Exception as e:
            logger.error("Save demo error: %s", e)
            db.rollback()

    async def _save_real(self, db, trade, setting,
                         size, price, title, result):
        try:
            from models.copy_trade import CopyTrade
            status = "executed" if result.get("success") else "failed"
            db.add(CopyTrade(
                copy_setting_id=setting.id,
                user_id=setting.user_id,
                trader_address=setting.trader_address,
                condition_id=trade.get("conditionId", ""),
                side=(trade.get("side") or "").upper(),
                size=size, price=price,
                status=status, is_demo=False,
                trader_tx=trade.get("transactionHash", ""),
                our_tx=result.get("transaction_hash", ""),
                market_title=title,
                error_msg=result.get("error") if not result.get("success") else None,
            ))
            db.commit()
            if result.get("success"):
                logger.info("✅ Real copy: %s $%.2f", status, size)
            else:
                logger.warning("❌ Failed: %s", result.get("error"))
        except Exception as e:
            logger.error("Save real error: %s", e)
            db.rollback()


copy_engine = CopyEngine()
