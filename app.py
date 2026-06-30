"""
Redstone PDM - Planning & Allocation Tool
==========================================
Module 2: Weekly planning board for job allocation to contractors.
Reads from the wisdom-sync PostgreSQL database.
"""

import os
import json
import psycopg2
import psycopg2.extras
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from functools import wraps

app = Flask(__name__, template_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", "redstone-pdm-2024")

DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "redstone2024")

CONTRACTORS = [
    "Ashley Everett",
    "Dave Lefevre",
    "Mark Ashpool",
    "Aziz Rehman",
    "Dave Duppa",
    "Richard Chambers",
    "Cassius Kwarteng",
    "James Rutland",
    "Dwain Hinze",
    "Ajax Smartfit",
]

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # First pass: drop the unique constraint if it exists from an earlier version
    try:
        cur.execute("""
            ALTER TABLE allocations 
            DROP CONSTRAINT IF EXISTS allocations_week_start_job_id_contractor_day_date_key
        """)
        conn.commit()
    except Exception:
        conn.rollback()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS allocations (
            id              SERIAL PRIMARY KEY,
            week_start      DATE NOT NULL,
            job_id          TEXT NOT NULL,
            contractor      TEXT NOT NULL,
            day_date        DATE NOT NULL,
            notes           TEXT DEFAULT '',
            is_survey       BOOLEAN DEFAULT FALSE,
            sort_order      INTEGER DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        );

        ALTER TABLE allocations DROP CONSTRAINT IF EXISTS allocations_week_start_job_id_contractor_day_date_key;

        CREATE TABLE IF NOT EXISTS contractor_days (
            id              SERIAL PRIMARY KEY,
            week_start      DATE NOT NULL,
            contractor      TEXT NOT NULL,
            day_date        DATE NOT NULL,
            status          TEXT DEFAULT 'available',
            UNIQUE(week_start, contractor, day_date)
        );

        CREATE TABLE IF NOT EXISTS published_weeks (
            week_start      DATE PRIMARY KEY,
            published_at    TIMESTAMPTZ DEFAULT NOW(),
            published_by    TEXT DEFAULT 'admin'
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("planner"))
        error = "Incorrect password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Helper Functions ──────────────────────────────────────────────────────────

