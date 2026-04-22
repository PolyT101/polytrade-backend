"""
models/copy_trade.py  — צור קובץ חדש בתיקיית models/
"""
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func
from db import Base

class CopyTrade(Base):
    __tablename__ = "copy_trades"

    id              = Column(Integer, primary_key=True, index=True)
    copy_setting_id = Column(Integer, nullable=True)
    user_id         = Column(Integer, ForeignKey("users.id"))
    trader_address  = Column(String)
    condition_id    = Column(String)
    side            = Column(String)       # BUY / SELL
    size            = Column(Float)        # גודל ב-USDC
    price           = Column(Float)        # מחיר 0-1
    status          = Column(String)       # demo / pending / executed / failed
    is_demo         = Column(Boolean, default=True)
    trader_tx       = Column(String, nullable=True)
    our_tx          = Column(String, nullable=True)
    market_title    = Column(String, nullable=True)
    error_msg       = Column(String, nullable=True)
    created_at      = Column(DateTime, server_default=func.now())
