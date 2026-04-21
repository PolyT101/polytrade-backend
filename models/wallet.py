"""
models/wallet.py
----------------
מודל DB לניהול ארנקים.

כל משתמש יכול להחזיק מספר ארנקים:
- ארנק "ברירת מחדל" — אחד בלבד, מסומן is_default=True
- ארנקים ייעודיים לקופי — נוצרים אוטומטית לכל קופי חדש (אם בוחרים)
- ניתן להעביר USDC בין כל ארנק לאחר
"""

from sqlalchemy import Column, String, Float, Boolean, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime, timezone


class Wallet(Base):
    __tablename__ = "wallets"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    user_id               = Column(String, ForeignKey("users.id"))
    address               = Column(String, unique=True, nullable=False)
    encrypted_private_key = Column(Text, nullable=False)

    # שם תצוגה שהמשתמש יכול לשנות
    label                 = Column(String, default="ארנק חדש")

    # האם זה ארנק ברירת המחדל?
    is_default            = Column(Boolean, default=False)

    # יתרה — מתעדכנת בכל polling (לא תמיד realtime)
    cached_usdc_balance   = Column(Float, default=0.0)
    cached_matic_balance  = Column(Float, default=0.0)
    balance_updated_at    = Column(DateTime, nullable=True)

    created_at            = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="wallets")

    @property
    def decrypted_private_key(self) -> str:
        from services.wallet_service import decrypt_private_key
        return decrypt_private_key(self.encrypted_private_key)

    def refresh_balance(self):
        """מרענן יתרה מהבלוקצ'יין ומעדכן cache."""
        from services.wallet_service import get_usdc_balance, get_matic_balance
        self.cached_usdc_balance  = get_usdc_balance(self.address)
        self.cached_matic_balance = get_matic_balance(self.address)
        self.balance_updated_at   = datetime.now(timezone.utc)