def get_week_dates(week_start):
    """Return list of 7 dates for the week starting on week_start."""
    start = datetime.strptime(week_start, "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range(7)]


def get_current_week_start():
    """Return the Monday of the current week."""
    today = date.today()
    return today - timedelta(days=today.weekday())


# ── Main Planner ──────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def planner():
    week_start_str = request.args.get("week", get_current_week_start().strftime("%Y-%m-%d"))
    week_start = datetime.strptime(week_start_str, "%Y-%m-%d").date()
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    prev_week = (week_start - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (week_start + timedelta(days=7)).strftime("%Y-%m-%d")

    return render_template(
        "planner.html",
        contractors=CONTRACTORS,
        week_start=week_start_str,
        week_dates=week_dates,
        prev_week=prev_week,
        next_week=next_week,
    )


# ── API: Jobs ─────────────────────────────────────────────────────────────────

@app.route("/api/jobs/unallocated")
@login_required
def api_unallocated_jobs():
    week_start = request.args.get("week", get_current_week_start().strftime("%Y-%m-%d"))
    conn = get_db()
    cur = conn.cursor()

    # Get all active jobs not yet allocated this week
    cur.execute("""
        SELECT j.job_id, j.display_id, j.tab, j.tab_label, j.pub_name,
               j.location_code, j.postcode, j.trade_type, j.sub_trade_type,
               j.description, j.due_date, j.due_time, j.status
        FROM jobs j
        WHERE j.tab IN ('CALLOUT', 'QUOTEREQUEST', 'QUOTE', 'MIV', 'PPM')
        ORDER BY j.due_date ASC NULLS LAST, j.tab ASC, j.pub_name ASC
    """, ())

    jobs = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(jobs)


@app.route("/api/jobs/allocated")
@login_required
def api_allocated_jobs():
    week_start = request.args.get("week", get_current_week_start().strftime("%Y-%m-%d"))
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT a.id, a.job_id, a.contractor, a.day_date, a.notes, a.is_survey, a.sort_order,
               j.display_id, j.tab, j.tab_label, j.pub_name, j.location_code,
               j.postcode, j.trade_type, j.description, j.due_date, j.due_time
        FROM allocations a
        JOIN jobs j ON j.job_id = a.job_id
        WHERE a.week_start = %s
        ORDER BY a.contractor, a.day_date, a.sort_order
    """, (week_start,))

    allocations = [dict(r) for r in cur.fetchall()]
    # Convert dates to strings
    for a in allocations:
        if a.get("day_date"):
            a["day_date"] = a["day_date"].isoformat()
        if a.get("due_date"):
            a["due_date"] = str(a["due_date"])
    cur.close()
    conn.close()
    return jsonify(allocations)


# ── API: Allocations ──────────────────────────────────────────────────────────

@app.route("/api/allocate", methods=["POST"])
@login_required
def api_allocate():
    data = request.json
    week_start = data["week_start"]
    job_id = data["job_id"]
    contractor = data["contractor"]
    day_date = data["day_date"]
    notes = data.get("notes", "")
    is_survey = data.get("is_survey", False)

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO allocations (week_start, job_id, contractor, day_date, notes, is_survey)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (week_start, job_id, contractor, day_date, notes, is_survey))
        result = cur.fetchone()
        conn.commit()
        return jsonify({"success": True, "id": result["id"]})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/allocate/<int:allocation_id>", methods=["DELETE"])
@login_required
def api_deallocate(allocation_id):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM allocations WHERE id = %s", (allocation_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/allocate/<int:allocation_id>/notes", methods=["PATCH"])
@login_required
def api_update_notes(allocation_id):
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE allocations SET notes=%s, updated_at=NOW() WHERE id=%s
        """, (data.get("notes", ""), allocation_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/contractor-day", methods=["POST"])
@login_required
def api_set_contractor_day():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO contractor_days (week_start, contractor, day_date, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (week_start, contractor, day_date)
            DO UPDATE SET status=%s
        """, (data["week_start"], data["contractor"], data["day_date"],
              data["status"], data["status"]))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/contractor-days")
@login_required
def api_get_contractor_days():
    week_start = request.args.get("week")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT contractor, day_date, status
        FROM contractor_days WHERE week_start=%s
    """, (week_start,))
    rows = cur.fetchall()
    result = {}
    for r in rows:
        key = f"{r['contractor']}_{r['day_date'].isoformat()}"
        result[key] = r["status"]
    cur.close()
    conn.close()
    return jsonify(result)


# ── Contractor View ───────────────────────────────────────────────────────────

@app.route("/week/<contractor_slug>")
def contractor_week(contractor_slug):
    week_start = request.args.get("week", get_current_week_start().strftime("%Y-%m-%d"))

    # Match contractor name from slug
    contractor = None
    for c in CONTRACTORS:
        if c.lower().replace(" ", "-") == contractor_slug:
            contractor = c
            break

    if not contractor:
        return "Contractor not found", 404

    week_start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
    week_dates = [week_start_date + timedelta(days=i) for i in range(7)]

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT a.id, a.job_id, a.contractor, a.day_date, a.notes, a.is_survey,
               j.display_id, j.tab_label, j.pub_name, j.location_code,
               j.postcode, j.trade_type, j.description, j.due_time
        FROM allocations a
        JOIN jobs j ON j.job_id = a.job_id
        WHERE a.week_start=%s AND a.contractor=%s
        ORDER BY a.day_date, a.is_survey, a.sort_order
    """, (week_start, contractor))

    allocations = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT day_date, status FROM contractor_days
        WHERE week_start=%s AND contractor=%s
    """, (week_start, contractor))
    day_statuses = {r["day_date"]: r["status"] for r in cur.fetchall()}

    cur.close()
    conn.close()

    # Group by day
    by_day = {}
    for d in week_dates:
        by_day[d] = [a for a in allocations if a["day_date"] == d]

    prev_week = (week_start_date - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (week_start_date + timedelta(days=7)).strftime("%Y-%m-%d")

    return render_template(
        "contractor.html",
        contractor=contractor,
        week_start=week_start,
        week_dates=week_dates,
        by_day=by_day,
        day_statuses=day_statuses,
        prev_week=prev_week,
        next_week=next_week,
        contractor_slug=contractor_slug,
    )


# Run init_db when module loads (works with both gunicorn and direct execution)
try:
    init_db()
except Exception as e:
    print(f"Warning: init_db failed: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
