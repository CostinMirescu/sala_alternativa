import os, sys, sqlite3
# adaugă rădăcina proiectului în sys.path
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.db import get_connection

app = create_app()
with app.app_context():
    con = get_connection()
    cur = con.cursor()
    rows = cur.execute("PRAGMA database_list;").fetchall()
    # PRAGMA database_list => (seq, name, file)
    print("DB files found:")
    for r in rows:
        # merge și r[2], dar r['file'] e mai clar dacă row_factory e Row
        path = r["file"] if isinstance(r, sqlite3.Row) else r[2]
        print(" -", path or "(in-memory)")
    con.close()

