# scripts/diag.py
from app import create_app
from app.db import get_connection
from app.utils import parse_iso
from datetime import datetime, timedelta

app = create_app()
with app.app_context():
    print("TZ:", app.config.get("TZ"))
    print("DATABASE_URL:", app.config.get("DATABASE_URL"))
    print("DATABASE_PATH:", app.config.get("DATABASE_PATH"))
    conn = get_connection(); cur = conn.cursor()

    cur.execute("PRAGMA database_list;")
    print("sqlite files:", [tuple(r) for r in cur.fetchall()])

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1;")
    print("tables:", [r[0] for r in cur.fetchall()])

    cur.execute("SELECT id,class_id,starts_at,ends_at FROM session ORDER BY id DESC LIMIT 5;")
    rows = cur.fetchall()
    print("last sessions:", [dict(r) for r in rows])

    if rows:
        s = rows[0]
        st = parse_iso(s["starts_at"]); en = parse_iso(s["ends_at"])
        now = datetime.now(tz=app.config["TZ"])
        phase = "start" if now < (en - timedelta(minutes=5)) else "end"
        print("latest session phase:", phase, "now:", now.isoformat())

print("OK: diag done.")
