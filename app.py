# app.py
import os
import time
import uuid
import requests
from io import BytesIO
from datetime import datetime
from PIL import Image
from flask import Flask, request, jsonify, send_file
import boto3
from botocore.exceptions import NoCredentialsError, ClientError

app = Flask(__name__, static_folder="static", template_folder="static")

# --- Config / Env ---
DO_INFERENCE_BASE = "https://inference.do-ai.run/v1/async-invoke"
DO_MODEL_ACCESS_KEY = os.getenv("DO_MODEL_ACCESS_KEY")  # Required
if not DO_MODEL_ACCESS_KEY:
    # don't raise here so app can still run in dev, but warn
    print("Warning: DO_MODEL_ACCESS_KEY not set. Serverless inference will fail until you set it.")

DO_INFERENCE_HEADERS = {
    "Authorization": f"Bearer {DO_MODEL_ACCESS_KEY}",
    "Content-Type": "application/json",
}

# DigitalOcean Spaces configuration (same as your old app)
SPACES_BUCKET = os.getenv("SPACES_BUCKET", "photosnap-bucket")
SPACES_REGION = os.getenv("SPACES_REGION", "sgp1")
SPACES_ENDPOINT = os.getenv("SPACES_ENDPOINT", f"https://{SPACES_REGION}.digitaloceanspaces.com")
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")

# Polling configuration for async jobs
DEFAULT_POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2.0"))   # seconds between polls
DEFAULT_POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "60"))       # total seconds to wait before timing out

# --- Utilities: Spaces client & upload ---
def configure_spaces_client():
    """Configure boto3 client for DigitalOcean Spaces"""
    try:
        session = boto3.session.Session()
        s3_client = session.client(
            's3',
            region_name=SPACES_REGION,
            endpoint_url=SPACES_ENDPOINT,
            aws_access_key_id=DO_SPACES_KEY,
            aws_secret_access_key=DO_SPACES_SECRET
        )
        return s3_client
    except Exception as e:
        app.logger.error(f"Failed to configure DigitalOcean Spaces client: {e}")
        return None

def upload_to_spaces(image_bytes: bytes, filename: str):
    """Upload image bytes to DigitalOcean Spaces and return public URL (or None)"""
    s3 = configure_spaces_client()
    if not s3:
        app.logger.error("Spaces client not configured")
        return None

    try:
        s3.put_object(
            Bucket=SPACES_BUCKET,
            Key=filename,
            Body=image_bytes,
            ContentType='image/png',
            ACL='public-read'
        )
        url = f"https://{SPACES_BUCKET}.{SPACES_REGION}.digitaloceanspaces.com/{filename}"
        return url
    except NoCredentialsError:
        app.logger.error("DigitalOcean Spaces credentials not found")
        return None
    except ClientError as e:
        app.logger.error(f"Failed to upload to DigitalOcean Spaces: {e}")
        return None
    except Exception as e:
        app.logger.error(f"Unexpected error during upload: {e}")
        return None

