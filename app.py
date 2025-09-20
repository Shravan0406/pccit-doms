import os, re, base64, smtplib, uuid, logging
from email.mime.text import MIMEText
from email.utils import parseaddr
from datetime import datetime
from functools import wraps
from pathlib import Path


from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, flash
import mysql.connector as mysql
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from io import BytesIO
from datetime import datetime, date
try:
    import qrcode  # pip install qrcode[pil]
except ImportError:
    qrcode = None

# ---------------------- Config ----------------------
BASE_DIR = os.path.dirname(__file__)
SSL_CA_PATH = os.path.join(BASE_DIR, "isrgrootx1.pem")
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "gateway01.us-east-1.prod.aws.tidbcloud.com"),
    "port": int(os.getenv("MYSQL_PORT", 4000)),
    "user": os.getenv("MYSQL_USER", "35y1kD58qEZM9KR.root"),
    "password": os.getenv("MYSQL_PASSWORD", "hzn8xpELksFIHUF2"),
    "database": os.getenv("MYSQL_DATABASE", "employeeportal"),
    "ssl_ca": os.environ.get("SSL_CA_PATH"),
}

SMTP_HOST = os.getenv("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "975e36001@smtp-brevo.com")
SMTP_PASS = os.getenv("SMTP_PASS", "JFp1Yq2jUASVOw5g")
SMTP_SENDER = os.getenv("SMTP_SENDER", SMTP_USER or "ssbandi04@gmail.com")
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://pccit-doms.vercel.app/")
SMTP_SECURITY = (os.getenv("SMTP_SECURITY", "starttls") or "starttls").lower()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me")

UPLOAD_DIR = "/tmp/uploads"
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
# ---------------------- DB Helpers ----------------------
_DB_READY = False
def _ensure_db():
    global _DB_READY
    if not _DB_READY:
        init_db()
        _DB_READY = True
def _root_conn_and_dbname():
    cfg = DB_CONFIG.copy()
    dbname = cfg.pop("database", None)
    return mysql.connect(**cfg), dbname

def get_conn():
    return mysql.connect(**DB_CONFIG)

def column_exists(cur, table, column):
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s AND column_name=%s
    """, (DB_CONFIG["database"], table, column))
    return cur.fetchone() is not None

def index_exists(cur, table, index_name):
    cur.execute("""
        SELECT 1 FROM information_schema.statistics 
        WHERE table_schema=%s AND table_name=%s AND index_name=%s
    """, (DB_CONFIG["database"], table, index_name))
    return cur.fetchone() is not None

def column_type(cur, table, column):
    cur.execute("""
        SELECT DATA_TYPE, COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH 
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s AND column_name=%s
    """, (DB_CONFIG["database"], table, column))
    return cur.fetchone()

def to_date(s):
    if not s: return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None

def parse_data_url(data_url: str):
    if not data_url or not data_url.startswith("data:"): return None
    try:
        header, b64 = data_url.split(",", 1)
        mime = header.split(";")[0].split(":", 1)[1] or "image/png"
        return mime, base64.b64decode(b64)
    except Exception:
        return None

def b64(blob):
    return base64.b64encode(blob).decode("ascii") if blob else ""

_num_re = re.compile(r"\d+")

def parse_selected_subjects(form) -> list:
    selected = form.getlist("subjects")
    if not selected:
        one = form.get("subject")
        if one: selected = [one]
    valid = {"paper1","paper2","paper3","paper4","paper5"}
    return [s.lower() for s in selected if s and s.lower() in valid]

def parse_attempts(form, key: str) -> int:
    vals = form.getlist(key)
    for v in reversed(vals):
        if not v: continue
        m = _num_re.search(str(v))
        if m:
            try: return int(m.group())
            except ValueError: continue
    return 0

def is_pwd_from_csv(csv_text: str) -> bool:
    return "PWD" in [x.strip().upper() for x in (csv_text or "").split(",") if x.strip()]

def generate_qr_bytes(text: str):
    """
    Returns (mime, bytes) for a PNG QR image for the given text.
    """
    if qrcode is None:
        return "image/png", b""  # graceful fallback if lib is missing

    qr = qrcode.QRCode(
        version=None,               # auto
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return "image/png", buf.getvalue()

def as_ymd(d):
    """Return YYYY-MM-DD for a date/datetime/str (or '' if missing)."""
    if not d:
        return ""
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y-%m-%d")
    # d is a string -> try to parse common formats, else return as-is
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s

# ---------------------- Email Helper ----------------------
app.logger.setLevel(logging.INFO)
def send_email(to_email: str, subject: str, html_body: str) -> bool:
    app.logger.setLevel(logging.INFO)
    log = app.logger
    log.info("send_email: to=%s subj=%s host=%s port=%s sec=%s",
             to_email, subject, SMTP_HOST, SMTP_PORT, SMTP_SECURITY)

    missing = [k for k, v in {
        "SMTP_HOST": SMTP_HOST, "SMTP_PORT": SMTP_PORT, "SMTP_USER": SMTP_USER,
        "SMTP_PASS": SMTP_PASS, "SMTP_SENDER": SMTP_SENDER
    }.items() if not v]
    if missing:
        log.error("SMTP config incomplete, missing: %s", ", ".join(missing))
        return False

    msg = MIMEText(html_body, "html")
    msg["Subject"] = subject
    msg["From"] = SMTP_SENDER
    msg["To"] = to_email
    envelope_from = parseaddr(SMTP_SENDER)[1] or SMTP_USER

    try:
        if SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
            server.set_debuglevel(1)  # log SMTP conversation
            if SMTP_SECURITY == "starttls":
                server.starttls()

        server.login(SMTP_USER, SMTP_PASS)
        resp = server.sendmail(envelope_from, [to_email], msg.as_string())
        server.quit()

        if resp:
            log.error("send_email: provider returned partial failures: %r", resp)
            return False

        log.info("send_email: SUCCESS to=%s", to_email)
        return True
    except Exception as e:
        log.exception("send_email failed: %s", e)
        return False

# ---------------------- Auth Guard ----------------------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("staff_user_id"):
            flash("Please log in as staff to access that page.", "error")
            return redirect(url_for("home") + "#staff")
        return fn(*args, **kwargs)
    return wrapper

# ---------------------- Init / Migrations ----------------------
def init_db():
    # Create DB
    root_conn, dbname = _root_conn_and_dbname()
    rcur = root_conn.cursor()
    rcur.execute(
        f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    rcur.close(); root_conn.close()

    conn = get_conn(); cur = conn.cursor()

    # NEW: Employee Accounts (for requirement #1/#2)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employee_accounts (
            name VARCHAR(120) NOT NULL,
            emp_code VARCHAR(40) NOT NULL PRIMARY KEY,
            desig VARCHAR(120) NOT NULL,
            emp_dob DATE NOT NULL,
            rec_type VARCHAR(40) NOT NULL,
            mobile VARCHAR(15) NOT NULL,
            email VARCHAR(190) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_empcode_accounts (emp_code),
            UNIQUE KEY uniq_email_accounts (email)
        )
    """)

    # Applicants
    cur.execute("""
        CREATE TABLE IF NOT EXISTS applicants (
            id INT AUTO_INCREMENT PRIMARY KEY,
            emp_code VARCHAR(40) NOT NULL,
            name VARCHAR(120) NOT NULL,
            designation VARCHAR(120) NOT NULL,
            recruitment_type VARCHAR(40) NOT NULL,
            mobile VARCHAR(20) NOT NULL,
            exam_purpose VARCHAR(120),
            doj DATE,
            category VARCHAR(64) NOT NULL,
            cit_charge VARCHAR(120),
            dob DATE,
            eligibility_year VARCHAR(10),
            roll_1991 VARCHAR(40),
            posting_place VARCHAR(120),
            attempts INT,
            subjects TEXT,
            lang_accounts VARCHAR(40),
            exam_center VARCHAR(120),
            sign_place VARCHAR(120),
            sign_date DATE,
            signature_blob LONGBLOB,
            signature_mime VARCHAR(50),
            photo_blob LONGBLOB,
            photo_mime VARCHAR(50),
            qr_blob LONGBLOB,
            qr_mime VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_code) REFERENCES employee_accounts(emp_code) ON DELETE CASCADE,
            UNIQUE KEY uniq_app_qr (qr_mime)
        )
    """)

    if not column_exists(cur, "applicants", "qr_blob"):
        cur.execute("ALTER TABLE applicants ADD COLUMN qr_blob LONGBLOB AFTER photo_mime")
    if not column_exists(cur, "applicants", "qr_mime"):
        cur.execute("ALTER TABLE applicants ADD COLUMN qr_mime VARCHAR(50) AFTER qr_blob")

    # Exam performance
    cur.execute("""
        CREATE TABLE IF NOT EXISTS exam_performance (
            id INT AUTO_INCREMENT PRIMARY KEY,
            applicant_id INT NOT NULL,
            emp_code VARCHAR(40) NOT NULL,
            p1_year VARCHAR(10), p1_result VARCHAR(40),
            p2_year VARCHAR(10), p2_result VARCHAR(40),
            p3_year VARCHAR(10), p3_result VARCHAR(40),
            p4_year VARCHAR(10), p4_result VARCHAR(40),
            p5_year VARCHAR(10), p5_result VARCHAR(40),
            UNIQUE KEY uniq_perf_applicant (applicant_id),
            FOREIGN KEY (emp_code) REFERENCES employee_accounts(emp_code) ON DELETE CASCADE,
            FOREIGN KEY (applicant_id) REFERENCES applicants(id) ON DELETE CASCADE
        )
    """)

    # Staff users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staff_users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            email VARCHAR(190) NOT NULL,
            emp_code VARCHAR(40) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            is_primary TINYINT(1) NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_staff_email (email),
            FOREIGN KEY (emp_code) REFERENCES employee_accounts(emp_code) ON DELETE CASCADE
        )
    """)

    # Staff login requests
    cur.execute("""
        CREATE TABLE IF NOT EXISTS staff_login_requests (
            id INT AUTO_INCREMENT PRIMARY KEY,
            requester_email VARCHAR(190) NOT NULL,
            emp_code VARCHAR(40) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            token VARCHAR(64) NOT NULL,
            status ENUM('pending','approved','rejected') NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_code) REFERENCES employee_accounts(emp_code) ON DELETE CASCADE,
            UNIQUE KEY uniq_token (token)
        )
    """)

    # Notifications
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            url TEXT,
            file_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # After CREATE TABLE notifications ...
    if not column_exists(cur, "notifications", "is_published"):
        cur.execute("""
            ALTER TABLE notifications
            ADD COLUMN is_published TINYINT(1) NOT NULL DEFAULT 1
            AFTER file_path
        """)


    # NEW: Results (for public results login)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS exam_results (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            emp_code VARCHAR(40) NOT NULL,
            designation VARCHAR(120) NOT NULL,
            dob DATE NOT NULL,
            roll_no VARCHAR(40),
            category VARCHAR(64),
            mobile VARCHAR(15),
            p1_marks VARCHAR(10), p1_result ENUM('Pass','Fail'),
            p2_marks VARCHAR(10), p2_result ENUM('Pass','Fail'),
            p3_marks VARCHAR(10), p3_result ENUM('Pass','Fail'),
            p4_marks VARCHAR(10), p4_result ENUM('Pass','Fail'),
            p5_marks VARCHAR(10), p5_result ENUM('Pass','Fail'),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_code) REFERENCES employee_accounts(emp_code) ON DELETE CASCADE,
            UNIQUE KEY uniq_empcode_results (emp_code)
        )
    """)
    # after CREATE TABLE exam_results (...)
    if not column_exists(cur, "exam_results", "mobile"):
        cur.execute("ALTER TABLE exam_results ADD COLUMN mobile VARCHAR(15) AFTER category")


    # Migrations for applicants columns
    ct = column_type(cur, "applicants", "category")
    if ct and (ct[0].lower() != "varchar" or (ct[2] or 0) < 64):
        cur.execute("ALTER TABLE applicants MODIFY COLUMN category VARCHAR(64) NOT NULL")
    if not column_exists(cur, "applicants", "signature_blob"):
        cur.execute("ALTER TABLE applicants ADD COLUMN signature_blob LONGBLOB AFTER sign_date")
    if not column_exists(cur, "applicants", "signature_mime"):
        cur.execute("ALTER TABLE applicants ADD COLUMN signature_mime VARCHAR(50) AFTER signature_blob")
    if not column_exists(cur, "applicants", "photo_blob"):
        cur.execute("ALTER TABLE applicants ADD COLUMN photo_blob LONGBLOB AFTER signature_mime")
    if not column_exists(cur, "applicants", "photo_mime"):
        cur.execute("ALTER TABLE applicants ADD COLUMN photo_mime VARCHAR(50) AFTER photo_blob")
    if not index_exists(cur, "applicants", "uniq_empcode"):
        cur.execute("ALTER TABLE applicants ADD UNIQUE KEY uniq_empcode (emp_code)")

    conn.commit()
    cur.close(); conn.close()
       
