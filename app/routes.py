from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for
from datetime import datetime, timezone, timedelta

from . import get_qr_serializer
from .db import get_connection, _hash_code
from flask import current_app
from flask import jsonify
from flask import send_file, current_app
from io import BytesIO
import qrcode
from itsdangerous import BadSignature, SignatureExpired


bp = Blueprint("main", __name__)

@bp.get("/")
def home():
    return render_template("index.html", title="Sala Alternativă")

# --- Helpers ---

def _parse_iso(s: str) -> datetime:
    # Acceptă "2025-09-23T10:00:00+02:00" sau "2025-09-23T10:00:00+0200"
    if s.endswith(('+01:00','+02:00','+03:00','+00:00','-01:00','-02:00')):
        s = s[:-3] + s[-2:]
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def _status_for(delta_seconds: int) -> str | None:
    if 0 <= delta_seconds < 5*60:
        return "prezent"
    if 5*60 <= delta_seconds < 10*60:
        return "întârziat"
    return None  # expirat


def _checkout_allowed(now, ends_at):
    delta = (now - ends_at).total_seconds()
    if delta < -300: return "early"   # cu >5 min înainte de final
    if delta >  300: return "late"    # la >5 min după final
    return "ok"


# --- Monitor ---
@bp.get("/monitor")
def monitor():
    """Afișează lista codurilor pentru o sesiune + statusuri/contor.
    Parametri: session_id (obligatoriu)
    """
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return "Lipsește ?session_id=...", 400

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, class_id, starts_at, ends_at FROM session WHERE id=?", (session_id,))
    sess = cur.fetchone()
    if not sess:
        return "Sesiune inexistentă", 404

    class_id = sess["class_id"]
    starts_at = _parse_iso(sess["starts_at"])  # aware
    ends_at = _parse_iso(sess["ends_at"])
    now = datetime.now(tz=current_app.config["TZ"])
    delta = int((now - starts_at).total_seconds())

    phase = "start" if now < (ends_at - timedelta(minutes=5)) else "end"

    # coduri autorizate pentru clasă
    cur.execute("SELECT code4_hash FROM authorized_code WHERE class_id=? ORDER BY id", (class_id,))
    code_hashes = [r[0] for r in cur.fetchall()]

    # attendance curent pentru sesiune
    cur.execute(
        "SELECT code4_hash, code4_plain FROM authorized_code WHERE class_id=? ORDER BY id",
        (class_id,),
    )
    rows = cur.fetchall()

    cur.execute("SELECT code4_hash, status FROM attendance WHERE session_id=?", (session_id,))
    status_map = {row[0]: row[1] for row in cur.fetchall()}

    codes_ui = []
    for r in rows:
        h = r[0]
        code4 = (r[1] or "").strip() or "????"  # fallback dacă nu e populat încă
        st = status_map.get(h, "neconfirmat")
        codes_ui.append({"code4": code4, "status": st})

    present_count = sum(1 for c in codes_ui if c["status"] in ("prezent","întârziat"))
    left_count = sum(1 for c in codes_ui if c["status"] == "plecat")

    # etichetă fereastră
    if delta < 0:
        window_label = "nu a început"
    elif delta < 5*60:
        window_label = "verde (0–5 min)"
    elif delta < 10*60:
        window_label = "galben (5–10 min)"
    else:
        window_label = "expirat (>10 min)"

    # ora curentă pentru header (server-side)
    ora_curenta = now.strftime("%H:%M")

    phase = "start" if now < (ends_at - timedelta(minutes=5)) else "end"

    from app import get_qr_serializer  # dacă l-ai pus în __init__.py
    s = get_qr_serializer(current_app)
    qr_token = s.dumps({"session_id": session_id, "phase": phase})
    qr_title = "Cod de început de oră" if phase == "start" else "Cod de final de oră"
    print(codes_ui)
    return render_template(
        "monitor.html",
        class_id=class_id,
        session_id=session_id,
        ora_curenta=ora_curenta,
        window_label=window_label,
        present_count=present_count,
        total=len(codes_ui),
        codes=codes_ui,
        qr_token=qr_token,
        qr_title=qr_title,
        left_count=left_count,
        phase=phase
    )


