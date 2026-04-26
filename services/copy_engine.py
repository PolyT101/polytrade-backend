from typing import Optional
import asyncio
import httpx
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DATA_API   = "https://data-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
POLL_SECS  = 10
PRICE_SECS = 30

PM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}


class CopyEngine:
    def __init__(self):
        self.running     = False
        self._task       = None
        self._price_task = None

    async def start(self):
        self.running     = True
        self._task       = asyncio.create_task(self._loop())
        self._price_task = asyncio.create_task(self._price_loop())
        logger.info("Copy engine started")

    async def stop(self):
        self.running = False
        for t in [self._task, self._price_task]:
            if t:
                t.cancel()

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
            logger.info("Watching %d traders", len(traders))
            async with httpx.AsyncClient(timeout=15) as client:
                for addr in traders:
                    relevant = [s for s in settings if s.trader_address == addr]
                    await self._check_trader(client, db, addr, relevant)
        except Exception as e:
            logger.error("Tick DB error: %s", e)
        finally:
            db.close()

    async def _check_trader(self, client, db, addr, settings):
        try:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": addr, "limit": 20},
                headers=PM_HEADERS
            )
            if not r.is_success:
                return
            trades = r.json()
            if not isinstance(trades, list) or not trades:
                return
            for setting in settings:
                last_ts    = self._get_watermark(db, setting)
                new_trades = [t for t in trades if int(t.get("timestamp", 0)) > last_ts]
                if not new_trades:
                    continue
                new_max = max(int(t.get("timestamp", 0)) for t in new_trades)
                self._set_watermark(db, setting, new_max)
                # Process oldest-first so BUY is always handled before its REDEEM/SELL
                new_trades_ordered = sorted(new_trades, key=lambda t: int(t.get("timestamp", 0)))
                logger.info("%d new trades from %s", len(new_trades_ordered), addr[:10])
                for trade in new_trades_ordered:
                    await self._process_trade(client, db, trade, setting)
        except Exception as e:
            logger.warning("Check trader error: %s", e)

    def _get_watermark(self, db, setting):
        try:
            from models.copy_settings import CopyEngineState
            state = db.query(CopyEngineState).filter(
                CopyEngineState.setting_id == setting.id
            ).first()
            return int(state.last_seen_ts) if state and state.last_seen_ts else 0
        except Exception:
            if setting.updated_at:
                return int(setting.updated_at.timestamp())
            return 0

    def _set_watermark(self, db, setting, ts):
        try:
            from models.copy_settings import CopyEngineState
            state = db.query(CopyEngineState).filter(
                CopyEngineState.setting_id == setting.id
            ).first()
            if state:
                state.last_seen_ts = ts
                db.commit()
            else:
                new_state = CopyEngineState(setting_id=setting.id, last_seen_ts=ts)
                db.add(new_state)
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    # Row might have been inserted by concurrent tick — update instead
                    db.query(CopyEngineState).filter(
                        CopyEngineState.setting_id == setting.id
                    ).update({"last_seen_ts": ts})
                    db.commit()
        except Exception as e:
            logger.error("Watermark save error: %s", e)
            try:
                db.rollback()
            except Exception:
                pass

    async def _process_trade(self, client, db, trade, setting):
        try:
            from models.copy_settings import CopyTrade
            condition_id  = trade.get("conditionId", "")
            side_raw      = (trade.get("side") or "").upper()
            trader_size   = float(trade.get("usdcSize") or trade.get("size") or 0)
            price         = float(trade.get("price") or 0.5)
            market_title  = trade.get("title") or trade.get("market") or ""
            outcome_index = int(trade.get("outcomeIndex") if trade.get("outcomeIndex") is not None else 0)
            token_id      = trade.get("asset") or ""   # token_id for live price
            trader_tx     = trade.get("transactionHash", "")
            trade_type = (trade.get("type") or "TRADE").upper()

            # MERGE = convert YES+NO→USDC, REDEEM = claim resolved market winnings
            # Both mean the trader is exiting — mirror sell our open position
            if trade_type in ("MERGE", "REDEEM"):
                if condition_id:
                    sell_mode = getattr(setting, "sell_mode", "mirror")
                    if sell_mode in ("mirror", "sell_all"):
                        if trade_type == "REDEEM":
                            # REDEEM = market resolved, winning share = $1
                            exit_price = 1.0
                        else:
                            # MERGE: try live midpoint from CLOB, fall back to event price
                            asset = trade.get("asset") or ""
                            live = await self._get_price(client, asset) if asset else None
                            exit_price = live if live is not None else (price if price > 0 else 1.0)
                        await self._mirror_sell(db, setting, condition_id, exit_price)
                return

            if trade_type != "TRADE":
                return
            if not condition_id or trader_size <= 0:
                return
            # Determine YES/NO: prefer explicit "outcome" field over outcomeIndex
            outcome_str = (trade.get("outcome") or "").strip().upper()
            if outcome_str in ("YES", "Y"):
                outcome_is_yes = True
            elif outcome_str in ("NO", "N"):
                outcome_is_yes = False
            else:
                outcome_is_yes = (outcome_index == 0)  # fallback: 0=YES, 1=NO
            if side_raw == "BUY":
                our_side = "YES" if outcome_is_yes else "NO"
            elif side_raw == "SELL":
                sell_mode = getattr(setting, "sell_mode", "mirror")
                if sell_mode in ("mirror", "sell_all"):
                    await self._mirror_sell(db, setting, condition_id, price)
                return
            else:
                our_side = "YES"
            # Dedup: skip if this tx_hash already saved for this setting
            if trader_tx:
                dup = db.query(CopyTrade).filter(
                    CopyTrade.tx_hash == trader_tx,
                    CopyTrade.copy_settings_id == setting.id,
                ).first()
                if dup:
                    return
            copy_size = self._calc_size(trader_size, setting)
            if copy_size < 1.0:
                return
            if not await self._check_budget(db, setting, copy_size):
                return
            if not await self._check_daily_trades(db, setting):
                return
            t_obj = CopyTrade(
                user_id=setting.user_id,
                copy_settings_id=setting.id,
                trader_address=setting.trader_address,
                market_id=condition_id,
                market_question=market_title,
                side=our_side,
                amount_usdc=copy_size,
                price_entry=price,
                status="demo",
                tx_hash=trader_tx,
            )
            db.add(t_obj)
            db.commit()
            db.refresh(t_obj)
            logger.info("Demo trade #%d: %s $%.2f | %s", t_obj.id, our_side, copy_size, market_title[:30])
        except Exception as e:
            logger.error("Process trade error: %s", e)
            try:
                db.rollback()
            except Exception:
                pass

    async def _mirror_sell(self, db, setting, condition_id, price):
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
            if open_trades:
                db.commit()
        except Exception as e:
            logger.error("Mirror-sell error: %s", e)

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
            settings = db.query(CopySettings).filter(
                CopySettings.is_active == True
            ).all()
            if not settings:
                return
            async with httpx.AsyncClient(timeout=15) as client:
                for setting in settings:
                    tp        = setting.take_profit_pct
                    sl        = setting.stop_loss_pct
                    sell_mode = setting.sell_mode or "mirror"
                    if not tp and not sl and sell_mode != "fixed":
                        continue
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
                        if tp and pnl_pct >= float(tp):
                            triggered = True
                            reason    = "TP"
                        elif sl and pnl_pct <= -float(sl):
                            triggered = True
                            reason    = "SL"
                        elif sell_mode == "fixed":
                            target = setting.entry_amount
                            if target and cur_val >= float(target):
                                triggered = True
                                reason    = "Fixed"
                        if triggered:
                            pnl = size * ((cur_price - entry) / entry) if entry > 0 else 0
                            trade.price_exit = cur_price
                            trade.pnl_usd    = round(pnl, 4)
                            trade.status     = "closed"
                            trade.closed_at  = datetime.now(timezone.utc)
                            logger.info("Auto-close [%s] #%d P&L=$%.2f", reason, trade.id, pnl)
            db.commit()
        except Exception as e:
            logger.error("TP/SL error: %s", e)
        finally:
            db.close()

    async def _get_price(self, client: httpx.AsyncClient, condition_id: str) -> Optional[float]:
        try:
            r = await client.get(f"{CLOB_API}/midpoint", params={"token_id": condition_id})
            if r.is_success:
                val = r.json().get("mid", 0)
                return float(val) if val else None
        except Exception:
            pass
        return None

    def _calc_size(self, trader_size: float, setting) -> float:
        mode = getattr(setting, "entry_mode", "fixed") or "fixed"
        amt  = float(getattr(setting, "entry_amount", 10) or 10)
        if mode == "percent":
            return trader_size * (amt / 100)
        return amt

    async def _check_budget(self, db, setting, needed: float) -> bool:
        max_loss = getattr(setting, "max_daily_loss_usd", None)
        if not max_loss:
            return True
        try:
            from models.copy_settings import CopyTrade
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            closed_today = db.query(CopyTrade).filter(
                CopyTrade.copy_settings_id == setting.id,
                CopyTrade.status == "closed",
                CopyTrade.closed_at >= today_start,
                CopyTrade.pnl_usd < 0,
            ).all()
            daily_loss = abs(sum(t.pnl_usd or 0 for t in closed_today))
            if daily_loss >= float(max_loss):
                logger.warning("Setting %d auto-stopped: daily loss limit reached", setting.id)
                return False
            return True
        except Exception:
            return True

    async def _check_daily_trades(self, db, setting) -> bool:
        max_daily = getattr(setting, "max_daily_trades", None)
        if not max_daily:
            return True
        try:
            from models.copy_settings import CopyTrade
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            count = db.query(CopyTrade).filter(
                CopyTrade.copy_settings_id == setting.id,
                CopyTrade.status == "demo",   # only count open positions, not closed ones
                CopyTrade.opened_at >= today_start,
            ).count()
            return count < int(max_daily)
        except Exception:
            return True


copy_engine = CopyEngine()
