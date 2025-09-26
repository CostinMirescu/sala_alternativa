from functools import wraps
from flask import session, redirect, url_for, g
from .db import get_connection

def load_current_teacher():
    tid = session.get("teacher_id")
    if not tid:
        g.teacher = None; return
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id,email,class_id FROM teacher WHERE id=?", (tid,))
    g.teacher = cur.fetchone()
    conn.close()

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "teacher", None):
            return redirect(url_for("dirig.login"))
        return view(*args, **kwargs)
    return wrapped
