"""
models/copy_settings.py
-----------------------
עדכון:
- כל CopySettings מקבל ארנק Polygon נפרד משלו
- הוספת take_profit_pct ו-stop_loss_pct (לא שדות חובה)
"""

from sqlalchemy import Column, String, Float, Boolean, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime, timezone


class User(Base):
    __tablename__ = "users"

    id                    = Column(String, primary_key=True)
    email                 = Column(String, unique=True, nullable=True)
    main_wallet_address   = Column(String, unique=True, nullable=True)
    created_at            = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    copy_settings = relationship("CopySettings", back_populates="user")
    copy_trades   = relationship("CopyTrade",    back_populates="user")
    watchlist        = relationship("WatchlistEntry",       back_populates="user")
    market_watchlist = relationship("MarketWatchlistEntry", back_populates="user")
    wallets       = relationship("Wallet",         back_populates="user")


class CopySettings(Base):
    """
    הגדרות קופי — כל שורה = משתמש אחד + טריידר אחד + ארנק נפרד.
    """
    __tablename__ = "copy_settings"

    id                        = Column(Integer, primary_key=True, autoincrement=True)
    user_id                   = Column(String, ForeignKey("users.id"))
    trader_address            = Column(String)
    trader_name               = Column(String)

    # ---- ארנק ייעודי לקופי הזה ----
    copy_wallet_address       = Column(String, unique=True)
    copy_wallet_encrypted_key = Column(Text)

    # ---- סטטוס ----
    is_active            = Column(Boolean, default=True)
    is_demo              = Column(Boolean, default=False)
    demo_balance_usd     = Column(Float,   default=1000.0)

    # ---- הגדרות כניסה ----
    mode                 = Column(String, default="fixed")
    fixed_amount_usd     = Column(Float,  default=50.0)
    percentage           = Column(Float,  default=5.0)
    max_per_trade_usd    = Column(Float,  default=100.0)

    # ---- מגבלות יומיות (אופציונלי) ----
    max_daily_trades     = Column(Integer, nullable=True)
    max_daily_loss_usd   = Column(Float,   nullable=True)
    max_daily_profit_usd = Column(Float,   nullable=True)

    # ---- Take Profit / Stop Loss פר עסקה (אופציונלי) ----
    take_profit_pct      = Column(Float, nullable=True)   # למשל 25.0 = מכור ב-+25%
    stop_loss_pct        = Column(Float, nullable=True)   # למשל 30.0 = מכור ב--30%

    # ---- מצב יציאה ----
    sell_mode            = Column(String, default="mirror")

    started_at           = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="copy_settings")

    @property
    def decrypted_private_key(self) -> str:
        from services.wallet_service import decrypt_private_key
        return decrypt_private_key(self.copy_wallet_encrypted_key)

    @property
    def live_usdc_balance(self) -> float:
        if self.is_demo:
            return self.demo_balance_usd
        from services.wallet_service import get_usdc_balance
        return get_usdc_balance(self.copy_wallet_address)


class CopyTrade(Base):
    __tablename__ = "copy_trades"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_id          = Column(String, ForeignKey("users.id"))
    trader_address   = Column(String)
    source_trade_id  = Column(String)

    token_id         = Column(String)
    side             = Column(String)
    price            = Column(Float)
    size             = Column(Float)
    cost_usdc        = Column(Float)

    is_demo          = Column(Boolean, default=False)
    order_id         = Column(String, nullable=True)
    status           = Column(String, default="open")

    current_price    = Column(Float, nullable=True)
    pnl_usd          = Column(Float, nullable=True)
    pnl_pct          = Column(Float, nullable=True)

    closed_price     = Column(Float, nullable=True)
    close_reason     = Column(String, nullable=True)
    # "take_profit" | "stop_loss" | "trader_sold" | "manual" | "daily_loss_limit"

    market_question  = Column(Text,   nullable=True)
    polymarket_url   = Column(String, nullable=True)

    opened_at        = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at        = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="copy_trades")
