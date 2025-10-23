# test_node_upload.py
import os, io, base64, requests
try:
    # loads MEDIA_NODE_BASE / MEDIA_NODE_API_KEY if you have a .env next to this file
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


MEDIA_NODE_BASE = os.getenv("MEDIA_NODE_BASE", "http://127.0.0.1:4000").rstrip("/")
MEDIA_NODE_API_KEY = os.getenv("MEDIA_NODE_API_KEY", "supersecret123")
USER_ID = os.getenv("TEST_USER_ID", "54")  # change if needed

def _print_header(title):
    print("\n" + "="*16, title, "="*16, flush=True)

def _post(endpoint: str, files: dict, data: dict):
    url = f"{MEDIA_NODE_BASE}{endpoint}"
    headers = {}
    if MEDIA_NODE_API_KEY:
        headers["X-API-KEY"] = MEDIA_NODE_API_KEY

    print(f"[POST] {url}", flush=True)
    print(f"[HEADERS] {headers}", flush=True)
    print(f"[FIELDS]  { {k:v for k,v in data.items()} }", flush=True)

    try:
        r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        print(f"[HIT] status={r.status_code}", flush=True)
        print(f"[BODY] { (r.text or '')[:300] }", flush=True)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        raise

def upload_screenshot():
    """
    Sends a 1x1 PNG (base64) as 'file', with user_id field.
    """
    _print_header("UPLOAD SCREENSHOT")
    # 1x1 transparent PNG
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQAB"
        "DQottAAAAABJRU5ErkJggg=="
    )
    png_bytes = base64.b64decode(png_b64)
    files = {
        "file": ("screenshot.png", io.BytesIO(png_bytes), "image/png")
    }
    data = {
        "user_id": str(USER_ID),
        "mime": "image/png",
        "event_id": ""
    }
    return _post("/api/v1/media/upload-screenshot", files, data)

def upload_recording():
    """
    Sends a tiny dummy 'mp4' byte stream; Node just writes bytes and records DB row.
    """
    _print_header("UPLOAD RECORDING")
    dummy_mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42mp41isom"  # harmless stub
    files = {
        "file": ("recording.mp4", io.BytesIO(dummy_mp4), "video/mp4")
    }
    data = {
        "user_id": str(USER_ID),
        "mime": "video/mp4",
        "duration_seconds": "3",
        "event_id": ""
    }
    return _post("/api/v1/media/upload-recording", files, data)

if __name__ == "__main__":
    print(f"[ENV] MEDIA_NODE_BASE={MEDIA_NODE_BASE}")
    print(f"[ENV] MEDIA_NODE_API_KEY={MEDIA_NODE_API_KEY}")
    print(f"[ENV] USER_ID={USER_ID}")

    # Quick connectivity probe
    try:
        _print_header("PING")
        r = requests.get(f"{MEDIA_NODE_BASE}/ping", timeout=10)
        print(f"[HIT] /ping status={r.status_code} body={(r.text or '')[:200]}")
    except Exception as e:
        print("[ERROR] /ping failed ->", e)

    try:
        resp1 = upload_screenshot()
        print("[RESULT screenshot]", resp1)
    except Exception:
        pass

    try:
        resp2 = upload_recording()
        print("[RESULT recording]", resp2)
    except Exception:
        pass
