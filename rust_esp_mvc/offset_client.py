"""Client Pre-Game Loader & In-Memory Hot-Patcher.

Pre-Game Mode:
  - Generates HWID (CPU + Motherboard + Disk Serials).
  - Fetches encrypted offset payload from API BEFORE game / EAC starts.
  - Decrypts payload and saves to encrypted local file `.offset_cache`.
  - Closes all network sockets before game execution.

In-Game Mode:
  - Decrypts local `.offset_cache` in-memory.
  - Dynamically patches `legacy.OFF` attributes & `_hv_decrypt` operations in `legacy_runtime.py`.
  - 0 Network calls while Rust is running!
"""

import base64
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import urllib.request

try:
    from tools.rust_esp_mvc import legacy_runtime as legacy
except ImportError:
    from rust_esp_mvc import legacy_runtime as legacy


CACHE_FILE = os.path.join(os.path.dirname(__file__), ".offset_cache")
LICENSE_FILE = os.path.join(os.path.dirname(__file__), "license.txt")



def generate_hwid():
    """Generate a unique 64-char SHA-256 HWID from CPU + CSPRODUCT UUID."""
    components = []
    try:
        out = subprocess.check_output("wmic csproduct get uuid", shell=True, text=True, stderr=subprocess.DEVNULL)
        lines = [l.strip() for l in out.splitlines() if l.strip() and "UUID" not in l]
        if lines:
            components.append(lines[0])
    except Exception:
        try:
            out = subprocess.check_output('powershell -Command "(Get-CimInstance Win32_ComputerSystemProduct).UUID"', shell=True, text=True, stderr=subprocess.DEVNULL)
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if lines:
                components.append(lines[0])
        except Exception:
            pass

    try:
        out = subprocess.check_output("wmic cpu get processorid", shell=True, text=True, stderr=subprocess.DEVNULL)
        lines = [l.strip() for l in out.splitlines() if l.strip() and "ProcessorId" not in l]
        if lines:
            components.append(lines[0])
    except Exception:
        try:
            out = subprocess.check_output('powershell -Command "(Get-CimInstance Win32_Processor).ProcessorId"', shell=True, text=True, stderr=subprocess.DEVNULL)
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if lines:
                components.append(lines[0])
        except Exception:
            pass

    if not components:
        components.append(os.environ.get("COMPUTERNAME", "DEFAULT_PC"))

    raw = ":".join(components)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()



def simple_aes_encrypt(data_bytes, secret_key):
    """Encrypt payload using keystream XOR + HMAC signature."""
    key_hash = hashlib.sha256(secret_key.encode("utf-8")).digest()
    iv = os.urandom(16)

    keystream = b""
    counter = 0
    while len(keystream) < len(data_bytes):
        block = hashlib.sha256(key_hash + iv + struct.pack("<I", counter)).digest()
        keystream += block
        counter += 1

    cipher_bytes = bytes(a ^ b for a, b in zip(data_bytes, keystream[:len(data_bytes)]))
    sig = hmac.new(key_hash, iv + cipher_bytes, hashlib.sha256).hexdigest()

    payload = {
        "iv": base64.b64encode(iv).decode("utf-8"),
        "cipher": base64.b64encode(cipher_bytes).decode("utf-8"),
        "sig": sig,
    }
    return json.dumps(payload)


