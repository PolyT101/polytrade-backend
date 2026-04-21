"""
copy_engine.py — גרסה 2
------------------------
חידושים:
1. כל קופי עובד עם ארנק Polygon נפרד שלו
2. Take Profit אוטומטי פר עסקה (אופציונלי)
3. Stop Loss אוטומטי פר עסקה (אופציונלי)
4. מעקב PnL בזמן אמת לכל עסקה פתוחה
"""

import asyncio
import logging
from datetime import datetime, timezone

from services.polymarket_service import get_trader_positions, get_market_price
from services.trading_service import place_order
from models.copy_settings import CopySettings, CopyTrade
from db import get_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30   # בדיקת עסקאות חדשות של טריידרים
PRICE_UPDATE_SECONDS  = 60   # עדכון מחיר + בדיקת TP/SL


class CopyEngine:
    def __init__(self):
        self._running = False

    async def start(self):
        self._running = True
        logger.info("Copy engine v2 started")
        asyncio.create_task(self._copy_loop())
        asyncio.create_task(self._tp_sl_loop())

    async def stop(self):
        self._running = False

    # ------------------------------------------------------------------ #
    #  לולאה 1 — מחפש עסקאות חדשות                                        #
    # ------------------------------------------------------------------ #

    async def _copy_loop(self):
        while self._running:
            try:
                await self._process_all_copy_settings()
            except Exception as e:
                logger.error(f"copy_loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _process_all_copy_settings(self):
        db = next(get_db())
        active = db.query(CopySettings).filter(CopySettings.is_active == True).all()

        trader_map: dict[str, list] = {}
        for s in active:
            trader_map.setdefault(s.trader_address, []).append(s)

        for trader_addr, settings_list in trader_map.items():
            await self._check_trader(trader_addr, settings_list, db)

    async def _check_trader(self, trader_addr: str, settings_list: list, db):
        try:
            positions = await get_trader_positions(trader_addr, closed=False)
        except Exception as e:
            logger.warning(f"fetch positions failed for {trader_addr}: {e}")
            return

        for setting in settings_list:
            if not self._within_daily_limits(setting, db):
                continue
            for pos in positions:
                trade_id = f"{trader_addr}:{pos.get('conditionId')}:{pos.get('side')}"
                exists = db.query(CopyTrade).filter(
                    CopyTrade.user_id == setting.user_id,
                    CopyTrade.source_trade_id == trade_id,
                ).first()
                if not exists:
                    await self._execute_copy_trade(setting, pos, trade_id, db)

    async def _execute_copy_trade(self, setting, position: dict, trade_id: str, db):
        price    = float(position.get("price", 0.5))
        token_id = position.get("asset", "")
        side     = position.get("side", "BUY")
        size     = self._calculate_size(setting, price)

        if size <= 0:
            return

        try:
            result = place_order(
                private_key=setting.decrypted_private_key,
                funder_address=setting.copy_wallet_address,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                is_demo=setting.is_demo,
            )

            trade = CopyTrade(
                user_id=setting.user_id,
                trader_address=setting.trader_address,
                source_trade_id=trade_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                cost_usdc=price * size,
                is_demo=setting.is_demo,
                order_id=result.get("order_id"),
                status="open",
                current_price=price,
                pnl_usd=0.0,
                pnl_pct=0.0,
                opened_at=datetime.now(timezone.utc),
                market_question=position.get("question", ""),
                polymarket_url=f"https://polymarket.com/event/{position.get('slug', '')}",
            )
            db.add(trade)
            db.commit()
            logger.info(f"✅ Copied: user={setting.user_id} {side} {size}@{price}")

        except Exception as e:
            logger.error(f"execute_copy_trade failed: {e}")

    # ------------------------------------------------------------------ #
    #  לולאה 2 — עדכון מחירים + בדיקת Take Profit / Stop Loss              #
    # ------------------------------------------------------------------ #

    async def _tp_sl_loop(self):
        while self._running:
            try:
                await self._check_tp_sl_all()
            except Exception as e:
                logger.error(f"tp_sl_loop error: {e}")
            await asyncio.sleep(PRICE_UPDATE_SECONDS)

    async def _check_tp_sl_all(self):
        db = next(get_db())
        open_trades = db.query(CopyTrade).filter(CopyTrade.status == "open").all()

        for trade in open_trades:
            setting = db.query(CopySettings).filter(
                CopySettings.user_id == trade.user_id,
                CopySettings.trader_address == trade.trader_address,
            ).first()
            if not setting:
                continue

            current_price = await get_market_price(trade.token_id)
            if current_price is None:
                continue

            # חשב PnL
            pnl_usd = (current_price - trade.price) * trade.size
            pnl_pct = ((current_price - trade.price) / trade.price) * 100

            trade.current_price = current_price
            trade.pnl_usd       = round(pnl_usd, 4)
            trade.pnl_pct       = round(pnl_pct, 2)

            # ---- Take Profit ---- (אופציונלי — רק אם הוגדר)
            if setting.take_profit_pct and pnl_pct >= setting.take_profit_pct:
                logger.info(f"🟢 Take Profit: trade={trade.id} pnl={pnl_pct:.1f}%")
                await self._close_trade(trade, setting, current_price, "take_profit", db)
                continue

            # ---- Stop Loss ---- (אופציונלי — רק אם הוגדר)
            if setting.stop_loss_pct and pnl_pct <= -setting.stop_loss_pct:
                logger.info(f"🔴 Stop Loss: trade={trade.id} pnl={pnl_pct:.1f}%")
                await self._close_trade(trade, setting, current_price, "stop_loss", db)
                continue

            db.commit()

    async def _close_trade(self, trade, setting, current_price: float, reason: str, db):
        """מוכר עסקה — אמיתית או דמו."""
        if not trade.is_demo:
            try:
                place_order(
                    private_key=setting.decrypted_private_key,
                    funder_address=setting.copy_wallet_address,
                    token_id=trade.token_id,
                    side="SELL",
                    price=current_price,
                    size=trade.size,
                    is_demo=False,
                )
            except Exception as e:
                logger.error(f"close_trade sell failed: {e}")
                return

        trade.status       = "closed"
        trade.closed_price = current_price
        trade.close_reason = reason
        trade.closed_at    = datetime.now(timezone.utc)
        db.commit()

    # ------------------------------------------------------------------ #
    #  פונקציות עזר                                                        #
    # ------------------------------------------------------------------ #

    def _calculate_size(self, setting, price: float) -> float:
        balance = setting.live_usdc_balance

        if setting.mode == "fixed":
            amount = min(setting.fixed_amount_usd, balance)
        else:
            amount = min(
                balance * setting.percentage / 100,
                setting.max_per_trade_usd,
                balance,
            )

        if amount <= 0 or price <= 0:
            return 0.0
        return round(amount / price, 2)

    def _within_daily_limits(self, setting, db) -> bool:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_trades = db.query(CopyTrade).filter(
            CopyTrade.user_id == setting.user_id,
            CopyTrade.trader_address == setting.trader_address,
            CopyTrade.opened_at >= today_start,
        ).all()

        if setting.max_daily_trades and len(today_trades) >= setting.max_daily_trades:
            return False

        if setting.max_daily_loss_usd:
            daily_loss = sum(abs(t.pnl_usd or 0) for t in today_trades if (t.pnl_usd or 0) < 0)
            if daily_loss >= setting.max_daily_loss_usd:
                return False

        if setting.max_daily_profit_usd:
            daily_profit = sum(t.pnl_usd or 0 for t in today_trades if (t.pnl_usd or 0) > 0)
            if daily_profit >= setting.max_daily_profit_usd:
                return False

        return True


copy_engine = CopyEngine()
