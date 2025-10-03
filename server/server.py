# -*- coding: utf-8 -*-
import os
import uuid
import json
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from flask import Flask, request, send_from_directory, render_template, abort, jsonify, url_for

# SendGrid official SDK
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, Content

VERSION = "proofok-simple-sendgrid-v2.1-debug"

# --- Email (SendGrid over HTTPS) ---
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "no-reply@example.com")
TO_EMAIL   = os.getenv("TO_EMAIL", "orders@example.com")
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "12"))  # used as our async wait cap
EMAIL_MODE = os.getenv("EMAIL_MODE", "async").lower()  # async | sync | off

# Optional override for building absolute links; else we infer from request
BASE_URL_OVERRIDE = os.getenv("BASE_URL", "").rstrip("/")

# --- App setup & storage ---
app = Flask(__name__)
BASE_DIR  = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DATA_DIR   = os.path.join(BASE_DIR, "data")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR,   exist_ok=True)

# Logging (to Render logs)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = app.logger

executor = ThreadPoolExecutor(max_workers=2)

# ----------------- Helpers -----------------

def base_url():
    """Return the external base URL (env override or request host)."""
    if BASE_URL_OVERRIDE:
        return BASE_URL_OVERRIDE
    return (request.host_url or "http://127.0.0.1:5000/").rstrip("/")

def record_path(token):
    return os.path.join(DATA_DIR, f"{token}.json")

def save_record(token, data):
    with open(record_path(token), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_record(token):
    p = record_path(token)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def send_via_sendgrid(subject, html, text):
    """Send an email using SendGrid SDK over HTTPS."""
    if not SENDGRID_API_KEY:
        raise RuntimeError("Missing SENDGRID_API_KEY")

    # Debug pre-send
    log.info("EMAIL: preparing SendGrid message | from=%s to=%s subject=%s",
             FROM_EMAIL, TO_EMAIL, subject)

    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=TO_EMAIL,
        subject=subject,
        html_content=html
    )
    # Add a text/plain alternative part
    message.add_content(Content("text/plain", text))
    # Ensure replies go to your orders inbox
    message.reply_to = Email(TO_EMAIL)

    sg = SendGridAPIClient(SENDGRID_API_KEY)
    resp = sg.send(message)

    # Debug post-send
    code = getattr(resp, "status_code", None)
    # resp.body may be bytes; decode safely
    try:
        body = getattr(resp, "body", b"")
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "ignore")
    except Exception:
        body = "<unreadable>"
    log.info("EMAIL: SendGrid response | status=%s body=%s", code, body)

    # SendGrid returns HTTP 202 on success
    if code is None or code >= 300:
        raise RuntimeError(f"SendGrid error {code}: {body}")

def send_email(subject, html, text):
    # Only SendGrid is used in this build
    send_via_sendgrid(subject, html, text)

def email_body(rec, decision, event):
    """Build subject/text/html bodies for the decision email."""
    proof_url = f"{base_url()}/proof/{rec['token']}"
    subject = "[Proof] {} -- {}".format(rec["original_name"], decision.upper())
    text = (
        "Proof decision received.\n\n"
        "File: {}\nLink: {}\nDecision: {}\nName: {}\nEmail: {}\nComment:\n{}\n\n"
        "Time (UTC): {}\nIP: {}\n"
    ).format(
        rec["original_name"],
        proof_url,
        decision,
        event.get("viewer_name", ""),
        event.get("viewer_email", ""),
        event.get("comment", ""),
        event["ts_utc"],
        event.get("ip", ""),
    )
    html = (
        "<h2>Proof decision received</h2>"
        "<p><b>File:</b> {}</p>"
        "<p><b>Link:</b> <a href='{}'>{}</a></p>"
        "<p><b>Decision:</b> {}</p>"
        "<p><b>Name:</b> {} &lt;{}&gt;</p>"
        "<p><b>Comment:</b><br>{}</p>"
        "<p><small>Time (UTC): {} | IP: {}</small></p>"
    ).format(
        rec["original_name"],
        proof_url,
        proof_url,
        decision,
        event.get("viewer_name", ""),
        event.get("viewer_email", ""),
        (event.get("comment", "") or "").replace("\n", "<br>"),
        event["ts_utc"],
        event.get("ip", ""),
    )
    return subject, html, text

# ----------------- Routes -----------------

@app.get("/")
def index():
    return (
        f"ProofOK is running ({VERSION}). "
        f"Try <a href='/healthz'>/healthz</a> or <a href='/upload'>/upload</a>.",
        200,
    )

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION, "time": datetime.datetime.utcnow().isoformat() + "Z"}

@app.get("/routes")
def routes():
    return {"routes": [str(r) for r in app.url_map.iter_rules()]}

@app.get("/upload")
def upload_form():
    return render_template("upload.html", version=VERSION)

