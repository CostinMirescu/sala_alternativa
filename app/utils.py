# app/utils.py
from datetime import datetime, timedelta
from itsdangerous import URLSafeTimedSerializer

def get_qr_serializer(app):
    # Folosește SECRET_KEY deja setat în config
    secret = app.config["SECRET_KEY"]
    return URLSafeTimedSerializer(secret, salt="qr")

def parse_iso(s: str) -> datetime:
    """
    Acceptă ISO cu offset cu sau fără “:”, ex:
    2025-09-26T10:05:00+03:00 sau 2025-09-26T10:05:00+0300
    Returnează datetime timezone-aware.
    """
    try:
        return datetime.fromisoformat(s)  # suportă +03:00
    except ValueError:
        if s and len(s) >= 5 and s[-3] == ':' and s[-6] in ('+', '-'):
            s = s[:-3] + s[-2:]           # transformă +03:00 -> +0300
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")

def parse_date_yyyy_mm_dd(value: str, tz):
    """'2025-09-26' -> datetime aware (00:00:00, TZ)"""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)

def inclusive_end_of_day(d: datetime):
    """primește un datetime aware la 00:00 și întoarce 23:59:59 pentru aceeași zi"""
    return d + timedelta(days=1) - timedelta(seconds=1)


def _hms(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        return parse_iso(iso_str).strftime("%H:%M:%S")
    except Exception:
        return ""  # fallback safe


def format_ts_local(iso_str: str, tz) -> str:
    """ISO with offset → 'YYYY-MM-DD HH:MM:SS' in given TZ"""
    if not iso_str:
        return ""
    try:
        dt = parse_iso(iso_str).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str  # fallback: lasă cum e
