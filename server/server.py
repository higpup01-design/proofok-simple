# -*- coding: utf-8 -*-
import os, uuid, json, datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from flask import Flask, request, send_from_directory, render_template, abort, jsonify, url_for
import requests

VERSION = "proofok-simple-sendgrid-v1"

# Email transport: SendGrid via HTTPS (works on free hosts)
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "no-reply@example.com")
TO_EMAIL   = os.getenv("TO_EMAIL", "orders@example.com")
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "12"))

# Optional override; otherwise we build from request host
BASE_URL_OVERRIDE = os.getenv("BASE_URL", "").rstrip("/")

app = Flask(__name__)
BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DATA_DIR   = os.path.join(BASE_DIR, "data")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR,   exist_ok=True)

executor = ThreadPoolExecutor(max_workers=2)

def base_url():
    if BASE_URL_OVERRIDE:
        return BASE_URL_OVERRIDE
    return (request.host_url or "http://127.0.0.1:5000/").rstrip("/")

def record_path(token): return os.path.join(DATA_DIR, f"{token}.json")
def save_record(token, d): open(record_path(token), "w", encoding="utf-8").write(json.dumps(d, indent=2))
def load_record(token):
    p = record_path(token)
    if not os.path.exists(p): return None
    return json.load(open(p, "r", encoding="utf-8"))

def send_via_sendgrid(subject, html, text):
    if not SENDGRID_API_KEY:
        raise RuntimeError("Missing SENDGRID_API_KEY")
    payload = {
        "from": {"email": FROM_EMAIL},
        "personalizations": [{"to": [{"email": TO_EMAIL}]}],
        "subject": subject,
        "content": [{"type": "text/plain", "value": text},
                    {"type": "text/html",  "value": html}],
        "reply_to": {"email": TO_EMAIL}
    }
    r = requests.post("https://api.sendgrid.com/v3/mail/send",
                      headers={"Authorization": "Bearer " + SENDGRID_API_KEY,
                               "Content-Type": "application/json"},
                      json=payload, timeout=SMTP_TIMEOUT)
    if r.status_code >= 300:
        raise RuntimeError(f"SendGrid error {r.status_code}: {r.text}")

def send_email(subject, html, text):
    send_via_sendgrid(subject, html, text)

@app.get("/")
def index():
    return (f"ProofOK is running ({VERSION}). "
            f"Try <a href='/healthz'>/healthz</a> or <a href='/upload'>/upload</a>.", 200)

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
        return render_template("uploaded.html", ok=False, message="Please choose a .pdf file.", version=VERSION)
    original_name = f.filename
    token = uuid.uuid4().hex[:12]
    token_dir = os.path.join(UPLOAD_DIR, token)
    os.makedirs(token_dir, exist_ok=True)
    safe_name = original_name.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(token_dir, safe_name)
    f.save(pdf_path)
    now = datetime.datetime.utcnow().isoformat() + "Z"
    rec = {"token": token, "original_name": original_name, "stored_name": safe_name,
           "created_utc": now, "status": "pending", "responses": []}
    save_record(token, rec)
    proof_link = f"{base_url()}/proof/{token}"
    return render_template("uploaded.html", ok=True, url=proof_link, token=token,
                           original_name=original_name, version=VERSION)

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
    rec = {"token": token, "original_name": original_name, "stored_name": safe_name,
           "created_utc": now, "status": "pending", "responses": []}
    save_record(token, rec)
    return jsonify({"ok": True, "token": token, "url": f"{base_url()}/proof/{token}"})

@app.get("/proof/<token>")
def proof_page(token):
    rec = load_record(token)
    if not rec: abort(404)
    return render_template("proof.html",
                           token=token,
                           original_name=rec["original_name"],
                           pdf_url=url_for("serve_pdf", token=token, filename=rec["stored_name"]),
                           base_url=base_url(),
                           version=VERSION)

@app.get("/p/<token>/<path:filename>")
def serve_pdf(token, filename):
    folder = os.path.join(UPLOAD_DIR, token)
    if not os.path.isdir(folder): abort(404)
    return send_from_directory(folder, filename, mimetype="application/pdf", as_attachment=False)

def email_body(rec, decision, event):
    proof_url = f"{base_url()}/proof/{rec['token']}"
    subject = "[Proof] {} -- {}".format(rec["original_name"], decision.upper())
    text = ("Proof decision received.\n\nFile: {}\nLink: {}\nDecision: {}\n"
            "Name: {}\nEmail: {}\nComment:\n{}\n\nTime (UTC): {}\nIP: {}\n").format(
            rec["original_name"], proof_url, decision, event.get("viewer_name",""),
            event.get("viewer_email",""), event.get("comment",""),
            event["ts_utc"], event.get("ip",""))
    html = ("<h2>Proof decision received</h2>"
            "<p><b>File:</b> {}</p>"
            "<p><b>Link:</b> <a href='{}'>{}</a></p>"
            "<p><b>Decision:</b> {}</p>"
            "<p><b>Name:</b> {} &lt;{}&gt;</p>"
            "<p><b>Comment:</b><br>{}</p>"
            "<p><small>Time (UTC): {} | IP: {}</small></p>").format(
            rec["original_name"], proof_url, proof_url, decision,
            event.get("viewer_name",""), event.get("viewer_email",""),
            (event.get("comment","") or "").replace("\n","<br>"),
            event["ts_utc"], event.get("ip",""))
    return subject, html, text

@app.post("/respond/<token>")
def respond_form(token):
    rec = load_record(token)
    if not rec:
        return render_template("result.html", ok=False, message="This proof link was not found.",
                               version=VERSION, token=token, original_name="")
    decision = (request.form.get("decision") or "").lower()
    comment  = (request.form.get("comment")  or "").strip()
    viewer_name  = (request.form.get("viewer_name")  or "").strip()
    viewer_email = (request.form.get("viewer_email") or "").strip()
    if decision not in ("approved","rejected"):
        return render_template("result.html", ok=False, message="Invalid decision.", version=VERSION,
                               token=token, original_name=rec["original_name"])
    if decision == "rejected" and not comment:
        return render_template("result.html", ok=False, message="Please add a comment when rejecting.",
                               version=VERSION, token=token, original_name=rec["original_name"])
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    event = {"ts_utc": datetime.datetime.utcnow().isoformat()+"Z", "decision": decision,
             "comment": comment, "viewer_name": viewer_name, "viewer_email": viewer_email, "ip": ip}
    rec["status"] = decision
    rec.setdefault("responses", []).append(event)
    save_record(token, rec)
    warning = ""
    subj, html, text = email_body(rec, decision, event)
    mode = os.getenv("EMAIL_MODE", "async").lower()
    if mode == "off":
        pass
    elif mode == "sync":
        try: send_email(subj, html, text)
        except Exception as e: warning = f"Email send failed: {e}"
    else:
        try:
            fut = executor.submit(send_email, subj, html, text)
            fut.result(timeout=SMTP_TIMEOUT)
        except FuturesTimeout:
            warning = f"Email is sending in background (timeout {SMTP_TIMEOUT}s)."
        except Exception as e:
            warning = f"Email send failed: {e}"
    return render_template("result.html", ok=True, message="Thank you. Your decision was recorded.",
                           warning=warning, token=token, original_name=rec["original_name"],
                           version=VERSION)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
