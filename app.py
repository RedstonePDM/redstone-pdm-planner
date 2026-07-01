"""
Redstone PDM - Planning & Allocation Tool
==========================================
Module 2: Weekly planning board for job allocation to contractors.
Reads from the wisdom-sync PostgreSQL database.
"""

import os
import json
import base64
import psycopg2
import psycopg2.extras
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from functools import wraps
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

app = Flask(__name__, template_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", "redstone-pdm-2024")

DATABASE_URL    = os.environ["DATABASE_URL"]
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "redstone2024")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "info@redstonepdm.com")
JOBCARD_URL     = os.environ.get("JOBCARD_URL", "https://redstone-pdm-jobcard-production.up.railway.app")
TEST_MODE       = os.environ.get("TEST_MODE", "false").lower() == "true"
TEST_EMAIL      = os.environ.get("TEST_EMAIL", "dave@redstonepdm.com")

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
            status          TEXT DEFAULT 'draft',
            published_at    TIMESTAMPTZ,
            reopened_at     TIMESTAMPTZ,
            revision_count  INTEGER DEFAULT 0,
            published_by    TEXT DEFAULT 'admin'
        );
    """)

    # Add status/revision columns if upgrading from old schema
    try:
        cur.execute("ALTER TABLE published_weeks ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft'")
        cur.execute("ALTER TABLE published_weeks ADD COLUMN IF NOT EXISTS reopened_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE published_weeks ADD COLUMN IF NOT EXISTS revision_count INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        conn.rollback()

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
    start = datetime.strptime(week_start, "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range(7)]


def get_current_week_start():
    today = date.today()
    return today - timedelta(days=today.weekday())


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(to_address, subject, body_html):
    if not SENDGRID_API_KEY:
        print(f"No SendGrid key — skipping email to {to_address}")
        return False
    try:
        if TEST_MODE:
            print(f"TEST MODE: redirecting email (was to {to_address}) to {TEST_EMAIL}")
            subject = f"[TEST] {subject}"
            to_address = TEST_EMAIL
        message = Mail(
            from_email=FROM_EMAIL,
            to_emails=to_address,
            subject=subject,
            html_content=body_html,
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        print(f"Email sent to {to_address}: {response.status_code}")
        return response.status_code in (200, 202)
    except Exception as e:
        print(f"Email error to {to_address}: {e}")
        return False


def get_contractor_emails():
    """Get email addresses for all contractors from the contractors_db table."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT name, email FROM contractors_db WHERE status='active' AND email IS NOT NULL")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {r["name"]: r["email"] for r in rows}
    except Exception as e:
        print(f"Could not fetch contractor emails: {e}")
        return {}