# --- Utilities: DigitalOcean serverless inference flow ---
def start_async_inference(model_id: str, input_payload: dict, tags: list = None):
    """
    Starts an async invoke job on DO serverless inference.
    Returns the parsed JSON response or raises an exception on HTTP error.
    """
    body = {"model_id": model_id, "input": input_payload}
    if tags:
        body["tags"] = tags

    # DO_INFERENCE_BASE already points to /v1/async-invoke, so POST there
    resp = requests.post(f"{DO_INFERENCE_BASE}", headers=DO_INFERENCE_HEADERS, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()

def get_job_status(request_id: str):
    """Check status of async job."""
    # Use the request id path under the base URL
    resp = requests.get(f"{DO_INFERENCE_BASE}/{request_id}/status", headers=DO_INFERENCE_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()

def get_job_result(request_id: str):
    """Fetch the final job result."""
    resp = requests.get(f"{DO_INFERENCE_BASE}/{request_id}", headers=DO_INFERENCE_HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.json()

def poll_until_complete(request_id: str, timeout_seconds: int = DEFAULT_POLL_TIMEOUT, poll_interval: float = DEFAULT_POLL_INTERVAL):
    """Poll the status endpoint until COMPLETE or timeout. Returns final status JSON once COMPLETE."""
    start = time.time()
    while True:
        status_json = get_job_status(request_id)
        status_val = (status_json.get("status") or status_json.get("state") or "").upper()
        app.logger.debug(f"Job {request_id} status: {status_val} / {status_json}")
        if status_val in ("COMPLETE", "SUCCEEDED", "SUCCESS"):
            # Return final result (not just status)
            return get_job_result(request_id)
        if status_val in ("FAILED", "ERROR"):
            raise RuntimeError(f"Inference job failed: {status_json}")
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Inference job polling timed out after {timeout_seconds}s")
        time.sleep(poll_interval)

# Helper: Download image bytes from URL or handle base64 inline
def extract_image_bytes_from_result(result_json):
    """
    Try to find image bytes in a variety of result shapes:
     - result_json['output'] may be a list of items with 'url' or 'base64' or 'image' keys
     - result_json may contain 'url' at top-level
     - If we find a URL, download it and return bytes
    Returns a tuple: (bytes, mime) or (None, None)
    """
    # 1) Top-level url
    url = result_json.get("url")
    if url:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "image/png")

    # 2) Output array
    output = result_json.get("output") or result_json.get("outputs") or result_json.get("results")
    if isinstance(output, list) and len(output) > 0:
        item = output[0]
        # possible keys: url, base64, b64, image
        if isinstance(item, dict):
            if item.get("url"):
                resp = requests.get(item["url"], timeout=30)
                resp.raise_for_status()
                return resp.content, resp.headers.get("Content-Type", "image/png")
            if item.get("base64") or item.get("b64"):
                b64data = item.get("base64") or item.get("b64")
                import base64
                return base64.b64decode(b64data), "image/png"
            if item.get("image") and isinstance(item.get("image"), str):
                # maybe "image" holds base64
                import base64
                return base64.b64decode(item.get("image")), "image/png"
    # 3) Try to find any url inside nested structures
    def find_first_url(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and v.startswith("http"):
                    return v
                found = find_first_url(v)
                if found:
                    return found
        if isinstance(obj, list):
            for el in obj:
                found = find_first_url(el)
                if found:
                    return found
        return None

    any_url = find_first_url(result_json)
    if any_url:
        resp = requests.get(any_url, timeout=30)
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "image/png")

    return None, None

# --- Routes ---
@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/generate', methods=['POST'])
def generate_image():
    """
    Generate an image using DigitalOcean serverless inference and return the image bytes directly.
    Request JSON:
      { "prompt": "...", "model_id": "fal-ai/flux/schnell", "options": { ... } }
    """
    if not DO_MODEL_ACCESS_KEY:
        return jsonify({"error": "Server not configured with DO_MODEL_ACCESS_KEY"}), 500

    data = request.json or {}
    prompt = data.get("prompt")
    model_id = data.get("model_id", "fal-ai/flux/schnell")
    options = data.get("options", {}) or {}

    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    try:
        # build input for DO model (include options inside input)
        input_payload = {"prompt": prompt, **options}

        start_resp = start_async_inference(model_id=model_id, input_payload=input_payload)
        # expected to contain a request id in several possible shapes
        request_id = start_resp.get("request_id") or start_resp.get("id") or start_resp.get("requestId")
        if not request_id:
            # If no request id available, return the start response for debugging
            return jsonify({"error": "Unexpected async-invoke response", "response": start_resp}), 500

        # Poll until complete
        final_result = poll_until_complete(request_id, timeout_seconds=DEFAULT_POLL_TIMEOUT, poll_interval=DEFAULT_POLL_INTERVAL)

        # extract image bytes (download from returned URL or decode base64)
        img_bytes, content_type = extract_image_bytes_from_result(final_result)
        if not img_bytes:
            return jsonify({"error": "No image found in inference result", "result": final_result}), 500

        # Return image directly
        return send_file(BytesIO(img_bytes), mimetype=content_type or "image/png")
    except TimeoutError as te:
        app.logger.error(te)
        return jsonify({"error": "Inference job timed out", "details": str(te)}), 504
    except requests.HTTPError as he:
        app.logger.error(f"HTTP error during inference: {he} - response: {getattr(he, 'response', None)}")
        return jsonify({"error": "HTTP error during inference", "details": str(he)}), 502
    except Exception as e:
        app.logger.exception("Error generating image")
        return jsonify({"error": "Failed to generate image", "details": str(e)}), 500

@app.route('/upload-to-spaces', methods=['POST'])
def upload_image_to_spaces():
    """
    Generate image via DO serverless inference, then upload to DigitalOcean Spaces and return the public URL.
    Request JSON:
      { "prompt": "...", "model_id": "fal-ai/flux/schnell", "options": { ... } }
    """
    if not DO_MODEL_ACCESS_KEY:
        return jsonify({"error": "Server not configured with DO_MODEL_ACCESS_KEY"}), 500

    data = request.json or {}
    prompt = data.get("prompt")
    model_id = data.get("model_id", "fal-ai/flux/schnell")
    options = data.get("options", {}) or {}

    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    try:
        input_payload = {"prompt": prompt, **options}
        start_resp = start_async_inference(model_id=model_id, input_payload=input_payload)
        request_id = start_resp.get("request_id") or start_resp.get("id") or start_resp.get("requestId")
        if not request_id:
            return jsonify({"error": "Unexpected async-invoke response", "response": start_resp}), 500

        final_result = poll_until_complete(request_id, timeout_seconds=DEFAULT_POLL_TIMEOUT, poll_interval=DEFAULT_POLL_INTERVAL)

        # extract image bytes
        img_bytes, content_type = extract_image_bytes_from_result(final_result)
        if not img_bytes:
            return jsonify({"error": "No image found in inference result", "result": final_result}), 500

        # prepare filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_id = str(uuid.uuid4())[:8]
        safe_prompt = "".join(c for c in (prompt or "")[:30] if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(' ', '_')
        filename = f"generated_images/{safe_prompt}_{timestamp}_{unique_id}.png"

        upload_url = upload_to_spaces(img_bytes, filename)
        if upload_url:
            return jsonify({
                "success": True,
                "message": "Image uploaded successfully to DigitalOcean Spaces",
                "url": upload_url,
                "filename": filename
            })
        else:
            return jsonify({"success": False, "error": "Failed to upload image to DigitalOcean Spaces"}), 500
    except TimeoutError as te:
        app.logger.error(te)
        return jsonify({"error": "Inference job timed out", "details": str(te)}), 504
    except Exception as e:
        app.logger.exception("Error during image generation or upload")
        return jsonify({"error": "Failed to generate or upload image", "details": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

if __name__ == '__main__':
    # startup warnings for missing Spaces creds
    if not DO_SPACES_KEY or not DO_SPACES_SECRET:
        print("Warning: DigitalOcean Spaces credentials (DO_SPACES_KEY / DO_SPACES_SECRET) not found.")
        print("Upload to Spaces functionality will not work without these credentials.")

    if not DO_MODEL_ACCESS_KEY:
        print("Warning: DO_MODEL_ACCESS_KEY not set. Serverless inference will fail until set.")

    app.run(host='0.0.0.0', port=8080)
