from flask import Flask, request, jsonify, render_template, send_file, abort
import re
import mysql.connector
from mysql.connector import Error, IntegrityError, pooling
import time
import traceback
import os
import uuid
import json
import secrets
import hashlib
import smtplib
from email.message import EmailMessage
from werkzeug.utils import secure_filename
from datetime import date, datetime, timedelta
from countries import normalize_country, validate_national_id, public_config, COUNTRY_NAMES

# A donor must wait this many days after their last donation before donating again.
WAITING_DAYS = 90

app = Flask(__name__, template_folder=".")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # max request size: 10 MB
HEALTH_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "private_uploads", "health_reports")
ALLOWED_HEALTH_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}


# ---------------------------------------------------------------------------
# Email (SMTP) configuration
#
# Settings are read from, in this order:
#   1. real environment variables (SMTP_EMAIL, SMTP_PASSWORD, ...)
#   2. a file called ".env" placed next to data.py  (easiest - no terminal needed)
# ---------------------------------------------------------------------------
BUILD = "v5 - 2026-10-31 (country + national ID per country)"
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
ENV_FOUND = False

def _load_dotenv():
    global ENV_FOUND
    path = ENV_PATH
    if not os.path.exists(path):
        return
    ENV_FOUND = True
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and val and not os.environ.get(key):
                os.environ[key] = val
_load_dotenv()


def _refresh_smtp_settings():
    """Re-read .env + environment on every send, so editing .env never needs a Flask restart."""
    global SMTP_HOST, SMTP_PORT, SMTP_EMAIL, SMTP_PASSWORD, ADMIN_CODE_RECEIVER, REPORT_RECEIVER
    _load_dotenv()
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "").strip()
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
    ADMIN_CODE_RECEIVER = os.environ.get("ADMIN_CODE_RECEIVER", "").strip() or SMTP_EMAIL
    REPORT_RECEIVER = os.environ.get("REPORT_RECEIVER", "").strip() or SMTP_EMAIL

# MySQL settings (put DB_PASSWORD in .env so replacing data.py never wipes it)
DB_HOST = os.environ.get("DB_HOST", "localhost").strip()
DB_USER = os.environ.get("DB_USER", "root").strip()
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "bloodconnect").strip()
DB_PORT = int(os.environ.get("DB_PORT", "3306") or 3306)

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "").strip()
# Google shows app passwords as "abcd efgh ijkl mnop" - the spaces must be removed.
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "BloodConnect")
# The built-in admin signup uses admin@gmail.com, a mailbox nobody here owns.
# Admin verification codes are therefore delivered to this address instead.
ADMIN_SIGNUP_EMAIL = "admin@gmail.com"
ADMIN_CODE_RECEIVER = os.environ.get("ADMIN_CODE_RECEIVER", "").strip() or SMTP_EMAIL
# "Report an issue" messages from the Contact page are emailed to this address.
# Replying to that email in Gmail goes straight to the person who reported.
REPORT_RECEIVER = os.environ.get("REPORT_RECEIVER", "").strip() or SMTP_EMAIL
_report_times = {}      # email -> last submit time (simple spam limit)
EMAIL_CODE_TTL_SECONDS = 10 * 60
_email_codes = {}       # email -> {hash, expires, attempts}
_verified_email_tokens = {}  # token -> {email, expires}

def normalize_email(value):
    return str(value or "").strip().lower()

