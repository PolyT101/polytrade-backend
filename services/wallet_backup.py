"""
services/wallet_backup.py
--------------------------
הצפנה ופענוח של גיבוי ארנקים.

אלגוריתם:
  - PBKDF2-SHA256  × 200,000 iterations  → מפתח AES 256-bit
  - AES-256-GCM   (authenticated encryption) → הצפנה + אימות שלמות
  - salt 32 bytes אקראי לכל גיבוי → מניע brute-force של rainbow tables

כך שאפילו מי שיש לו את הקוד המלא מ-GitHub + קובץ הגיבוי
לא יוכל לפצח ללא הסיסמה (brute-force × 200K iterations per attempt).
"""

import json
import base64
import secrets
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

PBKDF2_ITERATIONS = 200_000   # ~0.5 sec per attempt on modern CPU
KEY_LENGTH        = 32         # 256-bit AES key
SALT_LENGTH       = 32         # 256-bit salt
NONCE_LENGTH      = 12         # 96-bit GCM nonce (standard)
BACKUP_VERSION    = 2


# ------------------------------------------------------------------ #
#  Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-SHA256: password + salt → 256-bit AES key."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


# ------------------------------------------------------------------ #
#  Public API                                                          #
# ------------------------------------------------------------------ #

def encrypt_backup(wallets: list[dict], password: str) -> dict:
    """
    מקבל רשימת ארנקים (עם private_key בטקסט גלוי) + סיסמת משתמש.
    מחזיר dict מוצפן שבטוח לשמירה / הורדה.

    הפלט מכיל רק: version, algorithm, iterations, salt, iv, data (כולם base64/str).
    אין שום מידע רגיש בפלט.
    """
    salt  = secrets.token_bytes(SALT_LENGTH)
    nonce = secrets.token_bytes(NONCE_LENGTH)
    key   = _derive_key(password, salt)

    plaintext  = json.dumps(wallets, ensure_ascii=False).encode("utf-8")
    aesgcm     = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)   # GCM tag appended automatically

    return {
        "version":    BACKUP_VERSION,
        "algorithm":  "AES-256-GCM + PBKDF2-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "salt":       base64.b64encode(salt).decode(),
        "iv":         base64.b64encode(nonce).decode(),
        "data":       base64.b64encode(ciphertext).decode(),
    }


def decrypt_backup(backup: dict, password: str) -> list[dict]:
    """
    מקבל dict מוצפן + סיסמה.
    מחזיר רשימת ארנקים.
    זורק InvalidTag אם הסיסמה שגויה או הקובץ פגום (authenticated encryption).
    """
    try:
        salt       = base64.b64decode(backup["salt"])
        nonce      = base64.b64decode(backup["iv"])
        ciphertext = base64.b64decode(backup["data"])
    except (KeyError, Exception) as e:
        raise ValueError(f"קובץ גיבוי לא תקין: {e}")

    key    = _derive_key(password, salt)
    aesgcm = AESGCM(key)

    # InvalidTag is raised automatically if password wrong or data tampered
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))
