"""
🗓️ Attendance Portal - single file Streamlit app
-------------------------------------------------
Run locally :  streamlit run app.py
Deploy      :  Push to GitHub, then deploy on Streamlit Community Cloud.

DATA STORAGE
  * Default  : SQLite file (attendance.db)  -> fine for local use.
               !! On Streamlit Cloud the file system is reset on reboot / redeploy, so data is lost.
  * Cloud    : Put a free Postgres (Supabase / Neon) URL in Streamlit "Secrets":
                   DATABASE_URL = "postgresql://user:password@host:5432/dbname"
               Your data is then stored permanently.
"""
import os
import io
import sqlite3
import hashlib
import secrets
import calendar
from datetime import datetime, date, time, timedelta, timezone

import pandas as pd
import streamlit as st

# =====================================================================
#  CONFIG
# =====================================================================
SQLITE_PATH = os.environ.get("ATTENDANCE_DB", "attendance.db")

STATUSES = ["Present", "WFH", "Half Day", "Leave", "Absent"]
LEAVE_TYPES = ["Casual Leave", "Sick Leave", "Earned Leave", "Other"]

DEFAULT_SETTINGS = {
    "office_start": "09:30",      # office start time
    "grace_min": "15",            # grace minutes before "Late"
    "weekly_off": "6",            # Mon=0 ... Sun=6   (Sat+Sun => "5,6")
    "allow_signup": "1",          # 1 = anyone can sign up
    "signup_code": "",            # if set, sign-up requires this code
    "default_leave_quota": "12",  # per year
    "timezone": "Asia/Kolkata",
    "company": "My Office",
}

COLORS = {
    "Present": "#2e7d32", "WFH": "#1565c0", "Half Day": "#f9a825", "Leave": "#8e24aa",
    "Absent": "#c62828", "Off": "#9e9e9e", "Not yet": "#fb8c00", "-": "#cfd8dc",
}
ICONS = {"Present": "🏢", "WFH": "🏠", "Half Day": "🌗", "Leave": "🌴", "Absent": "❌",
         "Off": "⛱️", "Not yet": "⏳", "-": ""}
MONTHS = list(calendar.month_name)[1:]
CODES = {"Present": "P", "WFH": "W", "Half Day": "HD", "Leave": "L", "Absent": "A",
         "Off": "Off", "-": "", "Not yet": ""}


# =====================================================================
#  DATABASE LAYER  (SQLite  /  Postgres)
# =====================================================================
def _database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        return st.secrets.get("DATABASE_URL")
    except Exception:
        return None


def _bootstrap_admins():
    """If ADMIN_USERS = "mathi,ravi" is set in Streamlit Secrets (or env), those usernames become Admin on login.
    Only the app owner can edit Secrets, so this is a safe recovery route."""
    raw = os.environ.get("ADMIN_USERS")
    if not raw:
        try:
            raw = st.secrets.get("ADMIN_USERS")
        except Exception:
            raw = None
    if not raw:
        return set()
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return {str(i).strip().lower() for i in items if str(i).strip()}


DATABASE_URL = _database_url()
USE_PG = bool(DATABASE_URL)


@st.cache_resource
def _pg_conn(url):
    import psycopg2
    c = psycopg2.connect(url)
    c.autocommit = True
    return c


def _run(sql, params=(), fetch=False):
    if USE_PG:
        import psycopg2
        import psycopg2.extras
        sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                conn = _pg_conn(DATABASE_URL)
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    return [dict(r) for r in cur.fetchall()] if fetch else None
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                _pg_conn.clear()          # connection poyiduchu -> reconnect
                if attempt == 2:
                    raise
    else:
        c = sqlite3.connect(SQLITE_PATH)
        c.row_factory = sqlite3.Row
        try:
            cur = c.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if fetch else None
            c.commit()
            return rows
        finally:
            c.close()


def q(sql, params=(), one=False):
    rows = _run(sql, params, fetch=True)
    if one:
        return rows[0] if rows else None
    return rows


def x(sql, params=()):
    _run(sql, params)


