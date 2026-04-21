"""
trading_service.py
------------------
ביצוע עסקאות אמיתיות / דמו דרך py-clob-client.
כל משתמש מקבל ארנק ייעודי (custodial) שהשרת מנהל.
"""

import os
from decimal import Decimal
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = POLYGON  # 137


def get_client(private_key: str, funder_address: str) -> ClobClient:
    """
    יוצר ClobClient מוכן לביצוע עסקאות עבור ארנק ספציפי.
    """
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=0,   # EOA — ארנק רגיל עם private key
        funder=funder_address,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def place_order(
    private_key: str,
    funder_address: str,
    token_id: str,
    side: str,          # "BUY" או "SELL"
    price: float,       # מחיר בין 0 ל-1 (למשל 0.65)
    size: float,        # כמות מניות
    is_demo: bool = False,
) -> dict:
    """
    מבצע עסקה אמיתית או מחזיר סימולציה (דמו).

    במצב דמו:
    - לא מתחבר לבלוקצ'יין
    - מחזיר תוצאה מדומה לצורך בחינת הטריידר

    במצב אמיתי:
    - מבצע order אמיתי דרך CLOB API
    - מחייב שה-funder_address מחזיק מספיק USDC על Polygon
    """
    if is_demo:
        # סימולציה — מחזירים תשובה כאילו העסקה בוצעה
        return {
            "demo":      True,
            "status":    "simulated",
            "token_id":  token_id,
            "side":      side,
            "price":     price,
            "size":      size,
            "cost_usdc": round(price * size, 2),
            "order_id":  f"DEMO-{token_id[:8]}-{side}",
        }

    # עסקה אמיתית
    client = get_client(private_key, funder_address)

    from py_clob_client.clob_types import Side
    order_side = Side.BUY if side.upper() == "BUY" else Side.SELL

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=order_side,
    )

    resp = client.create_and_post_order(order_args)
    return {
        "demo":     False,
        "status":   "placed",
        "order_id": resp.get("orderID"),
        "token_id": token_id,
        "side":     side,
        "price":    price,
        "size":     size,
    }


def cancel_order(private_key: str, funder_address: str, order_id: str) -> dict:
    """
    מבטל פקודה פתוחה לפי order_id.
    """
    client = get_client(private_key, funder_address)
    result = client.cancel(order_id)
    return {"cancelled": True, "order_id": order_id, "result": result}


def get_open_orders(private_key: str, funder_address: str) -> list[dict]:
    """
    מחזיר את כל הפקודות הפתוחות של הארנק.
    """
    client = get_client(private_key, funder_address)
    return client.get_orders()


def get_balance(private_key: str, funder_address: str) -> dict:
    """
    מחזיר את יתרת ה-USDC בארנק.
    """
    client = get_client(private_key, funder_address)
    balance = client.get_balance()
    return {"address": funder_address, "usdc_balance": balance}
