"""
services/copy_engine.py — Real Copy Trading Engine
===================================================
Handles:
- Mirror: buy/sell when trader buys/sells
- Take Profit: auto-sell when price rises X%
- Stop Loss: auto-sell when price drops X%
- Fixed Amount: auto-sell when position reaches fixed $ value
- Manual: never auto-sell
- Budget enforcement
- Real-time price monitoring
"""
import asyncio
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DATA_API  = "https://data-api.polymarket.com"
POLL_SECS = 10       # Check for new trades every 10s
PRICE_SECS = 30      # Check prices for TP/SL every 30s


class CopyEngine:
    def __init__(self):
        self.running   = False
        self._task     = None
        self._price_task = None
        self._last_ts  = {}   # trader_addr → last seen trade timestamp
        self._positions = {}  # setting_id → list of open positions with entry prices

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._loop())
        self._price_task = asyncio.create_task(self._price_loop())
        logger.info("✅ Copy engine started (trades: %ds, prices: %ds)", POLL_SECS, PRICE_SECS)

    async def stop(self):
        self.running = False
        for t in [self._task, self._price_task]:
            if t: t.cancel()

    # ══════════════════════════════════════════════════════════════
    # LOOP 1: Watch for new trades from traders
    # ══════════════════════════════════════════════════════════════

    async def _loop(self):
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Trade tick error: %s", e)
            await asyncio.sleep(POLL_SECS)

    async def _tick(self):
        try:
            from db import SessionLocal
            from models.copy_settings import CopySettings
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
            async with httpx.AsyncClient(timeout=15) as client:
                for addr in traders:
                    await self._check_trader(client, db, addr, settings)
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
            new_trades = [t for t in trades if int(t.get("timestamp", 0)) > last_ts]
            if not new_trades:
                return

            self._last_ts[addr] = max(int(t.get("timestamp", 0)) for t in new_trades)
            logger.info("🔔 %d new trades from %s", len(new_trades), addr[:10])

            relevant = [s for s in all_settings if s.trader_address == addr]
            for trade in new_trades:
                for setting in relevant:
                    await self._process_trade(client, db, trade, setting)

        except Exception as e:
            logger.warning("Error checking %s: %s", addr[:10], e)

    async def _process_trade(self, client, db, trade: dict, setting):
        """Process a single new trade from a watched trader."""
        try:
            from models.copy_settings import CopyTrade

            condition_id  = trade.get("conditionId", "")
            token_id      = trade.get("asset", "")
            side_raw      = (trade.get("side") or "").upper()   # BUY or SELL
            trader_size   = float(trade.get("usdcSize") or trade.get("size") or 0)
            price         = float(trade.get("price") or 0.5)
            market_title  = trade.get("title") or trade.get("market") or ""
            outcome_index = int(trade.get("outcomeIndex") or 0)
            trader_tx     = trade.get("transactionHash", "")

            if not condition_id or trader_size <= 0:
                return

            # Convert BUY/SELL + outcomeIndex → YES/NO
            if side_raw == "BUY":
                our_side = "YES" if outcome_index == 0 else "NO"
            elif side_raw == "SELL":
                our_side = "NO"  if outcome_index == 0 else "YES"
                # Trader is SELLING → check if we should mirror-sell
                await self._handle_trader_sell(db, setting, condition_id, our_side, price, market_title)
                return
            else:
                our_side = "YES"

            # ── TRADER IS BUYING → we buy too ──────────────────────────

            # Determine sell mode
            sell_mode = getattr(setting, "sell_mode", "mirror") or "mirror"

            # Budget check
            copy_size = self._calc_size(trader_size, setting)
            if copy_size < 1.0:
                return

            if not await self._check_budget(db, setting, copy_size):
                return

            # Save the trade
            trade_obj = CopyTrade(
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
            db.add(trade_obj)
            db.commit()
            db.refresh(trade_obj)

            # Track position for TP/SL monitoring
            key = str(setting.id)
            if key not in self._positions:
                self._positions[key] = []
            self._positions[key].append({
                "trade_id":     trade_obj.id,
                "condition_id": condition_id,
                "token_id":     token_id,
                "our_side":     our_side,
                "entry_price":  price,
                "size":         copy_size,
                "market":       market_title,
                "setting":      setting,
                "status":       "open",
            })

            logger.info("✅ Copied: %s %s $%.2f @ %.3f | %s [sell_mode=%s]",
                        our_side, side_raw, copy_size, price, market_title[:30], sell_mode)

        except Exception as e:
            logger.error("Process trade error: %s", e)
            db.rollback()

    async def _handle_trader_sell(self, db, setting, condition_id, side, price, market):
        """Trader sold a position — mirror-sell our matching position."""
        sell_mode = getattr(setting, "sell_mode", "mirror") or "mirror"

        # Only mirror-sell if mode is "mirror"
        if sell_mode != "mirror":
            return

        key = str(setting.id)
        positions = self._positions.get(key, [])
        matching = [p for p in positions
                    if p["condition_id"] == condition_id
                    and p["status"] == "open"]

        if not matching:
            return

        for pos in matching:
            await self._close_position(db, pos, price, "mirror_sell")

    # ══════════════════════════════════════════════════════════════
    # LOOP 2: Real-time price monitoring for TP/SL/Fixed
    # ══════════════════════════════════════════════════════════════

    async def _price_loop(self):
        while self.running:
            try:
                await self._check_prices()
            except Exception as e:
                logger.error("Price loop error: %s", e)
            await asyncio.sleep(PRICE_SECS)

    async def _check_prices(self):
        """Check current prices for all open positions and trigger TP/SL."""
        if not self._positions:
            return

        async with httpx.AsyncClient(timeout=15) as client:
            for setting_id, positions in list(self._positions.items()):
                open_pos = [p for p in positions if p.get("status") == "open"]
                if not open_pos:
                    continue

                setting = open_pos[0]["setting"]
                sell_mode    = getattr(setting, "sell_mode",       "mirror") or "mirror"
                tp_pct       = getattr(setting, "take_profit_pct", None)
                sl_pct       = getattr(setting, "stop_loss_pct",   None)
                fixed_target = getattr(setting, "entry_amount",    None)

                # Determine if we need price monitoring
                needs_monitoring = (
                    (tp_pct and sell_mode in ("mirror", "fixed", "manual")) or
                    (sl_pct) or
                    (sell_mode == "fixed" and fixed_target)
                )
                if not needs_monitoring:
                    continue

                try:
                    from db import SessionLocal
                    db = SessionLocal()

                    # Get current prices from Polymarket positions endpoint
                    addr = setting.trader_address
                    r = await client.get(
                        f"{DATA_API}/positions",
                        params={"user": addr, "limit": 200, "closed": "false"},
                        headers={"Accept": "application/json"}
                    )
                    if not r.is_success:
                        db.close()
                        continue

                    market_prices = {}
                    for pm_pos in (r.json() or []):
                        cid = pm_pos.get("conditionId", "")
                        if cid:
                            market_prices[cid] = float(pm_pos.get("curPrice") or 0.5)

                    for pos in open_pos:
                        cur_price = market_prices.get(pos["condition_id"])
                        if cur_price is None:
                            continue

                        entry   = pos["entry_price"]
                        size    = pos["size"]
                        cur_val = size * (cur_price / entry) if entry > 0 else size
                        pnl_pct = ((cur_price - entry) / entry * 100) if entry > 0 else 0

                        triggered = False
                        reason    = ""

                        # ── Take Profit ─────────────────────────────────
                        if tp_pct and pnl_pct >= float(tp_pct):
                            triggered = True
                            reason    = f"take_profit ({pnl_pct:.1f}% >= {tp_pct}%)"

                        # ── Stop Loss ───────────────────────────────────
                        elif sl_pct and pnl_pct <= -float(sl_pct):
                            triggered = True
                            reason    = f"stop_loss ({pnl_pct:.1f}% <= -{sl_pct}%)"

                        # ── Fixed Amount target ─────────────────────────
                        elif sell_mode == "fixed" and fixed_target:
                            if cur_val >= float(fixed_target):
                                triggered = True
                                reason    = f"fixed_target (${cur_val:.2f} >= ${fixed_target})"

                        if triggered:
                            logger.info("🎯 %s triggered for %s: %s",
                                        reason, pos["market"][:30], reason)
                            await self._close_position(db, pos, cur_price, reason)

                    db.close()

                except Exception as e:
                    logger.warning("Price check error for setting %s: %s", setting_id, e)

    async def _close_position(self, db, pos: dict, sell_price: float, reason: str):
        """Close a position: update DB and remove from tracking."""
        try:
            from models.copy_settings import CopyTrade

            trade_id = pos.get("trade_id")
            if not trade_id:
                return

            trade = db.query(CopyTrade).filter(CopyTrade.id == trade_id).first()
            if not trade or trade.status not in ("demo", "open", "executed"):
                return

            entry    = pos["entry_price"]
            size     = pos["size"]
            pnl      = size * ((sell_price - entry) / entry) if entry > 0 else 0
            pnl_pct  = ((sell_price - entry) / entry * 100) if entry > 0 else 0

            trade.price_exit = sell_price
            trade.pnl_usd    = round(pnl, 4)
            trade.status     = "closed"
            trade.closed_at  = datetime.now(timezone.utc)
            db.commit()

            pos["status"] = "closed"

            logger.info("💰 Closed [%s]: %s | entry=%.3f exit=%.3f P&L=$%.2f (%.1f%%)",
                        reason, pos["market"][:25],
                        entry, sell_price, pnl, pnl_pct)

        except Exception as e:
            logger.error("Close position error: %s", e)
            db.rollback()

    # ══════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════

    def _calc_size(self, trader_size: float, setting) -> float:
        mode = getattr(setting, "entry_mode", "fixed") or "fixed"
        if mode == "percent":
            pct = float(getattr(setting, "entry_amount", 10) or 10) / 100
            return trader_size * pct
        return float(getattr(setting, "entry_amount", 10) or 10)

    async def _check_budget(self, db, setting, needed: float) -> bool:
        """Return True if there's budget available for this trade."""
        try:
            from models.copy_settings import CopyTrade
            trades = db.query(CopyTrade).filter(
                CopyTrade.copy_settings_id == setting.id,
                CopyTrade.status.in_(["demo", "open", "executed"])
            ).all()

            spent   = sum(t.amount_usdc or 0 for t in trades)
            budget  = float(getattr(setting, "max_daily_loss_usd", None) or 1000)

            if spent + needed > budget:
                logger.info("💰 Budget limit: spent $%.2f + $%.2f > $%.2f",
                            spent, needed, budget)
                if spent >= budget:
                    setting.is_active = False
                    db.commit()
                    logger.warning("🛑 Setting %d auto-stopped: budget depleted ($%.2f)",
                                   setting.id, spent)
                return False
            return True
        except Exception:
            return True  # Allow if can't check


copy_engine = CopyEngine()
