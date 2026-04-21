"""
models/withdrawal_whitelist.py
------------------------------
רשימת כתובות מאושרות למשיכה.
המשתמש מוסיף כתובות מראש — וניתן למשוך רק אליהן.
"""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime, timezone


class WithdrawalWhitelist(Base):
    __tablename__ = "withdrawal_whitelist"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(String, ForeignKey("users.id"))
    address    = Column(String, nullable=False)
    label      = Column(String, nullable=True)    # "ארנק MetaMask הראשי"
    is_active  = Column(Boolean, default=True)
    added_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="withdrawal_whitelist")
