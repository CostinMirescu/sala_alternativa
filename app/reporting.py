from datetime import datetime
from .db import get_connection
from .utils import parse_iso, _hms

def fetch_report_data(class_id: str, start_dt, end_dt, tz):
    """
    Returnează trei liste: (detail_rows, summary_rows, attempts_rows)
    - start_dt, end_dt: datetime AWARE (TZ Europe/Bucharest), capete incluse
    """
    conn = get_connection(); cur = conn.cursor()

    # sesiuni ale clasei în interval [start..end]
    cur.execute("""SELECT id, class_id, starts_at, ends_at
                   FROM session
                   WHERE class_id=? AND starts_at BETWEEN ? AND ?
                   ORDER BY starts_at ASC""",
                (class_id,
                 start_dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 end_dt.strftime("%Y-%m-%dT%H:%M:%S%z")))
    sessions = cur.fetchall()

    # coduri autorizate (hash + plaintext)
    cur.execute("SELECT code4_hash, code4_plain FROM authorized_code WHERE class_id=? ORDER BY id", (class_id,))
    auth_rows = cur.fetchall()
    code_map = {r["code4_hash"]: r["code4_plain"] for r in auth_rows}
    authorized_hashes = list(code_map.keys())

    detail_rows = []
    summary_rows = []

    for s in sessions:
        sid = s["id"]
        starts = parse_iso(s["starts_at"]); ends = parse_iso(s["ends_at"])

        # status curente
        cur.execute("""SELECT code4_hash, status, check_in_at, check_out_at
                       FROM attendance WHERE session_id=?""", (sid,))
        rows = cur.fetchall()
        att = {r["code4_hash"]: r for r in rows}

        prez=intr=plec=neconf=0
        for h in authorized_hashes:
            a = att.get(h)
            code4 = code_map.get(h, "????")
            if a:
                status = a["status"]
                ci = a["check_in_at"]; co = a["check_out_at"]
                final = "plecat" if co else status
                if final == "plecat": plec+=1
                elif final == "întârziat": intr+=1
                elif final == "prezent": prez+=1
                else: neconf+=1
                detail_rows.append({
                    "data": starts.strftime("%Y-%m-%d"),
                    "incepe": starts.strftime("%H:%M"),
                    "se_termina": ends.strftime("%H:%M"),
                    "clasa": class_id,
                    "sesiune_id": sid,
                    "cod4": code4,
                    "status_final": final,
                    "check_in_at": _hms(ci),
                    "check_out_at": _hms(co),
                    "status_checkin": status if status in ("prezent","întârziat") else "-",
                    "status_checkout": "plecat" if co else "-",
                })
            else:
                neconf += 1
                detail_rows.append({
                    "data": starts.strftime("%Y-%m-%d"),
                    "incepe": starts.strftime("%H:%M"),
                    "se_termina": ends.strftime("%H:%M"),
                    "clasa": class_id,
                    "sesiune_id": sid,
                    "cod4": code_map.get(h,"????"),
                    "status_final": "neconfirmat",
                    "check_in_at": "", "check_out_at": "",
                    "status_checkin": "-", "status_checkout": "-",
                })

        total = len(authorized_hashes) or 1
        rata = (prez + intr) / total
        summary_rows.append({
            "sesiune_id": sid, "data": starts.strftime("%Y-%m-%d"),
            "incepe": starts.strftime("%H:%M"), "se_termina": ends.strftime("%H:%M"),
            "prezenti": prez, "intarziati": intr, "plecati": plec, "neconfirmat": neconf,
            "rata_conformare": f"{rata:.0%}",
        })

    # attempts (antifraud) în interval
    cur.execute("""SELECT ts, device_id, code4_hash, success, reason, ip, user_agent
                   FROM attempt_log
                   WHERE class_id=? AND ts BETWEEN ? AND ?
                   ORDER BY ts ASC""",
                (class_id,
                 start_dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 end_dt.strftime("%Y-%m-%dT%H:%M:%S%z")))
    attempts = []
    REASON_RO = {
        "rate-limit": "prea multe încercări într-un minut",
        "device-used-for-other-code": "același dispozitiv folosit pentru alt cod în această oră",
        "duplicate-code": "cod deja folosit în această oră",
        "ok": "înregistrare reușită",
    }
    for r in cur.fetchall():
        attempts.append({
            "ts": r["ts"], "device_id": r["device_id"],
            "cod4": code_map.get(r["code4_hash"], "") if r["code4_hash"] else "",
            "success": r["success"], "reason": REASON_RO.get(r["reason"], r["reason"]),
            "ip": r["ip"], "ua": (r["user_agent"] or "")[:80]
        })

    conn.close()
    return detail_rows, summary_rows, attempts
