from flask import Flask, current_app
from dotenv import load_dotenv
import os
from zoneinfo import ZoneInfo
from flask.cli import with_appcontext
from .auth import load_current_teacher
from .dirig import bp as dirig_bp
from datetime import datetime, timedelta
from .db import get_connection
import csv
from .utils import aware_from_hhmm


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

    app.config["CHECKIN_OPEN_MIN_BEFORE"] = int(os.getenv("CHECKIN_OPEN_MIN_BEFORE", "5"))
    app.config["CHECKIN_CLOSE_MIN_AFTER"] = int(os.getenv("CHECKIN_CLOSE_MIN_AFTER", "10"))
    app.config["CHECKOUT_OPEN_MIN_BEFORE_END"] = int(os.getenv("CHECKOUT_OPEN_MIN_BEFORE_END", "5"))
    app.config["CHECKOUT_GRACE_MIN_AFTER_END"] = int(os.getenv("CHECKOUT_GRACE_MIN_AFTER_END", "5"))
    app.config["SESSION_LENGTH_MIN"] = int(os.getenv("SESSION_LENGTH_MIN", "50"))

    app.config.setdefault("AUTO_SESSIONS_ENABLED", os.getenv("AUTO_SESSIONS_ENABLED", "false").lower() == "true")

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

        # Ignorăm param. duration_min și folosim din config (sursa unică)
        duration_min = app.config["SESSION_LENGTH_MIN"]
        end = start + timedelta(minutes=duration_min)

        iso = "%Y-%m-%dT%H:%M:%S%z"
        s = _seed(class_id, start.strftime(iso), end.strftime(iso))
        click.echo(f"Session created id={s.id} for class {class_id} ({s.starts_at} → {s.ends_at})")

    from werkzeug.security import generate_password_hash

    @app.cli.command("create-teacher")
    @with_appcontext
    @click.option("--email", required=True)
    @click.option("--class", "class_id", required=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_teacher_cmd(email, class_id, password):

        from datetime import datetime
        conn = get_connection();
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO teacher(email,password_hash,class_id,created_at) VALUES (?,?,?,?)",
                    (email, generate_password_hash(password), class_id,
                     datetime.now(app.config["TZ"]).strftime("%Y-%m-%dT%H:%M:%S%z")))
        conn.commit();
        conn.close()
        click.echo(f"OK: teacher {email} for class {class_id}")

    @app.cli.command("seed-periods")
    @with_appcontext
    def seed_periods_cmd():
        """Inserează cele 7 sloturi orare."""
        slots = [
            (1, "08:00"),
            (2, "09:00"),
            (3, "10:00"),
            (4, "11:10"),
            (5, "12:10"),
            (6, "13:10"),
            (7, "14:10"),
        ]
        conn = get_connection();
        cur = conn.cursor()
        for no, hhmm in slots:
            cur.execute("INSERT OR REPLACE INTO period(period_no, start_hhmm) VALUES(?,?)", (no, hhmm))
        conn.commit();
        conn.close()
        click.echo("OK: periods seeded")

    @app.cli.command("import-schedule")
    @with_appcontext
    @click.argument("csv_path")
    def import_schedule_cmd(csv_path):
        """
        Importă orarul în tabelul `schedule`.
        Acceptă CSV cu sau fără header.
        Coloane (în această ordine dacă nu există header):
          weekday,period_no,class_id
        """
        import io, csv
        conn = get_connection();
        cur = conn.cursor()
        inserted = 0;
        replaced = 0;
        skipped = 0

        # Deschidem cu utf-8-sig ca să mâncăm BOM dacă există
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            data = f.read()

        # Detectăm dialectul (delimiter etc.), fallback la virgulă
        try:
            dialect = csv.Sniffer().sniff(data.splitlines()[0] if data else ",")
        except Exception:
            dialect = csv.excel
            dialect.delimiter = ','

        buf = io.StringIO(data)
        # Peek la prima linie
        first_line = buf.readline()
        has_header = False
        try:
            has_header = csv.Sniffer().has_header(first_line + "\n")
        except Exception:
            pass
        buf.seek(0)

        if has_header:
            reader = csv.DictReader(buf, dialect=dialect)
        else:
            reader = csv.reader(buf, dialect=dialect)

        def parse_row(row):
            if isinstance(row, dict):
                # normalizează cheile (lower/trim)
                keys = {k.strip().lower(): v for k, v in row.items()}
                try:
                    wd = int(str(keys.get("weekday", "")).strip())
                    per = int(str(keys.get("period_no", "")).strip())
                    cls = str(keys.get("class_id", "")).strip()
                except Exception:
                    raise ValueError("Row invalid (nu pot converti câmpuri).")
            else:
                if len(row) < 3:
                    raise ValueError("Row invalid (mai puțin de 3 coloane).")
                wd, per, cls = int(str(row[0]).strip()), int(str(row[1]).strip()), str(row[2]).strip()
            return wd, per, cls

        for raw in reader:
            try:
                wd, per, cls = parse_row(raw)
                # Validări simple
                if wd < 1 or wd > 5 or per < 1 or per > 7 or not cls:
                    skipped += 1
                    continue
                cur.execute("INSERT OR REPLACE INTO schedule(weekday, period_no, class_id) VALUES (?,?,?)",
                            (wd, per, cls))
                # INSERT OR REPLACE nu ne spune clar dacă a înlocuit; nuanța nu contează mult
                inserted += 1
            except Exception:
                skipped += 1
                continue

        conn.commit();
        conn.close()
        click.echo(f"Import schedule: ok={inserted}, skipped={skipped}")

    def _gen_session_for(class_id: str, date_obj, start_hhmm: str, tz):
        """Creează sesiune (dacă lipsește) pentru clasa dată, în ziua/ora dată."""
        starts = aware_from_hhmm(date_obj, start_hhmm, tz)
        ends = starts + timedelta(minutes=60)
        conn = get_connection();
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO session(class_id, starts_at, ends_at) VALUES (?,?,?)",
                        (class_id, starts.strftime("%Y-%m-%dT%H:%M:%S%z"), ends.strftime("%Y-%m-%dT%H:%M:%S%z")))
            conn.commit()
            sid = cur.lastrowid
        except Exception:
            # exista deja
            cur.execute("SELECT id FROM session WHERE class_id=? AND starts_at=?",
                        (class_id, starts.strftime("%Y-%m-%dT%H:%M:%S%z")))
            row = cur.fetchone();
            sid = row["id"] if row else None
        finally:
            conn.close()
        return sid

    @app.cli.command("gen-day")
    @with_appcontext
    @click.option("--date", "date_str", help="YYYY-MM-DD (default azi)")
    @click.option("--dry-run", is_flag=True, help="Nu inserează, doar afișează")
    def gen_day_cmd(date_str, dry_run):
        """Generează sesiunile pentru toate sloturile programate într-o zi (Lu–Vi)."""
        tz = current_app.config["TZ"]
        now = datetime.now(tz)
        date_obj = (datetime.strptime(date_str, "%Y-%m-%d") if date_str else now).date()
        weekday = (date_obj.weekday() + 1)  # 1..7 (1=Luni)
        if weekday > 5:
            click.echo("Zi nelucrătoare (Sa/Du) – nimic de generat.");
            return

        conn = get_connection();
        cur = conn.cursor()
        cur.execute("SELECT period_no, start_hhmm FROM period ORDER BY period_no")
        periods = cur.fetchall()
        cur.execute("SELECT weekday, period_no, class_id FROM schedule WHERE weekday=? ORDER BY period_no", (weekday,))
        sched = {(r["period_no"]): r["class_id"] for r in cur.fetchall()}
        conn.close()

        created = 0
        for p in periods:
            cls = sched.get(p["period_no"])
            if not cls: continue
            if dry_run:
                click.echo(f"would create: {date_obj} {p['start_hhmm']} class {cls}")
            else:
                sid = _gen_session_for(cls, date_obj, p["start_hhmm"], tz)
                if sid: created += 1
        click.echo(f"Done. sessions created or already present: {created}")

    @app.before_request
    def _load_teacher():
        load_current_teacher()

    app.register_blueprint(dirig_bp)

    return app



from pathlib import Path  # noqa: E402  (used in CLI)
