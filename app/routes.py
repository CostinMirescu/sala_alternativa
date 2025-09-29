from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for
from datetime import datetime, timezone, timedelta

from .utils import get_qr_serializer, parse_iso
from .db import get_connection, _hash_code
from flask import current_app
from flask import jsonify
from flask import send_file, current_app
from io import BytesIO
import qrcode
from itsdangerous import BadSignature, SignatureExpired
from datetime import datetime, timedelta
from .utils import aware_from_hhmm

def _windows(now, starts_at, ends_at, cfg):
    """
    Returnează dict cu ferestrele (active/sleep/end) și 'mode' curent.
    Reguli:
      - check-in: [T - open_before, T + close_after]
      - sleep   : (T + close_after, ends_at - checkout_open_before_end)
      - end     : [ends_at - checkout_open_before_end, ends_at + grace_after_end]
    """
    open_before   = timedelta(minutes=cfg["CHECKIN_OPEN_MIN_BEFORE"])
    close_after   = timedelta(minutes=cfg["CHECKIN_CLOSE_MIN_AFTER"])
    open_end      = timedelta(minutes=cfg["CHECKOUT_OPEN_MIN_BEFORE_END"])
    grace_after   = timedelta(minutes=cfg["CHECKOUT_GRACE_MIN_AFTER_END"])

    w_checkin_start = starts_at - open_before
    w_checkin_end   = starts_at + close_after
    w_end_start     = ends_at - open_end
    w_end_end       = ends_at + grace_after

    if now < w_checkin_start:
        mode = "pre"
    elif w_checkin_start <= now <= w_checkin_end:
        mode = "active"
    elif w_checkin_end < now < w_end_start:
        mode = "sleep"
    elif w_end_start <= now <= w_end_end:
        mode = "end"
    else:
        mode = "post"

    return {
        "mode": mode,
        "w_checkin_start": w_checkin_start,
        "w_checkin_end":   w_checkin_end,
        "w_end_start":     w_end_start,
        "w_end_end":       w_end_end,
    }

def _window_label(now, starts_at, cfg):
    """Pentru textul tău existent 'Fereastră check-in: ...' """
    open_before = timedelta(minutes=cfg["CHECKIN_OPEN_MIN_BEFORE"])
    close_after = timedelta(minutes=cfg["CHECKIN_CLOSE_MIN_AFTER"])
    if now < starts_at - open_before:
        return "nu a început"
    delta = int((now - starts_at).total_seconds())
    if delta < 5*60:
        return "verde (0–5 min)"
    if delta < 10*60:
        return "galben (5–10 min)"
    return "expirat (>10 min)"



bp = Blueprint("main", __name__)

@bp.get("/")
def home():
    return render_template("index.html", title="Sala Alternativă")

# --- Helpers ---


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
# @bp.get("/monitor")
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


    starts_at = parse_iso(sess["starts_at"])  # aware
    ends_at = parse_iso(sess["ends_at"])

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
        last2 = (code4 or "")[-2:]
        st = status_map.get(h, "neconfirmat")
        codes_ui.append({"last2": last2, "status": st})


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
    data_curenta = now.strftime("%d %b %Y")
    phase = "start" if now < (ends_at - timedelta(minutes=5)) else "end"


    s = get_qr_serializer(current_app)
    qr_token = s.dumps({"session_id": session_id, "phase": phase})
    qr_title = "Cod de început de oră" if phase == "start" else "Cod de final de oră"

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

        phase=phase,
        data_curenta=data_curenta

    )


