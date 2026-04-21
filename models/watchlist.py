"""
models/watchlist.py
-------------------
טריידרים שמורים למעקב — ללא קופי אוטומטי.
"""

from sqlalchemy import Column, String, Float, Boolean, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime, timezone


class WatchlistEntry(Base):
    __tablename__ = "watchlist"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_id          = Column(String, ForeignKey("users.id"))

    trader_address   = Column(String, nullable=False)
    trader_name      = Column(String, nullable=False)

    # נתונים שנשמרו בעת ההוספה — מתעדכנים בכל בדיקה
    pnl              = Column(String, nullable=True)    # "+$5.77M"
    roi              = Column(String, nullable=True)    # "+1.7%"
    win_rate         = Column(String, nullable=True)
    trades_count     = Column(Integer, nullable=True)
    style            = Column(String, nullable=True)    # "Active Trader" / "Whale"

    # האם קופי פעיל ממנו כרגע?
    # (מחושב dynamically — לא נשמר כאן, נמשך מ-CopySettings)

    notes            = Column(Text, nullable=True)      # הערות אישיות
    added_at         = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_checked_at  = Column(DateTime, nullable=True)  # מתי בדקנו לאחרונה

    user = relationship("User", back_populates="watchlist")