# ---------------------- Routes: Public ----------------------
@app.route("/")
def home():
    _ensure_db()
    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, title, url, file_path, created_at FROM notifications WHERE is_published=1 ORDER BY created_at DESC LIMIT 25")
    notices = cur.fetchall()
    cur.close(); conn.close()
    return render_template("index.html", data={}, error=None, notices=notices)

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No selected file"}), 400

    fname = f"{uuid.uuid4()}-{secure_filename(f.filename)}"
    path = os.path.join(UPLOAD_DIR, fname)
    f.save(path)
    return {"ok": True}


# Dedicated anchor for results login (notification will link here)
@app.route("/results")
def results_page():
    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, title, url, file_path, created_at FROM notifications WHERE is_published=1 ORDER BY created_at DESC LIMIT 25")
    notices = cur.fetchall()
    cur.close(); conn.close()
    return render_template("index.html", data={}, error=None, notices=notices, show_results_login=True)

# ---------------------- Employee Accounts (NEW) ----------------------
@app.route("/staff/signup", methods=["GET"])
def staff_signup_page():
    # Render main template, show signup section
    return render_template("index.html", data={}, show_staff_signup=True)

@app.route("/staff/signup", methods=["POST"])
def staff_signup_create():
    # safe .get() usage
    name     = (request.form.get("acc_name")  or "").strip()
    emp_code = (request.form.get("acc_emp")   or "").strip()
    desig    = (request.form.get("acc_desig") or "").strip()
    emp_dob  = (request.form.get("acc_dob")   or "").strip()  # HTML <input type="date"> gives YYYY-MM-DD
    rec_type = (request.form.get("acc_rt")    or "").strip()
    mobile   = (request.form.get("acc_mb")    or "").strip()
    email    = (request.form.get("acc_email") or "").strip().lower()
    pwd      = (request.form.get("acc_pass")  or "").strip()
    pwd2     = (request.form.get("acc_pass2") or "").strip()

    if not all([name, emp_code, desig, emp_dob, rec_type, mobile, email, pwd, pwd2]):
        flash("Please fill all fields.", "error")
        return redirect(url_for("staff_signup_page"))

    if pwd != pwd2:
        flash("Passwords do not match.", "error")
        return redirect(url_for("staff_signup_page"))

    ok = False
    try:
        conn = get_conn()
        cur = conn.cursor()
        # FIX 1: correct column name (mobile) and
        # FIX 2: provide 8 placeholders for 8 columns
        cur.execute("""
            INSERT INTO employee_accounts
                (name, emp_code, desig, emp_dob, rec_type, mobile, email, password_hash)
            VALUES
                (%s,   %s,      %s,     %s,      %s,       %s,     %s,    %s)
        """, (name, emp_code, desig, emp_dob, rec_type, mobile, email, generate_password_hash(pwd)))
        conn.commit()
        ok = True
    except mysql.Error as e:
        # duplicate key (email or emp_code) -> 1062
        if getattr(e, "errno", None) == 1062:
            flash("Employee code or email already exists.", "error")
        else:
            # Show brief reason to help you debug; remove .msg in production if you prefer generic
            flash(f"Error creating account: {e.msg}", "error")
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass

    if ok:
        flash("Account created successfully. You can now log in.", "success")

    # PRG pattern so flashes render reliably
    return redirect(url_for("staff_signup_page"))

