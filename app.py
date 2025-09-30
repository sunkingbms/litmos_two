# app.py - cleaned, hardened, non-blocking CSV processing with background worker
from dotenv import load_dotenv
load_dotenv()

import os
import io
import csv
import json
import uuid
import time
import logging
import traceback
import xml.etree.ElementTree as ET
from typing import Any, Tuple, Dict, List, Optional
from threading import Thread, Semaphore
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry as Urllib3Retry

from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_cors import CORS
from werkzeug.utils import secure_filename

# Optional Google libs (ok if missing)
try:
    from google.oauth2 import id_token
    from google.auth.transport import requests as grequests
    from google.cloud import storage
except Exception:
    id_token = None
    grequests = None
    storage = None

# --- Logging & dirs ---
LOG_DIR = os.getenv("LOG_DIR", "/tmp/logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("lumt")

# --- App bootstrap ---
SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    logger.error("SESSION_SECRET is required")
    raise RuntimeError("SESSION_SECRET environment variable must be set")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = SESSION_SECRET
CORS(app)

# --- Configuration ---
BASE_URL = os.getenv("LITMOS_BASE_URL", "https://api.litmos.com/v1.svc")
API_KEY = os.getenv("LITMOS_API_KEY", "")
SOURCE = os.getenv("LITMOS_SOURCE", "sourceapp")

MAX_RECORDS = int(os.getenv("MAX_RECORDS", "100"))
BG_MAX_WORKERS = int(os.getenv("BG_MAX_WORKERS", "2"))
BG_MAX_INFLIGHT = int(os.getenv("BG_MAX_INFLIGHT", "4"))
OUTBOUND_TIMEOUT = int(os.getenv("OUTBOUND_TIMEOUT", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
CONNECTION_POOL_SIZE = int(os.getenv("CONNECTION_POOL_SIZE", "10"))
MAX_POOL_SIZE = int(os.getenv("MAX_POOL_SIZE", "20"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_FACTOR = float(os.getenv("BACKOFF_FACTOR", "0.5"))
USER_OP_DELAY = float(os.getenv("USER_OP_DELAY", "0.02"))  # tiny sleep only in background

GCS_BUCKET = os.getenv("GCS_BUCKET")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "sunking.com")

# --- Thread pool / job store ---
WORKER_POOL = ThreadPoolExecutor(max_workers=BG_MAX_WORKERS)
SEMAPHORE = Semaphore(BG_MAX_INFLIGHT)
jobs: Dict[str, Dict] = {}

# --- HTTP session with retries (shared) ---
def create_http_session() -> requests.Session:
    session = requests.Session()
    retry = Urllib3Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
    )
    adapter = HTTPAdapter(pool_connections=CONNECTION_POOL_SIZE, pool_maxsize=MAX_POOL_SIZE, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session._default_timeout = OUTBOUND_TIMEOUT
    return session

HTTP_SESSION = create_http_session()

def request_with_retries(method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """Perform HTTP request using shared session and sensible timeouts."""
    if "timeout" not in kwargs:
        kwargs["timeout"] = OUTBOUND_TIMEOUT
    try:
        resp = HTTP_SESSION.request(method, url, **kwargs)
        ct = resp.headers.get("Content-Type", "")
        if "html" in ct.lower() or (not resp.ok and not ("json" in ct.lower() or "xml" in ct.lower())):
            logger.warning("Unexpected response from %s (status=%s ct=%s)", url, resp.status_code, ct)
            dump_debug({
                "when": __import__("datetime").datetime.utcnow().isoformat(),
                "url": url,
                "status": resp.status_code,
                "content_type": ct,
                "body_start": (resp.text or "")[:2000]
            })
        return resp
    except Exception as e:
        logger.exception("HTTP request error %s %s", method, url)
        dump_debug({
            "when": __import__("datetime").datetime.utcnow().isoformat(),
            "url": url,
            "error": str(e),
        })
        return None

def get_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "Accept": "application/json", "apikey": API_KEY}

def dump_debug(obj: Dict):
    try:
        with open(os.path.join(LOG_DIR, "debug.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, default=str) + "\n")
    except Exception:
        logger.exception("Failed to write debug dump")

def parse_response(resp: Optional[requests.Response]) -> Tuple[bool, Any]:
    """Parse response: prefer JSON, then XML, else raw text. Handle HTML errors gracefully."""
    if resp is None:
        return False, "no-response"
    status = resp.status_code
    ct = resp.headers.get("Content-Type", "")
    text = resp.text or ""
    
    # Handle success status codes
    if status in (204,):
        return True, None
    
    # Handle server errors
    if status >= 500:
        logger.warning("Server error (status=%s): %s", status, text[:500])
        return False, f"Server error ({status})"
    
    # Check for HTML responses (common error case)
    if "html" in ct.lower() or text.strip().startswith(("<!DOCTYPE", "<html", "<HTML")):
        logger.warning("Received HTML response instead of JSON/XML (status=%s)", status)
        dump_debug({
            "when": __import__("datetime").datetime.utcnow().isoformat(),
            "error": "html_response_instead_of_json",
            "status": status,
            "content_type": ct,
            "body_preview": text[:1000]
        })
        return False, f"API returned HTML error page (status {status})"
    
    # Try JSON parsing
    if "json" in ct.lower() or text.strip().startswith(("{", "[")):
        try:
            return True, resp.json()
        except ValueError as e:
            logger.warning("JSON parse error: %s. Content-Type=%s, Body preview=%s", str(e), ct, text[:200])
            dump_debug({
                "when": __import__("datetime").datetime.utcnow().isoformat(),
                "error": "json_parse_failed",
                "exception": str(e),
                "content_type": ct,
                "body_preview": text[:500]
            })
            return False, f"Invalid JSON response: {str(e)}"
    
    # Try XML parsing
    if "xml" in ct.lower() or text.strip().startswith("<"):
        try:
            root = ET.fromstring(text)
            def xml_to_obj(e):
                children = list(e)
                if not children:
                    return e.text or ""
                d = {}
                for c in children:
                    d[c.tag] = xml_to_obj(c)
                return d
            return True, xml_to_obj(root)
        except Exception as e:
            logger.warning("XML parse error: %s", str(e))
            return False, f"Invalid XML response: {str(e)}"
    
    # Default case: return raw text
    return False, text[:1000] if len(text) > 1000 else text

# --- GCS helper (optional) ---
def upload_file_to_gcs(file_obj, dest_path: str) -> Optional[str]:
    if not GCS_BUCKET:
        logger.warning("GCS_BUCKET not configured, skipping upload")
        return None
    if storage is None:
        raise RuntimeError("google.cloud.storage not available")
    try:
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(dest_path)
        try:
            file_obj.stream.seek(0)
        except Exception:
            pass
        blob.upload_from_file(file_obj.stream, content_type=file_obj.mimetype)
        return f"gs://{GCS_BUCKET}/{dest_path}"
    except Exception as e:
        logger.exception("Failed to upload to GCS")
        raise

# --- Per-row background operations ---
def deactivate_user_task(row: Dict[str, str], litmos_url: str, litmos_token: str) -> Tuple[bool, Any]:
    identifier = (row.get("username") or row.get("email") or row.get("Email") or
                  row.get("UserId") or row.get("user_id") or "").strip()
    if not identifier:
        return False, "missing identifier"
    try:
        headers = {"Authorization": f"Bearer {litmos_token}", "Content-Type": "application/json", "Accept": "application/json"}
        payload = {"user": {"email": identifier}, "action": "deactivate"}
        resp = request_with_retries("POST", litmos_url, headers=headers, json=payload, timeout=OUTBOUND_TIMEOUT)
        if resp is None:
            return False, "no-response"
        if 200 <= resp.status_code < 300:
            return True, {"status_code": resp.status_code}
        return False, f"{resp.status_code}:{(resp.text or '')[:1000]}"
    except Exception as e:
        logger.exception("deactivate_user_task exception")
        return False, str(e)

def activate_user_task(row: Dict[str, str], litmos_url: str, litmos_token: str) -> Tuple[bool, Any]:
    identifier = (row.get("username") or row.get("email") or row.get("Email") or
                  row.get("UserId") or row.get("user_id") or "").strip()
    if not identifier:
        return False, "missing identifier"
    try:
        headers = {"Authorization": f"Bearer {litmos_token}", "Content-Type": "application/json", "Accept": "application/json"}
        payload = {"user": {"email": identifier}, "action": "activate"}
        resp = request_with_retries("POST", litmos_url, headers=headers, json=payload, timeout=OUTBOUND_TIMEOUT)
        if resp is None:
            return False, "no-response"
        if 200 <= resp.status_code < 300:
            return True, {"status_code": resp.status_code}
        return False, f"{resp.status_code}:{(resp.text or '')[:1000]}"
    except Exception as e:
        logger.exception("activate_user_task exception")
        return False, str(e)

# --- High-level Litmos helpers (safe to call from background or UI) ---
def find_user_by_username(username: str) -> Optional[Dict]:
    url = f"{BASE_URL}/users?source={SOURCE}&search={username}&format=json"
    resp = request_with_retries("GET", url, headers=get_headers())
    ok, data = parse_response(resp)
    if not ok:
        logger.debug("find_user_by_username non-ok for %s", username)
        return None
    users = []
    if isinstance(data, dict):
        if "User" in data:
            users = data["User"] if isinstance(data["User"], list) else [data["User"]]
        elif "Users" in data and isinstance(data["Users"], dict) and "User" in data["Users"]:
            u = data["Users"]["User"]
            users = u if isinstance(u, list) else [u]
        else:
            users = [data]
    elif isinstance(data, list):
        users = data
    for u in users:
        if u.get("UserName", "").lower() == username.lower():
            return u
    return None

def get_user_details(user_id: str) -> Optional[Dict]:
    url = f"{BASE_URL}/users/{user_id}?source={SOURCE}&format=json"
    resp = request_with_retries("GET", url, headers=get_headers())
    ok, details = parse_response(resp)
    if not ok:
        logger.debug("get_user_details non-ok for %s", user_id)
        return None
    if isinstance(details, dict) and "User" in details:
        return details["User"]
    return details

def activate_user(username: str) -> Dict:
    try:
        logger.info("Activating user %s", username)
        user = find_user_by_username(username)
        if not user:
            return {"username": username, "success": False, "message": "User not found"}
        user_id = user.get("Id")
        if not user_id:
            return {"username": username, "success": False, "message": "User ID not found"}
        if user.get("Active") is True:
            return {"username": username, "success": True, "message": "Already active"}
        url = f"{BASE_URL}/users/{user_id}?source={SOURCE}&format=json"
        user_data = user.copy()
        user_data["Active"] = True
        resp = request_with_retries("PUT", url, headers=get_headers(), json=user_data)
        if resp and resp.status_code in (200, 204):
            return {"username": username, "success": True, "message": "User activated successfully"}
        return {"username": username, "success": False, "message": f"Activation failed: {resp.status_code if resp else 'no response'}"}
    except Exception as e:
        logger.exception("Exception activating user %s", username)
        return {"username": username, "success": False, "message": str(e)}

def deactivate_user(username: str) -> Dict:
    try:
        logger.info("Deactivating user %s", username)
        user = find_user_by_username(username)
        if not user:
            return {"username": username, "success": False, "message": "User not found"}
        user_id = user.get("Id")
        if not user_id:
            return {"username": username, "success": False, "message": "User ID not found"}
        if user.get("Active") is False:
            return {"username": username, "success": True, "message": "Already inactive"}
        url = f"{BASE_URL}/users/{user_id}?source={SOURCE}&format=json"
        user_data = user.copy()
        user_data["Active"] = False
        resp = request_with_retries("PUT", url, headers=get_headers(), json=user_data)
        if resp and resp.status_code in (200, 204):
            return {"username": username, "success": True, "message": "User deactivated successfully"}
        return {"username": username, "success": False, "message": f"Deactivation failed: {resp.status_code if resp else 'no response'}"}
    except Exception as e:
        logger.exception("Exception deactivating user %s", username)
        return {"username": username, "success": False, "message": str(e)}

# --- Background CSV processing (reads stable temp file) ---
def _process_streamed_csv_background(job_id: str, file_path: str, operation_type: str, litmos_url: str, litmos_token: str):
    logger.info("Job %s: background worker starting (file=%s)", job_id, file_path)
    jobs[job_id] = {"status": "running", "total": 0, "done": 0, "errors": []}
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for idx, row in enumerate(reader):
                if MAX_RECORDS > 0 and idx >= MAX_RECORDS:
                    jobs[job_id]["errors"].append({"row_index": idx, "error": f"skipped due to MAX_RECORDS ({MAX_RECORDS})"})
                    break
                jobs[job_id]["total"] += 1
                SEMAPHORE.acquire()
                def _run(r=row, i=idx):
                    try:
                        if operation_type == "activation":
                            ok, info = activate_user_task(r, litmos_url, litmos_token)
                        else:
                            ok, info = deactivate_user_task(r, litmos_url, litmos_token)
                        if not ok:
                            jobs[job_id]["errors"].append({"row_index": i, "error": info, "row": r})
                    except Exception as exc:
                        logger.exception("Unhandled exception in per-row worker")
                        jobs[job_id]["errors"].append({"row_index": i, "error": str(exc), "row": r})
                    finally:
                        jobs[job_id]["done"] += 1
                        SEMAPHORE.release()
                WORKER_POOL.submit(_run)
                time.sleep(USER_OP_DELAY)
        # wait until all done
        while jobs[job_id]["done"] < jobs[job_id]["total"]:
            time.sleep(0.5)
        jobs[job_id]["status"] = "completed" if not jobs[job_id]["errors"] else "completed_with_errors"
        logger.info("Job %s completed: done=%s errors=%s", job_id, jobs[job_id]["done"], len(jobs[job_id]["errors"]))
    except Exception as e:
        logger.exception("Background job %s failed while reading file: %s", job_id, e)
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["errors"].append({"error": "background failure", "detail": str(e)})
    finally:
        # cleanup temp file
        try:
            os.remove(file_path)
            logger.debug("Removed temp file %s", file_path)
        except Exception:
            logger.debug("Could not remove temp file %s", file_path, exc_info=True)

# --- Routes ---
@app.route("/api/process-csv", methods=["POST"])
def process_csv():
    """
    Save uploaded CSV to /tmp synchronously (fast), return 202 immediately,
    and spawn background worker that reads the temp file.
    """
    try:
        operation_type = (request.form.get("operation_type") or "deactivation").lower()
        file_key = "csv_file" if "csv_file" in request.files else "file"
        if file_key not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        upload = request.files[file_key]
        if not upload or upload.filename == "":
            return jsonify({"error": "No file uploaded or filename empty"}), 400

        # Save to stable temp file in /tmp (synchronous)
        tmpdir = "/tmp"
        os.makedirs(tmpdir, exist_ok=True)
        tmp_name = f"{uuid.uuid4().hex}-{secure_filename(upload.filename)}"
        tmp_path = os.path.join(tmpdir, tmp_name)
        try:
            upload.save(tmp_path)
        except Exception:
            # fallback copy
            try:
                upload.stream.seek(0)
            except Exception:
                pass
            with open(tmp_path, "wb") as of:
                while True:
                    chunk = upload.stream.read(64 * 1024)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    of.write(chunk)

        # CSV validation: count actual data rows (excluding header)
        MIN_RECORDS = int(os.getenv("MIN_RECORDS", "30"))
        try:
            with open(tmp_path, "r", encoding="utf-8", errors="replace") as fh:
                reader = csv.reader(fh)
                headers = next(reader, None)
                if not headers:
                    os.remove(tmp_path)
                    return jsonify({"error": "CSV file is empty or invalid"}), 400
                
                # Count data rows (excluding header)
                cnt = 0
                for row in reader:
                    if any(cell.strip() for cell in row):
                        cnt += 1
                    if cnt > MAX_RECORDS:
                        break
                
                # Validate minimum and maximum records
                if cnt < MIN_RECORDS:
                    os.remove(tmp_path)
                    return jsonify({"error": f"CSV must contain at least {MIN_RECORDS} users. Your file has {cnt} user(s)."}), 400
                
                if cnt > MAX_RECORDS:
                    os.remove(tmp_path)
                    return jsonify({"error": f"CSV must contain at most {MAX_RECORDS} users. Your file has {cnt} user(s)."}), 400
        except Exception as e:
            logger.exception("Error validating CSV file")
            try:
                os.remove(tmp_path)
            except:
                pass
            return jsonify({"error": f"Error reading CSV file: {str(e)}"}), 400

        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "queued", "total": 0, "done": 0, "errors": []}

        litmos_url = os.getenv("LITMOS_API_DEACT_URL") or os.getenv("LITMOS_API_URL") or f"{BASE_URL}/users"
        litmos_token = os.getenv("LITMOS_API_TOKEN")
        if not litmos_token:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            jobs.pop(job_id, None)
            return jsonify({"error": "server misconfiguration: missing LITMOS_API_TOKEN"}), 500

        bg_thread = Thread(target=_process_streamed_csv_background, args=(job_id, tmp_path, operation_type, litmos_url, litmos_token), daemon=True)
        bg_thread.start()

        return jsonify({"job_id": job_id, "status": "accepted"}), 202
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("process_csv top-level exception")
        dump_debug({"when": __import__("datetime").datetime.utcnow().isoformat(), "error": str(e), "trace": tb})
        if os.getenv("DEV_SHOW_TRACEBACK", "false").lower() in ("1", "true", "yes"):
            return jsonify({"error": "internal", "message": str(e), "trace": tb}), 500
        return jsonify({"error": "internal", "message": "internal server error"}), 500

@app.route("/api/job-status/<job_id>", methods=["GET"])
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job), 200

@app.route("/api/upload-csv", methods=["POST"])
def upload_csv():
    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400
    f = request.files["file"]
    unique_name = f"uploads/{uuid.uuid4().hex}-{secure_filename(f.filename)}"
    try:
        uri = upload_file_to_gcs(f, unique_name)
        return jsonify({"status": "queued", "gcs_uri": uri, "csv_object": unique_name}), 200
    except Exception as e:
        logger.exception("upload_csv failed")
        return jsonify({"error": str(e)}), 500

# minimal auth/login endpoints (optional)
@app.route("/login", methods=["GET"])
def login():
    try:
        return render_template("login.html", google_client_id=GOOGLE_CLIENT_ID)
    except Exception:
        return jsonify({"message": "login page"}), 200

@app.route("/login/callback", methods=["POST"])
def login_callback():
    if id_token is None or grequests is None or GOOGLE_CLIENT_ID is None:
        return jsonify({"success": False, "message": "OAuth not configured"}), 400
    try:
        data = request.get_json()
        token = data.get("credential")
        idinfo = id_token.verify_oauth2_token(token, grequests.Request(), GOOGLE_CLIENT_ID)
        email = idinfo.get("email")
        if not email.endswith(f"@{ALLOWED_DOMAIN}"):
            return jsonify({"success": False, "message": f"Access restricted to @{ALLOWED_DOMAIN} accounts."}), 403
        session["user"] = {"email": email, "name": idinfo.get("name")}
        return jsonify({"success": True})
    except Exception:
        logger.exception("login_callback failed")
        return jsonify({"success": False, "message": "Invalid login"}), 400

@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200

@app.route("/", methods=["GET"])
def index():
    user = session.get("user")
    if not user and GOOGLE_CLIENT_ID:
        return redirect(url_for("login"))
    try:
        return render_template("index.html", user=user or {"name": "User", "email": ""})
    except Exception:
        return jsonify({"message": "Litmos User Management Tool API"}), 200

@app.route("/activation", methods=["GET"])
def activation_page():
    user = session.get("user")
    if not user and GOOGLE_CLIENT_ID:
        return redirect(url_for("login"))
    try:
        return render_template("activation.html", user=user or {"name": "User", "email": ""})
    except Exception:
        return jsonify({"message": "Activation page"}), 200

@app.route("/deactivation", methods=["GET"])
def deactivation_page():
    user = session.get("user")
    if not user and GOOGLE_CLIENT_ID:
        return redirect(url_for("login"))
    try:
        return render_template("deactivation.html", user=user or {"name": "User", "email": ""})
    except Exception:
        return jsonify({"message": "Deactivation page"}), 200

@app.route("/results", methods=["GET"])
def results_page():
    user = session.get("user")
    if not user and GOOGLE_CLIENT_ID:
        return redirect(url_for("login"))
    try:
        return render_template("results.html", user=user or {"name": "User", "email": ""})
    except Exception:
        return jsonify({"message": "Results page"}), 200

# Global JSON-only error handler
@app.errorhandler(Exception)
def handle_uncaught_exception(e):
    tb = traceback.format_exc()
    logger.exception("Unhandled exception: %s", e)
    dump_debug({"when": __import__("datetime").datetime.utcnow().isoformat(), "error": str(e), "trace": tb})
    if os.getenv("DEV_SHOW_TRACEBACK", "false").lower() in ("1", "true", "yes"):
        return jsonify({"error": "internal", "message": str(e), "trace": tb}), 500
    return jsonify({"error": "internal", "message": "internal server error"}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_ENV", "") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