@app.post("/upload")
def upload_post():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return render_template(
            "uploaded.html",
            ok=False,
            message="Please choose a .pdf file.",
            version=VERSION,
        )

    original_name = f.filename
    token = uuid.uuid4().hex[:12]
    token_dir = os.path.join(UPLOAD_DIR, token)
    os.makedirs(token_dir, exist_ok=True)

    safe_name = original_name.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(token_dir, safe_name)
    f.save(pdf_path)

    now = datetime.datetime.utcnow().isoformat() + "Z"
    rec = {
        "token": token,
        "original_name": original_name,
        "stored_name": safe_name,
        "created_utc": now,
        "status": "pending",
        "responses": [],
    }
    save_record(token, rec)

    proof_link = f"{base_url()}/proof/{token}"
    return render_template(
        "uploaded.html",
        ok=True,
        url=proof_link,
        token=token,
        original_name=original_name,
        version=VERSION,
    )

@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a .pdf file"}), 400

    original_name = request.form.get("original_name", f.filename)
    token = uuid.uuid4().hex[:12]
    token_dir = os.path.join(UPLOAD_DIR, token)
    os.makedirs(token_dir, exist_ok=True)

    safe_name = original_name.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(token_dir, safe_name)
    f.save(pdf_path)

    now = datetime.datetime.utcnow().isoformat() + "Z"
    rec = {
        "token": token,
        "original_name": original_name,
        "stored_name": safe_name,
        "created_utc": now,
        "status": "pending",
        "responses": [],
    }
    save_record(token, rec)

    return jsonify({"ok": True, "token": token, "url": f"{base_url()}/proof/{token}"})

@app.get("/proof/<token>")
def proof_page(token):
    rec = load_record(token)
    if not rec:
        abort(404)
    return render_template(
        "proof.html",
        token=token,
        original_name=rec["original_name"],
        pdf_url=url_for("serve_pdf", token=token, filename=rec["stored_name"]),
        base_url=base_url(),
        version=VERSION,
    )

@app.get("/p/<token>/<path:filename>")
def serve_pdf(token, filename):
    folder = os.path.join(UPLOAD_DIR, token)
    if not os.path.isdir(folder):
        abort(404)
    return send_from_directory(
        folder, filename, mimetype="application/pdf", as_attachment=False
    )

@app.post("/respond/<token>")
def respond_form(token):
    rec = load_record(token)
    if not rec:
        return render_template(
            "result.html",
            ok=False,
            message="This proof link was not found.",
            version=VERSION,
            token=token,
            original_name="",
            base_url=base_url(),
        )

    decision = (request.form.get("decision") or "").lower()
    comment = (request.form.get("comment") or "").strip()
    viewer_name = (request.form.get("viewer_name") or "").strip()
    viewer_email = (request.form.get("viewer_email") or "").strip()

    if decision not in ("approved", "rejected"):
        return render_template(
            "result.html",
            ok=False,
            message="Invalid decision.",
            version=VERSION,
            token=token,
            original_name=rec["original_name"],
            base_url=base_url(),
        )

    if decision == "rejected" and not comment:
        return render_template(
            "result.html",
            ok=False,
            message="Please add a comment when rejecting.",
            version=VERSION,
            token=token,
            original_name=rec["original_name"],
            base_url=base_url(),
        )

    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    event = {
        "ts_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "decision": decision,
        "comment": comment,
        "viewer_name": viewer_name,
        "viewer_email": viewer_email,
        "ip": ip,
    }

    rec["status"] = decision
    rec.setdefault("responses", []).append(event)
    save_record(token, rec)

    # DEBUG: log what we are about to send
    log.info("RESPOND: token=%s decision=%s viewer=%s <%s> ip=%s",
             token, decision, viewer_name, viewer_email, ip)

    warning = ""
    subj, html, text = email_body(rec, decision, event)

    if EMAIL_MODE == "off":
        log.info("EMAIL: mode=off (skipping send)")
    elif EMAIL_MODE == "sync":
        try:
            send_email(subj, html, text)
            log.info("EMAIL: sync send completed ok")
        except Exception as e:
            warning = f"Email send failed: {e}"
            log.error("EMAIL: sync send failed | %s", e, exc_info=True)
    else:
        try:
            fut = executor.submit(send_email, subj, html, text)
            fut.result(timeout=SMTP_TIMEOUT)
            log.info("EMAIL: async send completed ok (within timeout)")
        except FuturesTimeout:
            warning = f"Email is sending in background (timeout {SMTP_TIMEOUT}s)."
            log.warning("EMAIL: async send timed out after %ss", SMTP_TIMEOUT)
        except Exception as e:
            warning = f"Email send failed: {e}"
            log.error("EMAIL: async send failed | %s", e, exc_info=True)

    return render_template(
        "result.html",
        ok=True,
        message="Thank you. Your decision was recorded.",
        warning=warning,
        token=token,
        original_name=rec["original_name"],
        version=VERSION,
        base_url=base_url(),
    )

# -------------- Dev only --------------
if __name__ == "__main__":
    # Local testing: http://127.0.0.1:5000/upload
    app.run(host="0.0.0.0", port=5000, debug=True)
