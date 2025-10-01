# worker/worker.py
import os
import base64
import json
import logging
import traceback
from flask import Flask, request, jsonify

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry as Urllib3Retry

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("worker")

app = Flask(__name__)

LITMOS_API_TOKEN = os.getenv("LITMOS_API_TOKEN")
LITMOS_BASE_URL = os.getenv("LITMOS_BASE_URL", "https://api.litmos.com/v1.svc")
OUTBOUND_TIMEOUT = int(os.getenv("OUTBOUND_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_FACTOR = float(os.getenv("BACKOFF_FACTOR", "0.5"))

# shared requests session with retries
def create_session():
    s = requests.Session()
    retry = Urllib3Retry(total=MAX_RETRIES, backoff_factor=BACKOFF_FACTOR,
                         status_forcelist=(429,500,502,503,504),
                         allowed_methods=frozenset(["GET","POST","PUT","DELETE","HEAD","OPTIONS"]))
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

HTTP = create_session()

def deactivate_in_litmos(identifier: str):
    url = os.getenv("LITMOS_API_DEACT_URL") or f"{LITMOS_BASE_URL}/users"
    headers = {"Authorization": f"Bearer {LITMOS_API_TOKEN}", "Content-Type": "application/json", "Accept": "application/json"}
    payload = {"user": {"email": identifier}, "action": "deactivate"}
    resp = HTTP.request("POST", url, headers=headers, json=payload, timeout=OUTBOUND_TIMEOUT)
    return resp

@app.route("/pubsub/push", methods=["POST"])
def pubsub_push():
    try:
        envelope = request.get_json()
        if not envelope:
            logger.error("Invalid Pub/Sub message: no JSON")
            return ("", 400)
        msg = envelope.get("message")
        if not msg or "data" not in msg:
            logger.error("Invalid Pub/Sub envelope")
            return ("", 400)
        data = base64.b64decode(msg["data"]).decode("utf-8")
        payload = json.loads(data)
        user = payload.get("user", {})
        identifier = user.get("user_id") or user.get("username") or user.get("email")
        if not identifier:
            logger.error("No identifier in message; acking to avoid infinite retries")
            return ("", 200)

        resp = deactivate_in_litmos(identifier)
        if resp is None:
            logger.error("No response from Litmos for %s", identifier)
            return ("", 500)  # return 5xx to cause Pub/Sub retry on transient failure

        if 200 <= resp.status_code < 300:
            logger.info("Deactivated %s -> %s", identifier, resp.status_code)
            return ("", 200)
        else:
            logger.error("Litmos error for %s: %s %s", identifier, resp.status_code, resp.text[:1000])
            # Decide: ack (200) to avoid poison messages, or return 500 to retry
            # For now, return 500 to allow retries for transient issues
            return ("", 500)

    except Exception as e:
        logger.exception("pubsub_push handler error")
        return ("", 500)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "port": os.environ.get("PORT", "unset")})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
