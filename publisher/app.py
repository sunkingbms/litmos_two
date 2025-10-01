# publisher/app.py
import os
import csv
import uuid
import json
import logging
from io import StringIO
from flask import Flask, request, jsonify
from google.cloud import pubsub_v1

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("publisher")

app = Flask(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC", "litmos-deactivate")
MIN_RECORDS = int(os.getenv("MIN_RECORDS", "30"))
MAX_RECORDS = int(os.getenv("MAX_RECORDS", "100"))

if not PROJECT_ID:
    logger.error("GCP_PROJECT / GOOGLE_CLOUD_PROJECT env must be set")

publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, PUBSUB_TOPIC)

# force JSON on all unhandled exceptions
@app.errorhandler(Exception)
def handle_uncaught_exception(e):
    import traceback
    logger.exception("Unhandled exception")
    tb = traceback.format_exc()
    body = {"error": "internal", "message": "internal server error"}
    if os.getenv("DEV_SHOW_TRACEBACK","false").lower() in ("1","true","yes"):
        body["traceback"] = tb
    resp = make_response(jsonify(body), 500)
    resp.headers["Content-Type"] = "application/json"
    return resp

@app.route("/api/process-csv", methods=["POST"])
def process_csv():
    try:
        if "file" not in request.files:
            return jsonify({"error": "no file uploaded"}), 400
        f = request.files["file"]
        content = f.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(StringIO(content))
        rows = []
        for i, row in enumerate(reader, start=1):
            rows.append(row)
            if i > MAX_RECORDS:
                return jsonify({"error": f"Record limit exceeded. Max {MAX_RECORDS} allowed."}), 400

        if len(rows) < MIN_RECORDS:
            return jsonify({"error": f"Minimum {MIN_RECORDS} records required."}), 400

        # Normalize and publish minimal messages
        publish_futures = []
        for row in rows:
            msg = {
                "user": {
                    "user_id": row.get("user_id") or row.get("UserId") or "",
                    "username": row.get("username") or row.get("Username") or row.get("email") or row.get("Email") or ""
                },
                "action": request.form.get("operation_type", "deactivation")
            }
            data = json.dumps(msg).encode("utf-8")
            future = publisher.publish(topic_path, data=data)
            publish_futures.append(future)

        # Wait briefly for publish confirmation (short timeout)
        published = 0
        for fut in publish_futures:
            try:
                fut.result(timeout=int(os.getenv("PUBSUB_PUBLISH_WAIT", "10")))
                published += 1
            except Exception:
                logger.exception("publish failed for a message (continuing)")

        job_id = str(uuid.uuid4())
        return jsonify({"job_id": job_id, "published": published}), 202

    except Exception as e:
        logger.exception("publisher/process-csv failed")
        return jsonify({"error": "internal", "message": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8080)), host="0.0.0.0")
