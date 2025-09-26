# app/utils.py
from datetime import datetime
from itsdangerous import URLSafeSerializer

def get_qr_serializer(app):
    # Folosește SECRET_KEY deja setat în config
    secret = app.config["SECRET_KEY"]
    return URLSafeSerializer(secret, salt="qr")

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
