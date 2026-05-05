"""
services/copy_engine.py — v4.0
================================
Copy-trading engine with real CLOB execution.

Trade lifecycle:
  • Wallet configured on setting  → real market order placed on Polymarket CLOB
    (is_real=True, clob_order_id stored)
  • No wallet / CLOB order fails  → demo mode (is_real=False, status="demo")
  • All closes (mirror, TP/SL)    → real SELL if is_real=True, else just DB update

Engine loops:
  _loop        → polls Data API for new trader activity every 10 s
  _price_loop  → checks TP / SL / CLOB market closure every 30 s
"""

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
    "Accept":     "application/json",
    "Origin":     "https://polymarket.com",
    "Referer":    "https://polymarket.com/",
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
        logger.info("Copy engine v4.0 started")

    async def stop(self):
        self.running = False
        for t in [self._task, self._price_task]:
            if t:
                t.cancel()

    # ─────────────────────────────────────────────────────────────────────
    #  Main poll loop
    # ─────────────────────────────────────────────────────────────────────

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

    # ─────────────────────────────────────────────────────────────────────
    #  Trader activity check
    # ─────────────────────────────────────────────────────────────────────

    async def _check_trader(self, client, db, addr, settings):
        try:
            r = await client.get(
                f"{DATA_API}/activity",
                params={"user": addr, "limit": 50},
                headers=PM_HEADERS,
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
                new_trades_ordered = sorted(new_trades, key=lambda t: int(t.get("timestamp", 0)))
                logger.info("%d new trades from %s", len(new_trades_ordered), addr[:10])
                for trade in new_trades_ordered:
                    await self._process_trade(client, db, trade, setting)
        except Exception as e:
            logger.warning("Check trader error: %s", e)

    # ─────────────────────────────────────────────────────────────────────
    #  Watermark helpers
    # ─────────────────────────────────────────────────────────────────────

    def _get_watermark(self, db, setting):
        try:
            from models.copy_settings import CopyEngineState
            state = db.query(CopyEngineState).filter(
                CopyEngineState.setting_id == setting.id
            ).first()
            return int(state.last_seen_ts) if state and state.last_seen_ts else 0
        except Exception:
            return int(setting.updated_at.timestamp()) if setting.updated_at else 0

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
                db.add(CopyEngineState(setting_id=setting.id, last_seen_ts=ts))
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    db.query(CopyEngineState).filter(
                        CopyEngineState.setting_id == setting.id
                    ).update({"last_seen_ts": ts})
                    db.commit()
        except Exception as e:
            logger.error("Watermark error: %s", e)
            try:
                db.rollback()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────
    #  Process a single trader activity event
    # ─────────────────────────────────────────────────────────────────────

    async def _process_trade(self, client, db, trade, setting):
        try:
            from models.copy_settings import CopyTrade

            condition_id  = (trade.get("conditionId") or trade.get("conditionId_")
                             or trade.get("marketId") or "")
            side_raw      = (trade.get("side") or "").upper()
            trader_size   = float(trade.get("usdcSize") or trade.get("size")
                                  or trade.get("amount") or 0)
            price         = float(trade.get("price") or trade.get("avgPrice") or 0.5)
            market_title  = (trade.get("title") or trade.get("market")
                             or trade.get("question") or "")
            outcome_index = int(trade.get("outcomeIndex") if trade.get("outcomeIndex") is not None else 0)
            token_id      = trade.get("asset") or ""
            trader_tx     = (trade.get("transactionHash") or trade.get("txHash")
                             or trade.get("hash") or "")
            trade_type    = (trade.get("type") or "TRADE").upper()
            sl            = getattr(setting, "stop_loss_pct", None)

            logger.info("Trade: type=%s side=%s cid=%s size=%.2f tx=%s",
                        trade_type, side_raw,
                        condition_id[:12] if condition_id else "EMPTY",
                        trader_size,
                        trader_tx[:12] if trader_tx else "")

            # ── MERGE / REDEEM ──────────────────────────────────────────
            # Trader is exiting. Mirror only when SL is NOT set.
            if trade_type in ("MERGE", "REDEEM"):
                if condition_id and not sl:
                    sell_mode = getattr(setting, "sell_mode", "mirror")
                    if sell_mode in ("mirror", "sell_all"):
                        if trade_type == "REDEEM":
                            exit_price = 1.0
                        else:
                            asset = trade.get("asset") or ""
                            live  = await self._get_price(client, asset) if asset else None
                            exit_price = live if live is not None else (price if price > 0 else 1.0)
                        await self._close_positions(db, setting, condition_id, exit_price,
                                                    reason="mirror")
                return

            if trade_type != "TRADE":
                return
            if not condition_id or trader_size <= 0:
                logger.warning("Skipping: empty cid=%r or size=%.2f", condition_id, trader_size)
                return

            # ── Determine YES / NO ──────────────────────────────────────
            outcome_str   = (trade.get("outcome") or "").strip().upper()
            if outcome_str in ("YES", "Y"):
                outcome_is_yes = True
            elif outcome_str in ("NO", "N"):
                outcome_is_yes = False
            else:
                outcome_is_yes = (outcome_index == 0)

            # ── SELL side ───────────────────────────────────────────────
            if side_raw == "SELL":
                if not sl:
                    sell_mode = getattr(setting, "sell_mode", "mirror")
                    if sell_mode in ("mirror", "sell_all"):
                        await self._close_positions(db, setting, condition_id, price,
                                                    reason="mirror")
                return

            # ── BUY side ────────────────────────────────────────────────
            if side_raw != "BUY":
                return

            our_side = "YES" if outcome_is_yes else "NO"

            # Dedup: skip if this tx_hash already saved for this setting
            if trader_tx:
                dup = db.query(CopyTrade).filter(
                    CopyTrade.tx_hash == trader_tx,
                    CopyTrade.copy_settings_id == setting.id,
                ).first()
                if dup:
                    return

            # ── Guards ──────────────────────────────────────────────────
            wallet    = await self._get_wallet_for_setting(db, setting)
            copy_size = await self._calc_size(trader_size, setting, db, wallet)
            if copy_size < 1.0:
                return
            if not await self._check_budget(db, setting):
                return
            if not await self._check_daily_trades(db, setting):
                return

            # ── Execute BUY ─────────────────────────────────────────────
            is_real       = False
            clob_order_id = None
            entry_price   = price

            if wallet and getattr(wallet, "encrypted_private_key", None) and token_id:
                from services.clob import execute_buy
                logger.info("Executing REAL BUY: $%.2f token=%s", copy_size, token_id[:12])
                result = await execute_buy(
                    wallet.encrypted_private_key, token_id, copy_size, price
                )
                if result["success"]:
                    is_real       = True
                    entry_price   = result.get("fill_price") or price
                    clob_order_id = result.get("order_id") or None
                    logger.info("Real BUY filled: price=%.4f order=%s",
                                entry_price, clob_order_id)
                else:
                    logger.warning("Real BUY failed (%s) — falling back to demo",
                                   result.get("error"))

            t_obj = CopyTrade(
                user_id          = setting.user_id,
                copy_settings_id = setting.id,
                trader_address   = setting.trader_address,
                market_id        = condition_id,
                token_id         = token_id or None,
                market_question  = market_title,
                side             = our_side,
                amount_usdc      = copy_size,
                price_entry      = entry_price,
                is_real          = is_real,
                clob_order_id    = clob_order_id,
                status           = "demo",
                tx_hash          = trader_tx,
            )
            db.add(t_obj)
            db.commit()
            db.refresh(t_obj)
            mode = "REAL" if is_real else "DEMO"
            logger.info("[%s] Trade #%d: %s $%.2f | %s",
                        mode, t_obj.id, our_side, copy_size, market_title[:30])

        except Exception as e:
            logger.error("Process trade error: %s", e)
            try:
                db.rollback()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────
    #  Close positions (mirror sell / TP / SL / CLOB-resolved)
    # ─────────────────────────────────────────────────────────────────────

    async def _close_positions(self, db, setting, condition_id: str,
                               exit_price: float, reason: str = ""):
        """Close all open demo positions for a given market.
        For is_real positions, execute a real SELL on CLOB first."""
        try:
            from models.copy_settings import CopyTrade

            open_trades = db.query(CopyTrade).filter(
                CopyTrade.copy_settings_id == setting.id,
                CopyTrade.market_id        == condition_id,
                CopyTrade.status           == "demo",
            ).all()

            if not open_trades:
                return

            wallet = await self._get_wallet_for_setting(db, setting)

            for t in open_trades:
                actual_exit = exit_price

                # Real position — try to execute a sell on CLOB
                if t.is_real and wallet and t.token_id:
                    from services.clob import execute_sell
                    entry  = t.price_entry or exit_price or 0.5
                    shares = t.amount_usdc / entry if entry > 0 else 0
                    if shares > 0:
                        result = await execute_sell(
                            wallet.encrypted_private_key, t.token_id, shares, exit_price
                        )
                        if result["success"]:
                            actual_exit   = result.get("fill_price") or exit_price
                            t.clob_order_id = result.get("order_id") or t.clob_order_id
                            logger.info("Real SELL [%s] #%d exit=%.4f", reason, t.id, actual_exit)
                        else:
                            logger.warning("Real SELL failed [%s] #%d: %s — using DB exit price",
                                           reason, t.id, result.get("error"))

                entry = t.price_entry or actual_exit
                pnl   = t.amount_usdc * ((actual_exit - entry) / entry) if entry > 0 else 0
                t.price_exit = actual_exit
                t.pnl_usd    = round(pnl, 4)
                t.status     = "closed"
                t.closed_at  = datetime.now(timezone.utc)
                logger.info("Close [%s] #%d exit=%.4f PnL=$%.2f", reason, t.id, actual_exit, pnl)

            db.commit()

        except Exception as e:
            logger.error("_close_positions error: %s", e)
            try:
                db.rollback()
            except Exception:
                pass

    # Keep old name as alias (called by router/scan endpoint)
    async def _mirror_sell(self, db, setting, condition_id, price):
        await self._close_positions(db, setting, condition_id, price, reason="mirror")

    # ─────────────────────────────────────────────────────────────────────
    #  Price loop — TP / SL / CLOB market closure
    # ─────────────────────────────────────────────────────────────────────

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
                    open_trades = db.query(CopyTrade).filter(
                        CopyTrade.copy_settings_id == setting.id,
                        CopyTrade.status           == "demo",
                    ).all()
                    if not open_trades:
                        continue

                    trader_pos = await self._get_trader_positions(client, setting.trader_address)
                    tp        = setting.take_profit_pct
                    sl        = setting.stop_loss_pct
                    sell_mode = setting.sell_mode or "mirror"
                    wallet    = await self._get_wallet_for_setting(db, setting)

                    # CLOB resolution batch for trades not in trader's positions
                    unresolved_cids: set = set()
                    for trade in open_trades:
                        cid = (trade.market_id or "").lower()
                        if not (trader_pos.get(cid) or trader_pos.get(trade.market_id or "")):
                            if trade.market_id:
                                unresolved_cids.add(trade.market_id)

                    clob_resolution: dict = {}
                    for cid in unresolved_cids:
                        res = await self._get_clob_resolution(client, cid)
                        if res is not None:
                            clob_resolution[cid] = res

                    for trade in open_trades:
                        cid      = (trade.market_id or "").lower()
                        pos_info = (trader_pos.get(cid)
                                    or trader_pos.get(trade.market_id or ""))
                        side = (trade.side or "YES").upper()

                        # ── Level 1: redeemable position ─────────────────
                        if pos_info and pos_info.get("redeemable"):
                            raw_ep     = pos_info.get("cur_price")
                            exit_price = float(raw_ep) if raw_ep is not None else 0.0
                            await self._close_one(db, trade, exit_price, "Redeemable",
                                                  wallet)
                            continue

                        # ── Level 2: CLOB says market is closed ──────────
                        if pos_info is None and trade.market_id in clob_resolution:
                            res        = clob_resolution[trade.market_id]
                            exit_price = res["yes"] if side == "YES" else res["no"]
                            await self._close_one(db, trade, exit_price, "CLOB-closed",
                                                  wallet)
                            continue

                        # ── Level 3: TP / SL / fixed ─────────────────────
                        if not tp and not sl and sell_mode != "fixed":
                            continue

                        cur_price = pos_info.get("cur_price") if pos_info else None
                        if cur_price is None:
                            pid = trade.token_id or trade.market_id
                            cur_price = await self._get_price(client, pid)
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
                        elif sell_mode == "fixed" and not sl:
                            target = setting.entry_amount
                            if target and cur_val >= float(target):
                                triggered = True
                                reason    = "Fixed"

                        if triggered:
                            await self._close_one(db, trade, cur_price, reason, wallet)

            db.commit()

        except Exception as e:
            logger.error("TP/SL error: %s", e)
        finally:
            db.close()

    async def _close_one(self, db, trade, exit_price: float,
                         reason: str, wallet=None):
        """Close a single CopyTrade — executes real SELL if is_real=True."""
        try:
            actual_exit = exit_price

            if trade.is_real and wallet and trade.token_id:
                from services.clob import execute_sell
                entry  = trade.price_entry or exit_price or 0.5
                shares = trade.amount_usdc / entry if entry > 0 else 0
                if shares > 0:
                    result = await execute_sell(
                        wallet.encrypted_private_key, trade.token_id, shares, exit_price
                    )
                    if result["success"]:
                        actual_exit       = result.get("fill_price") or exit_price
                        trade.clob_order_id = result.get("order_id") or trade.clob_order_id
                    else:
                        logger.warning("Real SELL failed [%s] #%d: %s",
                                       reason, trade.id, result.get("error"))

            entry = trade.price_entry or actual_exit
            pnl   = trade.amount_usdc * ((actual_exit - entry) / entry) if entry > 0 else 0
            trade.price_exit = actual_exit
            trade.pnl_usd    = round(pnl, 4)
            trade.status     = "closed"
            trade.closed_at  = datetime.now(timezone.utc)
            logger.info("Close [%s] #%d exit=%.4f PnL=$%.2f", reason, trade.id, actual_exit, pnl)

        except Exception as e:
            logger.error("_close_one error [%s] #%d: %s", reason, trade.id, e)

    # ─────────────────────────────────────────────────────────────────────
    #  Polymarket data helpers
    # ─────────────────────────────────────────────────────────────────────

    async def _get_trader_positions(self, client, trader_address: str) -> dict:
        try:
            r = await client.get(
                f"{DATA_API}/positions",
                params={"user": trader_address, "limit": 500, "closed": "false"},
                headers=PM_HEADERS,
            )
            if not r.is_success:
                return {}
            data = r.json()
            if not isinstance(data, list):
                return {}
            result = {}
            for p in data:
                cid = (p.get("conditionId") or p.get("market") or "").lower()
                if not cid:
                    continue
                cur = p.get("curPrice")
                result[cid] = {
                    "cur_price":  float(cur) if cur is not None else None,
                    "redeemable": bool(p.get("redeemable") or p.get("isRedeemable")),
                }
            return result
        except Exception as e:
            logger.warning("get_trader_positions error: %s", e)
            return {}

    async def _get_clob_resolution(self, client, condition_id: str) -> Optional[dict]:
        """Returns {yes, no} prices if market is closed on CLOB, else None."""
        try:
            r = await client.get(f"{CLOB_API}/markets/{condition_id}", headers=PM_HEADERS)
            if not r.is_success:
                return None
            m = r.json()
            if not m.get("closed"):
                return None
            tokens = m.get("tokens") or []
            if len(tokens) < 2:
                return None
            return {
                "yes": float(tokens[0].get("price", 0)),
                "no":  float(tokens[1].get("price", 0)),
            }
        except Exception:
            return None

    async def _get_price(self, client, token_id: str) -> Optional[float]:
        """Fetch midpoint price for a YES/NO token from CLOB."""
        try:
            r = await client.get(f"{CLOB_API}/midpoint", params={"token_id": token_id})
            if r.is_success:
                val = r.json().get("mid", 0)
                return float(val) if val else None
        except Exception:
            pass
        return None

    # ─────────────────────────────────────────────────────────────────────
    #  Wallet lookup
    # ─────────────────────────────────────────────────────────────────────

    async def _get_wallet_for_setting(self, db, setting):
        """
        Returns the Wallet DB object for this copy setting, or None.
        Priority: dedicated wallet (wallet_address) → default wallet → None.
        """
        try:
            from models.wallet import Wallet

            if setting.wallet_address:
                w = db.query(Wallet).filter(
                    Wallet.address == setting.wallet_address
                ).first()
                if w and w.encrypted_private_key:
                    return w

            # Fall back to the user's default wallet
            w = db.query(Wallet).filter(
                Wallet.user_id    == setting.user_id,
                Wallet.is_default == True,
            ).first()
            return w if (w and w.encrypted_private_key) else None

        except Exception as e:
            logger.debug("_get_wallet_for_setting error: %s", e)
            return None

    # ─────────────────────────────────────────────────────────────────────
    #  Size calculation
    # ─────────────────────────────────────────────────────────────────────

    async def _calc_size(self, trader_size: float, setting, db=None,
                         wallet=None) -> float:
        """
        fixed:   entry_amount USD, regardless of trader's position size.
        percent: entry_amount% of portfolio.
                 portfolio = real wallet USDC balance (if wallet known)
                            + value of all open positions for this setting.
                 Falls back to $100 base if wallet balance unavailable.
        """
        mode = getattr(setting, "entry_mode", "fixed") or "fixed"
        amt  = float(getattr(setting, "entry_amount", 10) or 10)

        if mode != "percent":
            return amt

        # ── Percent mode: build portfolio_size ───────────────────────────
        portfolio_size = 100.0  # base / fallback

        # Real wallet USDC balance (blocking RPC call in thread)
        if wallet and getattr(wallet, "address", None):
            try:
                from services.wallet_service import get_usdc_balance
                balance = await asyncio.to_thread(get_usdc_balance, wallet.address)
                portfolio_size = float(balance)
                logger.debug("Wallet %s USDC balance: $%.2f", wallet.address[:10], portfolio_size)
            except Exception as e:
                logger.debug("Balance fetch failed: %s — using $100 base", e)

        # Add open-position value
        if db is not None:
            try:
                from models.copy_settings import CopyTrade
                open_val = db.query(CopyTrade).filter(
                    CopyTrade.copy_settings_id == setting.id,
                    CopyTrade.status           == "demo",
                ).all()
                portfolio_size += sum(t.amount_usdc or 0 for t in open_val)
            except Exception:
                pass

        return portfolio_size * (amt / 100)

    # ─────────────────────────────────────────────────────────────────────
    #  Daily guards
    # ─────────────────────────────────────────────────────────────────────

    async def _check_budget(self, db, setting) -> bool:
        """Stop opening new trades when today's cumulative loss hits the limit."""
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
                CopyTrade.status           == "closed",
                CopyTrade.closed_at        >= today_start,
                CopyTrade.pnl_usd          < 0,
            ).all()
            daily_loss = abs(sum(t.pnl_usd or 0 for t in closed_today))
            if daily_loss >= float(max_loss):
                logger.warning("Setting %d: daily loss limit $%.2f reached", setting.id, daily_loss)
                return False
            return True
        except Exception:
            return True

    async def _check_daily_trades(self, db, setting) -> bool:
        """Stop opening new trades when today's BUY count hits the daily cap.
        Resets at UTC midnight. Sell/close operations are never blocked."""
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
                CopyTrade.opened_at        >= today_start,
                CopyTrade.status.in_(["demo", "closed"]),
            ).count()
            return count < int(max_daily)
        except Exception:
            return True


copy_engine = CopyEngine()
