from flask import Flask
from dotenv import load_dotenv
import os


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

    from .routes import bp as main_bp
    app.register_blueprint(main_bp)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ---- CLI commands ----
    import click
    from . import db as dbmod

    @app.cli.command("init-db")
    def init_db_cmd():
        """Create tables in SQLite (idempotent)."""
        dbmod.init_db()
        click.echo("DB initialized ✅")

    @app.cli.command("import-codes")
    @click.argument("csv_path")
    def import_codes_cmd(csv_path: str):
        """Import authorized codes from CSV (class_id,code4)."""
        res = dbmod.import_codes(Path(csv_path))
        click.echo(f"Imported: {res.inserted}, skipped duplicates: {res.skipped_duplicates}")

    @app.cli.command("seed-session")
    @click.option("--class", "class_id", required=True, help="Class id, e.g. 11C")
    @click.option("--start", "starts_at", required=True, help="Start ISO, e.g. 2025-09-23T10:00:00+02:00")
    @click.option("--end", "ends_at", required=True, help="End ISO, e.g. 2025-09-23T10:50:00+02:00")
    def seed_session_cmd(class_id: str, starts_at: str, ends_at: str):
        from .db import seed_session as _seed
        s = _seed(class_id, starts_at, ends_at)
        click.echo(f"Session created id={s.id} for class {s.class_id} ({s.starts_at} → {s.ends_at})")

    return app

from pathlib import Path  # noqa: E402  (used in CLI)
