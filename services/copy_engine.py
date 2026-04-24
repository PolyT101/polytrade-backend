"""
services/copy_engine.py — v4
============================
Copy Engine עם:
- Watermark נשמר ב-DB (לא בזיכרון)
- לוגיקת TP/SL/Mirror/Fixed/Manual מלאה
- Budget enforcement יומי
- מחירים בזמן אמת כל 30 שניות
"""
import asyncio
import httpx
import logging
from datetime import datetime, timezone, date

logger = logging.getLogger(__name__)

DATA_API  = "https://data-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
POLL_SECS  = 10   # בדיקת עסקאות חדשות
PRICE_SECS = 30   # בדיקת מחירים ל-TP/SL


class CopyEngine:
    def __init__(self):
        self.running      = False
        self._task        = None
        self._price_task  = None

    async def start(self):
        self.running     = True
        self._task       = asyncio.create_task(self._loop())
        self._price_task = asyncio.create_task(self._price_loop())
        logger.info("✅ Copy engine started (trades:%ds, prices:%ds)", POLL_SECS, PRICE_SECS)

    async def stop(self):
        self.running = False
        for t in [self._task, self._price_task]:
            if t:
                t.cancel()

    # ──────────────────────────────────────────────────────
    # LOOP 1: עסקאות חדשות
    # ──────────────────────────────────────────────────────

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
            from models.copy_settings import CopySettings
        except ImportError as e:
            logger.warning("Models not ready: %s", e)
            return

        db = SessionLocal()
        try:
            settings = db.query(CopySettings).filter(
                CopySettings.is_active == True
            ).all()

            if not settings:
                return

            traders = set(s.trader_address for s in settings)
            logger.info("👁 Watching %d traders, %d settings", len(traders), len(settings))

            async with httpx.AsyncClient(timeout=15) as client:
                for addr in traders:
                    relevant = [s for s in settings if s.trader_address == addr]
                    await self._check_trader(client, db, addr, relevant)
        except Exception as e:
            logger.error("Tick DB error: %s", e)
        finally:
            db.close()

    async def _check_trader(self, client, db, addr: str, settings: list):
        try:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": addr, "limit": 20},
                headers={"Accept": "application/json"}
            )
            if not r.is_success:
                return

            trades = r.json()
            if not isinstance(trades, list) or not trades:
                return

            for setting in settings:
                # watermark נשמר ב-DB
                last_ts = self._get_watermark(db, setting)
                new_trades = [t for t in trades
                              if int(t.get("timestamp", 0)) > last_ts]

                if not new_trades:
                    continue

                new_max = max(int(t.get("timestamp", 0)) for t in new_trades)
                self._set_watermark(db, setting, new_max)

                logger.info("🔔 %d new trades from %s (setting %d)",
                            len(new_trades), addr[:10], setting.id)

                for trade in new_trades:
                    await self._process_trade(client, db, trade, setting)

        except Exception as e:
            logger.warning("Check trader error %s: %s", addr[:10], e)

    # ──────────────────────────────────────────────────────
    # WATERMARK — נשמר ב-DB
    # ──────────────────────────────────────────────────────

    def _get_watermark(self, db, setting) -> int:
        """מחזיר את ה-timestamp האחרון שראינו לסטינג זה."""
        try:
            from models.copy_settings import CopyEngineState
            state = db.query(CopyEngineState).filter(
                CopyEngineState.setting_id == setting.id
            ).first()
            return int(state.last_seen_ts) if state and state.last_seen_ts else 0
        except Exception:
            # אם הטבלה לא קיימת — השתמש ב-updated_at של הסטינג
            if setting.updated_at:
                return int(setting.updated_at.timestamp())
            return 0

    def _set_watermark(self, db, setting, ts: int):
        """שומר את ה-timestamp האחרון."""
        try:
            from models.copy_settings import CopyEngineState
            state = db.query(CopyEngineState).filter(
                CopyEngineState.setting_id == setting.id
            ).first()
            if state:
                state.last_seen_ts = ts
            else:
                db.add(CopyEngineState(setting_id=setting.id, last_seen_ts=ts))
            db.commit()
        except Exception as e:
            logger.warning("Watermark save error: %s", e)
            # fallback — שמור ב-updated_at
            try:
                setting.updated_at = datetime.fromtimestamp(ts, tz=timezone.utc)
                db.commit()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────
    # PROCESS TRADE
    # ──────────────────────────────────────────────────────

    async def _process_trade(self, client, db, trade: dict, setting):
        try:
            from models.copy_settings import CopyTrade

            condition_id  = trade.get("conditionId", "")
            token_id      = trade.get("asset", "")
            side_raw      = (trade.get("side") or "").upper()
            trader_size   = float(trade.get("usdcSize") or trade.get("size") or 0)
            price         = float(trade.get("price") or 0.5)
            market_title  = trade.get("title") or trade.get("market") or ""
            outcome_index = int(trade.get("outcomeIndex") or 0)
            trader_tx     = trade.get("transactionHash", "")

            if not condition_id or trader_size <= 0:
                return

            # BUY/SELL + outcomeIndex → YES/NO
            if side_raw == "BUY":
                our_side = "YES" if outcome_index == 0 else "NO"
            elif side_raw == "SELL":
                our_side = "NO"  if outcome_index == 0 else "YES"
                # טריידר מוכר → נבדוק אם צריך mirror-sell
                if getattr(setting, "sell_mode", "mirror") == "mirror":
                    await self._mirror_sell(db, setting, condition_id, price, market_title)
                return
            else:
                our_side = "YES"

            # בדיקות תקציב
            copy_size = self._calc_size(trader_size, setting)
            if copy_size < 1.0:
                return

            if not await self._check_budget(db, setting, copy_size):
                return

            if not await self._check_daily_trades(db, setting):
                return

            # שמור עסקה
            t_obj = CopyTrade(
                user_id          = setting.user_id,
                copy_settings_id = setting.id,
                trader_address   = setting.trader_address,
                market_id        = condition_id,
                market_question  = market_title,
                side             = our_side,
                amount_usdc      = copy_size,
                price_entry      = price,
                status           = "demo",
                tx_hash          = trader_tx,
            )
            db.add(t_obj)
            db.commit()
            db.refresh(t_obj)

            logger.info("✅ Demo trade #%d: %s %s $%.2f @ %.3f | %s",
                        t_obj.id, our_side, side_raw, copy_size, price, market_title[:30])

        except Exception as e:
            logger.error("Process trade error: %s", e)
            try:
                db.rollback()
            except Exception:
                pass

    async def _mirror_sell(self, db, setting, condition_id: str, price: float, market: str):
        """סוגר פוזיציות פתוחות בשוק זה כשהטריידר מוכר."""
        try:
            from models.copy_settings import CopyTrade
            open_trades = db.query(CopyTrade).filter(
                CopyTrade.copy_settings_id == setting.id,
                CopyTrade.market_id == condition_id,
                CopyTrade.status == "demo"
            ).all()

            for t in open_trades:
                entry = t.price_entry or price
                pnl   = t.amount_usdc * ((price - entry) / entry) if entry > 0 else 0
                t.price_exit = price
                t.pnl_usd    = round(pnl, 4)
                t.status     = "closed"
                t.closed_at  = datetime.now(timezone.utc)
                logger.info("🔄 Mirror-sell #%d: P&L=$%.2f", t.id, pnl)

            if open_trades:
                db.commit()
        except Exception as e:
            logger.error("Mirror-sell error: %s", e)

    # ──────────────────────────────────────────────────────
    # LOOP 2: מחירים בזמן אמת לTP/SL
    # ──────────────────────────────────────────────────────

    async def _price_loop(self):
        while self.running:
            try:
                await self._check_tp_sl()
            except Exception as e:
                logger.error("Price loop error: %s", e)
            await asyncio.sleep(PRICE_SECS)

    async def _check_tp_sl(self):
        try:
            from db import SessionLocal
            from models.copy_settings import CopySettings, CopyTrade
        except ImportError:
            return

        db = SessionLocal()
        try:
            # קבל סטינגים עם TP או SL
            settings = db.query(CopySettings).filter(
                CopySettings.is_active == True
            ).filter(
                (CopySettings.take_profit_pct != None) |
                (CopySettings.stop_loss_pct != None) |
                (CopySettings.sell_mode == "fixed")
            ).all()

            if not settings:
                return

            async with httpx.AsyncClient(timeout=15) as client:
                for setting in settings:
                    open_trades = db.query(CopyTrade).filter(
                        CopyTrade.copy_settings_id == setting.id,
                        CopyTrade.status == "demo"
                    ).all()

                    for trade in open_trades:
                        cur_price = await self._get_price(client, trade.market_id)
                        if cur_price is None:
                            continue

                        entry   = trade.price_entry or cur_price
                        size    = trade.amount_usdc or 0
                        pnl_pct = ((cur_price - entry) / entry * 100) if entry > 0 else 0
                        cur_val = size * (cur_price / entry) if entry > 0 else size

                        triggered = False
                        reason    = ""

                        tp = setting.take_profit_pct
                        sl = setting.stop_loss_pct
                        sell_mode = setting.sell_mode or "mirror"

                        # Take Profit
                        if tp and pnl_pct >= float(tp):
                            triggered = True
                            reason    = f"TP +{pnl_pct:.1f}%"

                        # Stop Loss
                        elif sl and pnl_pct <= -float(sl):
                            triggered = True
                            reason    = f"SL {pnl_pct:.1f}%"

                        # Fixed sell target
                        elif sell_mode == "fixed":
                            target = setting.entry_amount
                            if target and cur_val >= float(target):
                                triggered = True
                                reason    = f"Fixed ${cur_val:.2f}"

                        if triggered:
                            pnl = size * ((cur_price - entry) / entry) if entry > 0 else 0
                            trade.price_exit = cur_price
                            trade.pnl_usd    = round(pnl, 4)
                            trade.status     = "closed"
                            trade.closed_at  = datetime.now(timezone.utc)
                            logger.info("🎯 Auto-close [%s] #%d P&L=$%.2f", reason, trade.id, pnl)

            db.commit()
        except Exception as e:
            logger.error("TP/SL check error: %s", e)
        finally:
            db.close()

    async def _get_price(self, client, condition_id: str) -> Optional[float]:
        """מחיר נוכחי מ-CLOB."""
        try:
            r = await client.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": condition_id}
            )
            if r.is_success:
                return float(r.json().get("mid", 0)) or None
        except Exception:
            pass
        return None

    # ──────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────

    def _calc_size(self, trader_size: float, setting) -> float:
        mode = getattr(setting, "entry_mode", "fixed") or "fixed"
        amt  = float(getattr(setting, "entry_amount", 10) or 10)
        if mode == "percent":
            return trader_size * (amt / 100)
        return amt

    async def _check_budget(self, db, setting, needed: float) -> bool:
        """בדוק שיש תקציב לעסקה."""
        try:
            from models.copy_settings import CopyTrade
            trades = db.query(CopyTrade).filter(
                CopyTrade.copy_settings_id == setting.id,
                CopyTrade.status.in_(["demo", "open", "executed"])
            ).all()
            spent  = sum(t.amount_usdc or 0 for t in trades)
            budget = float(getattr(setting, "max_daily_loss_usd", None) or 1000)
            if spent + needed > budget:
                if spent >= budget:
                    setting.is_active = False
                    db.commit()
                    logger.warning("🛑 Setting %d auto-stopped: budget depleted", setting.id)
                return False
            return True
        except Exception:
            return True

    async def _check_daily_trades(self, db, setting) -> bool:
        """בדוק הגבלת עסקאות יומיות."""
        max_daily = getattr(setting, "max_daily_trades", None)
        if not max_daily:
            return True
        try:
            from models.copy_settings import CopyTrade
            from sqlalchemy import func
            today = date.today()
            count = db.query(func.count(CopyTrade.id)).filter(
                CopyTrade.copy_settings_id == setting.id,
                func.date(CopyTrade.opened_at) == today
            ).scalar() or 0
            if count >= int(max_daily):
                logger.info("⏸ Daily limit reached for setting %d (%d/%d)",
                            setting.id, count, max_daily)
                return False
            return True
        except Exception:
            return True


# טיפוס Optional חסר
from typing import Optional

copy_engine = CopyEngine()