def init_db():
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS users(
            id {pk},
            username TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            salt TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'employee',
            dept TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            leave_quota REAL,
            created_at TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS attendance(
            id {pk},
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            check_in TEXT,
            check_out TEXT,
            note TEXT DEFAULT '',
            UNIQUE(user_id, date))""",
        f"""CREATE TABLE IF NOT EXISTS leaves(
            id {pk},
            user_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            leave_type TEXT NOT NULL,
            reason TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Pending',
            decided_by INTEGER,
            applied_at TEXT)""",
        "CREATE TABLE IF NOT EXISTS holidays(date TEXT PRIMARY KEY, name TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    ]
    for s_ in stmts:
        x(s_)
    for k, v in DEFAULT_SETTINGS.items():
        x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", (k, v))


# =====================================================================
#  SETTINGS / TIME
# =====================================================================
def get_settings():
    d = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key,value FROM settings"):
        d[r["key"]] = r["value"]
    return d


def set_setting(key, value):
    x("INSERT INTO settings(key,value) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _tz(s=None):
    name = (s or get_settings())["timezone"]
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))  # IST fallback


def now():
    return datetime.now(_tz()).replace(tzinfo=None)


def today():
    return now().date()


def _t(s):
    """'HH:MM' / 'HH:MM:SS' -> time"""
    p = [int(i) for i in str(s).split(":")]
    while len(p) < 3:
        p.append(0)
    return time(p[0], p[1], p[2])


def off_weekdays(s):
    return {int(i) for i in str(s["weekly_off"]).split(",") if i.strip() != ""}


# =====================================================================
#  USERS
# =====================================================================
def _hash(pin, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), 120_000).hex()


def user_count():
    return int(q("SELECT COUNT(*) AS n FROM users", one=True)["n"])


def create_user(username, name, pin, role="employee", dept=""):
    username = (username or "").strip().lower()
    name = (name or "").strip()
    if not username or not name:
        return False, "Name and username are required."
    if " " in username:
        return False, "Username cannot contain spaces."
    if len(pin or "") < 4:
        return False, "PIN / password must be at least 4 characters."
    if q("SELECT 1 AS k FROM users WHERE username=?", (username,), one=True):
        return False, "This username is already taken."
    salt = secrets.token_hex(16)
    x("INSERT INTO users(username,name,salt,pin_hash,role,dept,created_at) VALUES(?,?,?,?,?,?,?)",
      (username, name, salt, _hash(pin, salt), role, (dept or "").strip(),
       now().isoformat(timespec="seconds")))
    return True, "Account ready"


def authenticate(username, pin):
    u = q("SELECT * FROM users WHERE username=?", ((username or "").strip().lower(),), one=True)
    if not u or not u["active"]:
        return None
    if secrets.compare_digest(_hash(pin or "", u["salt"]), u["pin_hash"]):
        return u
    return None


def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def list_users(active_only=False):
    sql = "SELECT * FROM users" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
    return q(sql)


def update_user(uid, role=None, active=None, dept=None, quota=None, name=None, joined=None):
    u = get_user(uid)
    if not u:
        return
    if joined is not None:
        x("UPDATE users SET created_at=? WHERE id=?", (joined.isoformat() + "T00:00:00", uid))
    x("UPDATE users SET role=?, active=?, dept=?, leave_quota=?, name=? WHERE id=?",
      (role if role is not None else u["role"],
       int(active) if active is not None else u["active"],
       dept if dept is not None else u["dept"],
       quota if quota is not None else u["leave_quota"],
       name if name is not None else u["name"], uid))


def reset_pin(uid, new_pin):
    if len(new_pin or "") < 4:
        return False, "PIN must be at least 4 characters."
    salt = secrets.token_hex(16)
    x("UPDATE users SET salt=?, pin_hash=? WHERE id=?", (salt, _hash(new_pin, salt), uid))
    return True, "PIN changed"


def quota_of(user, s=None):
    s = s or get_settings()
    return float(user["leave_quota"]) if user.get("leave_quota") is not None else float(s["default_leave_quota"])


def _joined(u):
    try:
        return date.fromisoformat(str(u["created_at"])[:10])
    except Exception:
        return date(2000, 1, 1)


# =====================================================================
#  HOLIDAYS / LEAVES
# =====================================================================
def list_holidays(year=None):
    if year:
        return q("SELECT * FROM holidays WHERE date BETWEEN ? AND ? ORDER BY date",
                 (f"{year}-01-01", f"{year}-12-31"))
    return q("SELECT * FROM holidays ORDER BY date")


def add_holiday(d, name):
    x("INSERT INTO holidays(date,name) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET name=excluded.name",
      (d.isoformat(), (name or "").strip() or "Holiday"))


def delete_holiday(d):
    x("DELETE FROM holidays WHERE date=?", (d,))


def _is_off(d, offs, hol):
    return d.weekday() in offs or d.isoformat() in hol


def leave_days_count(start, end):
    s = get_settings()
    offs = off_weekdays(s)
    hol = {h["date"] for h in list_holidays()}
    n, d = 0, start
    while d <= end:
        if not _is_off(d, offs, hol):
            n += 1
        d += timedelta(days=1)
    return n


def apply_leave(uid, start, end, ltype, reason):
    if end < start:
        return False, "End date cannot be before the start date."
    if leave_days_count(start, end) == 0:
        return False, "All the selected days are holidays or weekly offs."
    dup = q("SELECT 1 AS k FROM leaves WHERE user_id=? AND status IN ('Pending','Approved') "
            "AND start_date<=? AND end_date>=?", (uid, end.isoformat(), start.isoformat()), one=True)
    if dup:
        return False, "You already have a leave request overlapping these dates."
    x("INSERT INTO leaves(user_id,start_date,end_date,leave_type,reason,applied_at) VALUES(?,?,?,?,?,?)",
      (uid, start.isoformat(), end.isoformat(), ltype, (reason or "").strip(),
       now().isoformat(timespec="seconds")))
    return True, "Leave request submitted."


def list_leaves(uid=None, status=None):
    sql = "SELECT l.*, u.name, u.dept FROM leaves l JOIN users u ON u.id=l.user_id WHERE 1=1"
    p = []
    if uid:
        sql += " AND l.user_id=?"
        p.append(uid)
    if status:
        sql += " AND l.status=?"
        p.append(status)
    return q(sql + " ORDER BY l.start_date DESC", tuple(p))


def decide_leave(leave_id, status, admin_id):
    x("UPDATE leaves SET status=?, decided_by=? WHERE id=?", (status, admin_id, leave_id))


def cancel_leave(leave_id, uid):
    x("DELETE FROM leaves WHERE id=? AND user_id=? AND status='Pending'", (leave_id, uid))


def _approved_leave_set(uid, start, end):
    out = set()
    for r in q("SELECT start_date,end_date FROM leaves WHERE user_id=? AND status='Approved' "
               "AND start_date<=? AND end_date>=?", (uid, end.isoformat(), start.isoformat())):
        d = max(date.fromisoformat(r["start_date"]), start)
        e = min(date.fromisoformat(r["end_date"]), end)
        while d <= e:
            out.add(d)
            d += timedelta(days=1)
    return out


# =====================================================================
#  ATTENDANCE
# =====================================================================
def get_record(uid, d):
    return q("SELECT * FROM attendance WHERE user_id=? AND date=?", (uid, d.isoformat()), one=True)


def _upsert(uid, d, status, ci, co, note):
    x("""INSERT INTO attendance(user_id,date,status,check_in,check_out,note) VALUES(?,?,?,?,?,?)
         ON CONFLICT(user_id,date) DO UPDATE SET status=excluded.status, check_in=excluded.check_in,
         check_out=excluded.check_out, note=excluded.note""",
      (uid, d.isoformat(), status, ci, co, note))


def check_in(uid, mode, note=""):
    """mode: 'Present' (office) / 'WFH'"""
    d = today()
    rec = get_record(uid, d)
    if rec and rec["check_in"]:
        return False, "You have already checked in today."
    if d in _approved_leave_set(uid, d, d):
        return False, "Your leave is approved for today. Please contact the admin if you need to check in."
    t = now().strftime("%H:%M:%S")
    _upsert(uid, d, mode, t, None, (note or "").strip())
    return True, f"Check-in {t[:5]}"


def check_out(uid):
    d = today()
    rec = get_record(uid, d)
    if not rec or not rec["check_in"]:
        return False, "Please check in first."
    if rec["check_out"]:
        return False, "You have already checked out today."
    t = now().strftime("%H:%M:%S")
    _upsert(uid, d, rec["status"], rec["check_in"], t, rec["note"] or "")
    return True, f"Check-out {t[:5]}"


def set_status(uid, d, status, ci=None, co=None, note=""):
    """Admin manual edit. status='Clear' => record delete"""
    if status == "Clear":
        x("DELETE FROM attendance WHERE user_id=? AND date=?", (uid, d.isoformat()))
        return
    _upsert(uid, d, status, ci, co, note)


def is_late(check_in_str, s=None):
    s = s or get_settings()
    limit = (datetime.combine(date(2000, 1, 1), _t(s["office_start"]))
             + timedelta(minutes=int(s["grace_min"]))).time()
    return _t(check_in_str) > limit


def hours_between(ci, co):
    if not ci or not co:
        return None
    a = datetime.combine(date(2000, 1, 1), _t(ci))
    b = datetime.combine(date(2000, 1, 1), _t(co))
    return round(max((b - a).total_seconds(), 0) / 3600, 2)


def _status_for(d, rec, leave_set, offs, hol, today_, joined=None):
    if rec:
        return rec["status"]
    if joined and d < joined:
        return "-"          # do not show Absent for days before the account was created
    if _is_off(d, offs, hol):
        return "Off"
    if d in leave_set:
        return "Leave"
    if d > today_:
        return "-"
    if d == today_:
        return "Not yet"
    return "Absent"


def days_between(uid, first, last):
    """Day-by-day status list from first to last (inclusive)."""
    s = get_settings()
    offs = off_weekdays(s)
    recs = {r["date"]: r for r in q("SELECT * FROM attendance WHERE user_id=? AND date BETWEEN ? AND ?",
                                    (uid, first.isoformat(), last.isoformat()))}
    hols = {h["date"]: h["name"] for h in q("SELECT * FROM holidays WHERE date BETWEEN ? AND ?",
                                           (first.isoformat(), last.isoformat()))}
    leave_set = _approved_leave_set(uid, first, last)
    t0 = today()
    joined = _joined(get_user(uid) or {})
    out, d = [], first
    while d <= last:
        rec = recs.get(d.isoformat())
        st_ = _status_for(d, rec, leave_set, offs, hols, t0, joined)
        ci = rec["check_in"] if rec else None
        co = rec["check_out"] if rec else None
        out.append({
            "date": d, "status": st_, "check_in": ci, "check_out": co,
            "hours": hours_between(ci, co),
            "late": bool(ci) and st_ in ("Present", "WFH") and is_late(ci, s),
            "holiday": hols.get(d.isoformat(), ""), "note": (rec["note"] if rec else "") or "",
        })
        d += timedelta(days=1)
    return out


def month_days(uid, year, month):
    n = calendar.monthrange(year, month)[1]
    return days_between(uid, date(year, month, 1), date(year, month, n))


def summarize(days):
    c = {k: 0 for k in STATUSES}
    late, hrs, mins, ins, working = 0, 0.0, 0, 0, 0
    for d in days:
        if d["status"] in c:
            c[d["status"]] += 1
        if d["status"] not in ("Off", "-"):
            working += 1
        if d["late"]:
            late += 1
        if d["hours"]:
            hrs += d["hours"]
        if d["check_in"]:
            t = _t(d["check_in"])
            mins += t.hour * 60 + t.minute
            ins += 1
    avg_in = None
    if ins:
        m = round(mins / ins)
        avg_in = f"{m // 60:02d}:{m % 60:02d}"
    leave_days = c["Leave"] + 0.5 * c["Half Day"]
    return {**c, "Late": late, "Hours": round(hrs, 1), "Avg In": avg_in,
            "Leave Days": leave_days, "Total Off": leave_days + c["Absent"],
            "Working Days": working}


def year_leave_used(uid, year):
    days = days_between(uid, date(year, 1, 1), date(year, 12, 31))
    return summarize(days)["Leave Days"]


def board(d):
    """Ellarodaiya status for date d."""
    s = get_settings()
    offs = off_weekdays(s)
    hols = {h["date"] for h in q("SELECT date FROM holidays WHERE date=?", (d.isoformat(),))}
    recs = {r["user_id"]: r for r in q("SELECT * FROM attendance WHERE date=?", (d.isoformat(),))}
    leaves = {r["user_id"] for r in q("SELECT user_id FROM leaves WHERE status='Approved' "
                                      "AND start_date<=? AND end_date>=?", (d.isoformat(), d.isoformat()))}
    t0 = today()
    rows = []
    for u in list_users(active_only=True):
        rec = recs.get(u["id"])
        st_ = _status_for(d, rec, {d} if u["id"] in leaves else set(), offs, hols, t0, _joined(u))
        ci = rec["check_in"] if rec else None
        co = rec["check_out"] if rec else None
        h = hours_between(ci, co)
        if ci and not co and d == t0:
            h = hours_between(ci, now().strftime("%H:%M:%S"))
        rows.append({
            "Name": u["name"], "Dept": u["dept"] or "-", "Status": st_,
            "In": ci[:5] if ci else "", "Out": co[:5] if co else "",
            "Hours": h if h is not None else "",
            "Late": "Late" if (ci and st_ in ("Present", "WFH") and is_late(ci, s)) else "",
            "Note": (rec["note"] if rec else "") or "",
        })
    return rows


def muster(year, month):
    rows = []
    for u in list_users(active_only=True):
        days = month_days(u["id"], year, month)
        sm = summarize(days)
        row = {"Name": u["name"], "Dept": u["dept"]}
        for dd in days:
            row[str(dd["date"].day)] = CODES.get(dd["status"], "")
        row.update({"Present": sm["Present"], "WFH": sm["WFH"], "Half Day": sm["Half Day"],
                    "Leave": sm["Leave"], "Absent": sm["Absent"], "Late": sm["Late"],
                    "Hours": sm["Hours"], "Avg In": sm["Avg In"] or ""})
        rows.append(row)
    return pd.DataFrame(rows)


def leaderboard(year, month):
    rows = []
    for u in list_users(active_only=True):
        days = month_days(u["id"], year, month)
        worked = [d for d in days if d["status"] in ("Present", "WFH") and d["check_in"]]
        on_time = sum(1 for d in worked if not d["late"])
        sm = summarize(days)
        rows.append({"Name": u["name"], "On-time days": on_time, "Late days": sm["Late"],
                     "Days worked": len(worked), "Avg In": sm["Avg In"] or "-",
                     "Hours": sm["Hours"], "Leave+Absent": sm["Total Off"]})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["On-time days", "Hours"], ascending=False).reset_index(drop=True)
        df.index += 1
    return df


# =====================================================================
#  UI HELPERS
# =====================================================================
def badge(text, color):
    return (f"<span style='background:{color};color:white;padding:3px 10px;border-radius:12px;"
            f"font-size:0.85rem;font-weight:600'>{text}</span>")


def calendar_html(days, year, month):
    head = "".join(f"<th style='padding:6px;color:#607d8b'>{d}</th>"
                   for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    by_day = {d["date"].day: d for d in days}
    t0 = today()
    rows = ""
    for week in calendar.monthcalendar(year, month):
        tds = ""
        for dnum in week:
            if dnum == 0:
                tds += "<td></td>"
                continue
            d = by_day[dnum]
            col = COLORS.get(d["status"], "#cfd8dc")
            label = d["holiday"] if d["holiday"] else d["status"]
            if d["status"] in ("-", "Not yet") and not d["holiday"]:
                label = ""
            sub = ""
            if d["check_in"]:
                sub = d["check_in"][:5] + (" ⚠️" if d["late"] else "")
            border = "3px solid #111" if d["date"] == t0 else "1px solid #eceff1"
            tds += (f"<td style='border:{border};border-radius:8px;padding:6px;height:74px;width:14%;"
                    f"vertical-align:top;background:{col}22'>"
                    f"<div style='font-weight:700'>{dnum}</div>"
                    f"<div style='font-size:0.72rem;color:{col};font-weight:700'>"
                    f"{ICONS.get(d['status'], '')} {label}</div>"
                    f"<div style='font-size:0.7rem;color:#455a64'>{sub}</div></td>")
        rows += f"<tr>{tds}</tr>"
    return (f"<table style='width:100%;border-collapse:separate;border-spacing:4px;text-align:left'>"
            f"<tr>{head}</tr>{rows}</table>")


def to_excel(df, sheet="Report"):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet[:31])
    return buf.getvalue()


def month_year_picker(key):
    t = today()
    c1, c2 = st.columns(2)
    m = c1.selectbox("Month", MONTHS, index=t.month - 1, key=f"{key}_m")
    y = c2.number_input("Year", min_value=2020, max_value=2100, value=t.year, step=1, key=f"{key}_y")
    return int(y), MONTHS.index(m) + 1


def kpi_row(sm):
    c = st.columns(6)
    c[0].metric("🏢 Office", sm["Present"])
    c[1].metric("🏠 WFH", sm["WFH"])
    c[2].metric("🌴 Leave", sm["Leave Days"])
    c[3].metric("❌ Absent", sm["Absent"])
    c[4].metric("⚠️ Late", sm["Late"])
    c[5].metric("⏱️ Avg in-time", sm["Avg In"] or "-")


def storage_warning():
    if not USE_PG:
        st.warning("⚠️ This app is using temporary storage (SQLite). On Streamlit Cloud, data is lost when the app "
                   "reboots or is redeployed. To keep data permanently, set `DATABASE_URL` (Postgres) in Secrets.")


# =====================================================================
#  PAGES
# =====================================================================
def auth_screen():
    s = get_settings()
    st.title(f"🗓️ {s['company']} - Attendance Portal")
    if user_count() == 0:
        st.info("First-time setup: the first account you create will be the **Admin**.")
        storage_warning()
        with st.form("setup"):
            name = st.text_input("Full name")
            username = st.text_input("Username")
            pin = st.text_input("PIN / Password (min 4)", type="password")
            company = st.text_input("Company / Team name", value=s["company"])
            if st.form_submit_button("Create admin"):
                ok, msg = create_user(username, name, pin, role="admin")
                if ok:
                    set_setting("company", company)
                    st.success("Admin account created. Please log in now.")
                    st.rerun()
                else:
                    st.error(msg)
        return

    tabs = st.tabs(["Login", "Sign up"] if s["allow_signup"] == "1" else ["Login"])
    with tabs[0]:
        with st.form("login"):
            username = st.text_input("Username")
            pin = st.text_input("PIN / Password", type="password")
            if st.form_submit_button("Login"):
                u = authenticate(username, pin)
                if u:
                    st.session_state["uid"] = u["id"]
                    st.rerun()
                else:
                    st.error("Incorrect username or PIN, or the account is inactive.")
    if len(tabs) > 1:
        with tabs[1]:
            with st.form("signup"):
                name = st.text_input("Full name", key="su_name")
                dept = st.text_input("Department / Team (optional)", key="su_dept")
                username = st.text_input("Choose a username", key="su_user")
                pin = st.text_input("Choose a PIN / Password (min 4)", type="password", key="su_pin")
                code = st.text_input("Join code", type="password", key="su_code") if s["signup_code"] else ""
                if st.form_submit_button("Create my account"):
                    if s["signup_code"] and code != s["signup_code"]:
                        st.error("Incorrect join code.")
                    else:
                        ok, msg = create_user(username, name, pin, dept=dept)
                        (st.success if ok else st.error)(msg + (" You can now log in from the Login tab." if ok else ""))


def page_my_day(u):
    t = now()
    d = t.date()
    rec = get_record(u["id"], d)
    s = get_settings()
    st.header(f"Welcome, {u['name']} 👋")
    st.caption(t.strftime("%A, %d %B %Y  |  %I:%M %p"))

    off = d.weekday() in off_weekdays(s) or any(h["date"] == d.isoformat() for h in list_holidays())
    if off and not rec:
        st.info("Today is a weekly off or holiday 🎉 (you can still check in if you are working).")
    leave_today = d in _approved_leave_set(u["id"], d, d)
    if leave_today and not rec:
        st.warning("Your leave is approved for today 🌴")

    with st.container(border=True):
        if not rec or not rec["check_in"]:
            st.subheader("Check-in")
            mode = st.radio("Where are you working from today?", ["Office 🏢", "Work From Home 🏠"], horizontal=True)
            note = st.text_input("Note (optional)", placeholder="e.g. client visit, doctor appointment...")
            if st.button("✅ Check in now", type="primary", disabled=leave_today):
                ok, msg = check_in(u["id"], "Present" if mode.startswith("Office") else "WFH", note)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
        elif not rec["check_out"]:
            late = is_late(rec["check_in"], s)
            st.subheader("You are checked in ✅")
            st.markdown(badge(rec["status"], COLORS[rec["status"]]) +
                        (" " + badge("Late", COLORS["Absent"]) if late else " " + badge("On time", COLORS["Present"])),
                        unsafe_allow_html=True)
            ci = datetime.combine(d, _t(rec["check_in"]))
            hrs = (t - ci).total_seconds() / 3600
            c1, c2 = st.columns(2)
            c1.metric("In-time", rec["check_in"][:5])
            c2.metric("Working since", f"{hrs:.1f} h")
            if st.button("🚪 Check out", type="primary"):
                ok, msg = check_out(u["id"])
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
        else:
            st.subheader("Day complete 🎉")
            hrs = hours_between(rec["check_in"], rec["check_out"])
            c1, c2, c3 = st.columns(3)
            c1.metric("In", rec["check_in"][:5])
            c2.metric("Out", rec["check_out"][:5])
            c3.metric("Hours", f"{hrs} h")
            st.markdown(badge(rec["status"], COLORS[rec["status"]]), unsafe_allow_html=True)

    st.subheader(f"{MONTHS[d.month - 1]} {d.year} - summary")
    sm = summarize(month_days(u["id"], d.year, d.month))
    kpi_row(sm)
    quota = quota_of(u, s)
    used = year_leave_used(u["id"], d.year)
    st.progress(min(used / quota, 1.0) if quota else 0.0,
                text=f"Leave balance {d.year}: {max(quota - used, 0):g} left (used {used:g} / {quota:g})")


def page_my_attendance(u):
    st.header("📅 My Attendance")
    y, m = month_year_picker("my")
    days = month_days(u["id"], y, m)
    sm = summarize(days)
    kpi_row(sm)
    c = st.columns(4)
    c[0].metric("Working days", sm["Working Days"])
    c[1].metric("Half days", sm["Half Day"])
    c[2].metric("Total leave + absent", sm["Total Off"])
    c[3].metric("Total hours", sm["Hours"])
    st.markdown(calendar_html(days, y, m), unsafe_allow_html=True)
    st.markdown(" ".join(badge(k, v) for k, v in COLORS.items() if k != "-"), unsafe_allow_html=True)
    df = pd.DataFrame([{
        "Date": d["date"].strftime("%d-%m-%Y"), "Day": d["date"].strftime("%a"),
        "Status": d["status"] + (f" ({d['holiday']})" if d["holiday"] else ""),
        "In": (d["check_in"] or "")[:5], "Out": (d["check_out"] or "")[:5],
        "Hours": d["hours"] if d["hours"] is not None else "",
        "Late": "Late" if d["late"] else "", "Note": d["note"],
    } for d in days])
    with st.expander("Detailed table"):
        st.dataframe(df, hide_index=True)
    st.download_button("⬇️ Download (Excel)", to_excel(df, "My Attendance"),
                       file_name=f"attendance_{u['username']}_{y}_{m:02d}.xlsx")


def page_leaves(u):
    st.header("🌴 Leaves")
    s = get_settings()
    quota = quota_of(u, s)
    used = year_leave_used(u["id"], today().year)
    c = st.columns(3)
    c[0].metric("Yearly quota", f"{quota:g}")
    c[1].metric("Used", f"{used:g}")
    c[2].metric("Balance", f"{quota - used:g}")

    with st.form("apply_leave"):
        st.subheader("Apply for leave")
        c1, c2 = st.columns(2)
        start = c1.date_input("From", value=today())
        end = c2.date_input("To", value=today())
        ltype = st.selectbox("Type", LEAVE_TYPES)
        reason = st.text_area("Reason")
        if st.form_submit_button("Submit request"):
            ok, msg = apply_leave(u["id"], start, end, ltype, reason)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()

    st.subheader("My requests")
    rows = list_leaves(uid=u["id"])
    if not rows:
        st.caption("You have not made any leave requests yet.")
    for r in rows:
        col = {"Pending": "#f9a825", "Approved": "#2e7d32", "Rejected": "#c62828"}[r["status"]]
        with st.container(border=True):
            a, b = st.columns([5, 1])
            a.markdown(f"**{r['start_date']} → {r['end_date']}**  |  {r['leave_type']}  \n{r['reason']}")
            b.markdown(badge(r["status"], col), unsafe_allow_html=True)
            if r["status"] == "Pending" and b.button("Cancel", key=f"cx{r['id']}"):
                cancel_leave(r["id"], u["id"])
                st.rerun()


def page_board(u):
    st.header("👥 Today's Board")
    d = st.date_input("Date", value=today())
    df = pd.DataFrame(board(d))
    if df.empty:
        st.info("No users found.")
        return
    c = st.columns(6)
    c[0].metric("🏢 In office", int((df.Status == "Present").sum()))
    c[1].metric("🏠 WFH", int((df.Status == "WFH").sum()))
    c[2].metric("🌗 Half day", int((df.Status == "Half Day").sum()))
    c[3].metric("🌴 Leave", int((df.Status == "Leave").sum()))
    c[4].metric("⏳ Not marked", int(df.Status.isin(["Not yet", "Absent"]).sum()))
    c[5].metric("⚠️ Late", int((df.Late == "Late").sum()))
    flt = st.multiselect("Filter status", sorted(df.Status.unique()))
    if flt:
        df = df[df.Status.isin(flt)]
    df = df.copy()
    df["Status"] = df["Status"].map(lambda s_: f"{ICONS.get(s_, '')} {s_}")
    st.dataframe(df, hide_index=True)
    st.button("🔄 Refresh")


def page_colleagues(u):
    """Read-only view of every colleague's attendance. No admin rights needed."""
    st.header("🧑‍🤝‍🧑 Colleagues")
    st.caption("Read-only view of your team's attendance. Only attendance details are shown.")
    y, m = month_year_picker("col")
    users = list_users(active_only=True)
    if not users:
        st.info("No users found.")
        return
    tab_all, tab_one = st.tabs(["Everyone - monthly summary", "Individual - day by day"])

    with tab_all:
        rows = []
        for x_ in users:
            sm = summarize(month_days(x_["id"], y, m))
            rows.append({
                "Name": x_["name"], "Dept": x_["dept"] or "-", "Office": sm["Present"], "WFH": sm["WFH"],
                "Half Day": sm["Half Day"], "Leave": sm["Leave Days"], "Absent": sm["Absent"],
                "Late": sm["Late"], "Total Hours": sm["Hours"], "Avg In-time": sm["Avg In"] or "-",
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True)
        st.download_button("⬇️ Download (Excel)", to_excel(df, f"{MONTHS[m - 1]} {y}"),
                           file_name=f"team_summary_{y}_{m:02d}.xlsx")

    with tab_one:
        labels = {f"{x_['name']} ({x_['dept'] or '-'})": x_ for x_ in users}
        who = st.selectbox("Colleague", list(labels))
        person = labels[who]
        days = month_days(person["id"], y, m)
        sm = summarize(days)
        kpi_row(sm)
        st.markdown(calendar_html(days, y, m), unsafe_allow_html=True)
        st.markdown(" ".join(badge(k, v) for k, v in COLORS.items() if k != "-"), unsafe_allow_html=True)
        df = pd.DataFrame([{
            "Date": d["date"].strftime("%d-%m-%Y"), "Day": d["date"].strftime("%a"),
            "Status": d["status"] + (f" ({d['holiday']})" if d["holiday"] else ""),
            "In": (d["check_in"] or "")[:5], "Out": (d["check_out"] or "")[:5],
            "Hours": d["hours"] if d["hours"] is not None else "",
            "Late": "Late" if d["late"] else "",
        } for d in days])
        with st.expander("Detailed table"):
            st.dataframe(df, hide_index=True)


def page_leaderboard(u):
    st.header("🏆 Punctuality Leaderboard")
    y, m = month_year_picker("lb")
    df = leaderboard(y, m)
    if df.empty:
        st.info("No data available.")
        return
    medals = ["🥇", "🥈", "🥉"]
    top = df.head(3)
    cols = st.columns(len(top))
    for i, (_, r) in enumerate(top.iterrows()):
        cols[i].metric(f"{medals[i]} {r['Name']}", f"{r['On-time days']} on-time", f"avg in {r['Avg In']}")
    st.dataframe(df)


def page_admin(u):
    st.header("🛠️ Admin")
    storage_warning()
    tabs = st.tabs(["Leave approvals", "Edit attendance", "Users", "Holidays", "Settings", "Reports"])

    with tabs[0]:
        pend = list_leaves(status="Pending")
        if not pend:
            st.success("No pending requests 👍")
        for r in pend:
            with st.container(border=True):
                a, b, c = st.columns([5, 1, 1])
                n = leave_days_count(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
                a.markdown(f"**{r['name']}** ({r['dept'] or '-'})  \n{r['start_date']} → {r['end_date']} "
                           f"({n} working day(s)) | {r['leave_type']}  \n_{r['reason']}_")
                if b.button("Approve", key=f"ap{r['id']}", type="primary"):
                    decide_leave(r["id"], "Approved", u["id"])
                    st.rerun()
                if c.button("Reject", key=f"rj{r['id']}"):
                    decide_leave(r["id"], "Rejected", u["id"])
                    st.rerun()
        with st.expander("All decided requests"):
            done = [r for r in list_leaves() if r["status"] != "Pending"]
            if done:
                st.dataframe(pd.DataFrame(done)[["name", "start_date", "end_date", "leave_type", "status", "reason"]],
                             hide_index=True)

    with tabs[1]:
        users = list_users()
        names = {f"{x_['name']} (@{x_['username']})": x_["id"] for x_ in users}
        who = st.selectbox("Employee", list(names))
        d = st.date_input("Date", value=today(), key="ed_date")
        rec = get_record(names[who], d) or {}
        status = st.selectbox("Status", STATUSES + ["Clear"],
                              index=(STATUSES.index(rec["status"]) if rec.get("status") in STATUSES else 0))
        c1, c2 = st.columns(2)
        ci = c1.time_input("In-time", value=_t(rec["check_in"]) if rec.get("check_in") else time(9, 30))
        co = c2.time_input("Out-time", value=_t(rec["check_out"]) if rec.get("check_out") else time(18, 0))
        use_t = st.checkbox("Save in/out times", value=bool(rec.get("check_in")))
        note = st.text_input("Note", value=rec.get("note") or "")
        if st.button("Save", type="primary"):
            set_status(names[who], d, status, ci.strftime("%H:%M:%S") if use_t else None,
                       co.strftime("%H:%M:%S") if use_t else None, note)
            st.success("Saved")

    with tabs[2]:
        st.caption("You can change role, leave quota and joining date here.")
        for x_ in list_users():
            with st.expander(f"{x_['name']} (@{x_['username']}) - {x_['role']}{'' if x_['active'] else ' [inactive]'}"):
                c = st.columns(4)
                dept = c[0].text_input("Dept", value=x_["dept"] or "", key=f"dp{x_['id']}")
                role = c[1].selectbox("Role", ["employee", "admin"], index=0 if x_["role"] == "employee" else 1,
                                      key=f"rl{x_['id']}")
                quota = c[2].number_input("Leave quota / year", value=float(quota_of(x_)), step=0.5,
                                          key=f"qt{x_['id']}")
                joined = c[3].date_input("Joining date", value=_joined(x_), key=f"jn{x_['id']}")
                active = st.checkbox("Active", value=bool(x_["active"]), key=f"ac{x_['id']}")
                c1, c2 = st.columns(2)
                if c1.button("Update", key=f"up{x_['id']}"):
                    if x_["id"] == u["id"] and (role != "admin" or not active):
                        st.error("You cannot remove your own admin access.")
                    else:
                        update_user(x_["id"], role=role, active=active, dept=dept, quota=quota, joined=joined)
                        st.success("Updated")
                        st.rerun()
                newpin = c2.text_input("New PIN", type="password", key=f"np{x_['id']}")
                if c2.button("Reset PIN", key=f"rp{x_['id']}"):
                    ok, msg = reset_pin(x_["id"], newpin)
                    (st.success if ok else st.error)(msg)

    with tabs[3]:
        c1, c2 = st.columns(2)
        hd = c1.date_input("Holiday date", key="hol_d")
        hn = c2.text_input("Holiday name", key="hol_n")
        if st.button("Add holiday"):
            add_holiday(hd, hn)
            st.rerun()
        for h in list_holidays(today().year):
            a, b = st.columns([6, 1])
            a.write(f"{h['date']}  -  {h['name']}")
            if b.button("🗑️", key=f"dh{h['date']}"):
                delete_holiday(h["date"])
                st.rerun()

    with tabs[4]:
        s = get_settings()
        with st.form("settings"):
            company = st.text_input("Company / Team name", value=s["company"])
            c1, c2 = st.columns(2)
            start = c1.time_input("Office start time", value=_t(s["office_start"]))
            grace = c2.number_input("Grace minutes", min_value=0, max_value=120, value=int(s["grace_min"]))
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            offs = st.multiselect("Weekly off days", day_names,
                                  default=[day_names[i] for i in sorted(off_weekdays(s))])
            quota = st.number_input("Default leave quota / year", min_value=0.0,
                                    value=float(s["default_leave_quota"]), step=0.5)
            tz = st.text_input("Timezone", value=s["timezone"])
            signup = st.checkbox("Anyone can sign up", value=s["allow_signup"] == "1")
            code = st.text_input("Sign-up join code (optional - if set, only people who know the code can sign up)",
                                 value=s["signup_code"])
            if st.form_submit_button("Save settings"):
                set_setting("company", company)
                set_setting("office_start", start.strftime("%H:%M"))
                set_setting("grace_min", int(grace))
                set_setting("weekly_off", ",".join(str(day_names.index(d)) for d in offs))
                set_setting("default_leave_quota", quota)
                set_setting("timezone", tz)
                set_setting("allow_signup", "1" if signup else "0")
                set_setting("signup_code", code.strip())
                st.success("Saved")

    with tabs[5]:
        y, m = month_year_picker("rep")
        df = muster(y, m)
        st.dataframe(df, hide_index=True)
        st.caption("P = Office, W = WFH, HD = Half day, L = Leave, A = Absent, Off = holiday/weekly off")
        st.download_button("⬇️ Monthly muster (Excel)", to_excel(df, f"{MONTHS[m - 1]} {y}"),
                           file_name=f"muster_{y}_{m:02d}.xlsx")
        rows = q("SELECT u.name, a.date, a.status, a.check_in, a.check_out, a.note FROM attendance a "
                 "JOIN users u ON u.id=a.user_id ORDER BY a.date DESC, u.name")
        st.download_button("⬇️ All raw records (CSV)", pd.DataFrame(rows).to_csv(index=False),
                           file_name="attendance_raw.csv")


# =====================================================================
#  MAIN
# =====================================================================
@st.cache_resource
def _setup_once():
    init_db()
    return True


PAGES = {
    "🏠 My Day": page_my_day, "📅 My Attendance": page_my_attendance, "🌴 Leaves": page_leaves,
    "👥 Today's Board": page_board, "🧑‍🤝‍🧑 Colleagues": page_colleagues, "🏆 Leaderboard": page_leaderboard, "🛠️ Admin": page_admin,
}


def main():
    st.set_page_config(page_title="Attendance Portal", page_icon="🗓️", layout="wide")
    _setup_once()
    uid = st.session_state.get("uid")
    user = get_user(uid) if uid else None
    if user and user["role"] != "admin" and user["username"] in _bootstrap_admins():
        update_user(user["id"], role="admin")
        user = get_user(uid)
    if not user or not user["active"]:
        st.session_state.pop("uid", None)
        auth_screen()
        return

    with st.sidebar:
        st.markdown(f"### 🗓️ {get_settings()['company']}")
        st.write(f"**{user['name']}**  \n{user['dept'] or ''} · {user['role']}")
        names = list(PAGES)
        if user["role"] != "admin":
            names.remove("🛠️ Admin")
        page = st.radio("Menu", names, label_visibility="collapsed")
        if st.button("Logout"):
            st.session_state.pop("uid", None)
            st.rerun()
    PAGES[page](user)


if __name__ == "__main__":
    main()