# --- Elev ---
@bp.route("/elev", methods=["GET", "POST"])
def elev():
    # 1) culegem parametrii atât din query, cât și din POST
    session_id = request.args.get("session_id", type=int) or request.form.get("session_id", type=int)
    token = request.args.get("token") or request.form.get("token")
    token_phase = None
    message = None
    status_final = None

    if token:
        s = get_qr_serializer(current_app)
        try:
            data = s.loads(token, max_age=current_app.config["QR_MAX_AGE"])
        except SignatureExpired:
            return render_template("elev.html", session_id=session_id, message="Token expirat", status_final=None)
        except BadSignature:
            return render_template("elev.html", session_id=session_id, message="Token invalid", status_final=None)

        token_phase = data.get("phase")        # "start" sau "end"
        session_id = int(data.get("session_id") or 0) or session_id

    if not session_id:
        return render_template("elev.html", session_id=None, message="Lipsește ?session_id=... sau token", status_final=None)

    if request.method == "GET":
        return render_template("elev.html", session_id=session_id, message=None, status_final=None)

    # --- POST: preluăm codul de 4 cifre ---
    d1 = (request.form.get("d1") or "").strip()
    d2 = (request.form.get("d2") or "").strip()
    d3 = (request.form.get("d3") or "").strip()
    d4 = (request.form.get("d4") or "").strip()
    code4 = f"{d1}{d2}{d3}{d4}"

    if len(code4) != 4 or not code4.isdigit():
        return render_template("elev.html", session_id=session_id, message="Cod invalid — introdu exact 4 cifre", status_final=None)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, class_id, starts_at, ends_at FROM session WHERE id=?", (session_id,))
    sess = cur.fetchone()
    if not sess:
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Sesiune inexistentă", status_final=None)

    class_id = sess["class_id"]
    starts_at = _parse_iso(sess["starts_at"])
    ends_at   = _parse_iso(sess["ends_at"])

    now = datetime.now(tz=current_app.config["TZ"])
    delta = int((now - starts_at).total_seconds())

    # cod autorizat?
    code_hash = _hash_code(class_id, code4)
    cur.execute("SELECT 1 FROM authorized_code WHERE class_id=? AND code4_hash=?", (class_id, code_hash))
    if not cur.fetchone():
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Cod neautorizat pentru această clasă", status_final=None)

    # ===== CHECK-OUT (phase=end) =====
    if token_phase == "end":
        # fereastră de check-out: [-5m, +5m] față de ends_at
        win = _checkout_allowed(now, ends_at)
        if win == "early":
            conn.close()
            return render_template("elev.html", session_id=session_id, message="Check-out disponibil cu 5 minute înainte de final.", status_final=None)
        if win == "late":
            conn.close()
            return render_template("elev.html", session_id=session_id, message="Fereastra de check-out a expirat.", status_final=None)

        # trebuie să existe check-in anterior
        cur.execute("SELECT 1 FROM attendance WHERE session_id=? AND code4_hash=?", (session_id, code_hash))
        if not cur.fetchone():
            conn.close()
            return render_template("elev.html", session_id=session_id, message="Nu poți face check-out fără check-in pentru această oră.", status_final=None)

        cur.execute(
            "UPDATE attendance SET status=?, check_out_at=? WHERE session_id=? AND code4_hash=?",
            ("plecat", now.strftime("%Y-%m-%dT%H:%M:%S%z"), session_id, code_hash),
        )
        conn.commit()
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Check-out înregistrat. O oră bună!", status_final="plecat")

    # ===== CHECK-IN (phase=start sau fără token) =====
    st = _status_for(delta)  # None dacă în afara ferestrei 0-10 min
    if st is None:
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Fereastra de check-in a expirat (după 10 minute de la început)", status_final=None)

    # Anti-fraud (device_id + rate-limit + device folosit pt. alt cod + duplicat)
    device_id = (request.form.get("device_id") or "").strip()
    if not device_id:
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Lipsește identificatorul dispozitivului", status_final=None)

    # 1) Rate limit
    cur.execute(
        "SELECT COUNT(*) FROM attempt_log WHERE session_id=? AND device_id=? AND ts >= ?",
        (session_id, device_id, (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S%z")),
    )
    if cur.fetchone()[0] >= 3:
        cur.execute(
            "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, class_id, device_id, None, 0, "rate-limit", request.remote_addr,
             request.headers.get("User-Agent",""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        conn.commit()
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Prea multe încercări. Încearcă din nou peste un minut.", status_final=None)

    # 2) Același device folosit pentru alt cod?
    cur.execute(
        "SELECT DISTINCT code4_hash FROM attendance WHERE session_id=? AND class_id=? AND code4_hash IS NOT NULL",
        (session_id, class_id),
    )
    used_hashes = {row[0] for row in cur.fetchall()}
    if used_hashes and (code_hash not in used_hashes):
        cur.execute(
            "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, class_id, device_id, code_hash, 0, "device-used-for-other-code",
             request.remote_addr, request.headers.get("User-Agent",""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        conn.commit()
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Acest dispozitiv a fost folosit deja pentru alt cod la această oră.", status_final=None)

    # 3) Duplicat: același cod deja a făcut check-in
    cur.execute("SELECT 1 FROM attendance WHERE session_id=? AND code4_hash=?", (session_id, code_hash))
    if cur.fetchone():
        cur.execute(
            "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, class_id, device_id, code_hash, 0, "duplicate-code",
             request.remote_addr, request.headers.get("User-Agent",""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        conn.commit()
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Acest cod a fost deja folosit pentru această oră.", status_final=None)

    # Insert check-in
    try:
        cur.execute(
            "INSERT INTO attendance(session_id, class_id, code4_hash, status, check_in_at)"
            " VALUES (?,?,?,?,?)",
            (session_id, class_id, code_hash, st, now.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        conn.commit()
        # log succes
        cur.execute(
            "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, class_id, device_id, code_hash, 1, "ok",
             request.remote_addr, request.headers.get("User-Agent",""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        conn.commit()
        status_final = st
        message = "Te-ai înregistrat cu succes"
    except Exception:
        message = "Ai fost deja înregistrat pentru această oră"
    finally:
        conn.close()

    return render_template("elev.html", session_id=session_id, message=message, status_final=status_final)



@bp.get("/api/monitor_status")
def api_monitor_status():
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return jsonify({"error": "missing session_id"}), 400

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, class_id, starts_at, ends_at FROM session WHERE id=?", (session_id,))
    sess = cur.fetchone()
    if not sess:
        conn.close()
        return jsonify({"error": "session not found"}), 404

    class_id = sess["class_id"]
    starts_at = _parse_iso(sess["starts_at"])  # aware
    ends_at = _parse_iso(sess["ends_at"])
    now = datetime.now(tz=current_app.config["TZ"])  # TZ corect
    delta = int((now - starts_at).total_seconds())
    phase = "start" if now < (ends_at - timedelta(minutes=5)) else "end"

    # coduri autorizate pentru clasă
    cur.execute("SELECT code4_hash FROM authorized_code WHERE class_id=? ORDER BY id", (class_id,))
    authorized = [r[0] for r in cur.fetchall()]

    # statusuri curente pentru sesiune
    cur.execute("SELECT code4_hash, status FROM attendance WHERE session_id=?", (session_id,))
    status_map = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()

    present_count = sum(1 for h in authorized if status_map.get(h) in ("prezent", "întârziat"))
    total = len(authorized)

    if delta < 0:
        window_label = "nu a început"
    elif delta < 5*60:
        window_label = "verde (0–5 min)"
    elif delta < 10*60:
        window_label = "galben (5–10 min)"
    else:
        window_label = "expirat (>10 min)"

    # token pentru faza curentă
    s = get_qr_serializer(current_app)
    qr_token = s.dumps({"session_id": session_id, "phase": phase})

    return jsonify({
        "class_id": class_id,
        "session_id": session_id,
        "ora_curenta": now.strftime("%H:%M"),
        "window_label": window_label,
        "present_count": present_count,
        "total": total,
        "phase": phase,
        "qr_token": qr_token,          # <— IMPORTANT
    })


@bp.get("/qr.png")
def qr_png():
    token = request.args.get("token", type=str)
    if not token:
        return "missing token", 400
    # URL pe care îl va deschide QR-ul pe telefon
    # (folosim token, nu session_id, ca să nu poată fi modificat)
    url = url_for("main.elev", token=token, _external=True)
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)
