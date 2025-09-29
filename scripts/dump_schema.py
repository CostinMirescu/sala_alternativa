# scripts/dump_schema.py
from app import create_app
from app.db import get_connection

app = create_app()
with app.app_context():
    c = get_connection().cursor()
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name;")
    sql = "\n\n".join(r[0] for r in c.fetchall() if r[0])
    open("docs/schema.sql", "w", encoding="utf-8").write(sql)
print("Schema salvată în docs/schema.sql")

