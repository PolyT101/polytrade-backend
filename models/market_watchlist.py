"""
models/market_watchlist.py
--------------------------
שמירת שאלות (markets) למעקב — נפרד מרשימת מעקב הטריידרים.
"""

from sqlalchemy import Column, String, Float, Boolean, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime, timezone


class MarketWatchlistEntry(Base):
    __tablename__ = "market_watchlist"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    user_id           = Column(String, ForeignKey("users.id"))

    # זיהוי השאלה בפולימארקט
    condition_id      = Column(String, nullable=False)   # מזהה ייחודי
    slug              = Column(String, nullable=True)    # לבניית ה-URL
    question          = Column(Text,   nullable=False)   # טקסט השאלה

    # נתוני שוק שנשמרו
    category          = Column(String, nullable=True)    # פוליטיקה / קריפטו / ספורט...
    yes_price         = Column(Float,  nullable=True)    # מחיר YES בעת השמירה
    no_price          = Column(Float,  nullable=True)    # מחיר NO
    volume            = Column(Float,  nullable=True)    # נזילות כוללת
    end_date          = Column(DateTime, nullable=True)  # תאריך סגירה
    days_remaining    = Column(Integer, nullable=True)

    # תווית מאיפה נשמר
    source_page       = Column(String, nullable=True)
    # "sniper" | "safe_profit" | "whales" | "smart_money" | "scanner" | "manual"

    polymarket_url    = Column(String, nullable=True)    # קישור ישיר לשאלה

    added_at          = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="market_watchlist")
