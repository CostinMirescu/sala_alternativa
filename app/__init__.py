from flask import Flask
from dotenv import load_dotenv
import os
from zoneinfo import ZoneInfo
from flask.cli import with_appcontext

def create_app():
    load_dotenv()
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )

    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")
    app.config["TIMEZONE"] = os.getenv("TIMEZONE", "Europe/Bucharest")
    app.config["DATABASE_URL"] = os.getenv("DATABASE_URL", "sqlite:///instance/sala.db")
    app.config["TZ"] = ZoneInfo(app.config["TIMEZONE"])
    app.config["QR_SALT"] = os.getenv("QR_SALT", "qr-signing-v1")
    app.config["QR_MAX_AGE"] = int(os.getenv("QR_MAX_AGE", "900"))  # secunde

    from .routes import bp as main_bp
    app.register_blueprint(main_bp)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ---- CLI commands ----
    import click
    from . import db as dbmod

    @app.cli.command("init-db")
    @with_appcontext
    def init_db_cmd():
        """Create tables in SQLite (idempotent)."""
        dbmod.init_db()
        click.echo("DB initialized ✅")

    @app.cli.command("import-codes")
    @with_appcontext
    @click.argument("csv_path")
    def import_codes_cmd(csv_path: str):
        """Import authorized codes from CSV (class_id,code4)."""
        res = dbmod.import_codes(Path(csv_path))
        click.echo(f"Imported: {res.inserted}, skipped duplicates: {res.skipped_duplicates}")

    @app.cli.command("seed-session")
    @with_appcontext
    @click.option("--class", "class_id", required=True, help="Class id, e.g. 11C")
    @click.option("--start", "starts_at", required=True, help="Start ISO, e.g. 2025-09-23T10:00:00+02:00")
    @click.option("--end", "ends_at", required=True, help="End ISO, e.g. 2025-09-23T10:50:00+02:00")
    def seed_session_cmd(class_id: str, starts_at: str, ends_at: str):
        from .db import seed_session as _seed
        s = _seed(class_id, starts_at, ends_at)
        click.echo(f"Session created id={s.id} for class {s.class_id} ({s.starts_at} → {s.ends_at})")

    @app.cli.command("seed-now")
    @with_appcontext
    @click.option("--class", "class_id", required=True)
    @click.option("--minutes-ago", "minutes_ago", default=2, type=int,
                  help="Câte minute în urmă să fie startul (default 2)")
    @click.option("--duration", "duration_min", default=50, type=int,
                  help="Durata în minute (default 50)")
    def seed_now_cmd(class_id: str, minutes_ago: int, duration_min: int):
        from datetime import datetime, timedelta
        from .db import seed_session as _seed
        tz = app.config["TZ"]
        start = datetime.now(tz) - timedelta(minutes=minutes_ago)
        end = start + timedelta(minutes=duration_min)
        iso = "%Y-%m-%dT%H:%M:%S%z"
        s = _seed(class_id, start.strftime(iso), end.strftime(iso))
        click.echo(f"Session created id={s.id} for class {class_id} ({s.starts_at} → {s.ends_at})")

    return app


from itsdangerous import URLSafeTimedSerializer


def get_qr_serializer(app):
    return URLSafeTimedSerializer(secret_key=app.config["SECRET_KEY"],
                                  salt=app.config["QR_SALT"])


from pathlib import Path  # noqa: E402  (used in CLI)
