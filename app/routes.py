from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for
from datetime import datetime, timezone, timedelta
from .db import get_connection, _hash_code
from flask import current_app

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
    now = datetime.now(tz=current_app.config["TZ"])
    delta = int((now - starts_at).total_seconds())

    # coduri autorizate pentru clasă
    cur.execute("SELECT code4_hash FROM authorized_code WHERE class_id=? ORDER BY id", (class_id,))
    code_hashes = [r[0] for r in cur.fetchall()]

    # attendance curent pentru sesiune
    cur.execute(
        "SELECT code4_hash, status FROM attendance WHERE session_id=?",
        (session_id,),
    )
    status_map = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()

    # proiectăm pentru UI (fără a expune codul real): afișăm mascat ultimele 2 cifre
    codes_ui = []
    for h in code_hashes:
        st = status_map.get(h, "neconfirmat")
        codes_ui.append({"hash": h, "masked": "••**", "status": st})

    present_count = sum(1 for c in codes_ui if c["status"] in ("prezent","întârziat"))

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

    return render_template(
        "monitor.html",
        class_id=class_id,
        session_id=session_id,
        ora_curenta=ora_curenta,
        window_label=window_label,
        present_count=present_count,
        total=len(codes_ui),
        codes=codes_ui,
    )


# --- Elev ---
@bp.route("/elev", methods=["GET", "POST"])
def elev():
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return "Lipsește ?session_id=...", 400

    message = None
    status_final = None

    if request.method == "POST":
        # avem 4 inputuri cu câte o cifră (ex. d1..d4)
        d1 = (request.form.get("d1") or "").strip()
        d2 = (request.form.get("d2") or "").strip()
        d3 = (request.form.get("d3") or "").strip()
        d4 = (request.form.get("d4") or "").strip()
        code4 = f"{d1}{d2}{d3}{d4}"

        if len(code4) != 4 or not code4.isdigit():
            message = "Cod invalid — introdu exact 4 cifre"
        else:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT id, class_id, starts_at FROM session WHERE id=?", (session_id,))
            sess = cur.fetchone()
            if not sess:
                conn.close()
                return "Sesiune inexistentă", 404

            class_id = sess["class_id"]
            starts_at = _parse_iso(sess["starts_at"])  # aware
            now = datetime.now(tz=current_app.config["TZ"])
            delta = int((now - starts_at).total_seconds())

            st = _status_for(delta)
            if st is None:
                message = "Fereastra de check‑in a expirat"
            else:
                code_hash = _hash_code(class_id, code4)
                # verificăm că există în lista autorizată
                cur.execute(
                    "SELECT 1 FROM authorized_code WHERE class_id=? AND code4_hash=?",
                    (class_id, code_hash),
                )
                if not cur.fetchone():
                    message = "Cod neautorizat pentru această clasă"
                else:
                    # --- Anti‑fraud rules ---
                    device_id = (request.form.get("device_id") or "").strip()
                    if not device_id:
                        message = "Lipsește identificatorul dispozitivului"
                    else:
                        # 1) Rate limit: max 3 încercări în 60s per device per sesiune
                        cur.execute(
                            "SELECT COUNT(*) FROM attempt_log WHERE session_id=? AND device_id=? AND ts >= ?",
                            (session_id, device_id, (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S%z")),
                        )
                        attempts_last_min = cur.fetchone()[0]
                        if attempts_last_min >= 3:
                            # log și refuz
                            cur.execute(
                                "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                                (session_id, class_id, device_id, None, 0, "rate-limit", request.remote_addr,
                                 request.headers.get("User-Agent", ""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
                            )
                            conn.commit()
                            message = "Prea multe încercări. Încearcă din nou peste un minut."
                            conn.close()
                            return render_template("elev.html", session_id=session_id, message=message,
                                                   status_final=None)

                        # 2) Același device a înregistrat deja ALT cod la această sesiune?
                        cur.execute(
                            "SELECT DISTINCT code4_hash FROM attendance WHERE session_id=? AND class_id=? AND code4_hash IS NOT NULL",
                            (session_id, class_id),
                        )
                        used_hashes = {row[0] for row in cur.fetchall()}
                        if used_hashes and (code_hash not in used_hashes):
                            cur.execute(
                                "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                                (session_id, class_id, device_id, code_hash, 0, "device-used-for-other-code",
                                 request.remote_addr, request.headers.get("User-Agent", ""),
                                 now.strftime("%Y-%m-%dT%H:%M:%S%z")),
                            )
                            conn.commit()
                            message = "Acest dispozitiv a fost folosit deja pentru alt cod la această oră."
                            conn.close()
                            return render_template("elev.html", session_id=session_id, message=message,
                                                   status_final=None)

                        # 3) Cod deja folosit (un check‑in per elev per sesiune)
                        cur.execute(
                            "SELECT 1 FROM attendance WHERE session_id=? AND code4_hash=?",
                            (session_id, code_hash),
                        )
                        if cur.fetchone():
                            cur.execute(
                                "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                                (session_id, class_id, device_id, code_hash, 0, "duplicate-code", request.remote_addr,
                                 request.headers.get("User-Agent", ""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
                            )
                            conn.commit()
                            message = "Acest cod a fost deja folosit pentru această oră."
                            conn.close()
                            return render_template("elev.html", session_id=session_id, message=message,
                                                   status_final=None)
                    # încercăm să insertăm attendance (unică per (session_id, code4_hash))
                    try:
                        cur.execute(
                            "INSERT INTO attendance(session_id, class_id, code4_hash, status, check_in_at)"
                            " VALUES (?,?,?,?,?)",
                            (session_id, class_id, code_hash, st, now.strftime("%Y-%m-%dT%H:%M:%S%z")),
                        )
                        conn.commit()
                        status_final = st
                        message = "Te-ai înregistrat cu succes"
                        cur.execute(
                            "INSERT INTO attempt_log(session_id,class_id,device_id,code4_hash,success,reason,ip,user_agent,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                            (session_id, class_id, device_id, code_hash, 1, "ok", request.remote_addr,
                             request.headers.get("User-Agent", ""), now.strftime("%Y-%m-%dT%H:%M:%S%z")),
                        )
                    except Exception:
                        # există deja o înregistrare
                        message = "Ai fost deja înregistrat pentru această oră"
                    finally:
                        conn.close()

    # Pentru GET sau după POST, afișăm pagina cu mesaj/status
    return render_template(
        "elev.html",
        session_id=session_id,
        message=message,
        status_final=status_final,
    )