def friendly_smtp_error(exc):
    """Turn raw smtplib/socket errors into a message that tells you what to fix."""
    if isinstance(exc, RuntimeError):
        return str(exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("Gmail rejected the login. Use a 16-character Google App Password "
                "(2-Step Verification must be ON), not your normal Gmail password.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The recipient email address was rejected. Check that it is correct."
    if isinstance(exc, (smtplib.SMTPConnectError, ConnectionError, TimeoutError, OSError)):
        return ("Could not connect to the mail server (%s:%s). Check your internet connection, "
                "firewall/antivirus, or try SMTP_PORT=465." % (SMTP_HOST, SMTP_PORT))
    return "Email sending failed: %s" % exc

def _send_via_brevo_api(api_key, to_email, subject, body, reply_to=None):
    """Send over HTTPS (port 443). Needed on hosts that block SMTP ports (e.g. Render free tier)."""
    import json as _json
    import urllib.request as _urlreq
    import urllib.error as _urlerr
    payload = {
        "sender": {"name": SMTP_FROM_NAME, "email": SMTP_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body,
    }
    if reply_to:
        payload["replyTo"] = {"email": reply_to.split("<")[-1].replace(">", "").strip()}
    req = _urlreq.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"api-key": api_key, "content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=20) as resp:
            resp.read()
    except _urlerr.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError("Brevo email API error %s: %s" % (exc.code, detail))

def send_email(to_email, subject, body, reply_to=None):
    _refresh_smtp_settings()
    brevo_key = os.environ.get("BREVO_API_KEY", "").strip()
    if brevo_key:
        if not SMTP_EMAIL:
            raise RuntimeError("Set SMTP_EMAIL to the sender address you verified in Brevo.")
        _send_via_brevo_api(brevo_key, to_email, subject, body, reply_to)
        return
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        raise RuntimeError("Email is not configured on the server. Set SMTP_EMAIL and SMTP_PASSWORD "
                           "(Gmail App Password) as environment variables on your host, or in the .env "
                           "file next to data.py (" + ENV_PATH + ").")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_EMAIL}>"
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

def try_send_email(to_email, subject, body):
    """Send an email and return (sent, error_text) instead of raising, so the admin can be told if it failed."""
    try:
        send_email(to_email, subject, body)
        return True, ""
    except Exception as exc:
        app.logger.error("Email to %s failed: %s", to_email, exc)
        return False, friendly_smtp_error(exc)


def code_hash(email, code):
    return hashlib.sha256((email + ":" + code).encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Connection pooling
#
# Previously every request opened a brand-new mysql.connector.connect(),
# and several routes never closed the connection if an exception happened
# in between (no try/finally). The frontend also polls three endpoints
# every 3 seconds, so leaked connections built up fast and eventually hit
# MySQL's default max_connections limit -> "1040 Too many connections".
#
# Fix: keep a small pool of real connections open and hand them out/take
# them back. Every route below now also uses try/finally so a connection
# is ALWAYS returned to the pool, even when something goes wrong.
# ---------------------------------------------------------------------------
_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="bloodconnect_pool",
            pool_size=10,              # raise this if you expect more concurrent users/tabs
            pool_reset_session=True,
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
        )
    return _pool


def db():
    """Borrow a connection from the pool. Calling .close() on it returns
    it to the pool instead of actually closing the TCP connection."""
    return get_pool().get_connection()


def ok(data=None, message=""):
    return jsonify(success=True, message=message, data=data)


def fail(message, status=400):
    return jsonify(success=False, message=message), status



@app.errorhandler(404)
def handle_not_found(error):
    if request.path.startswith('/api/'):
        return jsonify(success=False, message='API endpoint not found: ' + request.path), 404
    return error

@app.errorhandler(500)
def handle_internal_error(error):
    # Log the real exception so database/schema problems can be diagnosed.
    app.logger.error('Unhandled error on %s %s: %s', request.method, request.path, error)
    app.logger.error(traceback.format_exc())
    if request.path.startswith('/api/'):
        return jsonify(success=False, message='Chat/API server error. Check the Flask terminal for the exact database error, then restart Flask.'), 500
    return error


def get_authenticated_user(cur, payload=None, require_admin=False):
    payload = payload or {}
    raw_id = request.headers.get("X-User-ID") or payload.get("userId")
    try:
        user_id = int(raw_id)
    except (TypeError, ValueError):
        return None, "Please sign up or log in before continuing."
    cur.execute("SELECT id,name,email,is_admin,is_blocked FROM users WHERE id=%s LIMIT 1", (user_id,))
    user = cur.fetchone()
    if not user:
        return None, "Your session is no longer valid. Please log in again."
    # Dictionary cursors are used by admin routes; tuple cursors by others.
    is_admin = user.get("is_admin", False) if isinstance(user, dict) else user[3]
    is_blocked = user.get("is_blocked", False) if isinstance(user, dict) else user[4]
    if is_blocked and not is_admin:
        return None, "Your account has been blocked by the admin for misbehaviour or abuse. Please contact support."
    if require_admin and not bool(is_admin):
        return None, "Only an admin can perform this action."
    return user, None


def check_identity(d):
    """
    Validate the country and the national ID number (CNIC, Aadhaar, SSN, ...) sent by a form.
    Returns (country_code, cleaned_id, error_message). error_message is "" when everything is fine.
    The ID is stored in the legacy `phone` column and is never sent back to the browser.
    """
    country = normalize_country(d.get("country"))
    if not country:
        return None, "", "Please select your country."
    ok_, cleaned, error = validate_national_id(country, d.get("phone"))
    if not ok_:
        return country, "", error
    return country, cleaned, ""


_REPORT_MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def _fs_conn():
    return mysql.connector.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME)


def _fs_name(stored):
    return os.path.basename(str(stored or "").replace("\\", "/"))


def file_store_put(path):
    """Copy a saved report file into the database (key = its file name). Never raises."""
    try:
        name = _fs_name(path)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if not name or ext not in _REPORT_MIME or not os.path.isfile(path):
            return False
        with open(path, "rb") as fh:
            data = fh.read()
        con = _fs_conn(); cur = con.cursor()
        try:
            cur.execute("REPLACE INTO stored_files (name,mime,size,data) VALUES (%s,%s,%s,%s)",
                        (name, _REPORT_MIME[ext], len(data), data))
            con.commit()
        finally:
            cur.close(); con.close()
        return True
    except Exception:
        app.logger.exception("Could not store report file in the database (it is still on disk): %s", path)
        return False


def file_store_delete(stored):
    try:
        con = _fs_conn(); cur = con.cursor()
        try:
            cur.execute("DELETE FROM stored_files WHERE name=%s", (_fs_name(stored),))
            con.commit()
        finally:
            cur.close(); con.close()
    except Exception:
        app.logger.exception("Could not delete stored report file")


def serve_report(stored, disk_finder):
    """Return a Flask response for a report: from the database first, otherwise from disk
    (and then copy it into the database so it is safe from now on). None if it is nowhere."""
    name = _fs_name(stored)
    try:
        con = _fs_conn(); cur = con.cursor()
        try:
            cur.execute("SELECT mime,data FROM stored_files WHERE name=%s", (name,))
            row = cur.fetchone()
        finally:
            cur.close(); con.close()
    except Exception:
        app.logger.exception("Could not read report from the database")
        row = None
    if row:
        resp = app.response_class(bytes(row[1]), mimetype=row[0])
        resp.headers["Content-Disposition"] = 'inline; filename="' + name + '"'
    else:
        path = disk_finder(stored)
        if not path:
            return None
        file_store_put(path)
        mime = _REPORT_MIME.get(path.rsplit(".", 1)[-1].lower(), "application/octet-stream")
        resp = send_file(path, mimetype=mime, as_attachment=False, download_name=os.path.basename(path))
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def import_disk_reports():
    """At startup, copy every report file found on disk (this project and any older BloodConnect* folder
    next to it) into the database, if it is not already there."""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    folders = [HEALTH_REPORT_DIR, GOLDEN_DIR]
    try:
        for sib in os.listdir(parent):
            if sib.lower().startswith("bloodconnect"):
                for base in (os.path.join(parent, sib), os.path.join(parent, sib, sib)):
                    for sub in ("health_reports", "golden_blood"):
                        folders.append(os.path.join(base, "private_uploads", sub))
    except OSError:
        pass
    con = _fs_conn(); cur = con.cursor()
    try:
        cur.execute("SELECT name FROM stored_files")
        have = {r[0] for r in cur.fetchall()}
    finally:
        cur.close(); con.close()
    added = 0
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for fn in os.listdir(folder):
            if fn in have:
                continue
            if file_store_put(os.path.join(folder, fn)):
                have.add(fn); added += 1
    if added:
        print(f"Imported {added} existing report file(s) into the database.")


def init_database():
    # This runs once at startup, before the pool exists, so it uses a
    # plain connection (there's no database to point the pool at yet).
    try:
        c0 = mysql.connector.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD)
        cur = c0.cursor()
        try:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        finally:
            cur.close()
            c0.close()
    except Error:
        pass  # hosted DBs often forbid CREATE DATABASE; the database already exists there

    con = mysql.connector.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME)
    cur = con.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100), phone VARCHAR(30), city VARCHAR(100),
            blood_type VARCHAR(10), username VARCHAR(100) UNIQUE, email VARCHAR(150) UNIQUE,
            password VARCHAR(255), is_donor BOOLEAN DEFAULT FALSE, is_admin BOOLEAN DEFAULT FALSE,
            age INT NULL, gender VARCHAR(30) NULL, health_report_path VARCHAR(500) NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Migration for databases created before is_admin existed.
        try:
            cur.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE")
            con.commit()
        except Error as e:
            if e.errno != 1060:  # 1060 = Duplicate column name (already migrated)
                raise
        # Compatibility migrations for older user tables. CREATE TABLE IF NOT EXISTS
        # does not add columns to an existing database, which otherwise causes chat
        # contacts to fail with HTTP 500 (for example, unknown column is_donor).
        for col, ddl in [
            ("name", "ALTER TABLE users ADD COLUMN name VARCHAR(100) NULL"),
            ("phone", "ALTER TABLE users ADD COLUMN phone VARCHAR(30) NULL"),
            ("city", "ALTER TABLE users ADD COLUMN city VARCHAR(100) NULL"),
            ("blood_type", "ALTER TABLE users ADD COLUMN blood_type VARCHAR(10) NULL"),
            ("username", "ALTER TABLE users ADD COLUMN username VARCHAR(100) NULL"),
            ("email", "ALTER TABLE users ADD COLUMN email VARCHAR(150) NULL"),
            ("password", "ALTER TABLE users ADD COLUMN password VARCHAR(255) NULL"),
            ("is_donor", "ALTER TABLE users ADD COLUMN is_donor BOOLEAN DEFAULT FALSE"),
            ("last_donation_date", "ALTER TABLE users ADD COLUMN last_donation_date DATE NULL"),
            ("age", "ALTER TABLE users ADD COLUMN age INT NULL"),
            ("gender", "ALTER TABLE users ADD COLUMN gender VARCHAR(30) NULL"),
            ("health_report_path", "ALTER TABLE users ADD COLUMN health_report_path VARCHAR(500) NULL"),
            ("health_report_status", "ALTER TABLE users ADD COLUMN health_report_status VARCHAR(20) NOT NULL DEFAULT 'pending'"),
            ("health_report_reviewed_at", "ALTER TABLE users ADD COLUMN health_report_reviewed_at DATETIME NULL"),
            ("is_blocked", "ALTER TABLE users ADD COLUMN is_blocked BOOLEAN NOT NULL DEFAULT FALSE"),
            ("blocked_reason", "ALTER TABLE users ADD COLUMN blocked_reason VARCHAR(255) NULL"),
            ("blocked_at", "ALTER TABLE users ADD COLUMN blocked_at DATETIME NULL"),
        ]:
            try:
                cur.execute(ddl)
                con.commit()
            except Error as e:
                if e.errno != 1060:
                    raise
        cur.execute("""CREATE TABLE IF NOT EXISTS donors (
            id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100), phone VARCHAR(30), city VARCHAR(100),
            blood_type VARCHAR(10), email VARCHAR(150), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Migration for older donor tables that were created without a phone column.
        try:
            cur.execute("ALTER TABLE donors ADD COLUMN phone VARCHAR(30) AFTER name")
            con.commit()
        except Error as e:
            if e.errno != 1060:
                raise
        # New donor profile fields.
        for col, ddl in [
            ("email", "ALTER TABLE donors ADD COLUMN email VARCHAR(150) AFTER blood_type"),
            ("address", "ALTER TABLE donors ADD COLUMN address VARCHAR(255) AFTER city"),
            ("hospital", "ALTER TABLE donors ADD COLUMN hospital VARCHAR(150) AFTER address"),
            ("last_donation_date", "ALTER TABLE donors ADD COLUMN last_donation_date DATE NULL AFTER hospital"),
            ("age", "ALTER TABLE donors ADD COLUMN age INT NULL"),
            ("gender", "ALTER TABLE donors ADD COLUMN gender VARCHAR(30) NULL"),
            ("health_report_path", "ALTER TABLE donors ADD COLUMN health_report_path VARCHAR(500) NULL"),
            ("health_report_status", "ALTER TABLE donors ADD COLUMN health_report_status VARCHAR(20) NOT NULL DEFAULT 'pending'"),
            ("health_report_reviewed_at", "ALTER TABLE donors ADD COLUMN health_report_reviewed_at DATETIME NULL"),
        ]:
            try:
                cur.execute(ddl)
                con.commit()
            except Error as e:
                if e.errno != 1060:
                    raise
        cur.execute("""CREATE TABLE IF NOT EXISTS blood_requests (
            id INT AUTO_INCREMENT PRIMARY KEY, patient_name VARCHAR(100), phone VARCHAR(30), blood_type VARCHAR(10),
            units INT, city VARCHAR(100), urgency VARCHAR(50), notes TEXT, status VARCHAR(30) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, accepted_at TIMESTAMP NULL)""")
        # New request location/contact details.
        for col, ddl in [
            ("email", "ALTER TABLE blood_requests ADD COLUMN email VARCHAR(150) AFTER phone"),
            ("address", "ALTER TABLE blood_requests ADD COLUMN address VARCHAR(255) AFTER city"),
            ("hospital", "ALTER TABLE blood_requests ADD COLUMN hospital VARCHAR(150) AFTER address"),
        ]:
            try:
                cur.execute(ddl)
                con.commit()
            except Error as e:
                if e.errno != 1060:
                    raise
        cur.execute("""CREATE TABLE IF NOT EXISTS patients (
            id INT PRIMARY KEY, patient_name VARCHAR(100), phone VARCHAR(30), email VARCHAR(150), blood_type VARCHAR(10), units INT,
            city VARCHAR(100), address VARCHAR(255), hospital VARCHAR(150), urgency VARCHAR(50), notes TEXT, status VARCHAR(30) DEFAULT 'accepted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, accepted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Migrate older patient tables so all submitted patient details are retained.
        for col, ddl in [
            ("email", "ALTER TABLE patients ADD COLUMN email VARCHAR(150) AFTER phone"),
            ("address", "ALTER TABLE patients ADD COLUMN address VARCHAR(255) AFTER city"),
            ("hospital", "ALTER TABLE patients ADD COLUMN hospital VARCHAR(150) AFTER address"),
        ]:
            try:
                cur.execute(ddl)
                con.commit()
            except Error as e:
                if e.errno != 1060:
                    raise
        # Country of the person (ISO code, e.g. PK / IN / US). Rows created before this
        # column existed were all validated as Pakistani CNICs, so they default to PK.
        for table in ("users", "donors", "blood_requests", "patients"):
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN country VARCHAR(2) NULL DEFAULT 'PK'")
                con.commit()
            except Error as e:
                if e.errno != 1060:
                    raise
        cur.execute("""CREATE TABLE IF NOT EXISTS private_messages (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            sender_id INT NOT NULL,
            receiver_id INT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_chat_pair (sender_id, receiver_id, id),
            CONSTRAINT fk_message_sender FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
            CONSTRAINT fk_message_receiver FOREIGN KEY (receiver_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        con.commit()
        # Unread tracking: is_read=0 means the receiver has not opened that message yet.
        # Wrapped so that a problem here can never stop the rest of the tables (e.g. golden_blood) from loading.
        try:
            try:
                cur.execute("ALTER TABLE private_messages ADD COLUMN is_read TINYINT(1) NOT NULL DEFAULT 0")
                # Column was just added: everything that already exists counts as read.
                cur.execute("UPDATE private_messages SET is_read=1")
                con.commit()
            except Error as e:
                if e.errno != 1060:
                    raise
            try:
                cur.execute("CREATE INDEX idx_chat_unread ON private_messages (receiver_id, is_read, sender_id)")
                con.commit()
            except Error as e:
                if e.errno != 1061:  # 1061 = duplicate key name (already created)
                    raise
        except Exception:
            app.logger.exception("Chat unread-tracking migration failed (chat badges may not work)")
        # Golden Blood (Rh-null): every row starts as 'pending'. Only an admin can mark it
        # 'verified', and only after entering the qualified laboratory that confirmed Rh-null.
        cur.execute("""CREATE TABLE IF NOT EXISTS golden_blood (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            kind VARCHAR(10) NOT NULL,
            full_name VARCHAR(100) NOT NULL, age INT NOT NULL, gender VARCHAR(30) NOT NULL,
            email VARCHAR(150) NOT NULL, address VARCHAR(255) NOT NULL, city VARCHAR(100) NOT NULL,
            country VARCHAR(2) NOT NULL, blood_group VARCHAR(20) NOT NULL,
            health_report_path VARCHAR(500) NOT NULL, blood_test_report_path VARCHAR(500) NOT NULL,
            verification_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            lab_name VARCHAR(200) NULL, lab_reference VARCHAR(120) NULL, review_note VARCHAR(500) NULL,
            reviewed_by INT NULL, reviewed_at DATETIME NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_golden_user (user_id, kind), INDEX idx_golden_status (verification_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # Uploaded reports (health reports, Golden Blood reports) are kept INSIDE the database, so they
        # never get lost when the project folder is replaced by a new zip.
        cur.execute("""CREATE TABLE IF NOT EXISTS stored_files (
            name VARCHAR(255) PRIMARY KEY, mime VARCHAR(100) NOT NULL, size INT NOT NULL,
            data LONGBLOB NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        con.commit()
    finally:
        cur.close()
        con.close()
    try:
        import_disk_reports()
    except Exception:
        app.logger.exception("Could not import existing report files into the database")


def user_dict(row):
    if not row:
        return None
    return {"id": row[0], "name": row[1], "phone": row[2], "city": row[3], "bloodType": row[4],
            "username": row[5], "email": row[6], "passwordHash": row[7], "isDonor": bool(row[8]),
            "isAdmin": bool(row[9]), "createdAt": row[10].timestamp() * 1000 if row[10] else 0,
            "isBlocked": bool(row[11]) if len(row) > 11 else False,
            "blockedReason": (row[12] or "") if len(row) > 12 else "",
            "country": (row[13] or "") if len(row) > 13 else ""}


@app.get("/")
def home():
    resp = app.make_response(render_template("index.html", geo=public_config()))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/api/version")
def api_version():
    """Open http://127.0.0.1:5000/api/version to confirm the NEW data.py is the one running."""
    return jsonify(build=BUILD, file=os.path.abspath(__file__),
                   features=["reject-removes-donor", "block-users", "admin-manage-users", "login-only-lists", "reason-emails"])


# Make sure every table/column exists even if the app is started without `python data.py`
# (for example with `flask run` or an IDE), otherwise new columns like is_blocked are missing.
_schema_ready = False


@app.before_request
def _ensure_schema_once():
    global _schema_ready
    if _schema_ready:
        return
    try:
        init_database()
        _schema_ready = True
    except Exception as exc:
        app.logger.error("Could not prepare database tables: %s", exc)


@app.post("/api/find-user-by-username")
def find_user_by_username():
    username = str((request.get_json(silent=True) or {}).get("username", "")).strip()
    con = db()
    cur = con.cursor()
    try:
        q = ("SELECT id,name,phone,city,blood_type,username,email,password,is_donor,is_admin,created_at,is_blocked,blocked_reason,country "
             "FROM users WHERE LOWER(username)=LOWER(%s) LIMIT 1")
        cur.execute(q, (username,))
        row = cur.fetchone()
        if not row and "@" not in username:
            cur.execute(
                "SELECT id,name,phone,city,blood_type,username,email,password,is_donor,is_admin,created_at,is_blocked,blocked_reason,country "
                "FROM users WHERE LOWER(SUBSTRING_INDEX(email,'@',1))=LOWER(%s) LIMIT 1",
                (username,))
            row = cur.fetchone()
        return ok(user_dict(row))
    finally:
        cur.close()
        con.close()


@app.post("/api/find-user-by-email")
def find_user_by_email():
    email = str((request.get_json(silent=True) or {}).get("email", "")).strip().lower()
    con = db()
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT id,name,phone,city,blood_type,username,email,password,is_donor,is_admin,created_at,is_blocked,blocked_reason,country "
            "FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1",
            (email,))
        row = cur.fetchone()
        return ok(user_dict(row))
    finally:
        cur.close()
        con.close()


@app.post("/api/send-email-verification")
def send_email_verification():
    payload = request.get_json(silent=True) or {}
    email = normalize_email(payload.get("email"))
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", email):
        return fail("Please enter a valid email address.")
    # Do not reveal whether an account already exists; registration checks it separately.
    code = f"{secrets.randbelow(1000000):06d}"
    try:
        _refresh_smtp_settings()
        recipient = ADMIN_CODE_RECEIVER if email == ADMIN_SIGNUP_EMAIL else email
        send_email(recipient, "Verify your BloodConnect email",
                   f"Your BloodConnect email verification code is: {code}\n\nThis code expires in 10 minutes. If you did not request it, ignore this email.")
    except Exception as exc:
        app.logger.error("Could not send verification email to %s: %s", email, exc)
        return fail(friendly_smtp_error(exc), 503)
    _email_codes[email] = {"hash": code_hash(email, code), "expires": time.time() + EMAIL_CODE_TTL_SECONDS, "attempts": 0}
    return ok({"email": email}, "Verification code sent. Check your inbox and spam folder.")


@app.post("/api/report-issue")
def report_issue():
    d = request.get_json(silent=True) or {}
    name = re.sub(r"[\r\n]+", " ", str(d.get("name", "")).strip())[:100]
    email = normalize_email(d.get("email"))
    issue = str(d.get("issue", "")).strip()[:5000]
    if not name or not issue:
        return fail("Please enter your name and describe the issue.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return fail("Please enter a valid email address.")
    now = time.time()
    if now - _report_times.get(email, 0) < 30:
        return fail("Please wait a few seconds before sending another report.", 429)
    body = (f"New issue report from the BloodConnect Contact page\n"
            f"{'-' * 50}\nName:  {name}\nEmail: {email}\n{'-' * 50}\n\n{issue}\n\n"
            f"{'-' * 50}\nJust press Reply in Gmail to answer {name} at {email}.")
    try:
        _refresh_smtp_settings()
        send_email(REPORT_RECEIVER, f"BloodConnect issue report from {name}", body, reply_to=f"{name} <{email}>".replace(",", " "))
    except Exception as exc:
        app.logger.error("Could not send issue report: %s", exc)
        return fail(friendly_smtp_error(exc), 503)
    _report_times[email] = now
    return ok({}, "Your report has been submitted successfully.")


@app.get("/api/email-status")
def email_status():
    """Open http://127.0.0.1:5000/api/email-status to check the email setup.
    Add ?to=someone@example.com to actually send a test email."""
    _refresh_smtp_settings()
    info = {"build": BUILD, "envFile": ENV_PATH, "envFileFound": ENV_FOUND, "smtpHost": SMTP_HOST, "smtpPort": SMTP_PORT,
            "smtpEmailSet": bool(SMTP_EMAIL), "smtpPasswordSet": bool(SMTP_PASSWORD), "brevoApiKeySet": bool(os.environ.get("BREVO_API_KEY", "").strip()),
            "passwordLength": len(SMTP_PASSWORD)}
    to = normalize_email(request.args.get("to"))
    if to:
        try:
            send_email(to, "BloodConnect test email", "If you can read this, email sending works.")
            info["testSent"] = True
        except Exception as exc:
            app.logger.error("Test email failed: %s", exc)
            info["testSent"] = False
            info["error"] = friendly_smtp_error(exc)
    return jsonify(info)


@app.post("/api/verify-email-code")
def verify_email_code():
    payload = request.get_json(silent=True) or {}
    email = normalize_email(payload.get("email"))
    code = str(payload.get("code", "")).strip()
    record = _email_codes.get(email)
    if not record or time.time() > record["expires"]:
        _email_codes.pop(email, None)
        return fail("Verification code expired. Please request a new one.", 400)
    record["attempts"] += 1
    if record["attempts"] > 5:
        _email_codes.pop(email, None)
        return fail("Too many incorrect attempts. Please request a new code.", 429)
    if not re.fullmatch(r"\d{6}", code) or not secrets.compare_digest(record["hash"], code_hash(email, code)):
        return fail("Incorrect verification code. Please check the email and try again.", 400)
    _email_codes.pop(email, None)
    token = secrets.token_urlsafe(32)
    _verified_email_tokens[token] = {"email": email, "expires": time.time() + 15 * 60}
    return ok({"emailVerificationToken": token}, "Email verified successfully.")


@app.post("/api/create-user")
def create_user():
    # Accept JSON for older clients and multipart form data for donor health reports.
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        try:
            d = json.loads(request.form.get("data", "{}"))
        except (TypeError, ValueError):
            return fail("Invalid user data.")
        report = request.files.get("healthReport")
    else:
        d = (request.get_json(silent=True) or {}).get("data")
        report = None
    if not isinstance(d, dict):
        return fail("Invalid user data.")
    email = normalize_email(d.get("email"))
    verification_token = str(d.get("emailVerificationToken", ""))
    verification = _verified_email_tokens.get(verification_token)
    if not verification or verification.get("email") != email or time.time() > verification.get("expires", 0):
        return fail("Please verify your email address before creating an account.", 403)
    is_admin = bool(d.get("isAdmin", False))
    is_donor = False  # a user becomes a donor only by submitting the Donate Now form
    age = None
    gender = str(d.get("gender", "")).strip()
    country = None      # the admin account has no country / ID
    if not is_admin:
        try:
            age = int(d.get("age"))
        except (TypeError, ValueError):
            return fail("Please enter a valid age.")
        if age < 1 or age > 120:
            return fail("Age must be between 1 and 120.")
        if gender not in {"Male", "Female", "Other", "Prefer not to say"}:
            return fail("Please select your gender.")
        country, d["phone"], id_error = check_identity(d)
        if id_error:
            return fail(id_error)
    last_donation_obj = None
    if is_donor:
        try:
            last_donation_obj = datetime.strptime(str(d.get("lastDonationDate", "")).strip(), "%Y-%m-%d").date()
        except ValueError:
            return fail("Please enter the date of your last blood donation.")
        if last_donation_obj > date.today():
            return fail("Last donation date cannot be in the future.")
        if not report or not report.filename:
            return fail("Donors must upload their health report.")
        original_name = secure_filename(report.filename)
        ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        if ext not in ALLOWED_HEALTH_EXTENSIONS:
            return fail("Health report must be a PDF, PNG, JPG, or JPEG file.")
    con = db()
    cur = con.cursor()
    saved_report_path = None
    try:
        if is_donor:
            os.makedirs(HEALTH_REPORT_DIR, exist_ok=True)
            ext = original_name.rsplit(".", 1)[-1].lower()
            safe_name = f"health_report_{uuid.uuid4().hex}.{ext}"
            saved_report_path = os.path.join(HEALTH_REPORT_DIR, safe_name)
            report.save(saved_report_path)
            file_store_put(saved_report_path)
        cur.execute("""INSERT INTO users (name,phone,city,blood_type,username,email,password,is_donor,is_admin,age,gender,health_report_path,created_at,last_donation_date,country)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FROM_UNIXTIME(%s/1000),%s,%s)""",
                    (d.get("name", ""), d.get("phone", ""), d.get("city", ""), d.get("bloodType", ""),
                     d.get("username", ""), email, d.get("passwordHash", ""),
                     is_donor, is_admin, age, gender or None, saved_report_path,
                     int(d.get("createdAt", int(time.time() * 1000))), last_donation_obj, country))
        con.commit()
        new_id = cur.lastrowid
        cur.execute("SELECT id,name,phone,city,blood_type,username,email,password,is_donor,is_admin,created_at,is_blocked,blocked_reason,country FROM users WHERE id=%s", (new_id,))
        user = user_dict(cur.fetchone())
        if user:
            user["age"] = age
            user["gender"] = gender or None
        _verified_email_tokens.pop(verification_token, None)
        try:
            send_email(email, "Welcome to BloodConnect — registration successful",
                       f"Hello {d.get('name') or d.get('username')},\n\nYour BloodConnect account has been registered successfully with this email address.\n\nIf you did not create this account, please contact the BloodConnect administrator.")
        except Exception as exc:
            # Verification email already proved SMTP delivery; do not undo a committed account
            # if the second confirmation message temporarily fails.
            app.logger.error("Account created but confirmation email failed for %s: %s", email, exc)
        return ok(user, "Email verified and account created successfully. A confirmation email has been sent.")
    except IntegrityError:
        con.rollback()
        if saved_report_path and os.path.exists(saved_report_path):
            os.remove(saved_report_path)
        return fail("Email or username already registered.", 409)
    except Exception:
        con.rollback()
        if saved_report_path and os.path.exists(saved_report_path):
            os.remove(saved_report_path)
        raise
    finally:
        cur.close()
        con.close()


def add_months(d, months):
    """Add calendar months while keeping the day valid (e.g. Jan 31 -> Apr 30)."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def donor_availability(last_donation_value):
    """Return (available_now, days_until_available, eligible_date)."""
    if not last_donation_value:
        return False, None, None
    if isinstance(last_donation_value, datetime):
        last_donation = last_donation_value.date()
    elif isinstance(last_donation_value, date):
        last_donation = last_donation_value
    else:
        try:
            last_donation = datetime.strptime(str(last_donation_value), "%Y-%m-%d").date()
        except ValueError:
            return False, None, None
    eligible_date = last_donation + timedelta(days=WAITING_DAYS)
    days = (eligible_date - date.today()).days
    return days <= 0, max(0, days), eligible_date


@app.post("/api/add-donor")
def add_donor():
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        try:
            d = json.loads(request.form.get("data", "{}"))
        except (TypeError, ValueError):
            return fail("Invalid donor data.")
        report = request.files.get("healthReport")
    else:
        d = (request.get_json(silent=True) or {}).get("data")
        report = None
    if not isinstance(d, dict):
        return fail("Invalid donor data.")
    country, d["phone"], id_error = check_identity(d)
    if id_error:
        return fail(id_error)
    if not str(d.get("name", "")).strip() or not str(d.get("bloodType", "")).strip():
        return fail("Donor name and blood group are required.")
    try:
        age = int(d.get("age"))
    except (TypeError, ValueError):
        return fail("Please enter a valid age.")
    if age < 1 or age > 120:
        return fail("Age must be between 1 and 120.")
    gender = str(d.get("gender", "")).strip()
    if gender not in {"Male", "Female", "Other", "Prefer not to say"}:
        return fail("Please select your gender.")
    if not str(d.get("email", d.get("userEmail", ""))).strip() or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", str(d.get("email", d.get("userEmail", ""))).strip()):
        return fail("A valid email address is required.")
    if not str(d.get("city", "")).strip() or not str(d.get("address", "")).strip() or not str(d.get("hospital", "")).strip():
        return fail("City, address and hospital are required.")
    if len(str(d.get("address", "")).strip()) < 10:
        return fail("Please enter your complete address (house no., street, area).")
    last_donation = str(d.get("lastDonationDate", "")).strip()
    if not last_donation:
        return fail("Please enter the date of your last blood donation.")
    try:
        last_donation_obj = datetime.strptime(last_donation, "%Y-%m-%d").date()
    except ValueError:
        return fail("Please enter a valid last donation date.")
    if last_donation_obj > date.today():
        return fail("Last donation date cannot be in the future.")
    if not report or not report.filename:
        return fail("Please upload your health report.")
    original_name = secure_filename(report.filename)
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if ext not in ALLOWED_HEALTH_EXTENSIONS:
        return fail("Health report must be a PDF, PNG, JPG, or JPEG file.")
    saved_report_path = None
    con = db(); cur = con.cursor()
    try:
        auth_user, auth_error = get_authenticated_user(cur, d)
        if auth_error:
            return fail(auth_error, 401)
        os.makedirs(HEALTH_REPORT_DIR, exist_ok=True)
        saved_report_path = os.path.join(HEALTH_REPORT_DIR, f"donor_report_{uuid.uuid4().hex}.{ext}")
        report.save(saved_report_path)
        file_store_put(saved_report_path)
        cur.execute("INSERT INTO donors (name,phone,city,address,hospital,last_donation_date,blood_type,email,age,gender,health_report_path,created_at,country) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FROM_UNIXTIME(%s/1000),%s)",
                    (d.get("name", ""), d.get("phone", ""), d.get("city", ""), d.get("address", ""),
                     d.get("hospital", ""), last_donation_obj, d.get("bloodType", ""),
                     d.get("userEmail", d.get("email", "")), age, gender, saved_report_path,
                     int(d.get("createdAt", int(time.time() * 1000))), country))
        # Only now does this account become a donor.
        cur.execute("UPDATE users SET is_donor=TRUE WHERE id=%s", (auth_user[0],))
        con.commit(); d["id"] = cur.lastrowid
        d.pop("phone", None)   # never echo the ID number back
        d["country"] = country
        return ok(d, "Donor saved successfully.")
    except Exception:
        con.rollback()
        if saved_report_path and os.path.exists(saved_report_path): os.remove(saved_report_path)
        raise
    finally:
        cur.close(); con.close()


@app.route("/api/get-donors", methods=["GET", "POST"])
def get_donors():
    """Load donors added through the Donate Now form. CNIC (stored in the phone column) is never returned."""
    con = db()
    cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur)
        if auth_error:      # donor names are only visible to logged-in (and not blocked) members
            return fail(auth_error, 401)
        # Current donor table: every saved row is a donor record.
        cur.execute("""
            SELECT id, name, phone, city, address, hospital, last_donation_date, blood_type, email, country,
                   UNIX_TIMESTAMP(created_at)*1000 AS createdAt
            FROM donors
            WHERE NOT EXISTS (SELECT 1 FROM users bu WHERE LOWER(bu.email)=LOWER(donors.email) AND bu.is_blocked=TRUE)
            ORDER BY created_at DESC, id DESC
        """)
        donor_rows = cur.fetchall()

        legacy_rows = []   # signup alone no longer makes someone a donor
    finally:
        cur.close()
        con.close()

    rows=[]
    seen=set()
    for r in donor_rows + legacy_rows:
        name=str(r.get('name') or '').strip()
        phone=str(r.get('phone') or '').strip()
        blood=str(r.get('blood_type') or '').strip().upper()
        available_now, days_until_available, eligible_date = donor_availability(r.get('last_donation_date'))
        # Only suppress a true duplicate record; different database IDs remain visible.
        # The country is part of the key: the same ID digits in two countries are two different people.
        key=(name.lower(), str(r.get('country') or 'PK'), re.sub(r'[^0-9A-Za-z+]','',phone), blood)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            'id': r.get('id'),
            'name': name or 'Donor',
            'city': str(r.get('city') or '').strip(),
            'country': str(r.get('country') or 'PK').strip(),
            'address': str(r.get('address') or '').strip(),
            'hospital': str(r.get('hospital') or '').strip(),
            'bloodType': blood,
            'userEmail': str(r.get('email') or '').strip(),
            'lastDonationDate': r.get('last_donation_date').isoformat() if r.get('last_donation_date') else '',
            'available': bool(available_now),
            'daysUntilAvailable': days_until_available,
            'eligibleDate': eligible_date.isoformat() if eligible_date else '',
            'donations': 0,
            'createdAt': r.get('createdAt') or 0,
        })
    rows.sort(key=lambda x: x.get('createdAt') or 0, reverse=True)
    return ok(rows)


@app.post("/api/add-request")
def add_request():
    d = (request.get_json(silent=True) or {}).get("data")
    if not isinstance(d, dict):
        return fail("Invalid blood request data.")
    if not str(d.get("patientName", "")).strip() or not str(d.get("bloodType", "")).strip():
        return fail("Patient name and blood group are required.")
    country, d["phone"], id_error = check_identity(d)
    if id_error:
        return fail(id_error)
    if not str(d.get("email", "")).strip():
        return fail("Email is required.")
    if not str(d.get("city", "")).strip() or not str(d.get("address", "")).strip() or not str(d.get("hospital", "")).strip():
        return fail("City, address and hospital are required.")
    if len(str(d.get("address", "")).strip()) < 10:
        return fail("Please enter the complete address (hospital / patient address).")
    con = db()
    cur = con.cursor()
    try:
        _, auth_error = get_authenticated_user(cur, d)
        if auth_error:
            return fail(auth_error, 401)
        created = int(d.get("createdAt", int(time.time() * 1000)))
        cur.execute("""INSERT INTO blood_requests (patient_name,phone,email,blood_type,units,city,address,hospital,urgency,notes,status,created_at,country)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FROM_UNIXTIME(%s/1000),%s)""",
                    (d.get("patientName", ""), d.get("phone", ""), d.get("email", ""), d.get("bloodType", ""), int(d.get("units", 1)),
                     d.get("city", ""), d.get("address", ""), d.get("hospital", ""), d.get("urgency", "Not specified"), d.get("notes", ""),
                     d.get("status", "pending"), created, country))
        con.commit()
        d["id"] = cur.lastrowid
        return ok(d, "Blood request saved successfully.")
    finally:
        cur.close()
        con.close()


def resolve_report_path(stored):
    """Find the uploaded report on disk.
    The database stores a full path from when the file was uploaded. If the project
    folder was moved/renamed since, that path is stale, so fall back to looking for the
    same file name inside the current private_uploads/health_reports folder."""
    if not stored:
        return None
    base = os.path.abspath(HEALTH_REPORT_DIR)
    candidates = [os.path.abspath(stored), os.path.join(base, os.path.basename(str(stored).replace("\\", "/")))]
    for path in candidates:
        try:
            inside = os.path.commonpath([base, path]) == base
        except ValueError:      # different drive on Windows
            inside = False
        if inside and os.path.isfile(path):
            return path
    return None


REPORT_SOURCES = {"user": "users", "donor": "donors"}


@app.get("/api/admin/health-reports")
def admin_health_reports():
    """Every donor submission for the admin, newest first.
    Donors without an uploaded report are still listed (fileAvailable=False, hasReport=False)
    so the admin never loses sight of a donor who submitted the form."""
    con = db()
    cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        cur.execute("""SELECT id, name, email, city, country, address, hospital, blood_type, age, gender,
                              health_report_status, health_report_path, last_donation_date,
                              (SELECT u.id FROM users u WHERE LOWER(u.email)=LOWER(donors.email) AND u.is_admin=FALSE LIMIT 1) AS userId,
                              (SELECT u.is_blocked FROM users u WHERE LOWER(u.email)=LOWER(donors.email) AND u.is_admin=FALSE LIMIT 1) AS isBlocked,
                              CAST(UNIX_TIMESTAMP(created_at)*1000 AS SIGNED) AS createdAt
                       FROM donors""")
        donor_rows = cur.fetchall()
        rows = [dict(r, source="donor") for r in donor_rows]
        seen_emails = {str(r.get("email") or "").strip().lower() for r in donor_rows}
        cur.execute("""SELECT id, name, email, city, country, NULL AS address, NULL AS hospital, blood_type, age, gender,
                              health_report_status, health_report_path, last_donation_date,
                              id AS userId, is_blocked AS isBlocked,
                              CAST(UNIX_TIMESTAMP(created_at)*1000 AS SIGNED) AS createdAt
                       FROM users WHERE is_donor=TRUE AND health_report_path IS NOT NULL""")
        for r in cur.fetchall():
            if str(r.get("email") or "").strip().lower() in seen_emails:
                continue   # same person already listed from the donors table
            rows.append(dict(r, source="user"))
        rows.sort(key=lambda r: r.get("createdAt") or 0, reverse=True)
        for row in rows:
            row["healthReportStatus"] = row.pop("health_report_status") or "pending"
            row["bloodType"] = row.pop("blood_type")
            ldd = row.pop("last_donation_date", None)
            row["lastDonationDate"] = ldd.isoformat() if ldd else None
            stored = row.pop("health_report_path")
            row["isBlocked"] = bool(row.get("isBlocked"))
            row["hasReport"] = bool(stored)
            row["fileAvailable"] = resolve_report_path(stored) is not None
        return ok(rows)
    finally:
        cur.close()
        con.close()


@app.get("/api/admin/health-reports/<source>/<int:rid>/file")
@app.get("/api/admin/health-reports/<int:rid>/file", defaults={"source": "user"})
def admin_health_report_file(source, rid):
    table = REPORT_SOURCES.get(source)
    if not table:
        return fail("Unknown report source.", 404)
    con = db()
    cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        if table == "users":
            cur.execute("SELECT health_report_path FROM users WHERE id=%s AND is_donor=TRUE", (rid,))
        else:
            cur.execute("SELECT health_report_path FROM donors WHERE id=%s", (rid,))
        row = cur.fetchone()
        if not row or not row.get("health_report_path"):
            return fail("Health report not found.", 404)
        resp = serve_report(row["health_report_path"], resolve_report_path)
        if resp is None:
            return fail("The report file is missing on the server. It was uploaded before reports were stored in the database and the old file could not be found - ask the person to upload it again.", 404)
        return resp
    finally:
        cur.close()
        con.close()


@app.post("/api/admin/health-reports/review")
def admin_review_health_report():
    payload = request.get_json(silent=True) or {}
    try:
        rid = int(payload.get("userId"))
    except (TypeError, ValueError):
        return fail("Invalid donor ID.")
    table = REPORT_SOURCES.get(str(payload.get("source") or "user"))
    if not table:
        return fail("Unknown report source.")
    status = str(payload.get("status", "")).lower()
    if status not in {"approved", "rejected"}:
        return fail("Choose approved or rejected.")
    reason = str(payload.get("reason") or "").strip()[:500]
    if status == "rejected" and len(reason) < 3:
        return fail("Please write the reason for rejecting this donor. It is sent to them by email.")
    con = db()
    cur = con.cursor(dictionary=True)
    removed_file = None
    donor_name = donor_email = None
    try:
        _, auth_error = get_authenticated_user(cur, payload, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        if status == "approved":
            extra = " AND is_donor=TRUE" if table == "users" else ""
            cur.execute(f"UPDATE {table} SET health_report_status=%s, health_report_reviewed_at=NOW() WHERE id=%s{extra} AND health_report_path IS NOT NULL", (status, rid))
            if cur.rowcount == 0:
                return fail("Donor or health report not found.", 404)
            con.commit()
            return ok({"userId": rid, "source": payload.get("source") or "user", "status": status, "removed": False}, "Health report approved.")

        # REJECTED: the donor is removed from the donor list and the private report is deleted.
        cur.execute(f"SELECT id, name, email, health_report_path FROM {table} WHERE id=%s" + (" AND is_donor=TRUE" if table == "users" else ""), (rid,))
        row = cur.fetchone()
        if not row:
            return fail("Donor or health report not found.", 404)
        donor_name, donor_email = row.get("name"), row.get("email")
        removed_file = resolve_report_path(row.get("health_report_path"))
        removed_stored = row.get("health_report_path")
        if table == "donors":
            cur.execute("DELETE FROM donors WHERE id=%s", (rid,))
            # The account stops being a donor unless another donor submission of theirs remains.
            cur.execute("""UPDATE users SET is_donor=FALSE
                           WHERE LOWER(email)=LOWER(%s) AND is_admin=FALSE
                           AND NOT EXISTS (SELECT 1 FROM donors d WHERE LOWER(d.email)=LOWER(%s))""", (donor_email, donor_email))
        else:
            cur.execute("""UPDATE users SET is_donor=FALSE, health_report_path=NULL,
                           health_report_status='rejected', health_report_reviewed_at=NOW() WHERE id=%s""", (rid,))
        con.commit()
    finally:
        cur.close()
        con.close()
    if removed_file:
        try:
            os.remove(removed_file)
        except OSError:
            pass
    if removed_stored:
        file_store_delete(removed_stored)
    email_sent, email_error = False, "No email address on record."
    if donor_email:
        email_sent, email_error = try_send_email(
            donor_email, "BloodConnect - your donor application was rejected",
            f"Hello {donor_name or 'donor'},\n\n"
            "Your BloodConnect donor application has been REJECTED after the admin reviewed your health report, "
            "and you have been removed from the donor list.\n\n"
            f"Reason: {reason}\n\n"
            "You are welcome to apply again from the Donate Now page with a valid, up-to-date health report.\n\n"
            "If you think this is a mistake, please contact the BloodConnect support team.")
    return ok({"userId": rid, "source": payload.get("source") or "user", "status": status, "removed": True,
               "emailSent": email_sent, "emailError": email_error, "email": donor_email},
              "Health report rejected. The donor has been removed from the donor list.")


# ---------------------------------------------------------------------------
# Admin: manage / block users
# ---------------------------------------------------------------------------
@app.get("/api/admin/users")
def admin_users():
    con = db()
    cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        cur.execute("""SELECT id, name, email, city, country, blood_type, is_donor, is_blocked, blocked_reason,
                              EXISTS(SELECT 1 FROM donors d WHERE LOWER(d.email)=LOWER(users.email)) AS has_donor_row,
                              CAST(UNIX_TIMESTAMP(blocked_at)*1000 AS SIGNED) AS blockedAt,
                              CAST(UNIX_TIMESTAMP(created_at)*1000 AS SIGNED) AS createdAt
                       FROM users WHERE is_admin=FALSE
                       ORDER BY is_blocked DESC, created_at DESC, id DESC""")
        rows = []
        for r in cur.fetchall():
            rows.append({"id": r["id"], "name": r["name"] or "", "email": r["email"] or "", "city": r["city"] or "", "country": r["country"] or "",
                         "bloodType": r["blood_type"] or "", "isDonor": bool(r["is_donor"]) or bool(r["has_donor_row"]),
                         "isBlocked": bool(r["is_blocked"]), "blockedReason": r["blocked_reason"] or "",
                         "blockedAt": r["blockedAt"], "createdAt": r["createdAt"] or 0})
        return ok(rows)
    finally:
        cur.close()
        con.close()


@app.post("/api/admin/users/block")
def admin_block_user():
    payload = request.get_json(silent=True) or {}
    try:
        rid = int(payload.get("userId"))
    except (TypeError, ValueError):
        return fail("Invalid user ID.")
    blocked = bool(payload.get("blocked"))
    reason = str(payload.get("reason") or "").strip()[:255]
    if blocked and len(reason) < 3:
        return fail("Please write the reason for blocking this user.")
    con = db()
    cur = con.cursor(dictionary=True)
    target = None
    try:
        me, auth_error = get_authenticated_user(cur, payload, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        if rid == me["id"]:
            return fail("You cannot block your own account.")
        cur.execute("SELECT id, name, email, is_admin FROM users WHERE id=%s", (rid,))
        target = cur.fetchone()
        if not target:
            return fail("User not found.", 404)
        if target["is_admin"]:
            return fail("Admin accounts cannot be blocked.")
        if blocked:
            cur.execute("UPDATE users SET is_blocked=TRUE, blocked_reason=%s, blocked_at=NOW() WHERE id=%s", (reason, rid))
        else:
            cur.execute("UPDATE users SET is_blocked=FALSE, blocked_reason=NULL, blocked_at=NULL WHERE id=%s", (rid,))
        con.commit()
    finally:
        cur.close()
        con.close()
    if blocked:
        email_sent, email_error = try_send_email(
            target["email"], "BloodConnect - your account has been blocked",
            f"Hello {target['name'] or 'member'},\n\n"
            "Your BloodConnect account has been BLOCKED by an administrator, so you can no longer log in, chat, "
            "request blood or donate.\n\n"
            f"Reason: {reason}\n\n"
            "If you think this is a mistake, please contact the BloodConnect support team.")
    else:
        email_sent, email_error = try_send_email(
            target["email"], "BloodConnect - your account has been unblocked",
            f"Hello {target['name'] or 'member'},\n\nYour BloodConnect account has been unblocked. "
            "You can log in again. Please follow the community rules.")
    return ok({"userId": rid, "blocked": blocked, "reason": reason if blocked else "",
               "emailSent": email_sent, "emailError": email_error, "email": target["email"]},
              "User blocked." if blocked else "User unblocked.")


@app.route("/api/get-requests", methods=["GET", "POST"])
def get_requests():
    con = db()
    cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, require_admin=True)
        if auth_error:      # patient requests (names, hospital, notes) are for admins only
            return fail(auth_error, 403)
        cur.execute(
            "SELECT id,patient_name,phone,email,blood_type,units,city,country,address,hospital,urgency,notes,status,"
            "UNIX_TIMESTAMP(created_at)*1000 createdAt,UNIX_TIMESTAMP(accepted_at)*1000 acceptedAt "
            "FROM blood_requests ORDER BY created_at DESC")
        rows = cur.fetchall()
    finally:
        cur.close()
        con.close()
    for r in rows:
        r["patientName"] = r.pop("patient_name")
        r["bloodType"] = r.pop("blood_type")
        r.pop("phone", None)     # CNIC is never sent to the browser
    return ok(rows)


@app.post("/api/accept-request")
def accept_request():
    payload = request.get_json(silent=True) or {}
    rid = payload.get("id")
    con = db()
    cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, payload, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        cur.execute(
            "SELECT id,patient_name,phone,email,blood_type,units,city,country,address,hospital,urgency,notes,status,"
            "UNIX_TIMESTAMP(created_at)*1000 createdAt FROM blood_requests WHERE id=%s", (rid,))
        r = cur.fetchone()
        if not r:
            return fail("Request not found.", 404)

        accepted = int(time.time() * 1000)
        cur.execute("UPDATE blood_requests SET status='accepted',accepted_at=FROM_UNIXTIME(%s/1000) WHERE id=%s",
                    (accepted, rid))
        cur.execute("""INSERT INTO patients (id,patient_name,phone,email,blood_type,units,city,country,address,hospital,urgency,notes,status,created_at,accepted_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'accepted',FROM_UNIXTIME(%s/1000),FROM_UNIXTIME(%s/1000))
                       ON DUPLICATE KEY UPDATE patient_name=VALUES(patient_name),phone=VALUES(phone),email=VALUES(email),blood_type=VALUES(blood_type),
                       units=VALUES(units),city=VALUES(city),country=VALUES(country),address=VALUES(address),hospital=VALUES(hospital),urgency=VALUES(urgency),notes=VALUES(notes),status='accepted',
                       accepted_at=VALUES(accepted_at)""",
                    (r["id"], r["patient_name"], r["phone"], r.get("email", ""), r["blood_type"], r["units"], r["city"], r.get("country") or "PK",
                     r.get("address", ""), r.get("hospital", ""), r["urgency"], r["notes"], r["createdAt"], accepted))
        con.commit()
        r["patientName"] = r.pop("patient_name")
        r["bloodType"] = r.pop("blood_type")
        r["status"] = "accepted"
        r["acceptedAt"] = accepted
        r.pop("phone", None)
        return ok(r, "Request accepted successfully.")
    except Error as e:
        try:
            con.rollback()
        except Exception:
            pass
        return fail(f"Could not accept this request: {e}", 500)
    except Exception as e:
        try:
            con.rollback()
        except Exception:
            pass
        return fail(f"Could not accept this request: {e}", 500)
    finally:
        cur.close()
        con.close()


@app.route("/api/get-patients", methods=["GET", "POST"])
def get_patients():
    """Return only admin-approved patients for the public Patients page."""
    con = db()
    cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur)
        if auth_error:      # patient names are only visible to logged-in (and not blocked) members
            return fail(auth_error, 401)
        cur.execute("""
            SELECT id, patient_name, phone, blood_type, units, city, country, address, hospital, status,
                   UNIX_TIMESTAMP(created_at)*1000 AS createdAt,
                   UNIX_TIMESTAMP(accepted_at)*1000 AS acceptedAt
            FROM patients
            WHERE LOWER(COALESCE(status,'')) = 'accepted'
            ORDER BY COALESCE(accepted_at, created_at) DESC, id DESC
        """)
        patient_rows = cur.fetchall()
    finally:
        cur.close()
        con.close()

    rows=[]
    seen=set()
    for r in patient_rows:
        name=str(r.get('patient_name') or '').strip()
        phone=str(r.get('phone') or '').strip()
        blood=str(r.get('blood_type') or '').strip().upper()
        key=(name.lower(), str(r.get('country') or 'PK'), re.sub(r'[^0-9A-Za-z+]','',phone), blood)
        if not name or key in seen:
            continue
        seen.add(key)
        rows.append({
            'id': r.get('id'),
            'patientName': name,
            'bloodType': blood,
            'city': str(r.get('city') or '').strip(),
            'country': str(r.get('country') or 'PK').strip(),
            'address': str(r.get('address') or '').strip(),
            'hospital': str(r.get('hospital') or '').strip(),
            'status': 'accepted',
            'units': r.get('units') or 0,
            'createdAt': r.get('createdAt') or 0,
            'acceptedAt': r.get('acceptedAt') or r.get('createdAt') or 0,
        })
    rows.sort(key=lambda x: x.get('acceptedAt') or x.get('createdAt') or 0, reverse=True)
    return ok(rows)



def contact_role(r):
    """Admin -> Admin, registered donor -> Donor, everyone else -> Patient."""
    if r.get('is_admin'):
        return 'Admin'
    if r.get('is_donor') or r.get('donor_row_id') is not None:
        return 'Donor'
    return 'Patient'


@app.get("/api/chat/contacts")
def chat_contacts():
    con = db(); cur = con.cursor(dictionary=True)
    try:
        me, err = get_authenticated_user(cur)
        if err: return fail(err, 401)
        my_id = me['id'] if isinstance(me, dict) else me[0]
        cur.execute("""SELECT u.id, u.name, u.email, u.is_donor, u.is_admin, d.id AS donor_row_id,
                    COALESCE(NULLIF(u.blood_type,''), d.blood_type) AS blood_type,
                    COALESCE(NULLIF(u.city,''), d.city) AS city,
                    (SELECT COUNT(*) FROM private_messages pm
                       WHERE pm.sender_id=u.id AND pm.receiver_id=%s AND pm.is_read=0) AS unread,
                    (SELECT MAX(pm2.id) FROM private_messages pm2
                       WHERE (pm2.sender_id=u.id AND pm2.receiver_id=%s)
                          OR (pm2.sender_id=%s AND pm2.receiver_id=u.id)) AS last_id
                    FROM users u LEFT JOIN donors d ON LOWER(d.email)=LOWER(u.email)
                    WHERE u.id<>%s AND u.is_blocked=FALSE
                    ORDER BY (unread>0) DESC, (last_id IS NULL) ASC, last_id DESC, u.name""",
                    (my_id, my_id, my_id, my_id))
        rows = cur.fetchall()
        return ok([{'id':r['id'],'name':r['name'] or 'BloodConnect member','email':r['email'],
                    'isDonor':bool(r['is_donor']) or r['donor_row_id'] is not None,'role':contact_role(r),'bloodType':r['blood_type'] or '', 'city':r['city'] or '',
                    'unread':int(r['unread'] or 0)} for r in rows])
    except Exception as e:
        app.logger.exception('Failed to load chat contacts')
        return fail('Could not load chat contacts: ' + str(e), 500)
    finally:
        cur.close(); con.close()


@app.get("/api/chat/unread")
def chat_unread():
    """Total unread messages for the logged-in user (used for the badge on the Messages button)."""
    con = db(); cur = con.cursor(dictionary=True)
    try:
        me, err = get_authenticated_user(cur)
        if err: return fail(err, 401)
        my_id = me['id'] if isinstance(me, dict) else me[0]
        cur.execute("""SELECT COUNT(*) AS n FROM private_messages pm JOIN users u ON u.id=pm.sender_id
                    WHERE pm.receiver_id=%s AND pm.is_read=0 AND u.is_blocked=FALSE""", (my_id,))
        return ok({'unread': int(cur.fetchone()['n'] or 0)})
    except Exception as e:
        app.logger.exception('Failed to count unread messages')
        return fail('Could not load unread count: ' + str(e), 500)
    finally:
        cur.close(); con.close()


@app.get("/api/chat/messages/<int:other_id>")
def chat_messages(other_id):
    con = db(); cur = con.cursor(dictionary=True)
    try:
        me, err = get_authenticated_user(cur)
        if err: return fail(err, 401)
        my_id = me['id'] if isinstance(me, dict) else me[0]
        cur.execute("SELECT id FROM users WHERE id=%s", (other_id,))
        if not cur.fetchone(): return fail('Chat user not found.', 404)
        cur.execute("""SELECT id,sender_id,receiver_id,message,
                    created_at AS createdAt
                    FROM private_messages WHERE (sender_id=%s AND receiver_id=%s)
                    OR (sender_id=%s AND receiver_id=%s) ORDER BY id ASC LIMIT 500""",
                    (my_id,other_id,other_id,my_id))
        rows = cur.fetchall()
        # Opening the conversation marks the other person's messages to me as read.
        cur.execute("UPDATE private_messages SET is_read=1 WHERE sender_id=%s AND receiver_id=%s AND is_read=0",
                    (other_id,my_id))
        con.commit()
        return ok([{'id':r['id'],'senderId':r['sender_id'],'receiverId':r['receiver_id'],
                    'message':r['message'],'createdAt':r['createdAt'].isoformat(sep=' ') if r['createdAt'] else ''} for r in rows])
    except Exception as e:
        app.logger.exception('Failed to load chat messages')
        return fail('Could not load messages: ' + str(e), 500)
    finally:
        cur.close(); con.close()


@app.post("/api/chat/send")
def chat_send():
    payload = request.get_json(silent=True) or {}
    try: other_id = int(payload.get('recipientId'))
    except (TypeError, ValueError): return fail('Choose a valid chat contact.')
    message = str(payload.get('message') or '').strip()
    if not message: return fail('Please type a message.')
    if len(message) > 4000: return fail('Message is too long (maximum 4000 characters).')
    con = db(); cur = con.cursor(dictionary=True)
    try:
        me, err = get_authenticated_user(cur, payload)
        if err: return fail(err, 401)
        my_id = me['id'] if isinstance(me, dict) else me[0]
        if other_id == my_id: return fail('You cannot message yourself.')
        cur.execute("SELECT id,is_blocked FROM users WHERE id=%s", (other_id,))
        target = cur.fetchone()
        if not target: return fail('This user account does not exist.', 404)
        if target['is_blocked']: return fail('This user is not available for chat.', 403)
        cur.execute("INSERT INTO private_messages(sender_id,receiver_id,message) VALUES(%s,%s,%s)",
                    (my_id,other_id,message))
        con.commit()
        cur.execute("""SELECT id,sender_id,receiver_id,message,
                    created_at AS createdAt
                    FROM private_messages WHERE id=%s""", (cur.lastrowid,))
        r=cur.fetchone()
        return ok({'id':r['id'],'senderId':r['sender_id'],'receiverId':r['receiver_id'],
                   'message':r['message'],'createdAt':r['createdAt'].isoformat(sep=' ') if r['createdAt'] else ''}, 'Message sent.')
    except Exception as e:
        app.logger.exception('Failed to send chat message')
        return fail('Could not send message: ' + str(e), 500)
    finally:
        cur.close(); con.close()


# ---------------------------------------------------------------------------
# Golden Blood (Rh-null) - register as donor / request Golden Blood
# Nothing here is ever auto-approved: status starts as "pending" and only an admin
# can set "verified", and only with the name + reference of the qualified laboratory.
# ---------------------------------------------------------------------------
# Set UPLOAD_DIR in .env (or in your host's variables) to keep reports in one fixed folder that does not
# change when you extract a new copy of the project. Default: private_uploads next to data.py.
GOLDEN_DIR = os.path.join(os.environ.get("UPLOAD_DIR", "").strip() or os.path.join(os.path.dirname(os.path.abspath(__file__)), "private_uploads"), "golden_blood")
GOLDEN_GROUPS = {"Rh-null", "Unknown"}
GOLDEN_STATUSES = {"pending", "verified", "rejected", "needs_info"}
_FILE_MAGIC = {"pdf": (b"%PDF",), "png": (b"\x89PNG",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",)}


def _save_golden_file(storage, label, prefix):
    """Validate (extension + real file header) and save one upload. Returns (path, error)."""
    if not storage or not storage.filename:
        return None, f"Please upload your {label}."
    name = secure_filename(storage.filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _FILE_MAGIC:
        return None, f"The {label} must be a PDF, PNG, JPG or JPEG file."
    head = storage.stream.read(8)
    storage.stream.seek(0)
    if not any(head.startswith(m) for m in _FILE_MAGIC[ext]):
        return None, f"The {label} is not a valid {ext.upper()} file."
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = os.path.join(GOLDEN_DIR, f"{prefix}_{uuid.uuid4().hex}.{ext}")
    storage.save(path)
    file_store_put(path)
    return path, ""


def _golden_public(r, owner=False):
    out = {"id": r["id"], "kind": r["kind"], "name": r["full_name"], "city": r["city"], "country": r["country"],
           "bloodGroup": r["blood_group"], "status": r["verification_status"],
           "labName": r["lab_name"] if r["verification_status"] == "verified" else None,
           "reviewedAt": r["reviewed_at"].isoformat() if r.get("reviewed_at") else None}
    if owner:
        out.update({"reviewNote": r.get("review_note"), "createdAt": r["created_at"].isoformat() if r.get("created_at") else None})
    return out


@app.post("/api/golden-blood/submit")
def golden_submit():
    try:
        d = json.loads(request.form.get("data", "{}"))
    except (TypeError, ValueError):
        return fail("Invalid form data.")
    if not isinstance(d, dict):
        return fail("Invalid form data.")
    kind = str(d.get("kind", "")).strip()
    if kind not in ("donor", "request"):
        return fail("Please choose whether you are registering as a donor or requesting Golden Blood.")
    full_name = str(d.get("fullName", "")).strip()
    email = normalize_email(d.get("email"))
    address = str(d.get("address", "")).strip()
    city = str(d.get("city", "")).strip()
    gender = str(d.get("gender", "")).strip()
    group = str(d.get("bloodGroup", "")).strip()
    country = normalize_country(d.get("country"))
    try:
        age = int(d.get("age"))
    except (TypeError, ValueError):
        return fail("Please enter a valid age.")
    if not full_name or len(full_name) > 100:
        return fail("Please enter your full name.")
    if age < 1 or age > 120:
        return fail("Age must be between 1 and 120.")
    if gender not in {"Male", "Female", "Other", "Prefer not to say"}:
        return fail("Please select your gender.")
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return fail("A valid email address is required.")
    if len(address) < 10 or len(address) > 255:
        return fail("Please enter your complete address (house no., street, area).")
    if not city or len(city) > 100:
        return fail("Please enter your city.")
    if not country:
        return fail("Please select your country.")
    if group not in GOLDEN_GROUPS:
        return fail("Please select a blood group.")
    paths = []
    con = db(); cur = con.cursor(dictionary=True)
    try:
        user, auth_error = get_authenticated_user(cur, d)
        if auth_error:
            return fail(auth_error, 401)
        cur.execute("SELECT id FROM golden_blood WHERE user_id=%s AND kind=%s AND verification_status IN ('pending','needs_info','verified') LIMIT 1",
                    (user["id"], kind))
        if cur.fetchone():
            return fail("You already have an active Golden Blood " + ("donor registration" if kind == "donor" else "request")
                        + ". Please wait for the review result or check its status below.")
        hp, err = _save_golden_file(request.files.get("healthReport"), "health report", "golden_health")
        if err:
            return fail(err)
        paths.append(hp)
        bp, err = _save_golden_file(request.files.get("bloodTestReport"), "blood test report", "golden_labtest")
        if err:
            raise ValueError(err)
        paths.append(bp)
        # status is NEVER taken from the browser: it is always created as pending.
        cur.execute("""INSERT INTO golden_blood (user_id,kind,full_name,age,gender,email,address,city,country,blood_group,
                       health_report_path,blood_test_report_path,verification_status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')""",
                    (user["id"], kind, full_name, age, gender, email, address, city, country, group, hp, bp))
        con.commit()
        rid = cur.lastrowid
        try_send_email(email, "BloodConnect - Golden Blood submission received",
                       "Hello " + full_name + ",\n\nWe received your Golden Blood (Rh-null) "
                       + ("donor registration" if kind == "donor" else "request")
                       + ". Your status is PENDING. Rh-null must be confirmed by a qualified laboratory before you "
                       "are shown as Verified. We will email you after the review.\n\nBloodConnect")
        return ok({"id": rid, "status": "pending"}, "Submitted. Your status is Pending until a qualified laboratory confirmation is reviewed by our team.")
    except ValueError as exc:
        con.rollback()
        for pth in paths:
            if pth and os.path.exists(pth): os.remove(pth)
        return fail(str(exc))
    except Exception:
        con.rollback()
        for pth in paths:
            if pth and os.path.exists(pth): os.remove(pth)
        raise
    finally:
        cur.close(); con.close()


@app.get("/api/golden-blood/donors")
def golden_verified_donors():
    """Public list: ONLY lab-verified donors, with no email, address or files."""
    con = db(); cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur)
        if auth_error:
            return fail(auth_error, 401)
        cur.execute("SELECT * FROM golden_blood WHERE kind='donor' AND verification_status='verified' ORDER BY reviewed_at DESC LIMIT 200")
        return ok([_golden_public(r) for r in cur.fetchall()])
    finally:
        cur.close(); con.close()


@app.get("/api/golden-blood/mine")
def golden_mine():
    con = db(); cur = con.cursor(dictionary=True)
    try:
        user, auth_error = get_authenticated_user(cur)
        if auth_error:
            return fail(auth_error, 401)
        cur.execute("SELECT * FROM golden_blood WHERE user_id=%s ORDER BY id DESC", (user["id"],))
        return ok([_golden_public(r, owner=True) for r in cur.fetchall()])
    finally:
        cur.close(); con.close()


@app.get("/api/admin/golden-blood")
def admin_golden_list():
    con = db(); cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        cur.execute("SELECT * FROM golden_blood ORDER BY FIELD(verification_status,'pending','needs_info','verified','rejected'), id DESC LIMIT 300")
        rows = []
        for r in cur.fetchall():
            x = _golden_public(r, owner=True)
            x.update({"age": r["age"], "gender": r["gender"], "email": r["email"], "address": r["address"],
                      "labName": r["lab_name"], "labReference": r["lab_reference"]})
            rows.append(x)
        return ok(rows)
    finally:
        cur.close(); con.close()


def _find_golden_file(stored):
    """Locate a stored report. First the current upload folder; then the exact folder saved with the row,
    but only if it really is a .../private_uploads/golden_blood folder (so the DB can never point elsewhere)."""
    if not stored:
        return None
    stored = str(stored)
    name = os.path.basename(stored.replace("\\", "/"))
    cand = os.path.join(os.path.abspath(GOLDEN_DIR), name)
    if os.path.isfile(cand):
        return cand
    folder = os.path.dirname(os.path.abspath(stored.replace("\\", os.sep)))
    if os.path.basename(folder) == "golden_blood" and os.path.basename(os.path.dirname(folder)) == "private_uploads":
        cand = os.path.join(folder, name)
        if os.path.isfile(cand):
            return cand
    # Fallback: an older copy of the project (BloodConnect*, BloodConnect_v5, ...) extracted next to this one.
    # Only the file NAME is used, and only inside a private_uploads/golden_blood folder of a sibling BloodConnect* folder.
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        parent = os.path.dirname(here)
        for sib in sorted(os.listdir(parent)):
            if not sib.lower().startswith("bloodconnect"):
                continue
            for base in (os.path.join(parent, sib), os.path.join(parent, sib, sib)):
                cand = os.path.join(base, "private_uploads", "golden_blood", name)
                if os.path.abspath(base) != here and os.path.isfile(cand):
                    return cand
    except OSError:
        pass
    return None


@app.get("/api/admin/golden-blood/<int:rid>/file/<which>")
def admin_golden_file(rid, which):
    col = {"health": "health_report_path", "bloodtest": "blood_test_report_path"}.get(which)
    if not col:
        return fail("Unknown file.", 404)
    con = db(); cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        cur.execute(f"SELECT {col} AS p FROM golden_blood WHERE id=%s", (rid,))
        row = cur.fetchone()
        resp = serve_report(row["p"] if row else None, _find_golden_file)
        if resp is None:
            return fail("File not found. Use the Replace button under this submission to attach the report again (it will then be stored safely in the database).", 404)
        return resp
    finally:
        cur.close(); con.close()


@app.post("/api/admin/golden-blood/<int:rid>/replace-file/<which>")
def admin_golden_replace_file(rid, which):
    """Admin re-attaches a report (PDF/PNG/JPG) to an existing submission, e.g. when the original file went missing."""
    col = {"health": "health_report_path", "bloodtest": "blood_test_report_path"}.get(which)
    if not col:
        return fail("Unknown file.", 404)
    con = db(); cur = con.cursor(dictionary=True)
    try:
        _, auth_error = get_authenticated_user(cur, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        cur.execute("SELECT id FROM golden_blood WHERE id=%s", (rid,))
        if not cur.fetchone():
            return fail("Submission not found.", 404)
        label = "health report" if which == "health" else "blood test report"
        path, err = _save_golden_file(request.files.get("file"), label, "golden_health" if which == "health" else "golden_labtest")
        if err:
            return fail(err)
        cur.execute(f"UPDATE golden_blood SET {col}=%s WHERE id=%s", (path, rid))
        con.commit()
        return ok({"id": rid}, "File attached.")
    except Exception as e:
        app.logger.exception("Failed to replace golden blood file")
        return fail("Could not attach file: " + str(e), 500)
    finally:
        cur.close(); con.close()


@app.post("/api/admin/golden-blood/review")
def admin_golden_review():
    d = request.get_json(silent=True) or {}
    status = str(d.get("status", "")).strip()
    note = str(d.get("note", "")).strip()[:500]
    lab_name = str(d.get("labName", "")).strip()[:200]
    lab_ref = str(d.get("labReference", "")).strip()[:120]
    try:
        rid = int(d.get("id"))
    except (TypeError, ValueError):
        return fail("Invalid request.")
    if status not in GOLDEN_STATUSES or status == "pending":
        return fail("Invalid status.")
    if status == "verified":
        # Hard rule: no Verified status without a qualified laboratory confirmation.
        if d.get("labConfirmed") is not True or len(lab_name) < 3 or len(lab_ref) < 2:
            return fail("To mark as Verified you must confirm that a qualified laboratory confirmed Rh-null, and enter the laboratory name and its report reference number.")
    elif not note:
        return fail("Please write a short note so the person knows why.")
    con = db(); cur = con.cursor(dictionary=True)
    try:
        admin, auth_error = get_authenticated_user(cur, d, require_admin=True)
        if auth_error:
            return fail(auth_error, 403)
        cur.execute("SELECT * FROM golden_blood WHERE id=%s", (rid,))
        row = cur.fetchone()
        if not row:
            return fail("Submission not found.", 404)
        if status == "verified":
            cur.execute("""UPDATE golden_blood SET verification_status='verified', blood_group='Rh-null', lab_name=%s, lab_reference=%s,
                           review_note=%s, reviewed_by=%s, reviewed_at=NOW() WHERE id=%s""", (lab_name, lab_ref, note or None, admin["id"], rid))
        else:
            cur.execute("""UPDATE golden_blood SET verification_status=%s, lab_name=NULL, lab_reference=NULL,
                           review_note=%s, reviewed_by=%s, reviewed_at=NOW() WHERE id=%s""", (status, note, admin["id"], rid))
        con.commit()
        label = {"verified": "VERIFIED", "rejected": "NOT APPROVED", "needs_info": "NEEDS MORE INFORMATION"}[status]
        try_send_email(row["email"], "BloodConnect - Golden Blood review: " + label,
                       "Hello " + row["full_name"] + ",\n\nYour Golden Blood (Rh-null) submission is now: " + label + "."
                       + ("\n\nNote from our team: " + note if note else "") + "\n\nBloodConnect")
        return ok({"id": rid, "status": status}, "Status updated.")
    finally:
        cur.close(); con.close()


if __name__ != "__main__":
    # Running under gunicorn (hosting): create tables automatically on startup.
    try:
        init_database()
    except Exception:
        app.logger.exception("init_database failed at startup")

if __name__ == "__main__":
    print("BloodConnect build:", BUILD)
    if SMTP_EMAIL and SMTP_PASSWORD:
        print("Email: configured (" + SMTP_EMAIL + ")")
    else:
        print("Email: NOT configured. .env " + ("found but SMTP_EMAIL/SMTP_PASSWORD are empty: " if ENV_FOUND else "NOT found. Create it here: ") + ENV_PATH)
    try:
        init_database()
        print("MySQL database and tables are ready.")
        print("Open: http://127.0.0.1:5000")
        # threaded=True lets Flask handle the three 3-second polling
        # requests concurrently instead of queueing them one by one.
        app.run(host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "5000")),
                debug=os.environ.get("FLASK_DEBUG", "1") == "1", threaded=True)
    except Error as e:
        print("MySQL connection error:", e)
        print("Check that MySQL is running and that DB_PASSWORD in the .env file (next to data.py) is your MySQL root password.")