def send_schedule_emails(week_start_str, is_revision=False):
    """Send schedule emails to all engineers with allocations this week."""
    week_dt = datetime.strptime(week_start_str, "%Y-%m-%d").date()
    week_end = week_dt + timedelta(days=4)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT a.contractor, a.job_id, a.day_date, a.notes, a.is_survey,
               j.pub_name, j.postcode, j.description, j.trade_type, j.display_id
        FROM allocations a
        JOIN jobs j ON j.job_id = a.job_id
        WHERE a.week_start = %s
        ORDER BY a.contractor, a.day_date
    """, (week_dt,))
    allocs = cur.fetchall()
    cur.close()
    conn.close()

    if not allocs:
        print("No allocations found — no emails sent")
        return 0

    # Group by contractor
    from collections import defaultdict
    by_contractor = defaultdict(list)
    for a in allocs:
        by_contractor[a["contractor"]].append(a)

    emails = get_contractor_emails()
    sent = 0
    subject_prefix = "⚠ REVISED SCHEDULE" if is_revision else "Your Schedule"

    for contractor_name, jobs in by_contractor.items():
        email = emails.get(contractor_name)
        if not email:
            print(f"No email for {contractor_name} — skipping")
            continue

        # Build job table rows grouped by day
        from itertools import groupby
        jobs_sorted = sorted(jobs, key=lambda x: x["day_date"])
        rows_html = ""
        for day_date, day_jobs in groupby(jobs_sorted, key=lambda x: x["day_date"]):
            day_jobs = list(day_jobs)
            day_label = day_date.strftime("%A, %d %b")
            for j in day_jobs:
                survey_tag = " <strong style='color:#e67e22'>[SURVEY]</strong>" if j["is_survey"] else ""
                notes_row = f"<br><em style='color:#c0392b'>{j['notes']}</em>" if j["notes"] else ""
                rows_html += f"""
                <tr>
                  <td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#555;font-size:13px;white-space:nowrap'>{day_label}</td>
                  <td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;font-weight:600;font-size:13px'>{j['pub_name'] or '—'}{survey_tag}</td>
                  <td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;font-size:12px;color:#666'>{j['postcode'] or '—'}</td>
                  <td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;font-size:12px;color:#555'>{(j['description'] or '')[:60]}{'...' if len(j['description'] or '') > 60 else ''}{notes_row}</td>
                </tr>"""

        revision_banner = ""
        if is_revision:
            revision_banner = """
            <div style='background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:14px 16px;margin-bottom:20px'>
              <strong style='color:#856404'>⚠ Your schedule has been updated.</strong>
              <div style='color:#856404;font-size:13px;margin-top:4px'>Please review your jobs below — changes have been made to your week.</div>
            </div>"""

        body = f"""
        <div style='font-family:Segoe UI,sans-serif;max-width:640px;margin:0 auto'>
          <div style='background:#1a2332;padding:20px 24px;border-radius:10px 10px 0 0'>
            <span style='color:white;font-size:20px;font-weight:800'>Redstone <span style='color:#c0392b'>PDM</span></span>
          </div>
          <div style='background:white;padding:24px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 10px 10px'>
            <p style='font-size:15px;color:#1a2332;margin-bottom:8px'>Hi <strong>{contractor_name.split()[0]}</strong>,</p>
            {revision_banner}
            <p style='color:#555;font-size:13px;margin-bottom:16px'>
              {'Your updated schedule' if is_revision else 'Your confirmed schedule'} for the week commencing
              <strong>{week_dt.strftime('%d %B %Y')}</strong>:
            </p>
            <table style='width:100%;border-collapse:collapse;margin-bottom:20px'>
              <thead>
                <tr style='background:#1a2332'>
                  <th style='padding:10px 12px;color:white;text-align:left;font-size:12px'>Day</th>
                  <th style='padding:10px 12px;color:white;text-align:left;font-size:12px'>Site</th>
                  <th style='padding:10px 12px;color:white;text-align:left;font-size:12px'>Postcode</th>
                  <th style='padding:10px 12px;color:white;text-align:left;font-size:12px'>Works</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <div style='background:#f5f6f8;border-radius:8px;padding:14px 16px;margin-bottom:16px'>
              <p style='font-size:13px;color:#555;margin:0'>
                Log in to <a href='{JOBCARD_URL}' style='color:#c0392b;font-weight:600'>Redstone PDM</a>
                to view your jobs and submit job cards. Complete your job card on the day of each visit.
              </p>
            </div>
            <p style='font-size:11px;color:#aaa;margin:0'>Redstone PDM &nbsp;·&nbsp; {week_dt.strftime('%d %B %Y')}</p>
          </div>
        </div>"""

        if send_email(email, f"Redstone PDM — {subject_prefix} w/c {week_dt.strftime('%d %b %Y')}", body):
            sent += 1

    return sent


# ── Main Planner ──────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def planner():
    week_start_str = request.args.get("week", get_current_week_start().strftime("%Y-%m-%d"))
    week_start = datetime.strptime(week_start_str, "%Y-%m-%d").date()
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    prev_week = (week_start - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (week_start + timedelta(days=7)).strftime("%Y-%m-%d")

    # Get publish status for this week
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status, revision_count FROM published_weeks WHERE week_start=%s", (week_start_str,))
        pub = cur.fetchone()
        week_status = pub["status"] if pub else "draft"
        revision_count = pub["revision_count"] if pub else 0
    except Exception:
        conn.rollback()
        week_status = "draft"
        revision_count = 0
    cur.close()
    conn.close()

    return render_template(
        "planner.html",
        contractors=CONTRACTORS,
        week_start=week_start_str,
        week_dates=week_dates,
        prev_week=prev_week,
        next_week=next_week,
        week_status=week_status,
        revision_count=revision_count,
    )


# ── API: Publish / Reopen ────────────────────────────────────────────────────

@app.route("/api/publish", methods=["POST"])
@login_required
def api_publish():
    data = request.json
    week_start = data.get("week_start")
    if not week_start:
        return jsonify({"success": False, "error": "Missing week_start"})

    conn = get_db()
    cur = conn.cursor()
    try:
        # Check if this is a revision
        cur.execute("SELECT revision_count FROM published_weeks WHERE week_start=%s", (week_start,))
        existing = cur.fetchone()
        is_revision = existing is not None
        rev_count = (existing["revision_count"] or 0) + 1 if is_revision else 0

        cur.execute("""
            INSERT INTO published_weeks (week_start, status, published_at, revision_count, published_by)
            VALUES (%s, 'published', NOW(), %s, 'admin')
            ON CONFLICT (week_start) DO UPDATE
            SET status='published', published_at=NOW(), revision_count=%s
        """, (week_start, rev_count, rev_count))
        conn.commit()

        # Send emails
        sent = send_schedule_emails(week_start, is_revision=is_revision)

        return jsonify({
            "success": True,
            "is_revision": is_revision,
            "emails_sent": sent,
            "revision_count": rev_count,
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)})
    finally:
        cur.close()
        conn.close()


@app.route("/api/reopen", methods=["POST"])
@login_required
def api_reopen():
    data = request.json
    week_start = data.get("week_start")
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO published_weeks (week_start, status, reopened_at)
            VALUES (%s, 'draft', NOW())
            ON CONFLICT (week_start) DO UPDATE
            SET status='draft', reopened_at=NOW()
        """, (week_start,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)})
    finally:
        cur.close()
        conn.close()