# --- Elev ---
@bp.route("/elev", methods=["GET", "POST"])
def elev():

    # Acceptă token din query SAU din POST (hidden field)
    token = request.args.get("token") or request.form.get("token")
    session_id = None
    token_phase = None

    # Dacă a venit cu session_id în query, dar avem și token → redirect la varianta fără session_id
    if request.method == "GET":
        sid_in_qs = request.args.get("session_id")
        if token and sid_in_qs:
            return redirect(url_for("main.elev", token=token), code=302)

    if not token:
        return render_template("token_error.html", reason="Lipsește tokenul semnat din link."), 404

    # Validare token + extragem session_id/faza
    s = get_qr_serializer(current_app)
    try:
        data = s.loads(token, max_age=current_app.config["QR_MAX_AGE"])
    except SignatureExpired:
        return render_template("token_error.html", reason="Tokenul a expirat. Scanează din nou codul QR."), 404
    except BadSignature:
        return render_template("token_error.html", reason="Token invalid. Te rugăm scanează din nou codul QR."), 404

    token_phase = data.get("phase")            # "start" sau "end"
    session_id  = int(data.get("session_id"))  # din token, nu din URL

    if request.method == "GET":
        # doar randăm formularul; POST-ul va include tokenul ca hidden
        return render_template("elev.html", session_id=session_id, message=None, status_final=None, token=token)

    # --- POST: preluăm codul de 4 cifre ---
    d1 = (request.form.get("d1") or "").strip()
    d2 = (request.form.get("d2") or "").strip()
    d3 = (request.form.get("d3") or "").strip()
    d4 = (request.form.get("d4") or "").strip()
    code4 = f"{d1}{d2}{d3}{d4}"

    if len(code4) != 4 or not code4.isdigit():
        return render_template("elev.html", session_id=session_id, message="Cod invalid — introdu exact 4 cifre", status_final=None, token=token)

    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT id, class_id, starts_at, ends_at FROM session WHERE id=?", (session_id,))
    sess = cur.fetchone()
    if not sess:
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Sesiune inexistentă", status_final=None, token=token)

    class_id = sess["class_id"]

    starts_at = parse_iso(sess["starts_at"])
    ends_at   = parse_iso(sess["ends_at"])

    now = datetime.now(tz=current_app.config["TZ"])
    delta = int((now - starts_at).total_seconds())

    # cod autorizat?
    code_hash = _hash_code(class_id, code4)
    cur.execute("SELECT 1 FROM authorized_code WHERE class_id=? AND code4_hash=?", (class_id, code_hash))
    if not cur.fetchone():
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Cod neautorizat pentru această clasă", status_final=None, token=token)

    # ===== CHECK-OUT (phase=end) =====
    if token_phase == "end":
        # fereastră de check-out: [-5m, +5m] față de ends_at
        win = _checkout_allowed(now, ends_at)
        if win == "early":
            conn.close()
            return render_template("elev.html", session_id=session_id, message="Check-out disponibil cu 5 minute înainte de final.", status_final=None, token=token)
        if win == "late":
            conn.close()
            return render_template("elev.html", session_id=session_id, message="Fereastra de check-out a expirat.", status_final=None, token=token)

        # trebuie să existe check-in anterior
        cur.execute("SELECT 1 FROM attendance WHERE session_id=? AND code4_hash=?", (session_id, code_hash))
        if not cur.fetchone():
            conn.close()
            return render_template("elev.html", session_id=session_id, message="Nu poți face check-out fără check-in pentru această oră.", status_final=None, token=token)

        cur.execute(
            "UPDATE attendance SET status=?, check_out_at=? WHERE session_id=? AND code4_hash=?",
            ("plecat", now.strftime("%Y-%m-%dT%H:%M:%S%z"), session_id, code_hash),
        )
        conn.commit()
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Check-out înregistrat. O oră bună!", status_final="plecat", token=token)

    # ===== CHECK-IN (phase=start sau fără token) =====
    st = _status_for(delta)  # None dacă în afara ferestrei 0-10 min
    if st is None:
        conn.close()

        return render_template("elev.html", session_id=session_id, message="Ora a început de mai mult de zece minute. Nu mai este permis check-in-ul.", status_final=None, token=token)

    # Anti-fraud (device_id + rate-limit + device folosit pt. alt cod + duplicat)
    device_id = (request.form.get("device_id") or "").strip()
    if not device_id:
        conn.close()
        return render_template("elev.html", session_id=session_id, message="Lipsește identificatorul dispozitivului", status_final=None, token=token)

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
        return render_template("elev.html", session_id=session_id, message="Prea multe încercări. Încearcă din nou peste un minut.", status_final=None, token=token)

    # 2) Acelasi device a validat deja alt cod (blocăm doar după succes)
    cur.execute(
        "SELECT code4_hash FROM attempt_log "
        "WHERE session_id=? AND device_id=? AND success=1 "
        "ORDER BY ts DESC LIMIT 1",
        (session_id, device_id),
    )
    row = cur.fetchone()
    if row:
        prior_hash = row[0]
        if prior_hash and prior_hash != code_hash:
            cur.execute(
                "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, class_id, device_id, code_hash, 0, "device-used-for-other-code",
                 request.remote_addr, request.headers.get("User-Agent", ""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
            )
            conn.commit()
            conn.close()
            return render_template("elev.html", session_id=session_id,
                                   message="Acest dispozitiv a fost folosit deja pentru un alt cod la această oră.",
                                   status_final=None , token=token)


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

    return render_template("elev.html", session_id=session_id, message=message, status_final=status_final, token=token)



@bp.get("/api/monitor_status")
def api_monitor_status():
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        sid = _find_or_create_current_session(current_app.config["TZ"])
        if not sid:
            return jsonify({"mode": "off"}), 200
        session_id = sid

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, class_id, starts_at, ends_at FROM session WHERE id=?", (session_id,))
    sess = cur.fetchone()
    if not sess:
        conn.close()
        return jsonify({"error": "session not found"}), 404

    class_id = sess["class_id"]

    starts_at = parse_iso(sess["starts_at"])  # aware
    ends_at = parse_iso(sess["ends_at"])
    now = datetime.now(tz=current_app.config["TZ"])
    wins = _windows(now, starts_at, ends_at, current_app.config)
    mode_internal = wins["mode"]
    delta = int((now - starts_at).total_seconds())
    phase = "start" if now < (ends_at - timedelta(minutes=5)) else "end"

    # coduri autorizate pentru clasă
    cur.execute("SELECT code4_hash FROM authorized_code WHERE class_id=? ORDER BY id", (class_id,))
    authorized = [r[0] for r in cur.fetchall()]

    # statusuri curente pentru sesiune
    cur.execute("SELECT code4_hash, status FROM attendance WHERE session_id=?", (session_id,))
    status_map = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute("SELECT code4_hash, code4_plain FROM authorized_code WHERE class_id=? ORDER BY id", (class_id,))
    rows = cur.fetchall()
    codes = []
    for h, code4 in rows:
        st = status_map.get(h, "neconfirmat")
        last2 = (code4 or "")[-2:]
        codes.append({"last2": last2, "status": st})
    conn.close()


    before_end_5m = ends_at - timedelta(minutes=5)

    if delta < 0:
        mode = "pre"
        window_label = "nu a început"
    elif delta < 5*60:
        mode = "active"
        window_label = "verde (0–5 min)"
    elif delta < 10*60:
        mode = "active"
        window_label = "galben (5–10 min)"
    elif now < before_end_5m:
        mode = "sleep"
        window_label = "ora"
    else:
        mode = "end"
        window_label = "expirat (>10 min)"

    sleep_until = before_end_5m.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Publicăm "off" pentru pre/post (ecran unificat)
    if mode_internal in ("pre", "post"):
        mode = "off"
        reason = mode_internal
    else:
        mode = mode_internal
        reason = None

    # Token doar în active/end
    qr_token = None
    if mode in ("active", "end"):
        s = get_qr_serializer(current_app)
        phase = "start" if mode == "active" else "end"
        qr_token = s.dumps({"session_id": session_id, "phase": phase})


    # Dacă suntem "pre", anunțăm când se deschide fereastra (T-5)
    next_window_at = None
    next_window_hhmm = None
    if mode_internal == "pre":
        nxt = wins["w_checkin_start"]  # datetime aware
        next_window_at = nxt.strftime("%Y-%m-%dT%H:%M:%S%z")
        next_window_hhmm = nxt.strftime("%H:%M")


    # prezent acum (doar pentru calcul intern)
    present_now = sum(1 for h in authorized if status_map.get(h) in ("prezent", "întârziat"))
    total = len(authorized)

    # citește snapshot-ul (dacă există)
    cur2 = get_connection().cursor()
    cur2.execute("SELECT present_frozen, present_frozen_at FROM session WHERE id=?", (session_id,))
    snap = cur2.fetchone()
    present_frozen = snap["present_frozen"] if snap else None
    present_frozen_at = snap["present_frozen_at"] if snap else None

    # dacă am depășit +10 min și nu avem snapshot, îl setăm ACUM (o singură dată)
    delta = int((now - starts_at).total_seconds())
    if delta >= 10 * 60 and present_frozen is None:
        cur2.execute(
            "UPDATE session SET present_frozen=?, present_frozen_at=? WHERE id=?",
            (present_now, now.strftime("%Y-%m-%dT%H:%M:%S%z"), session_id),
        )
        cur2.connection.commit()
        present_frozen = present_now
        present_frozen_at = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    cur2.connection.close()

    # ce raportăm UI-ului?
    if delta >= 10 * 60 and present_frozen is not None:
        present_count = present_frozen  # ÎNGHEȚAT
    else:
        present_count = present_now

    left_count = sum(1 for st in status_map.values() if st == "plecat")

    data_curenta = now.strftime("%d %b %Y")  # ex: 23 Sep 2025


    window_label = _window_label(now, starts_at, current_app.config)

    return jsonify({
        "class_id": class_id,
        "session_id": session_id,
        "ora_curenta": now.strftime("%H:%M"),
        "window_label": window_label,
        "present_count": present_count,
        "total": total,
        "phase": phase,
        "qr_token": qr_token,          # <— IMPORTANT
        "data_curenta": data_curenta,
        "mode": mode,

        "reason": reason,
        "sleep_until": sleep_until,
        "left_count": left_count,
        "next_window_at": next_window_at,  # ISO sau None
        "next_window_hhmm": next_window_hhmm,  # "HH:MM" sau None
        "codes": codes

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


def _find_or_create_current_session(tz):
    now = datetime.now(tz)
    weekday = now.isoweekday()  # 1..7
    if weekday > 5:
        return None  # weekend

    # află ce period ar fi în fereastra noastră (start-5 .. end+10)
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT period_no, start_hhmm FROM period ORDER BY period_no")
    periods = cur.fetchall()

    candidate = None
    for p in periods:
        start_today = aware_from_hhmm(now.date(), p["start_hhmm"], tz)
        end_today = start_today + timedelta(minutes=60)
        if (start_today - timedelta(minutes=5)) <= now <= (end_today + timedelta(minutes=10)):
            candidate = (p["period_no"], start_today, end_today)
            break
    if not candidate:
        conn.close()
        return None

    period_no, starts, ends = candidate
    # caută în schedule clasa programată
    cur.execute("SELECT class_id FROM schedule WHERE weekday=? AND period_no=?", (weekday, period_no))
    row = cur.fetchone()
    if not row:
        conn.close(); return None
    class_id = row["class_id"]

    # cauți sesiunea existentă sau creezi dacă flagul e ON
    starts_iso = starts.strftime("%Y-%m-%dT%H:%M:%S%z")
    cur.execute("SELECT id FROM session WHERE class_id=? AND starts_at=?", (class_id, starts_iso))
    srow = cur.fetchone()
    if srow:
        sid = srow["id"]; conn.close(); return sid

    # creează doar dacă e activat
    if not current_app.config.get("AUTO_SESSIONS_ENABLED", False):
        conn.close(); return None

    try:
        cur.execute("INSERT INTO session(class_id, starts_at, ends_at) VALUES (?,?,?)",
                    (class_id, starts_iso, ends.strftime("%Y-%m-%dT%H:%M:%S%z")))
        conn.commit()
        sid = cur.lastrowid
    finally:
        conn.close()
    return sid


@bp.get("/monitor")
def monitor_auto():
    # dacă ai ?session_id, folosește ruta existentă (nu o mai arăt aici)
    sid = request.args.get("session_id", type=int)
    if sid:
        return monitor()  # ruta ta existentă care randă monitor pentru un id

    sid = _find_or_create_current_session(current_app.config["TZ"])
    if sid:
        return redirect(url_for("monitor", session_id=sid), code=302)
    # nici o sesiune validă: arată monitor “off” (poți avea un template minimal)
    return render_template("monitor_off.html")