def simple_aes_decrypt(encrypted_json_str, secret_key):

    """Decrypt payload using secret key."""
    key_hash = hashlib.sha256(secret_key.encode("utf-8")).digest()
    data = json.loads(encrypted_json_str)

    iv = base64.b64decode(data["iv"])
    cipher_bytes = base64.b64decode(data["cipher"])
    expected_sig = data["sig"]

    # Verify signature
    sig = hmac.new(key_hash, iv + cipher_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("HMAC signature verification failed - Payload tampered!")

    # Reconstruct keystream
    keystream = b""
    counter = 0
    while len(keystream) < len(cipher_bytes):
        block = hashlib.sha256(key_hash + iv + struct.pack("<I", counter)).digest()
        keystream += block
        counter += 1

    plain_bytes = bytes(a ^ b for a, b in zip(cipher_bytes, keystream[:len(cipher_bytes)]))
    return plain_bytes.decode("utf-8")


def fetch_offsets_pregame(server_url, license_key):
    """Fetch offsets from server BEFORE launching the game, then save encrypted cache."""
    hwid = generate_hwid()
    print(f"[+] Loader HWID: {hwid[:16]}...")
    print(f"[+] Contacting Offset API Server ({server_url})...")

    if "upstash.io" in server_url:
        endpoint = f"{server_url.rstrip('/')}/get/offsets"
        req = urllib.request.Request(
            endpoint,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Authorization": f"Bearer {UPSTASH_TOKEN}",
            },
        )
    elif "/api/" in server_url or server_url.endswith(".json"):
        endpoint = server_url
        req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
    else:
        endpoint = f"{server_url.rstrip('/')}/v1/client/offsets"
        req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
        if license_key:
            req.add_header("X-License-Key", license_key)
            req.add_header("X-HWID", hwid)



    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"[!] Server Error ({e.code}): {err_body}")
        raise RuntimeError(f"Authentication/API failure: {e.code}")

    if "result" in data and data["result"]:
        # Upstash Redis REST API response (AES encrypted)
        raw_result = data["result"]
        if isinstance(raw_result, str) and "cipher" in raw_result:
            decrypted_payload_json = simple_aes_decrypt(raw_result, MASTER_SECRET_KEY)
            payload_obj = json.loads(decrypted_payload_json)
        else:
            payload_obj = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            decrypted_payload_json = json.dumps(payload_obj)
    elif "encrypted_data" in data:
        client_secret = f"{license_key}:{hwid}"
        decrypted_payload_json = simple_aes_decrypt(data["encrypted_data"], client_secret)
        payload_obj = json.loads(decrypted_payload_json)
    else:
        # Direct raw JSON payload from Vercel / Edge / CDN
        payload_obj = data
        decrypted_payload_json = json.dumps(payload_obj)

    print(f"[+] Successfully fetched & decrypted build {payload_obj.get('build_id')} offsets!")




    # Save to encrypted local cache for in-game offline use
    local_secret = f"LOCAL_MACHINE_SECRET_{hwid}"
    encrypted_cache = simple_aes_encrypt(decrypted_payload_json.encode("utf-8"), local_secret)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(encrypted_cache)

    print("[+] Encrypted local offset cache saved. Network connections closed.")
    return json.loads(decrypted_payload_json)


def apply_offsets_in_memory(payload=None):
    """Apply offset values to legacy.OFF in memory without modifying disk files."""
    if payload is None:
        # Load from local cache
        hwid = generate_hwid()
        local_secret = f"LOCAL_MACHINE_SECRET_{hwid}"
        if not os.path.exists(CACHE_FILE):
            print("[!] Local offset cache missing! Run loader first.")
            return False
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            raw_cache = f.read()
        decrypted_str = simple_aes_decrypt(raw_cache, local_secret)
        payload = json.loads(decrypted_str)

    offs = payload.get("offsets", {})
    decryptions = payload.get("decryptions", {})

    # Patch legacy.OFF attributes
    for k, v in offs.items():
        if hasattr(legacy.OFF, k):
            setattr(legacy.OFF, k, v)

    # Patch decryption functions in memory
    def make_decrypter(ops):
        def decrypter(val):
            return legacy._hv_decrypt(val, ops)
        return decrypter

    if "inventory" in decryptions:
        legacy.decrypt_player_inventory = make_decrypter(decryptions["inventory"])
    if "eyes" in decryptions:
        legacy.decrypt_player_eyes = make_decrypter(decryptions["eyes"])
    if "cl_active_item" in decryptions:
        legacy.decrypt_cl_active_item = make_decrypter(decryptions["cl_active_item"])

    print(f"[+] In-memory offsets & decryptions applied successfully! (Build {payload.get('build_id', 'unknown')})")
    return True


UPSTASH_TOKEN = "gQAAAAAAAlxLAAIgcDIwM2ZhYWNjY2YwODU0MzMyOTVjYTRjMzI0ZmYyNTdkNQ"
DEFAULT_SERVER_URL = "https://frank-cowbird-154699.upstash.io"
MASTER_SECRET_KEY = "RUST_PRIVATE_ESP_SECRET_KEY_2026_SECURE"





def fetch_and_apply_auto(server_url=DEFAULT_SERVER_URL):
    """Automatically fetch latest offsets on startup, then patch memory."""
    license_key = "DEV_OPEN_ACCESS"
    print(f"[+] Transparent offset check (Server: {server_url})...")
    try:
        payload = fetch_offsets_pregame(server_url, license_key)
        apply_offsets_in_memory(payload)
    except Exception as e:
        print(f"[!] Server fetch skipped ({e}). Falling back to encrypted local cache...")
        apply_offsets_in_memory()




if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fetch":
        server_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
        license_key = sys.argv[3] if len(sys.argv) > 3 else "TEST_KEY"
        fetch_offsets_pregame(server_url, license_key)
    elif len(sys.argv) > 1 and sys.argv[1] == "--apply":
        apply_offsets_in_memory()
    else:
        print("Usage: python -m tools.offset_client [--fetch SERVER_URL LICENSE_KEY | --apply]")