# ---------------------- Applicant Submit (unchanged core) ----------------------
def empcode_exists(emp_code: str) -> bool:
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM employee_accounts WHERE emp_code=%s LIMIT 1", (emp_code,))
    found = cur.fetchone() is not None
    cur.close(); conn.close()
    return found

@app.route("/submit", methods=["POST"])
def submit():
    emp_code = request.form.get("empCode")
    if not emp_code or not empcode_exists(emp_code):
        # Render with inline error under Apply form emp code
        return render_template("index.html", data={}, invalid_empcode_apply=True, preserve_apply=True)
    name = request.form.get("name")
    designation = request.form.get("designation")
    recruitment_type = request.form.get("recruitmentType")
    mobile = request.form.get("mobile")
    exam_purpose = request.form.get("exampurpose")
    doj = to_date(request.form.get("doj"))

    category_csv = request.form.get("category", "").strip()
    cit_charge = request.form.get("citCharge")
    dob = to_date(request.form.get("dob"))
    eligibility_year = request.form.get("eligibilityYear")
    roll_1991 = request.form.get("roll1991")
    posting_place = request.form.get("postingPlace")
    attempts = request.form.get("attempts")
    selected_subjects = parse_selected_subjects(request.form)
    subjects = ",".join(selected_subjects) if selected_subjects else ""

    lang_accounts = request.form.get("language")
    exam_center = request.form.get("examCenter")
    sign_place = request.form.get("signPlace")
    sign_date = to_date(request.form.get("signDate"))

    sig_file = request.files.get("signatureUpload")
    sig_blob, sig_mime = None, None
    if sig_file and sig_file.filename:
        sig_blob = sig_file.read()
        sig_mime = sig_file.mimetype or "image/png"
    else:
        sig_data_url = request.form.get("signatureData")
        parsed = parse_data_url(sig_data_url) if sig_data_url else None
        if parsed:
            sig_mime, sig_blob = parsed

    photo_file = request.files.get("photoUpload")
    photo_blob = photo_file.read() if (photo_file and photo_file.filename) else None
    photo_mime = (photo_file.mimetype if (photo_file and photo_file.filename) else None) or "image/png"

    attempts = request.form.get("attempts")
    attempts_int = int(attempts) if (attempts and str(attempts).isdigit()) else 0
    if attempts_int >= 10:
        return render_template(
            "index.html",
            data={}, error="",
            success_message="You are not eligible to apply because all your attempts to complete this exams were already used."
        ), 400

    if not all([name, designation, recruitment_type, emp_code, mobile, doj]) or not category_csv:
        return render_template("index.html", data={}, error="Missing required fields in application form."), 400

    if not sig_blob:
        return render_template("index.html", data={}, error="Please provide a handwritten signature (draw or upload) before submitting."), 400

    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT 1 FROM employee_accounts WHERE emp_code=%s AND mobile=%s LIMIT 1",
        (emp_code, mobile)
    )
    pair_ok = cur.fetchone() is not None
    cur.close(); conn.close()
    if not pair_ok:
        return render_template("index.html", data={}, invalid_mobile_apply=True, preserve_apply=True)

    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO applicants (
            name, designation, recruitment_type, emp_code, mobile, exam_purpose, doj, category, cit_charge, dob,
            eligibility_year, roll_1991, posting_place, subjects, lang_accounts, exam_center, sign_place, sign_date,
            signature_blob, signature_mime, photo_blob, photo_mime
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            id = LAST_INSERT_ID(id),
            name=VALUES(name), designation=VALUES(designation), recruitment_type=VALUES(recruitment_type),
            mobile=VALUES(mobile), exam_purpose=VALUES(exam_purpose), doj=VALUES(doj), category=VALUES(category),
            cit_charge=VALUES(cit_charge), dob=VALUES(dob), eligibility_year=VALUES(eligibility_year),
            roll_1991=VALUES(roll_1991), posting_place=VALUES(posting_place), subjects=VALUES(subjects),
            lang_accounts=VALUES(lang_accounts), exam_center=VALUES(exam_center), sign_place=VALUES(sign_place),
            sign_date=VALUES(sign_date), signature_blob=COALESCE(VALUES(signature_blob), signature_blob),
            signature_mime=COALESCE(VALUES(signature_mime), signature_mime), photo_blob=COALESCE(VALUES(photo_blob), photo_blob),
            photo_mime=COALESCE(VALUES(photo_mime), photo_mime)
    """, (
        name, designation, recruitment_type, emp_code, mobile, exam_purpose, doj, category_csv, cit_charge, dob,
        eligibility_year, roll_1991, posting_place, subjects, lang_accounts, exam_center, sign_place, sign_date,
        sig_blob, sig_mime, photo_blob, photo_mime
    ))
    applicant_id = cur.lastrowid

    def yr_res(form, key):
        vals = form.getlist(key)
        year = vals[0].strip() if len(vals) >= 1 and vals[0] else None
        result = vals[1].strip() if len(vals) >= 2 and vals[1] else None
        return year, result

    p1y, p1r = yr_res(request.form, "paper1")
    p2y, p2r = yr_res(request.form, "paper2")
    p3y, p3r = yr_res(request.form, "paper3")
    p4y, p4r = yr_res(request.form, "paper4")
    p5y, p5r = yr_res(request.form, "paper5")

    cur.execute("""
        INSERT INTO exam_performance (
            applicant_id, emp_code,
            p1_year, p1_result,
            p2_year, p2_result,
            p3_year, p3_result,
            p4_year, p4_result,
            p5_year, p5_result
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            p1_year   = VALUES(p1_year),   p1_result = VALUES(p1_result),
            p2_year   = VALUES(p2_year),   p2_result = VALUES(p2_result),
            p3_year   = VALUES(p3_year),   p3_result = VALUES(p3_result),
            p4_year   = VALUES(p4_year),   p4_result = VALUES(p4_result),
            p5_year   = VALUES(p5_year),   p5_result = VALUES(p5_result)
    """, (
        applicant_id, emp_code,
        p1y, p1r,
        p2y, p2r,
        p3y, p3r,
        p4y, p4r,
        p5y, p5r
    ))


    # --- Build a stable QR payload (include key fields that identify the applicant) ---
    qr_payload = (
        f"AdmitCard|EmpCode:{emp_code}|Name:{name}|"
        f"DOB:{as_ymd(dob)}|"
        f"Mobile:{mobile}|Roll:{roll_1991 or emp_code}"
    )

    qr_mime, qr_bytes = generate_qr_bytes(qr_payload)

    # Store QR image with the applicant
    cur.execute(
        "UPDATE applicants SET qr_blob=%s, qr_mime=%s WHERE id=%s",
        (qr_bytes, qr_mime, applicant_id)
    )

    conn.commit(); cur.close(); conn.close()

    qr_b64 = b64(qr_bytes)
    photo_b64 = b64(photo_blob)
    sig_b64 = b64(sig_blob)
    data = {
        "rollNo": roll_1991,
        "name": name,
        "designation": designation,
        "mobile": mobile,
        "office": cit_charge or "",
        "dob": dob or "",
        "empCode": emp_code,
        "pwd": "Yes" if is_pwd_from_csv(category_csv) else "No",
        "scribe": "No",
        "paper1": ("Yes" if "paper1" in set(selected_subjects) else "-"),
        "paper2": ("Yes" if "paper2" in set(selected_subjects) else "-"),
        "paper3": ("Yes" if "paper3" in set(selected_subjects) else "-"),
        "paper4": ("Yes" if "paper4" in set(selected_subjects) else "-"),
        "paper5": ("Yes" if "paper5" in set(selected_subjects) else "-"),
        "center": exam_center or "",
        "examDate": datetime.now().strftime("%d-%m-%Y"),
        "photo_data_uri": f"data:{photo_mime};base64,{photo_b64}" if photo_b64 else "",
        "signature_data_uri": f"data:{sig_mime};base64,{sig_b64}" if sig_b64 else "",
        "photo": photo_b64,
        "signature": sig_b64,
        "qrCode": f"data:{qr_mime};base64,{qr_b64}" if qr_b64 else "",
    }
    flash("Application form successfully submitted", "success")
    return redirect("/")
    #return render_template("index.html", data=data, error=None,
                           #success_message="Application Submitted Successfully")

# ---------------------- Existing Applicant Login (Admit Card) ----------------------
@app.route("/login", methods=["POST"])
def login():
    emp_code = (request.form.get("empCode") or "").strip()
    email    = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if not (emp_code and email and password):
        return render_template("index.html", data={}, error="Please provide Employee Code, Email and Password"), 400

    conn = get_conn(); cur = conn.cursor(dictionary=True)

    # 1) Validate credentials against employee_accounts
    cur.execute(
        "SELECT * FROM employee_accounts WHERE emp_code=%s AND email=%s LIMIT 1",
        (emp_code, email)
    )
    acct = cur.fetchone()
    if not acct or not check_password_hash(acct["password_hash"], password):
        cur.close(); conn.close()
        # keep the same error plumbing your template expects
        return render_template("index.html", data={}, error="Invalid credentials.", invalid_empcode_admit=(acct is None)), 401

    # 2) Fetch the most recent applicant record for this employee
    cur.execute(
        "SELECT * FROM applicants WHERE emp_code=%s ORDER BY updated_at DESC, created_at DESC LIMIT 1",
        (emp_code,)
    )
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return render_template("index.html", data={}, error="No application found. Please submit the application first."), 404

    # 3) Ensure a QR exists (generate & persist if missing)
    qr_blob = row.get("qr_blob")
    qr_mime = row.get("qr_mime") or "image/png"
    if not qr_blob:
        qr_payload = (
            f"AdmitCard|EmpCode:{row.get('emp_code')}|Name:{row.get('name')}|"
            f"DOB:{as_ymd(row.get('dob'))}|"
            f"Mobile:{row.get('mobile')}|Roll:{(row.get('roll_1991') or row.get('emp_code'))}"
        )
        qr_mime, qr_blob = generate_qr_bytes(qr_payload)
        cur.execute("UPDATE applicants SET qr_blob=%s, qr_mime=%s WHERE id=%s", (qr_blob, qr_mime, row["id"]))
        conn.commit()

    # 4) Shape admit-card payload (same keys your template already uses)
    chosen = {s.strip().lower() for s in (row.get("subjects") or "").split(",") if s.strip()}
    photo_b64 = b64(row.get("photo_blob"));  sig_b64 = b64(row.get("signature_blob"))
    photo_mime = row.get("photo_mime") or "image/png"; sig_mime = row.get("signature_mime") or "image/png"

    data = {
        "rollNo": row.get("emp_code"),
        "name": row.get("name"),
        "designation": row.get("designation"),
        "mobile": row.get("mobile"),
        "office": row.get("cit_charge") or "",
        "dob": (row.get("dob").strftime("%Y-%m-%d") if row.get("dob") else ""),
        "empCode": row.get("emp_code"),
        "pwd": "Yes" if is_pwd_from_csv(row.get("category") or "") else "No",
        "scribe": "No",
        "paper1": ("Yes" if "paper1" in chosen else "-"),
        "paper2": ("Yes" if "paper2" in chosen else "-"),
        "paper3": ("Yes" if "paper3" in chosen else "-"),
        "paper4": ("Yes" if "paper4" in chosen else "-"),
        "paper5": ("Yes" if "paper5" in chosen else "-"),
        "center": row.get("exam_center") or "",
        "examDate": datetime.now().strftime("%d-%m-%Y"),
        "photo_data_uri": f"data:{photo_mime};base64,{photo_b64}" if photo_b64 else "",
        "signature_data_uri": f"data:{sig_mime};base64,{sig_b64}" if sig_b64 else "",
        "photo": photo_b64,
        "signature": sig_b64,
        "qrCode": f"data:{qr_mime};base64,{b64(qr_blob)}" if qr_blob else "",
    }

    cur.close(); conn.close()
    return render_template("index.html", data=data, error=None)


# ---------------------- Staff Login & Approval (unchanged) ----------------------
@app.route("/_dev/test_email")
def _dev_test_email():
    to = request.args.get("to") or os.getenv("SMTP_TEST_TO")
    if not to:
        return "Provide ?to=someone@example.com or set SMTP_TEST_TO env", 400
    ok = send_email(to, "SMTP test from Dept Exams", "<p>Hello from Flask ✅</p>")
    return ("Email sent ✅" if ok else "Email failed ❌"), (200 if ok else 500)

@app.route("/staff-auth", methods=["POST"])
def staff_auth():
    email = (request.form.get("staffEmail") or "").strip().lower()
    emp_code = (request.form.get("staffEmpCode") or "").strip()
    password = (request.form.get("staffPassword") or "")

    if not (email and emp_code and password):
        flash("Please fill email, employee code, and password.", "error")
        return redirect(url_for("home") + "#staff")

    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        # Validate against employee_accounts
        cur.execute(
            "SELECT * FROM employee_accounts WHERE emp_code=%s AND email=%s LIMIT 1",
            (emp_code, email)
        )
        acct = cur.fetchone()
        if not acct or not check_password_hash(acct["password_hash"], password):
            return render_template(
                "index.html",
                data={}, error="Invalid credentials.", invalid_empcode_staff=(acct is None)
            ), 401

        # First staff becomes primary (no email in this branch)
        cur.execute("SELECT * FROM staff_users WHERE is_primary=1 LIMIT 1")
        primary = cur.fetchone()
        if primary is None:
            cur.execute("""
                INSERT INTO staff_users (email, emp_code, password_hash, is_primary)
                VALUES (%s, %s, %s, 1)
            """, (email, emp_code, generate_password_hash(password)))
            conn.commit()
            cur.execute("SELECT LAST_INSERT_ID() AS id")
            new_id = cur.fetchone()["id"]
            session["staff_user_id"] = new_id
            session["staff_email"] = email
            flash("Welcome! You are registered as the primary staff user.", "success")
            return redirect(url_for("staff_notifications"))

        # If the user already exists as staff and the password matches, just log them in
        cur.execute("SELECT * FROM staff_users WHERE email=%s LIMIT 1", (email,))
        existing = cur.fetchone()
        if existing and check_password_hash(existing["password_hash"], password):
            session["staff_user_id"] = existing["id"]
            session["staff_email"] = existing["email"]
            flash("Logged in successfully.", "success")
            return redirect(url_for("staff_notifications"))

        # Otherwise create or update a pending approval request, then email the primary
        token = uuid.uuid4().hex
        pwd_hash = generate_password_hash(password)

        cur.execute("""
            SELECT id FROM staff_login_requests
            WHERE requester_email=%s AND status='pending' LIMIT 1
        """, (email,))
        if cur.fetchone():
            cur.execute("""
                UPDATE staff_login_requests
                SET emp_code=%s, password_hash=%s, token=%s, updated_at=NOW()
                WHERE requester_email=%s AND status='pending'
            """, (emp_code, pwd_hash, token, email))
        else:
            cur.execute("""
                INSERT INTO staff_login_requests (requester_email, emp_code, password_hash, token, status)
                VALUES (%s,%s,%s,%s,'pending')
            """, (email, emp_code, pwd_hash, token))
        conn.commit()

        approve_url = url_for("staff_request_action", token=token, _external=True) + "?action=approve"
        reject_url  = url_for("staff_request_action", token=token, _external=True) + "?action=reject"
        html = f"""
            <p>Dear Primary Staff User,</p>
            <p><b>{email}</b> is requesting access to the staff portal.</p>
            <p>Approve or reject:</p>
            <p><a href="{approve_url}">Approve</a> | <a href="{reject_url}">Reject</a></p>
            <p>Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        """

        ok = send_email(primary["email"], "Staff login approval request", html)
        if ok:
            flash("Request sent to primary for approval.", "info")
        else:
            flash("Could not send email to primary. Check SMTP settings & logs.", "error")
        return redirect(url_for("home") + "#staff")

    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

    
@app.route("/staff/requests/<token>")
def staff_request_action(token):
    action = (request.args.get("action") or "").lower()
    if action not in ("approve","reject"):
        return "Invalid action.", 400

    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM staff_login_requests WHERE token=%s AND status='pending' LIMIT 1", (token,))
    req = cur.fetchone()
    if not req:
        cur.close(); conn.close()
        return "This request is not pending or token is invalid.", 400

    if action == "approve":
        cur.execute("SELECT id FROM staff_users WHERE email=%s LIMIT 1", (req["requester_email"],))
        exists = cur.fetchone()
        if exists:
            cur.execute("UPDATE staff_users SET emp_code=%s WHERE id=%s",
                        (req["emp_code"], exists["id"]))
        else:
            cur.execute("""
                INSERT INTO staff_users (email, emp_code, password_hash, is_primary)
                VALUES (%s,%s,%s,0)
            """, (req["requester_email"], req["emp_code"], req["password_hash"]))
        cur.execute("UPDATE staff_login_requests SET status='approved' WHERE id=%s", (req["id"],))
        conn.commit()
        cur.close(); conn.close()
        return f"Approved. {req['requester_email']} can now log in."
    else:
        cur.execute("UPDATE staff_login_requests SET status='rejected' WHERE id=%s", (req["id"],))
        conn.commit()
        cur.close(); conn.close()
        return "Rejected."

@app.route("/logout")
def logout():
    session.pop("staff_user_id", None)
    session.pop("staff_email", None)
    flash("Logged out.", "success")
    return redirect(url_for("home"))

# ---------------------- Staff: Release Application Form ----------------------
from datetime import datetime
@app.route("/staff/release-application-form", methods=["POST"])
@login_required
def release_application_form():
    """Publishes the 'Application Form' notification that opens the Apply/Notice UI on the homepage."""
    current_year = datetime.now().year
    title = f"Apply for Departmental Exams for Ministerial Staff {current_year} –"
    url   = "/#notice"  # clicking this opens the apply/login panel on the home page

    conn = get_conn(); cur = conn.cursor(dictionary=True)

    # Avoid duplicate published copies
    cur.execute("""
        SELECT id FROM notifications
        WHERE is_published=1 AND title=%s AND (url=%s OR url IS NULL)
        LIMIT 1
    """, (title, url))
    exists = cur.fetchone()

    if not exists:
        cur.execute("""
            INSERT INTO notifications (title, url, is_published)
            VALUES (%s, %s, 1)
        """, (title, url))
        conn.commit()

    cur.close(); conn.close()
    flash("Application Form released to the front page.", "success")
    return redirect(url_for("staff_notifications"))

# ---------------------- Staff Unpublish ----------------------
@app.route("/staff/unpublish", methods=["POST"])
@login_required
def staff_unpublish():
    ids = request.form.getlist("ids")
    if not ids:
        flash("Please select at least one notification to unpublish.", "error")
        return redirect(url_for("staff_notifications"))

    # sanitize: keep only ints
    ids = [i for i in ids if str(i).isdigit()]
    if not ids:
        flash("No valid notifications selected.", "error")
        return redirect(url_for("staff_notifications"))

    qmarks = ",".join(["%s"] * len(ids))
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE notifications SET is_published=0 WHERE id IN ({qmarks})", ids)
    conn.commit()
    cur.close(); conn.close()

    flash("Selected notifications have been unpublished.", "success")
    return redirect(url_for("staff_notifications"))


# ---------------------- Staff: Notifications + NEW Results ----------------------
@app.route("/staff/notifications", methods=["GET", "POST"])
@login_required
def staff_notifications():
    if request.method == "POST":
        title = (request.form.get("nt_title") or "").strip()
        link  = (request.form.get("nt_link") or "").strip()
        file  = request.files.get("nt_file")

        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("staff_notifications"))

        file_path = None
        if file and file.filename:
            fname = secure_filename(file.filename)
            save_path = os.path.join(UPLOAD_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{fname}")
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            file.save(save_path)
            file_path = save_path.replace("\\", "/")

        conn = get_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO notifications (title, url, file_path) VALUES (%s,%s,%s)",
                    (title, link or None, file_path))
        conn.commit(); cur.close(); conn.close()
        flash("Notification published on the front page.", "success")
        return redirect(url_for("staff_notifications"))

    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, title, url, file_path, created_at FROM notifications ORDER BY created_at DESC LIMIT 50")
    items = cur.fetchall()
    cur.close(); conn.close()
    return render_template("index.html", data={}, error=None, notices=items, show_staff_dashboard=True)

# NEW: Staff → Upload Results
@app.route("/staff/results", methods=["GET", "POST"])
@login_required
def staff_results():
    if request.method == "POST":
        name = (request.form.get("r_name") or "").strip()
        emp = (request.form.get("r_emp") or "").strip()
        desig = (request.form.get("r_desig") or "").strip()
        dob = (request.form.get("r_dob") or "").strip()
        roll = (request.form.get("r_roll") or "").strip()
        category = (request.form.get("r_cat") or "").strip()
        mobile = (request.form.get("r_mob") or "").strip()
        

        p = {}
        for i in range(1,6):
            p[f"p{i}_marks"] = (request.form.get(f"p{i}_marks") or "").strip()
            p[f"p{i}_result"] = (request.form.get(f"p{i}_result") or "").strip() or None

        if not all([name, emp, desig, dob]):
            flash("Please fill required fields: Name, Emp Code, Designation, Date of Birth", "error")
            return redirect(url_for("staff_results"))
        
        def _norm_result(v: str):
            """Map form values to valid ENUM or None."""
            v = (v or "").strip().lower()
            if v in ("pass", "p"):
                return "Pass"
            if v in ("fail", "f"):
                return "Fail"
            return None

        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO exam_results (
                name, emp_code, designation, dob, roll_no, category, mobile, p1_marks, p1_result, p2_marks, p2_result, p3_marks, p3_result, p4_marks, p4_result, p5_marks, p5_result
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                name=VALUES(name), designation=VALUES(designation), dob=VALUES(dob), roll_no=VALUES(roll_no),
                category=VALUES(category),  
                p1_marks=VALUES(p1_marks), p1_result=VALUES(p1_result),
                p2_marks=VALUES(p2_marks), p2_result=VALUES(p2_result),
                p3_marks=VALUES(p3_marks), p3_result=VALUES(p3_result),
                p4_marks=VALUES(p4_marks), p4_result=VALUES(p4_result),
                p5_marks=VALUES(p5_marks), p5_result=VALUES(p5_result)
        """, (
            name, emp, desig, dob, roll, category, mobile,
            p["p1_marks"], _norm_result(p["p1_result"]), p["p2_marks"], _norm_result(p["p2_result"]),
            p["p3_marks"], _norm_result(p["p3_result"]), p["p4_marks"], _norm_result(p["p4_result"]),
            p["p5_marks"], _norm_result(p["p5_result"])
        ))
        conn.commit(); cur.close(); conn.close()
        flash("Result uploaded/updated successfully.", "success")
        return redirect(url_for("staff_results"))

    # GET
    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, name, emp_code, designation, created_at FROM exam_results ORDER BY created_at DESC LIMIT 50")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("index.html", data={}, error=None, notices=[], show_results_uploader=True, results_rows=rows)

# NEW: Staff → Release Results (creates a front-page notification)
@app.route("/staff/release-results", methods=["POST"])
@login_required
def staff_release_results():
    year = datetime.now().year
    title = f"Results for Department of Ministerial Examinations – {year} –"
    url = f"{SITE_BASE_URL}/results"
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO notifications (title, url) VALUES (%s,%s)", (title, url))
    conn.commit(); cur.close(); conn.close()
    flash("Results notification released on the front page.", "success")
    return redirect(url_for("staff_notifications"))

# ---------------------- Public: Results Login & View ----------------------
@app.route("/results-auth", methods=["POST"])
def results_auth():
    emp = (request.form.get("res_emp") or "").strip()
    email = (request.form.get("res_email") or "").strip()
    password = (request.form.get("res_email") or "").strip()

    if not emp or not empcode_exists(emp):
        return render_template("index.html", data={}, invalid_empcode_results=True, show_results_login=True)

    if not all([ emp, email, password]):
        err = "Please provide Employee Code, E-mail, and Password."
        return render_template("index.html", data={}, error=err, show_results_login=True)

    conn = get_conn(); cur = conn.cursor(dictionary=True)

    # 1) Validate credentials against employee_accounts
    cur.execute(
        "SELECT * FROM employee_accounts WHERE emp_code=%s AND email=%s LIMIT 1",
        (emp_code, email)
    )
    acct = cur.fetchone()
    if not acct or not check_password_hash(acct["password_hash"], password):
        cur.close(); conn.close()
        # keep the same error plumbing your template expects
        return render_template("index.html", data={}, error="Invalid credentials.", invalid_empcode_results=(acct is None)), 401

    conn = get_conn(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM exam_results WHERE emp_code=%s AND mobile=%s LIMIT 1",(emp, mobile))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        err = "Invalid credentials for results login."
        return render_template("index.html", data={}, error=err, show_results_login=True)

    cur.close(); conn.close()

    # Shape a results payload (similar to admit card, but with marks & pass/fail)
    res = {
        "name": row["name"],
        "designation": row["designation"],
        "empCode": row["emp_code"],
        "rollNo": row.get("roll_no") or "-",
        "category": row.get("category") or "-",
        "mobile": row.get("mobile") or "-",
        "p1_marks": row.get("p1_marks") or "-",
        "p1_result": row.get("p1_result") or "-",
        "p2_marks": row.get("p2_marks") or "-",
        "p2_result": row.get("p2_result") or "-",
        "p3_marks": row.get("p3_marks") or "-",
        "p3_result": row.get("p3_result") or "-",
        "p4_marks": row.get("p4_marks") or "-",
        "p4_result": row.get("p4_result") or "-",
        "p5_marks": row.get("p5_marks") or "-",
        "p5_result": row.get("p5_result") or "-",
        "examDate": datetime.now().strftime("%d-%m-%Y"),
    }
    # Re-render with the results card visible
    return render_template("index.html", data={}, error=None, results_view=res, show_results_card=True)

# ---------------------- Static uploads ----------------------
@app.route("/uploads/<path:filename>")
def get_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)
