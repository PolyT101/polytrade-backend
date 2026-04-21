# Polymarket CopyTrade — Backend

## מבנה הפרויקט

```
backend/
├── main.py                          # נקודת כניסה — FastAPI app
├── db.py                            # חיבור DB
├── requirements.txt
├── railway.toml                     # דפלוי ל-Railway
├── .env.example                     # משתני סביבה לדוגמה
├── routers/
│   ├── traders.py                   # GET טריידרים מפולימארקט
│   ├── copy.py                      # start/stop קופי
│   └── portfolio.py                 # פורטפוליו + מכירה ידנית
├── services/
│   ├── polymarket_service.py        # קריאות ל-Polymarket API
│   ├── trading_service.py           # ביצוע עסקאות אמיתיות/דמו
│   └── copy_engine.py               # מנוע הקופי האוטומטי
└── models/
    └── copy_settings.py             # User, CopySettings, CopyTrade
```

---

## התקנה מקומית

```bash
cd backend
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # מלא את .env בערכים שלך
uvicorn main:app --reload
```

API docs זמינים ב: http://localhost:8000/docs

---

## דפלוי ל-Railway

1. פתח חשבון ב-https://railway.app (חינם)
2. לחץ "New Project" → "Deploy from GitHub repo"
3. בחר את ה-repo שלך
4. הוסף משתני סביבה:
   - `DATABASE_URL` — Railway יוצר PostgreSQL אוטומטי, העתק את ה-URL
   - `ENCRYPTION_KEY` — מחרוזת אקראית של 32 תווים
5. Railway יפעיל אוטומטית לפי railway.toml

---

## API Endpoints

| Method | Path | תיאור |
|--------|------|--------|
| GET | /api/traders | רשימת טריידרים רווחיים |
| GET | /api/traders/{addr} | פרופיל טריידר מלא |
| GET | /api/traders/{addr}/positions | פוזיציות פתוחות/סגורות |
| GET | /api/traders/{addr}/history | היסטוריית רווח לגרף |
| POST | /api/copy/start | התחל קופי |
| POST | /api/copy/stop/{id} | עצור קופי |
| POST | /api/copy/resume/{id} | חזור לקופי |
| GET | /api/copy/settings/{user_id} | הגדרות קופי |
| GET | /api/portfolio/{user_id} | פורטפוליו מלא |
| GET | /api/portfolio/{user_id}/{addr}/open | עסקאות פתוחות |
| GET | /api/portfolio/{user_id}/{addr}/closed | עסקאות סגורות |
| POST | /api/portfolio/sell/{trade_id} | מכור עסקה ידנית |

---

## הצעדים הבאים

- [ ] חיבור הפרונטאנד ל-API האמיתי (החלף mock data)
- [ ] הוסף אימות משתמשים (JWT)
- [ ] הצפן private keys עם AES (encryption_service.py)
- [ ] הפעל WebSocket לעדכונים בזמן אמת
- [ ] הוסף ניטור ו-alerts (Telegram bot?)
