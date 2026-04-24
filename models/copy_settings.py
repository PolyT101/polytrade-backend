"""
models/copy_settings.py — v3
הוספת CopyEngineState לשמירת watermark
"""
from sqlalchemy import Column, String, Float, Boolean, Integer, DateTime, ForeignKey, Text, BigInteger
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime, timezone


class User(Base):
    __tablename__ = "users"

    id                    = Column(String, primary_key=True)
    email                 = Column(String, unique=True, nullable=True)
    main_wallet_address   = Column(String, unique=True, nullable=True)
    password_hash         = Column(String, nullable=True)
    created_at            = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    copy_settings        = relationship("CopySettings",      back_populates="user")
    copy_trades          = relationship("CopyTrade",          back_populates="user")
    watchlist            = relationship("WatchlistEntry",     back_populates="user")
    market_watchlist     = relationship("MarketWatchlistEntry", back_populates="user")
    wallets              = relationship("Wallet",             back_populates="user")
    withdrawal_whitelist = relationship("WithdrawalWhitelist", back_populates="user")


class CopySettings(Base):
    __tablename__ = "copy_settings"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    user_id              = Column(String, ForeignKey("users.id"))
    trader_address       = Column(String)
    trader_name          = Column(String)
    is_active            = Column(Boolean, default=True)

    # ארנק
    wallet_address       = Column(String, nullable=True)
    encrypted_wallet_key = Column(Text,   nullable=True)

    # כניסה
    entry_mode           = Column(String, default="fixed")   # fixed | percent
    entry_amount         = Column(Float,  default=10.0)

    # הגנות
    take_profit_pct      = Column(Float,  nullable=True)
    stop_loss_pct        = Column(Float,  nullable=True)
    max_daily_trades     = Column(Integer, nullable=True)
    max_daily_loss_usd   = Column(Float,  nullable=True)

    # מכירה
    sell_mode            = Column(String, default="mirror")  # mirror | fixed | manual | sell_all

    # מטא
    created_at           = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at           = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                                  onupdate=lambda: datetime.now(timezone.utc))

    user        = relationship("User",         back_populates="copy_settings")
    engine_state = relationship("CopyEngineState", back_populates="setting",
                                uselist=False, cascade="all, delete-orphan")


class CopyEngineState(Base):
    """שמירת watermark לכל copy setting — לא מתאפס ב-Redeploy."""
    __tablename__ = "copy_engine_state"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    setting_id   = Column(Integer, ForeignKey("copy_settings.id"), unique=True)
    last_seen_ts = Column(BigInteger, default=0)  # Unix timestamp של העסקה האחרונה
    updated_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))

    setting = relationship("CopySettings", back_populates="engine_state")


class CopyTrade(Base):
    __tablename__ = "copy_trades"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    user_id            = Column(String, ForeignKey("users.id"))
    copy_settings_id   = Column(Integer, ForeignKey("copy_settings.id"), nullable=True)
    trader_address     = Column(String)
    market_id          = Column(String)
    market_question    = Column(String, nullable=True)
    side               = Column(String)   # YES | NO
    amount_usdc        = Column(Float)
    price_entry        = Column(Float, nullable=True)
    price_exit         = Column(Float, nullable=True)
    pnl_usd            = Column(Float, nullable=True)
    status             = Column(String, default="demo")  # demo | open | closed | cancelled
    tx_hash            = Column(String, nullable=True)
    opened_at          = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at          = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="copy_trades")
