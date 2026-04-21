"""
routers/smart_money.py
-----------------------
GET /api/smart-money/activity  — פעילות כסף חכם / לוויתנים / מידע פנים
GET /api/smart-money/activity?type=smart_money
GET /api/smart-money/activity?type=whale
GET /api/smart-money/activity?type=insider
"""

from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from services.smart_money import get_smart_money_activity

router = APIRouter()


@router.get("/activity")
async def smart_money_activity(
    trader_type: Optional[str] = Query(None),
    # None = הכל | "smart_money" | "whale" | "insider"
    limit: int = Query(60, ge=1, le=150),
):
    """
    מחזיר פעילות של משקיעים חריגים.
    אפשר לסנן לפי סוג: smart_money / whale / insider
    """
    try:
        results = await get_smart_money_activity(limit=limit)
        if trader_type:
            results = [r for r in results if r["trader_type"] == trader_type]
        return results
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
