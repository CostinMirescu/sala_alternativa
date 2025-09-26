from flask import Blueprint, render_template, request, redirect, url_for, session, g, current_app, send_file
from werkzeug.security import check_password_hash


from .auth import login_required
from .db import get_connection
from .routes import _parse_iso  # reutilizăm helperul tău
from .reporting import fetch_report_data
from datetime import datetime, timedelta
from io import StringIO, BytesIO
import csv, zipfile


bp = Blueprint("dirig", __name__, url_prefix="/diriginti")

def week_bounds_now(tz):
    now = datetime.now(tz)
    start = (now - timedelta(days= (now.weekday()))).replace(hour=0,minute=0,second=0,microsecond=0)
    end   = start + timedelta(days=7) - timedelta(seconds=1)
    return start, end

@bp.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT id,email,password_hash,class_id FROM teacher WHERE email=?", (email,))
        row = cur.fetchone(); conn.close()
        if row and check_password_hash(row["password_hash"], password):
            session["teacher_id"] = row["id"]
            return redirect(url_for("dirig.raport"))
        return render_template("dirig_login.html", error="Credențiale invalide")
    return render_template("dirig_login.html")

@bp.route("/logout")
def logout():
    session.pop("teacher_id", None)
    return redirect(url_for("dirig.login"))

@bp.route("/raport")
@login_required
def raport():
    tz = current_app.config["TZ"]
    # interval
    dfrom = request.args.get("from"); dto = request.args.get("to")
    if dfrom and dto:
        start = tz.localize(datetime.strptime(dfrom, "%Y-%m-%d"))
        end   = tz.localize(datetime.strptime(dto,   "%Y-%m-%d")) + timedelta(days=1) - timedelta(seconds=1)
    else:
        start, end = week_bounds_now(tz)

    class_id = g.teacher["class_id"]
    detail_rows, summary_rows, attempts = fetch_report_data(class_id, start, end, tz)

    return render_template("dirig_raport.html",
                           class_id=class_id, start=start.date(), end=end.date(),
                           detail=detail_rows, summary=summary_rows, attempts=attempts
                           )


@bp.route("/export")
@login_required
def export_zip():
    tz = current_app.config["TZ"]
    dfrom = request.args.get("from"); dto = request.args.get("to")
    if dfrom and dto:
        start = tz.localize(datetime.strptime(dfrom, "%Y-%m-%d"))
        end   = tz.localize(datetime.strptime(dto,   "%Y-%m-%d")) + timedelta(days=1) - timedelta(seconds=1)
    else:
        start, end = week_bounds_now(tz)

    class_id = g.teacher["class_id"]
    detail_rows, summary_rows, attempts = fetch_report_data(class_id, start, end, tz)

    # build ZIP with 3 CSVs
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def write_csv(name, rows, header):
            sio = StringIO(); w = csv.DictWriter(sio, fieldnames=header)
            w.writeheader()
            for r in rows: w.writerow(r)
            z.writestr(name, sio.getvalue())

        write_csv("attendance.csv", detail_rows,
                  ["data","incepe","se_termina","clasa","sesiune_id","cod4",
                   "status_final","check_in_at","check_out_at","status_checkin","status_checkout"])

        write_csv("summary.csv", summary_rows,
                  ["sesiune_id","data","incepe","se_termina","prezenti","intarziati","plecati","neconfirmat","rata_conformare"])

        write_csv("attempts.csv", attempts,
                  ["ts","device_id","cod4","success","reason","ip","ua"])

    buf.seek(0)
    fname = f"raport_{class_id}_{start.date()}_{end.date()}.zip"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/zip")