@app.route("/api/week_status")
@login_required
def api_week_status():
    week_start = request.args.get("week")
    if not week_start:
        return jsonify({"status": "draft", "revision_count": 0})
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status, revision_count FROM published_weeks WHERE week_start=%s", (week_start,))
        row = cur.fetchone()
        return jsonify({
            "status": row["status"] if row else "draft",
            "revision_count": row["revision_count"] if row else 0,
        })
    except Exception:
        conn.rollback()
        return jsonify({"status": "draft", "revision_count": 0})
    finally:
        cur.close()
        conn.close()


# ── API: Jobs ─────────────────────────────────────────────────────────────────

@app.route("/api/jobs/unallocated")
@login_required
def api_unallocated_jobs():
    week_start = request.args.get("week", get_current_week_start().strftime("%Y-%m-%d"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT j.job_id, j.display_id, j.tab, j.tab_label, j.pub_name,
               j.location_code, j.postcode, j.trade_type, j.sub_trade_type,
               j.description, j.due_date, j.due_time, j.status
        FROM jobs j
        WHERE j.tab IN ('CALLOUT', 'QUOTEREQUEST', 'QUOTE', 'MIV', 'PPM')
        ORDER BY j.due_date ASC NULLS LAST, j.tab ASC, j.pub_name ASC
    """)
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
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO allocations (week_start, job_id, contractor, day_date, notes, is_survey)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (data["week_start"], data["job_id"], data["contractor"],
              data["day_date"], data.get("notes",""), data.get("is_survey",False)))
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
        """, (data.get("notes",""), allocation_id))
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
            ON CONFLICT (week_start, contractor, day_date) DO UPDATE SET status=%s
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
    cur.execute("SELECT contractor, day_date, status FROM contractor_days WHERE week_start=%s", (week_start,))
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


try:
    init_db()
except Exception as e:
    print(f"Warning: init_db failed: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
