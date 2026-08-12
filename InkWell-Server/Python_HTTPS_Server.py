"""Inkwell -- HTTPS/HTTP server, either with a small local GUI or fully
headless (for Docker / any container or server environment).

Serves the editor's Web front end (this script's own directory) plus a
JSON API for accounts, notebooks and notes.

Data layout under WORKSPACE_DIR (chosen via the GUI's directory picker
in GUI mode, or INKWELL_WORKSPACE_DIR in headless mode):

    WORKSPACE_DIR/
      users.json                     accounts: password + PIN hashes
      settings/
        <username>.json              per-user editor settings
      users/
        <username>/
          notebooks.json             {"Notebook Name": {hidden, created, modified, order}}
          <Notebook Name>/
            notes_meta.json          {"note.txt": {hidden, created, modified, order, encrypted}}
            note.txt
            ...

Any stray .txt files that were sitting directly in the old flat-file
layout's directory are left alone and exposed read-only via
GET /api/legacy-files so a logged-in user can import them into a
notebook of their choice (POST /api/legacy-import) -- nothing is moved
or deleted automatically.

Auth is username/password (PBKDF2-HMAC-SHA256, per-user salt, and the
iteration count used is stored alongside each hash so it can be raised
in the future without breaking existing accounts) with a random
session token in an HttpOnly, Secure, SameSite=Lax cookie, held in
memory only (sessions do not survive a server restart). Hiding a
notebook or note is *not* itself a security boundary -- it is a
declutter toggle, gated back open by a separate 4-digit PIN (also
PBKDF2-HMAC-SHA256) each user sets for themselves. If an
INKWELL_ENCRYPTION_KEY is configured, notes that are hidden (directly,
or because their notebook is hidden) are additionally encrypted at
rest with AES-256-GCM -- see the module docstring further down, near
the encryption helpers, for exactly what that does and doesn't protect
against.

Run modes
---------
GUI (default when a display and Tkinter are available and
INKWELL_HEADLESS isn't set): a small local window to pick the
workspace directory, address, and port, with Start/Stop and a log.

Headless (Docker, or any environment without a display -- set
INKWELL_HEADLESS=1, or it's used automatically if Tkinter isn't
importable at all): configured entirely through environment variables,
see run_headless() below for the full list. No GUI dependency at all
in this mode -- the tkinter import is skipped entirely.
"""

import base64
import calendar
import hashlib
import http.server
import json
import os
import re
import secrets
import signal
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

try:
    from faster_whisper import WhisperModel
    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    FASTER_WHISPER_AVAILABLE = False

DEFAULT_ADDY = "0.0.0.0"
DEFAULT_PORT = 8060

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVE_DIR = SCRIPT_DIR  # the front end's own files -- always served from here

# Root of all user data (accounts, notebooks, notes, per-user settings).
# Changeable via the GUI's directory picker (GUI mode) or
# INKWELL_WORKSPACE_DIR (headless mode) before the server starts.
WORKSPACE_DIR = os.path.join(SERVE_DIR, "workspace")

# This GUI's own preferences (workspace directory / address / port last
# used) -- local-only, never served over the API. Not used in headless
# mode, which is configured via environment variables each time.
GUI_SETTINGS_PATH = os.path.join(SCRIPT_DIR, "server_gui_settings.json")

SESSION_COOKIE = "inkwell_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days of inactivity

# OWASP's current (2023-era) minimum recommendation for PBKDF2-HMAC-SHA256.
# Stored per-hash (see _hash_secret/_verify_secret) specifically so this
# can be raised again later without breaking anyone's existing password
# or PIN -- old hashes keep verifying at whatever count they were
# created with; only newly-set secrets pick up a higher count.
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 10

# Guards every read-modify-write of a JSON metadata file, and the
# in-memory session/lockout dicts -- ThreadingHTTPServer handles
# requests concurrently, so without this two near-simultaneous requests
# could clobber each other's changes.
DATA_LOCK = threading.RLock()

# token -> {"username": <lowercased key>, "created": ts, "seen": ts}
SESSIONS: dict = {}

# username -> {"count": int, "locked_until": ts}  (failed PIN attempts)
PIN_ATTEMPTS: dict = {}
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SECONDS = 30

# username -> {"count": int, "locked_until": ts}  (failed login attempts --
# same shape/limits as PIN_ATTEMPTS, kept as a separate dict since the two
# are logically unrelated events)
LOGIN_ATTEMPTS: dict = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 30

# IP address -> {"count": int, "locked_until": ts}  (failed admin-reset
# attempts -- keyed by IP rather than username since there's no logged-in
# account involved yet at that point, and a much longer/harsher lockout
# since this endpoint can reset *any* account's password if guessed)
ADMIN_ATTEMPTS: dict = {}
ADMIN_MAX_ATTEMPTS = 5
ADMIN_LOCKOUT_SECONDS = 300

# Loaded once at startup (see _load_admin_password / run_headless /
# ServerGUI) from INKWELL_ADMIN_PASSWORD or INKWELL_ADMIN_PASSWORD_FILE.
# None means the admin-reset-password feature is simply unavailable.
ADMIN_PASSWORD: str | None = None

# Loaded once at startup (see _load_encryption_key) from
# INKWELL_ENCRYPTION_KEY / INKWELL_ENCRYPTION_KEY_FILE, then run through
# a KDF into a 32-byte AES-256 key. None means hidden notes are stored
# as plain text, same as before this existed.
ENCRYPTION_KEY_BYTES: bytes | None = None

# Loaded once at startup (see _load_stt_config) from INKWELL_STT_PROVIDER
# and friends -- lets speech-to-text (for clients like a VR keyboard-free
# editor) be either fully disabled, run locally on this same server via
# faster-whisper, or proxied out to a remote provider. "disabled" means
# POST /api/transcribe just returns 503 -- no client ever needs its own
# provider credentials, since the request always goes through this
# server first, same as everything else in the API.
STT_PROVIDER = "disabled"  # "disabled" | "local" | "openai" | "groq" | "google" | "custom"
STT_API_KEY: str | None = None  # for openai/groq/google/custom (optional for custom) -- unused for "local"
STT_LOCAL_MODEL_SIZE = "base"  # faster-whisper model name, only used for "local"
# For "custom" -- e.g. a Whisper server on another PC on your network
# exposing the same OpenAI-shaped /v1/audio/transcriptions endpoint
# (faster-whisper-server/Speaches, whisper-asr-webservice, etc.).
# Include the scheme and any path prefix the server itself uses, e.g.
# "http://192.168.1.50:8000/v1" -- this server appends
# "/audio/transcriptions" to it.
STT_CUSTOM_URL: str | None = None
STT_CUSTOM_MODEL_NAME = "whisper-1"  # sent as the "model" field -- most self-hosted servers ignore it

# Lazily loaded on first use (loading a Whisper model takes real time and
# memory, no reason to pay that cost if local STT is never actually used
# in a given run), guarded by a lock since ThreadingHTTPServer can call
# in from multiple requests concurrently.
_LOCAL_WHISPER_MODEL = None
_LOCAL_WHISPER_LOCK = threading.Lock()

NAME_RE = re.compile(r"^[A-Za-z0-9 _\-.()'&]{1,80}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")


def load_gui_settings() -> dict:
    if not os.path.isfile(GUI_SETTINGS_PATH):
        return {}
    try:
        with open(GUI_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_gui_settings(workspace_dir: str, addy: str, port: int) -> None:
    data = {"workspace_dir": workspace_dir, "address": addy, "port": port}
    try:
        with open(GUI_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------
# Password / PIN hashing
# ---------------------------------------------------------------------

def _hash_secret(secret: str, salt_hex: str = None, iterations: int = None):
    """Returns (digest_hex, salt_hex, iterations). Always hashes at
    PBKDF2_ITERATIONS for *new* secrets (iterations param left as
    None); an explicit iterations value is only ever passed in by
    _verify_secret, to re-hash at whatever count the stored hash was
    originally created with."""
    if salt_hex is None:
        salt_hex = secrets.token_hex(16)
    if iterations is None:
        iterations = PBKDF2_ITERATIONS
    digest = hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    ).hex()
    return digest, salt_hex, iterations


def _verify_secret(secret: str, digest_hex: str, salt_hex: str, iterations) -> bool:
    if not digest_hex or not salt_hex:
        return False
    # Old data saved before this field existed -- fall back to what was
    # the only iteration count in use at the time.
    if not isinstance(iterations, int):
        iterations = 200_000
    check, _, _ = _hash_secret(secret, salt_hex, iterations)
    return secrets.compare_digest(check, digest_hex)


# ---------------------------------------------------------------------
# Path / filesystem helpers
# ---------------------------------------------------------------------

def _users_json_path() -> str:
    return os.path.join(WORKSPACE_DIR, "users.json")


def _load_users() -> dict:
    path = _users_json_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_users(users: dict) -> None:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    path = _users_json_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, path)


def _user_dir(username_key: str) -> str:
    return os.path.join(WORKSPACE_DIR, "users", username_key)


def _notebooks_json_path(username_key: str) -> str:
    return os.path.join(_user_dir(username_key), "notebooks.json")


def _load_notebooks(username_key: str) -> dict:
    path = _notebooks_json_path(username_key)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_notebooks(username_key: str, data: dict) -> None:
    os.makedirs(_user_dir(username_key), exist_ok=True)
    path = _notebooks_json_path(username_key)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _notebook_dir(username_key: str, notebook: str) -> str:
    return os.path.join(_user_dir(username_key), notebook)


def _notes_meta_path(username_key: str, notebook: str) -> str:
    return os.path.join(_notebook_dir(username_key, notebook), "notes_meta.json")


def _load_notes_meta(username_key: str, notebook: str) -> dict:
    path = _notes_meta_path(username_key, notebook)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_notes_meta(username_key: str, notebook: str, data: dict) -> None:
    os.makedirs(_notebook_dir(username_key, notebook), exist_ok=True)
    path = _notes_meta_path(username_key, notebook)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _valid_name(name: str) -> bool:
    """Notebook / note-title validation -- also doubles as anti-traversal
    since it disallows '/', '\\', and leading/trailing dots via the
    charset, and rejects '.'/'..' explicitly."""
    if not name or name in (".", ".."):
        return False
    return bool(NAME_RE.match(name)) and not name.startswith(".") and not name.endswith(".")


def _valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name or ""))


def _note_filename(name: str) -> str:
    return name if name.lower().endswith(".txt") else name + ".txt"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _next_order(items: dict) -> int:
    """Next free order value for a new entry in a notebooks/notes_meta
    dict -- one past whatever the highest existing order is (0 if the
    dict is empty), so new items land at the end of the current order
    rather than colliding with an existing position."""
    existing = [info.get("order") for info in items.values() if isinstance(info.get("order"), int)]
    return (max(existing) + 1) if existing else 0


def _ensure_order(items: dict) -> bool:
    """Backfills a missing/invalid 'order' field on any entries that
    don't have one (stably, alphabetically among just the missing
    ones, appended after everything that already has a valid order) --
    self-heals data written before ordering existed, or written by a
    future entry that skipped it for any reason. Returns True if it
    changed anything, so the caller knows whether to persist."""
    have_order = {
        name: info["order"] for name, info in items.items() if isinstance(info.get("order"), int)
    }
    missing = sorted(name for name in items if name not in have_order)
    if not missing:
        return False
    next_order = (max(have_order.values()) + 1) if have_order else 0
    for name in missing:
        items[name]["order"] = next_order
        next_order += 1
    return True


# ---------------------------------------------------------------------
# At-rest encryption for hidden notes
#
# What this protects against: someone who gets hold of the raw files
# under WORKSPACE_DIR without going through this app at all -- a stolen
# backup, a misconfigured volume/bucket, another tenant on shared
# storage, etc. -- can't read a hidden note's content without also
# having INKWELL_ENCRYPTION_KEY.
#
# What this does NOT protect against: anyone who can reach the running
# app as that user (or as the server process itself) still gets the
# plaintext on request, same as any other note -- the encryption key
# lives in the server's environment and is used automatically, with no
# extra per-request secret. It is not tied to, or derived from, the
# user's account password or PIN. A 4-digit PIN in particular would be
# far too low-entropy to derive a real encryption key from (10,000
# possible values is instant to brute-force offline against a stolen
# file), which is why this is a separate, higher-entropy, operator-set
# key rather than reusing the PIN.
#
# Only notes that are "effectively hidden" (the note itself is hidden,
# or its notebook is) get encrypted; anything visible stays plain text
# so it's directly readable/greppable/backupable like normal files.
# ---------------------------------------------------------------------

_ENCRYPTION_KDF_SALT = b"inkwell-hidden-notes-v1"
_ENCRYPTED_MAGIC = b"INKWELLENC1"


def _derive_encryption_key(passphrase: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), _ENCRYPTION_KDF_SALT, 200_000)


def _encrypt_bytes(plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(ENCRYPTION_KEY_BYTES).encrypt(nonce, plaintext, None)
    return _ENCRYPTED_MAGIC + nonce + ciphertext


def _decrypt_bytes(data: bytes) -> bytes:
    if not data.startswith(_ENCRYPTED_MAGIC):
        raise ValueError("Not an encrypted note (missing magic header)")
    nonce = data[len(_ENCRYPTED_MAGIC):len(_ENCRYPTED_MAGIC) + 12]
    ciphertext = data[len(_ENCRYPTED_MAGIC) + 12:]
    return AESGCM(ENCRYPTION_KEY_BYTES).decrypt(nonce, ciphertext, None)


def _effective_hidden(notebooks: dict, notebook: str, note_info: dict) -> bool:
    nb_hidden = bool(notebooks.get(notebook, {}).get("hidden"))
    return nb_hidden or bool(note_info.get("hidden"))


def _sync_note_encryption(notebooks: dict, key: str, notebook: str, filename: str, meta: dict) -> bool:
    """Makes one note's on-disk bytes match what they should be right
    now: encrypted if it's effectively hidden and a key is configured,
    plain text otherwise. Called whenever hidden state could have
    changed (hide/unhide a note or its notebook) and, as a self-heal
    pass, when a notebook's notes are listed -- covers the key being
    added/removed from the config between runs, or a notebook-level
    hide/unhide that needs to cascade to notes that don't have their
    own hidden flag set. Returns True if it rewrote the file."""
    info = meta.get(filename)
    if info is None:
        return False
    path = os.path.join(_notebook_dir(key, notebook), filename)
    if not os.path.isfile(path):
        return False

    should_be_encrypted = bool(ENCRYPTION_KEY_BYTES) and _effective_hidden(notebooks, notebook, info)
    is_encrypted = bool(info.get("encrypted"))
    if should_be_encrypted == is_encrypted:
        return False

    if is_encrypted and not ENCRYPTION_KEY_BYTES:
        # Would need to decrypt first (to either re-encrypt with a new
        # key, or land on plain text) but there's no key configured at
        # all to do that with -- leave the file alone rather than
        # crash. This note stays encrypted, and therefore unreadable
        # via the API too, until a key is configured again.
        return False

    try:
        with open(path, "rb") as f:
            data = f.read()
        if is_encrypted:
            data = _decrypt_bytes(data)
        if should_be_encrypted:
            data = _encrypt_bytes(data)
        with open(path, "wb") as f:
            f.write(data)
    except (OSError, ValueError) as e:
        # Most likely cause: the note is encrypted but the configured
        # key can no longer decrypt it (key changed). Leave the file
        # untouched rather than risk losing data.
        message = f"Couldn't re-encrypt/decrypt {notebook}/{filename}: {e}"
        if _gui_instance:
            _gui_instance.log(message)
        else:
            print(message, file=sys.stderr)
        return False

    info["encrypted"] = should_be_encrypted
    return True


def run_startup_encryption_sync() -> None:
    """Runs once, right after the server starts (see run_headless() and
    ServerGUI.start_server()), bringing every user's notes in line with
    the current INKWELL_ENCRYPTION_KEY configuration immediately --
    rather than leaving each note as-is until it happens to be listed
    or have its hidden state toggled through the API, which
    _sync_note_encryption() alone only covers reactively. Matters most
    right after setting the key for the first time: existing hidden
    notes get encrypted right away, not left sitting in plain text for
    an unbounded time -- exactly the situation this exists for. (Notes
    already encrypted with a key that's since been removed from config
    stay encrypted rather than getting corrupted or silently dropped
    back to plain text -- there's no way to decrypt them without that
    key, by design; see _sync_note_encryption()'s handling of that
    case.)"""
    if not CRYPTO_AVAILABLE:
        return
    users_dir = os.path.join(WORKSPACE_DIR, "users")
    if not os.path.isdir(users_dir):
        return

    changed_count = 0
    with DATA_LOCK:
        for username_key in os.listdir(users_dir):
            if not os.path.isdir(os.path.join(users_dir, username_key)):
                continue
            notebooks = _load_notebooks(username_key)
            for notebook_name in list(notebooks.keys()):
                meta = _load_notes_meta(username_key, notebook_name)
                meta_changed = False
                for filename in list(meta.keys()):
                    if _sync_note_encryption(notebooks, username_key, notebook_name, filename, meta):
                        meta_changed = True
                        changed_count += 1
                if meta_changed:
                    _save_notes_meta(username_key, notebook_name, meta)

    if changed_count:
        message = (
            f"Encryption sync: updated {changed_count} note(s) on startup "
            f"to match the current INKWELL_ENCRYPTION_KEY configuration."
        )
        if _gui_instance:
            _gui_instance.log(message)
        else:
            print(message)


# ---------------------------------------------------------------------
# Speech-to-text
#
# One endpoint (POST /api/transcribe) regardless of provider -- clients
# (e.g. a VR editor where typing is awkward) always talk to this
# server, never to a third-party STT provider directly, so provider
# API keys only ever live in this process's environment, not in any
# distributed client. Which provider actually handles a given request
# is entirely a server-side configuration choice (INKWELL_STT_PROVIDER),
# made once at startup -- see _load_stt_config().
# ---------------------------------------------------------------------

def _get_local_whisper_model():
    global _LOCAL_WHISPER_MODEL
    with _LOCAL_WHISPER_LOCK:
        if _LOCAL_WHISPER_MODEL is None:
            # Model weights are downloaded from Hugging Face on first use
            # (needs internet access that one time) and cached here after
            # that -- kept under WORKSPACE_DIR specifically so they survive
            # a container restart instead of re-downloading every time.
            model_dir = os.path.join(WORKSPACE_DIR, "stt-models")
            os.makedirs(model_dir, exist_ok=True)
            _LOCAL_WHISPER_MODEL = WhisperModel(
                STT_LOCAL_MODEL_SIZE, device="cpu", compute_type="int8", download_root=model_dir
            )
        return _LOCAL_WHISPER_MODEL


def _transcribe_local(audio_bytes: bytes) -> str:
    model = _get_local_whisper_model()
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        segments, _info = model.transcribe(tmp.name)
        return " ".join(seg.text.strip() for seg in segments).strip()


def _build_multipart_body(fields: dict, file_field: str, filename: str, file_bytes: bytes, content_type: str):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode())
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _transcribe_openai_compatible(audio_bytes: bytes, base_url: str, api_key: str | None, model_name: str) -> str:
    """Covers OpenAI's and Groq's transcription APIs (Groq's is
    deliberately API-compatible with OpenAI's -- same shape, different
    base URL/model), and any self-hosted server exposing the same
    /v1/audio/transcriptions shape (e.g. faster-whisper-server/Speaches,
    whisper-asr-webservice) via the "custom" provider. api_key is
    optional specifically for that last case -- a lot of self-hosted
    Whisper servers don't require any auth at all, and sending a
    malformed/empty Bearer header to one that doesn't expect it is
    worth avoiding rather than assuming every provider wants one."""
    body, content_type = _build_multipart_body(
        fields={"model": model_name},
        file_field="file",
        filename="audio.wav",
        file_bytes=audio_bytes,
        content_type="audio/wav",
    )
    headers = {"Content-Type": content_type}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base_url}/audio/transcriptions",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("text") or "").strip()


def _transcribe_google(audio_bytes: bytes, api_key: str) -> str:
    """Expects 16kHz mono 16-bit PCM WAV from the client -- Google's API
    needs the sample rate/encoding declared up front rather than reading
    it from the file, so the client and this pairing need to agree on a
    format; that's the most universally-supported one."""
    payload = {
        "config": {"encoding": "LINEAR16", "sampleRateHertz": 16000, "languageCode": "en-US"},
        "audio": {"content": base64.b64encode(audio_bytes).decode("ascii")},
    }
    req = urllib.request.Request(
        f"https://speech.googleapis.com/v1/speech:recognize?key={api_key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("results") or []
    return " ".join(
        r["alternatives"][0]["transcript"] for r in results if r.get("alternatives")
    ).strip()



# ---------------------------------------------------------------------

class EditorRequestHandler(http.server.SimpleHTTPRequestHandler):
    # Python's http.server defaults to HTTP/1.0 behavior, which closes
    # the TCP connection after every single response. Reverse proxies
    # (cloudflared among them) expect to reuse connections the way
    # HTTP/1.1 allows; when one tries to send a second request (or
    # there's still unread data buffered) on a connection this server
    # already closed, the OS sends back a hard RST instead of a clean
    # close -- which shows up on the proxy's side as exactly "read:
    # connection reset by peer", not any kind of rejection by the app
    # itself. Every response here already sets Content-Length (or is a
    # static file served by the parent class, which does the same), so
    # enabling real keep-alive is safe -- the client always knows
    # exactly where a response ends.
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SERVE_DIR, **kwargs)

    def _log(self, message: str) -> None:
        """Every logging path in this class funnels through here, so
        headless mode (Docker) actually gets request/connection logs
        via plain stdout -- `docker logs` / Portainer's log view --
        rather than only the GUI's own log window ever seeing them."""
        if _gui_instance:
            _gui_instance.log(message)
        else:
            print(message, flush=True)

    def log_message(self, fmt, *args):
        # Kept for BaseHTTPRequestHandler compatibility, but request
        # logging itself now goes through log_request() below instead,
        # so it can redact query strings before this ever gets called.
        self._log("%s - %s" % (self.address_string(), fmt % args))

    def log_request(self, code="-", size="-"):
        # Overridden rather than relying on the default (which logs
        # self.requestline verbatim) specifically to strip the query
        # string -- notebook and note names travel as query parameters
        # (e.g. "?notebook=Secrets&name=diary.txt"), and logging them
        # in plain text would defeat the point of hiding something if
        # anyone with log access could just read the name there
        # instead. The endpoint path itself, method, and response code
        # still get logged -- that's the operationally useful part.
        if isinstance(code, http.HTTPStatus):
            code = code.value
        path_without_query = self.path.split("?", 1)[0]
        self._log(
            '%s - "%s %s %s" %s %s'
            % (self.address_string(), self.command, path_without_query, self.request_version, code, size)
        )

    def setup(self):
        super().setup()
        self._log(f"Connection opened: {self.client_address[0]}:{self.client_address[1]}")

    def finish(self):
        try:
            super().finish()
        finally:
            self._log(f"Connection closed: {self.client_address[0]}:{self.client_address[1]}")

    # -- low level helpers -------------------------------------------------

    def _send_json(self, status: int, data: dict, extra_headers=None) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        raw = self._read_raw_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _read_raw_body(self) -> bytes:
        # Some clients/proxies -- in practice, Cloudflare Tunnel among
        # them -- send Transfer-Encoding: chunked instead of a
        # Content-Length header, even for requests whose original
        # sender did include one; a reverse proxy is free to re-encode
        # in transit. Reading only by Content-Length, as this used to,
        # silently leaves a chunked body's actual bytes sitting unread
        # on the socket -- which then get misparsed as the start of
        # the *next* request on a keep-alive connection, corrupting
        # that connection's framing entirely. That's serious enough
        # (garbled responses, or the connection eventually getting
        # killed mid-stream with unread data still buffered -- which
        # is exactly what produces a client-visible "connection reset
        # by peer") that both encodings need to be handled correctly.
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._read_chunked_body()
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _read_chunked_body(self) -> bytes:
        chunks = []
        while True:
            size_line = self.rfile.readline(65536 + 2).strip()
            if b";" in size_line:  # chunk extensions -- rare, safe to ignore
                size_line = size_line.split(b";", 1)[0]
            try:
                chunk_size = int(size_line, 16)
            except ValueError:
                break  # malformed -- stop rather than loop forever
            if chunk_size == 0:
                # Trailing headers (if any), then the final blank line.
                while True:
                    line = self.rfile.readline(65536 + 2)
                    if line in (b"\r\n", b"\n", b""):
                        break
                break
            chunks.append(self.rfile.read(chunk_size))
            self.rfile.read(2)  # each chunk is followed by a CRLF
        return b"".join(chunks)

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    # -- sessions ------------------------------------------------------

    def _get_cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _current_username(self):
        """Returns the lowercased username key for the current session,
        or None if not authenticated (also prunes expired sessions)."""
        token = self._get_cookie(SESSION_COOKIE)
        if not token:
            return None
        with DATA_LOCK:
            session = SESSIONS.get(token)
            if not session:
                return None
            if time.time() - session["seen"] > SESSION_TTL_SECONDS:
                del SESSIONS[token]
                return None
            session["seen"] = time.time()
            return session["username"]

    def _require_auth(self):
        username = self._current_username()
        if not username:
            self._send_json(401, {"error": "Not signed in"})
            return None
        return username

    def _set_session_cookie(self, token):
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}",
        )

    def _clear_session_cookie(self):
        self.send_header(
            "Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"
        )

    # -- routing ---------------------------------------------------------

    def _origin_is_trusted(self) -> bool:
        """Best-effort CSRF defense in depth for state-changing requests.
        SameSite=Lax on the session cookie already blocks the classic
        cross-site form-POST attack in any modern browser; this is a
        second, independent check using the Origin/Referer header, for
        older or unusual clients. Requests with neither header (many
        legitimate non-browser API clients, and curl by default, omit
        both) are allowed through unchanged -- this is one layer, not
        the only one."""
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not source:
            return True
        host = self.headers.get("Host", "")
        try:
            source_host = urllib.parse.urlparse(source).netloc
        except ValueError:
            return False
        return source_host == host

    def do_GET(self):
        path, query = self._query()
        if path.startswith("/shared/"):
            self._h_serve_shared(path)
            return
        routes = {
            "/api/me": self._h_me,
            "/api/notebooks": self._h_list_notebooks,
            "/api/notes": self._h_list_notes,
            "/api/notes/content": self._h_note_content,
            "/api/search": self._h_search,
            "/api/legacy-files": self._h_legacy_files,
            "/api/pin/status": self._h_pin_status,
            "/api/settings": self._h_get_settings,
            "/api/shares": self._h_list_shares,
        }
        if path in routes:
            routes[path](query)
            return
        super().do_GET()

    def do_POST(self):
        if not self._origin_is_trusted():
            self._send_json(403, {"error": "Cross-site request blocked"})
            return
        path, query = self._query()
        routes = {
            "/api/register": self._h_register,
            "/api/login": self._h_login,
            "/api/logout": self._h_logout,
            "/api/password/change": self._h_change_password,
            "/api/admin/reset-password": self._h_admin_reset_password,
            "/api/pin/set": self._h_pin_set,
            "/api/pin/verify": self._h_pin_verify,
            "/api/notebooks": self._h_create_notebook,
            "/api/notebooks/rename": self._h_rename_notebook,
            "/api/notebooks/hide": self._h_hide_notebook,
            "/api/notebooks/reorder": self._h_reorder_notebooks,
            "/api/notes/rename": self._h_rename_note,
            "/api/notes/hide": self._h_hide_note,
            "/api/notes/reorder": self._h_reorder_notes,
            "/api/legacy-import": self._h_legacy_import,
            "/api/settings": self._h_post_settings,
            "/api/share": self._h_create_share,
            "/api/share/sync": self._h_sync_share,
            "/api/transcribe": self._h_transcribe,
        }
        if path == "/api/notes/save":
            self._h_save_note(query)
            return
        if path in routes:
            routes[path](query)
            return
        self._send_json(404, {"error": "Unknown endpoint: %s" % path})

    def do_DELETE(self):
        if not self._origin_is_trusted():
            self._send_json(403, {"error": "Cross-site request blocked"})
            return
        path, query = self._query()
        if path == "/api/notebooks":
            self._h_delete_notebook(query)
        elif path == "/api/notes":
            self._h_delete_note(query)
        elif path == "/api/share":
            self._h_delete_share(query)
        else:
            self._send_json(404, {"error": "Unknown endpoint: %s" % path})

    # -- accounts ----------------------------------------------------------

    def _h_register(self, query):
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not _valid_username(username):
            self._send_json(400, {"error": "Username must be 3-32 characters: letters, numbers, - or _"})
            return
        if len(password) < MIN_PASSWORD_LENGTH:
            self._send_json(400, {"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters"})
            return

        key = username.lower()
        with DATA_LOCK:
            users = _load_users()
            if key in users:
                self._send_json(409, {"error": "That username is already taken"})
                return
            digest, salt, iterations = _hash_secret(password)
            users[key] = {
                "display": username,
                "pw_hash": digest,
                "pw_salt": salt,
                "pw_iterations": iterations,
                "pin_hash": None,
                "pin_salt": None,
                "pin_iterations": None,
                "created": _iso_now(),
            }
            _save_users(users)
            os.makedirs(_user_dir(key), exist_ok=True)
            _save_notebooks(key, {})

            token = secrets.token_hex(32)
            SESSIONS[token] = {"username": key, "created": time.time(), "seen": time.time()}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._set_session_cookie(token)
        body_out = json.dumps({"username": username}).encode("utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _h_login(self, query):
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        key = username.lower()

        with DATA_LOCK:
            attempt = LOGIN_ATTEMPTS.setdefault(key, {"count": 0, "locked_until": 0})
            if time.time() < attempt["locked_until"]:
                remaining = int(attempt["locked_until"] - time.time())
                self._send_json(429, {"error": f"Too many attempts -- try again in {remaining}s"})
                return

            users = _load_users()
            user = users.get(key)
            ok = user and _verify_secret(password, user["pw_hash"], user["pw_salt"], user.get("pw_iterations"))
            if not ok:
                attempt["count"] += 1
                if attempt["count"] >= LOGIN_MAX_ATTEMPTS:
                    attempt["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
                    attempt["count"] = 0
                self._send_json(401, {"error": "Incorrect username or password"})
                return
            attempt["count"] = 0
            token = secrets.token_hex(32)
            SESSIONS[token] = {"username": key, "created": time.time(), "seen": time.time()}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._set_session_cookie(token)
        body_out = json.dumps({"username": user["display"]}).encode("utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _h_logout(self, query):
        token = self._get_cookie(SESSION_COOKIE)
        with DATA_LOCK:
            if token and token in SESSIONS:
                del SESSIONS[token]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._clear_session_cookie()
        body_out = b'{"status": "ok"}'
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def _h_me(self, query):
        key = self._current_username()
        if not key:
            self._send_json(401, {"error": "Not signed in"})
            return
        with DATA_LOCK:
            users = _load_users()
        user = users.get(key, {})
        self._send_json(200, {"username": user.get("display", key)})

    def _h_change_password(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        current = body.get("current_password") or ""
        new = body.get("new_password") or ""
        if len(new) < MIN_PASSWORD_LENGTH:
            self._send_json(400, {"error": f"New password must be at least {MIN_PASSWORD_LENGTH} characters"})
            return

        with DATA_LOCK:
            users = _load_users()
            user = users.get(key)
            if not user or not _verify_secret(current, user["pw_hash"], user["pw_salt"], user.get("pw_iterations")):
                self._send_json(401, {"error": "Incorrect current password"})
                return
            digest, salt, iterations = _hash_secret(new)
            user["pw_hash"] = digest
            user["pw_salt"] = salt
            user["pw_iterations"] = iterations
            _save_users(users)
        self._send_json(200, {"status": "ok"})

    def _h_admin_reset_password(self, query):
        """Recovery path for a forgotten password -- does not require an
        existing session, since the whole point is that the account is
        locked out. Gated by a separate admin password configured only
        by whoever runs the server (INKWELL_ADMIN_PASSWORD /
        INKWELL_ADMIN_PASSWORD_FILE), not tied to any one user account.
        Resetting a password also invalidates every existing session for
        that account, in case the reset is happening because the
        account was compromised."""
        if not ADMIN_PASSWORD:
            self._send_json(503, {"error": "Admin password reset isn't configured on this server"})
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        client_ip = self.client_address[0]
        with DATA_LOCK:
            attempt = ADMIN_ATTEMPTS.setdefault(client_ip, {"count": 0, "locked_until": 0})
            if time.time() < attempt["locked_until"]:
                remaining = int(attempt["locked_until"] - time.time())
                self._send_json(429, {"error": f"Too many attempts -- try again in {remaining}s"})
                return

            admin_password = body.get("admin_password") or ""
            admin_ok = secrets.compare_digest(admin_password, ADMIN_PASSWORD)
            if not admin_ok:
                attempt["count"] += 1
                if attempt["count"] >= ADMIN_MAX_ATTEMPTS:
                    attempt["locked_until"] = time.time() + ADMIN_LOCKOUT_SECONDS
                    attempt["count"] = 0
                self._send_json(401, {"error": "Incorrect admin password"})
                return
            attempt["count"] = 0

            username = (body.get("username") or "").strip()
            new_password = body.get("new_password") or ""
            key = username.lower()
            if len(new_password) < MIN_PASSWORD_LENGTH:
                self._send_json(400, {"error": f"New password must be at least {MIN_PASSWORD_LENGTH} characters"})
                return

            users = _load_users()
            user = users.get(key)
            if not user:
                self._send_json(404, {"error": "No account with that username"})
                return
            digest, salt, iterations = _hash_secret(new_password)
            user["pw_hash"] = digest
            user["pw_salt"] = salt
            user["pw_iterations"] = iterations
            _save_users(users)

            for token in [t for t, s in SESSIONS.items() if s["username"] == key]:
                del SESSIONS[token]

        self._send_json(200, {"status": "ok"})

    # -- PIN (gates *revealing* hidden items -- hiding itself needs no PIN) -

    def _h_pin_status(self, query):
        key = self._require_auth()
        if not key:
            return
        with DATA_LOCK:
            users = _load_users()
        has_pin = bool(users.get(key, {}).get("pin_hash"))
        self._send_json(200, {"has_pin": has_pin})

    def _h_pin_set(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        pin = str(body.get("pin") or "")
        password = body.get("password") or ""
        if not re.match(r"^\d{4}$", pin):
            self._send_json(400, {"error": "PIN must be exactly 4 digits"})
            return

        with DATA_LOCK:
            users = _load_users()
            user = users.get(key)
            if not user or not _verify_secret(password, user["pw_hash"], user["pw_salt"], user.get("pw_iterations")):
                self._send_json(401, {"error": "Incorrect account password"})
                return
            digest, salt, iterations = _hash_secret(pin)
            user["pin_hash"] = digest
            user["pin_salt"] = salt
            user["pin_iterations"] = iterations
            _save_users(users)
        self._send_json(200, {"status": "ok"})

    def _h_pin_verify(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        pin = str(body.get("pin") or "")

        with DATA_LOCK:
            attempt = PIN_ATTEMPTS.setdefault(key, {"count": 0, "locked_until": 0})
            if time.time() < attempt["locked_until"]:
                remaining = int(attempt["locked_until"] - time.time())
                self._send_json(429, {"ok": False, "error": f"Too many attempts -- try again in {remaining}s"})
                return

            users = _load_users()
            user = users.get(key, {})
            has_pin = bool(user.get("pin_hash"))
            ok = has_pin and _verify_secret(pin, user["pin_hash"], user["pin_salt"], user.get("pin_iterations"))

            if ok:
                attempt["count"] = 0
            elif has_pin:
                attempt["count"] += 1
                if attempt["count"] >= PIN_MAX_ATTEMPTS:
                    attempt["locked_until"] = time.time() + PIN_LOCKOUT_SECONDS
                    attempt["count"] = 0

        if not has_pin:
            self._send_json(400, {"ok": False, "error": "No PIN has been set yet -- set one in Settings"})
            return
        self._send_json(200, {"ok": ok})

    # -- notebooks -----------------------------------------------------

    def _h_list_notebooks(self, query):
        key = self._require_auth()
        if not key:
            return
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if _ensure_order(notebooks):
                _save_notebooks(key, notebooks)
        out = [
            {"name": name, "hidden": bool(info.get("hidden")), "modified": info.get("modified")}
            for name, info in notebooks.items()
        ]
        out.sort(key=lambda n: (notebooks[n["name"]].get("order", 0), n["name"].lower()))
        self._send_json(200, {"notebooks": out})

    def _h_create_notebook(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        name = (body.get("name") or "").strip()
        if not _valid_name(name):
            self._send_json(400, {"error": "Invalid notebook name"})
            return
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if name in notebooks:
                self._send_json(409, {"error": "A notebook with that name already exists"})
                return
            now = _iso_now()
            notebooks[name] = {"hidden": False, "created": now, "modified": now, "order": _next_order(notebooks)}
            _save_notebooks(key, notebooks)
            os.makedirs(_notebook_dir(key, name), exist_ok=True)
        self._send_json(200, {"name": name})

    def _h_rename_notebook(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        old = (body.get("old") or "").strip()
        new = (body.get("new") or "").strip()
        if not _valid_name(new):
            self._send_json(400, {"error": "Invalid notebook name"})
            return
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if old not in notebooks:
                self._send_json(404, {"error": "Notebook not found"})
                return
            if new in notebooks and new != old:
                self._send_json(409, {"error": "A notebook with that name already exists"})
                return
            old_dir = _notebook_dir(key, old)
            new_dir = _notebook_dir(key, new)
            try:
                if old != new:
                    os.makedirs(old_dir, exist_ok=True)
                    os.rename(old_dir, new_dir)
                info = notebooks.pop(old)
                info["modified"] = _iso_now()
                notebooks[new] = info
                _save_notebooks(key, notebooks)
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return
        self._send_json(200, {"name": new})

    def _h_hide_notebook(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        name = (body.get("name") or "").strip()
        hidden = bool(body.get("hidden"))
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if name not in notebooks:
                self._send_json(404, {"error": "Notebook not found"})
                return
            notebooks[name]["hidden"] = hidden
            notebooks[name]["modified"] = _iso_now()
            _save_notebooks(key, notebooks)

            if CRYPTO_AVAILABLE:
                meta = _load_notes_meta(key, name)
                changed = False
                for filename in list(meta.keys()):
                    if _sync_note_encryption(notebooks, key, name, filename, meta):
                        changed = True
                if changed:
                    _save_notes_meta(key, name, meta)
        self._send_json(200, {"name": name, "hidden": hidden})

    def _h_reorder_notebooks(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        order = body.get("order")
        if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
            self._send_json(400, {"error": "'order' must be a list of notebook names"})
            return
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if set(order) != set(notebooks.keys()) or len(order) != len(notebooks):
                self._send_json(400, {"error": "Order list must include every notebook exactly once"})
                return
            for idx, name in enumerate(order):
                notebooks[name]["order"] = idx
            _save_notebooks(key, notebooks)
        self._send_json(200, {"status": "ok"})

    def _h_delete_notebook(self, query):
        key = self._require_auth()
        if not key:
            return
        name = (query.get("name", [""])[0] or "").strip()
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if name not in notebooks:
                self._send_json(404, {"error": "Notebook not found"})
                return
            del notebooks[name]
            _save_notebooks(key, notebooks)
            path = _notebook_dir(key, name)
            try:
                if os.path.isdir(path):
                    for root, dirs, files in os.walk(path, topdown=False):
                        for f in files:
                            os.remove(os.path.join(root, f))
                        for d in dirs:
                            os.rmdir(os.path.join(root, d))
                    os.rmdir(path)
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return
        self._send_json(200, {"status": "ok"})

    # -- notes -----------------------------------------------------------

    def _h_list_notes(self, query):
        key = self._require_auth()
        if not key:
            return
        notebook = (query.get("notebook", [""])[0] or "").strip()
        if not _valid_name(notebook):
            self._send_json(400, {"error": "Invalid notebook name"})
            return
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if notebook not in notebooks:
                self._send_json(404, {"error": "Notebook not found"})
                return
            meta = _load_notes_meta(key, notebook)
            nb_dir = _notebook_dir(key, notebook)
            try:
                on_disk = sorted(
                    f for f in os.listdir(nb_dir)
                    if os.path.isfile(os.path.join(nb_dir, f)) and f.lower().endswith(".txt")
                )
            except OSError:
                on_disk = []
            changed = False
            for f in on_disk:
                if f not in meta:
                    meta[f] = {"hidden": False, "created": _iso_now(), "modified": _iso_now(), "order": _next_order(meta)}
                    changed = True
            for f in list(meta.keys()):
                if f not in on_disk:
                    del meta[f]
                    changed = True
            if _ensure_order(meta):
                changed = True
            if CRYPTO_AVAILABLE:
                for filename in list(meta.keys()):
                    if _sync_note_encryption(notebooks, key, notebook, filename, meta):
                        changed = True
            if changed:
                _save_notes_meta(key, notebook, meta)

        out = [
            {"name": name, "hidden": bool(info.get("hidden")), "modified": info.get("modified")}
            for name, info in meta.items()
        ]
        out.sort(key=lambda n: (meta[n["name"]].get("order", 0), n["name"].lower()))
        self._send_json(200, {"notebook": notebook, "notes": out})

    def _h_note_content(self, query):
        key = self._require_auth()
        if not key:
            return
        notebook = (query.get("notebook", [""])[0] or "").strip()
        name = (query.get("name", [""])[0] or "").strip()
        if not _valid_name(notebook) or not name:
            self._send_json(400, {"error": "Invalid notebook or note name"})
            return
        filename = os.path.basename(_note_filename(name))
        path = os.path.join(_notebook_dir(key, notebook), filename)
        if not os.path.isfile(path):
            self._send_json(404, {"error": "Note not found"})
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send_json(500, {"error": str(e)})
            return

        with DATA_LOCK:
            meta = _load_notes_meta(key, notebook)
        if meta.get(filename, {}).get("encrypted"):
            if not ENCRYPTION_KEY_BYTES:
                self._send_json(500, {
                    "error": "This note is encrypted at rest, but no encryption key is configured "
                             "to decrypt it -- check INKWELL_ENCRYPTION_KEY on the server."
                })
                return
            try:
                data = _decrypt_bytes(data)
            except ValueError:
                self._send_json(500, {
                    "error": "Couldn't decrypt this note -- the configured encryption key doesn't match "
                             "the one it was encrypted with."
                })
                return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _h_save_note(self, query):
        key = self._require_auth()
        if not key:
            return
        notebook = (query.get("notebook", [""])[0] or "").strip()
        name = (query.get("name", [""])[0] or "").strip()
        if not _valid_name(notebook) or not name:
            self._send_json(400, {"error": "Invalid notebook or note name"})
            return
        filename = os.path.basename(_note_filename(name))
        body = self._read_raw_body()

        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            if notebook not in notebooks:
                now = _iso_now()
                notebooks[notebook] = {"hidden": False, "created": now, "modified": now, "order": _next_order(notebooks)}
                _save_notebooks(key, notebooks)
            nb_dir = _notebook_dir(key, notebook)
            os.makedirs(nb_dir, exist_ok=True)
            path = os.path.join(nb_dir, filename)

            meta = _load_notes_meta(key, notebook)
            existing_info = meta.get(filename, {})
            will_be_encrypted = bool(ENCRYPTION_KEY_BYTES) and _effective_hidden(notebooks, notebook, existing_info)
            to_write = _encrypt_bytes(body) if will_be_encrypted else body

            try:
                with open(path, "wb") as f:
                    f.write(to_write)
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return

            now = _iso_now()
            if filename not in meta:
                meta[filename] = {
                    "hidden": False, "created": now, "modified": now,
                    "order": _next_order(meta), "encrypted": will_be_encrypted,
                }
            else:
                meta[filename]["modified"] = now
                meta[filename]["encrypted"] = will_be_encrypted
            _save_notes_meta(key, notebook, meta)
            notebooks[notebook]["modified"] = now
            _save_notebooks(key, notebooks)

        self._send_json(200, {"status": "ok", "notebook": notebook, "name": filename})

    def _h_rename_note(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        notebook = (body.get("notebook") or "").strip()
        old = os.path.basename(_note_filename((body.get("old") or "").strip()))
        new = os.path.basename(_note_filename((body.get("new") or "").strip()))
        if not _valid_name(notebook) or not old or not new:
            self._send_json(400, {"error": "Invalid request"})
            return
        with DATA_LOCK:
            meta = _load_notes_meta(key, notebook)
            if old not in meta:
                self._send_json(404, {"error": "Note not found"})
                return
            if new in meta and new != old:
                self._send_json(409, {"error": "A note with that name already exists"})
                return
            old_path = os.path.join(_notebook_dir(key, notebook), old)
            new_path = os.path.join(_notebook_dir(key, notebook), new)
            try:
                if old != new:
                    os.rename(old_path, new_path)
                info = meta.pop(old)
                info["modified"] = _iso_now()
                meta[new] = info
                _save_notes_meta(key, notebook, meta)
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return
        self._send_json(200, {"name": new})

    def _h_hide_note(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        notebook = (body.get("notebook") or "").strip()
        name = os.path.basename(_note_filename((body.get("name") or "").strip()))
        hidden = bool(body.get("hidden"))
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            meta = _load_notes_meta(key, notebook)
            if name not in meta:
                self._send_json(404, {"error": "Note not found"})
                return
            meta[name]["hidden"] = hidden
            meta[name]["modified"] = _iso_now()
            if CRYPTO_AVAILABLE:
                _sync_note_encryption(notebooks, key, notebook, name, meta)
            _save_notes_meta(key, notebook, meta)
        self._send_json(200, {"name": name, "hidden": hidden})

    def _h_reorder_notes(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        notebook = (body.get("notebook") or "").strip()
        order = body.get("order")
        if not _valid_name(notebook):
            self._send_json(400, {"error": "Invalid notebook name"})
            return
        if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
            self._send_json(400, {"error": "'order' must be a list of note names"})
            return
        order = [os.path.basename(_note_filename(n)) for n in order]
        with DATA_LOCK:
            meta = _load_notes_meta(key, notebook)
            if set(order) != set(meta.keys()) or len(order) != len(meta):
                self._send_json(400, {"error": "Order list must include every note exactly once"})
                return
            for idx, name in enumerate(order):
                meta[name]["order"] = idx
            _save_notes_meta(key, notebook, meta)
        self._send_json(200, {"status": "ok"})

    def _h_delete_note(self, query):
        key = self._require_auth()
        if not key:
            return
        notebook = (query.get("notebook", [""])[0] or "").strip()
        name = os.path.basename(_note_filename((query.get("name", [""])[0] or "").strip()))
        with DATA_LOCK:
            meta = _load_notes_meta(key, notebook)
            if name not in meta:
                self._send_json(404, {"error": "Note not found"})
                return
            path = os.path.join(_notebook_dir(key, notebook), name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return
            del meta[name]
            _save_notes_meta(key, notebook, meta)
        self._send_json(200, {"status": "ok"})

    # -- search (used by the "reveal hidden" tree search, but also matches
    #    plain visible items -- the hidden/PIN gating happens client-side) -

    def _h_search(self, query):
        key = self._require_auth()
        if not key:
            return
        q = (query.get("q", [""])[0] or "").strip().lower()
        if len(q) < 1:
            self._send_json(200, {"notebooks": [], "notes": []})
            return
        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            matched_notebooks = [
                {"name": n, "hidden": bool(i.get("hidden"))}
                for n, i in notebooks.items() if q in n.lower()
            ]
            matched_notes = []
            for nb_name in notebooks:
                meta = _load_notes_meta(key, nb_name)
                for note_name, info in meta.items():
                    if q in note_name.lower():
                        matched_notes.append({
                            "notebook": nb_name,
                            "name": note_name,
                            "hidden": bool(info.get("hidden")) or bool(notebooks[nb_name].get("hidden")),
                        })
        matched_notebooks.sort(key=lambda n: n["name"].lower())
        matched_notes.sort(key=lambda n: n["name"].lower())
        self._send_json(200, {"notebooks": matched_notebooks, "notes": matched_notes})

    # -- legacy flat-file import ------------------------------------------

    def _h_legacy_files(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            names = sorted(
                f for f in os.listdir(WORKSPACE_DIR)
                if os.path.isfile(os.path.join(WORKSPACE_DIR, f))
                and not f.startswith(".")
                and f.lower().endswith(".txt")
            )
        except OSError:
            names = []
        self._send_json(200, {"files": names})

    def _h_legacy_import(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        notebook = (body.get("notebook") or "").strip()
        filenames = body.get("filenames") or []
        if not _valid_name(notebook) or not isinstance(filenames, list):
            self._send_json(400, {"error": "Invalid request"})
            return

        with DATA_LOCK:
            notebooks = _load_notebooks(key)
            now = _iso_now()
            if notebook not in notebooks:
                notebooks[notebook] = {"hidden": False, "created": now, "modified": now, "order": _next_order(notebooks)}
            nb_dir = _notebook_dir(key, notebook)
            os.makedirs(nb_dir, exist_ok=True)
            meta = _load_notes_meta(key, notebook)
            imported = []
            for raw_name in filenames:
                src_name = os.path.basename(str(raw_name))
                src_path = os.path.join(WORKSPACE_DIR, src_name)
                if not os.path.isfile(src_path) or not src_name.lower().endswith(".txt"):
                    continue
                dest_name = src_name
                dest_path = os.path.join(nb_dir, dest_name)
                counter = 1
                base, ext = os.path.splitext(dest_name)
                while os.path.exists(dest_path):
                    dest_name = f"{base} ({counter}){ext}"
                    dest_path = os.path.join(nb_dir, dest_name)
                    counter += 1
                try:
                    os.replace(src_path, dest_path)
                except OSError:
                    continue
                meta[dest_name] = {"hidden": False, "created": now, "modified": now, "order": _next_order(meta)}
                imported.append(dest_name)
            if CRYPTO_AVAILABLE:
                for dest_name in imported:
                    _sync_note_encryption(notebooks, key, notebook, dest_name, meta)
            _save_notes_meta(key, notebook, meta)
            notebooks[notebook]["modified"] = now
            _save_notebooks(key, notebooks)

        self._send_json(200, {"notebook": notebook, "imported": imported})

    # -- per-user editor settings ------------------------------------------

    def _settings_path(self, key):
        return os.path.join(WORKSPACE_DIR, "settings", f"{key}.json")

    def _h_get_settings(self, query):
        key = self._require_auth()
        if not key:
            return
        path = self._settings_path(key)
        if not os.path.isfile(path):
            self._send_json(200, {})
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
        except OSError as e:
            self._send_json(500, {"error": str(e)})

    def _h_post_settings(self, query):
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_raw_body()
            json.loads(body.decode("utf-8"))  # validate before writing
            os.makedirs(os.path.join(WORKSPACE_DIR, "settings"), exist_ok=True)
            with open(self._settings_path(key), "wb") as f:
                f.write(body)
            self._send_json(200, {"status": "ok"})
        except (OSError, ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": "Invalid settings data: %s" % e})

    # -- sharing -------------------------------------------------------
    #
    # A share is a fully self-contained static HTML page, built
    # client-side (it already has the Markdown renderer needed for
    # this, so there's no reason to duplicate that logic here) and
    # handed to the server purely to store and serve back out under a
    # random, unguessable token -- e.g. /shared/<username>/<token>.
    # Reading a share needs no login at all, by design (that's the
    # point of a share link); the token itself is what makes it only
    # reachable by whoever has the link, not who's signed in.
    #
    # Whether something is eligible to share in the first place (i.e.
    # not hidden) is enforced client-side, same as hiding itself is
    # everywhere else in this app -- a declutter/access convenience,
    # not a hard security boundary.
    #
    # A small per-user registry (shared/<username>/shares.json) tracks
    # which notebook/note currently has an active share and under what
    # token, so that:
    #   - sharing something a second time reuses the same URL instead
    #     of minting a new one and orphaning the old link, and
    #   - saving a note that's currently shared can silently push the
    #     update to its existing link (POST /api/share/sync) without
    #     the person needing to re-share it by hand every time.
    # Each entry also carries an "expires_days" field, always present
    # but unused for now (nothing currently reads it to auto-expire
    # anything) -- there deliberately so that adding real expiration
    # later is a small, additive change rather than a data migration.
    # _h_serve_shared already honors it if it's ever set by hand or by
    # a future UI, treating an expired share as if it doesn't exist.

    MAX_SHARE_HTML_BYTES = 5_000_000

    def _shares_path(self, key: str) -> str:
        return os.path.join(WORKSPACE_DIR, "shared", key, "shares.json")

    def _load_shares(self, key: str) -> dict:
        path = self._shares_path(key)
        if not os.path.isfile(path):
            return {"notes": {}, "notebooks": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {"notes": {}, "notebooks": {}}
        data.setdefault("notes", {})
        data.setdefault("notebooks", {})
        return data

    def _save_shares(self, key: str, data: dict) -> None:
        shared_dir = os.path.join(WORKSPACE_DIR, "shared", key)
        os.makedirs(shared_dir, exist_ok=True)
        path = self._shares_path(key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def _write_share_file(self, key: str, token: str, encoded: bytes) -> None:
        shared_dir = os.path.join(WORKSPACE_DIR, "shared", key)
        os.makedirs(shared_dir, exist_ok=True)
        with open(os.path.join(shared_dir, f"{token}.html"), "wb") as f:
            f.write(encoded)

    # -- speech-to-text --------------------------------------------------

    MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25MB -- matches common provider limits

    def _h_transcribe(self, query):
        key = self._require_auth()
        if not key:
            return
        if STT_PROVIDER == "disabled":
            self._send_json(503, {"error": "Speech-to-text isn't configured on this server"})
            return

        audio_bytes = self._read_raw_body()
        if not audio_bytes:
            self._send_json(400, {"error": "No audio data received"})
            return
        if len(audio_bytes) > self.MAX_AUDIO_BYTES:
            self._send_json(400, {"error": "Audio clip is too large"})
            return

        try:
            if STT_PROVIDER == "local":
                if not FASTER_WHISPER_AVAILABLE:
                    self._send_json(503, {
                        "error": "Local speech-to-text is configured but the 'faster-whisper' "
                                 "package isn't installed on the server"
                    })
                    return
                text = _transcribe_local(audio_bytes)
            elif STT_PROVIDER == "openai":
                text = _transcribe_openai_compatible(
                    audio_bytes, "https://api.openai.com/v1", STT_API_KEY, "whisper-1"
                )
            elif STT_PROVIDER == "groq":
                text = _transcribe_openai_compatible(
                    audio_bytes, "https://api.groq.com/openai/v1", STT_API_KEY, "whisper-large-v3-turbo"
                )
            elif STT_PROVIDER == "google":
                text = _transcribe_google(audio_bytes, STT_API_KEY)
            elif STT_PROVIDER == "custom":
                if not STT_CUSTOM_URL:
                    self._send_json(503, {"error": "INKWELL_STT_URL isn't set for the 'custom' provider"})
                    return
                text = _transcribe_openai_compatible(
                    audio_bytes, STT_CUSTOM_URL.rstrip("/"), STT_API_KEY, STT_CUSTOM_MODEL_NAME
                )
            else:
                self._send_json(500, {"error": f"Unknown STT provider configured: {STT_PROVIDER}"})
                return
        except urllib.error.HTTPError as e:
            self._send_json(502, {"error": f"Speech-to-text provider returned an error (HTTP {e.code})"})
            return
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            self._send_json(502, {"error": f"Couldn't reach the speech-to-text provider: {e}"})
            return
        except Exception as e:  # noqa: BLE001 -- transcription libraries raise all sorts; never 500 with a stack trace leak
            self._send_json(500, {"error": f"Transcription failed: {e}"})
            return

        self._send_json(200, {"text": text})

    def _h_create_share(self, query):
        """Explicit "Share" action -- creates a new share, or reuses
        (and overwrites the content of) an existing one for the same
        notebook/note, so repeated sharing never orphans an old link."""
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        kind = body.get("kind")
        notebook = (body.get("notebook") or "").strip()
        name = (body.get("name") or "").strip() if kind == "note" else None
        html = body.get("html")
        if kind not in ("note", "notebook") or not notebook or (kind == "note" and not name):
            self._send_json(400, {"error": "Invalid share request"})
            return
        if not isinstance(html, str) or not html.strip():
            self._send_json(400, {"error": "Missing HTML content to share"})
            return
        encoded = html.encode("utf-8")
        if len(encoded) > self.MAX_SHARE_HTML_BYTES:
            self._send_json(400, {"error": "That's too large to share"})
            return

        with DATA_LOCK:
            shares = self._load_shares(key)
            bucket = shares["notes"] if kind == "note" else shares["notebooks"]
            share_key = f"{notebook}/{name}" if kind == "note" else notebook
            entry = bucket.get(share_key)
            now = _iso_now()
            if entry:
                token = entry["token"]
                entry["updated"] = now
            else:
                token = secrets.token_urlsafe(16)
                entry = {"token": token, "notebook": notebook, "created": now, "updated": now, "expires_days": None}
                if kind == "note":
                    entry["name"] = name
                bucket[share_key] = entry
            try:
                self._write_share_file(key, token, encoded)
                self._save_shares(key, shares)
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return

        self._send_json(200, {"token": token, "path": f"/shared/{key}/{token}"})

    def _h_sync_share(self, query):
        """Silent background sync, called after every note save --
        updates that note's existing share in place if (and only if)
        one already exists; never creates a new share on its own, so
        saving a note never shares something that wasn't explicitly
        shared to begin with."""
        key = self._require_auth()
        if not key:
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        notebook = (body.get("notebook") or "").strip()
        name = (body.get("name") or "").strip()
        html = body.get("html")
        if not notebook or not name or not isinstance(html, str):
            self._send_json(400, {"error": "Invalid request"})
            return
        encoded = html.encode("utf-8")
        if len(encoded) > self.MAX_SHARE_HTML_BYTES:
            self._send_json(400, {"error": "That's too large to share"})
            return

        with DATA_LOCK:
            shares = self._load_shares(key)
            entry = shares["notes"].get(f"{notebook}/{name}")
            if not entry:
                self._send_json(200, {"updated": False})
                return
            token = entry["token"]
            entry["updated"] = _iso_now()
            try:
                self._write_share_file(key, token, encoded)
                self._save_shares(key, shares)
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return

        self._send_json(200, {"updated": True, "token": token})

    def _h_list_shares(self, query):
        key = self._require_auth()
        if not key:
            return
        with DATA_LOCK:
            shares = self._load_shares(key)
        # Grouped by notebook rather than a flat most-recent list --
        # each notebook's own index share (if it has one) comes first
        # within its group, followed by its individual note shares, so
        # the cascading relationship (revoking the notebook takes its
        # notes with it) reads clearly in the UI. Groups themselves are
        # ordered by whichever share in them was touched most recently.
        notebooks_by_name = {}
        for entry in shares["notebooks"].values():
            notebooks_by_name[entry.get("notebook")] = {
                "kind": "notebook", "notebook": entry.get("notebook"),
                "path": f"/shared/{key}/{entry['token']}",
                "created": entry.get("created"), "updated": entry.get("updated"),
                "expires_days": entry.get("expires_days"),
            }
        notes_by_notebook = {}
        for entry in shares["notes"].values():
            notes_by_notebook.setdefault(entry.get("notebook"), []).append({
                "kind": "note", "notebook": entry.get("notebook"), "name": entry.get("name"),
                "path": f"/shared/{key}/{entry['token']}",
                "created": entry.get("created"), "updated": entry.get("updated"),
                "expires_days": entry.get("expires_days"),
            })

        all_notebook_names = set(notebooks_by_name) | set(notes_by_notebook)

        def group_sort_key(nb_name):
            candidates = [notebooks_by_name[nb_name]["updated"]] if nb_name in notebooks_by_name else []
            candidates += [n["updated"] for n in notes_by_notebook.get(nb_name, [])]
            return max((c or "" for c in candidates), default="")

        out = []
        for nb_name in sorted(all_notebook_names, key=group_sort_key, reverse=True):
            if nb_name in notebooks_by_name:
                out.append(notebooks_by_name[nb_name])
            out.extend(sorted(notes_by_notebook.get(nb_name, []), key=lambda n: n.get("updated") or "", reverse=True))

        self._send_json(200, {"shares": out})

    def _h_delete_share(self, query):
        """Revoking a note's share removes just that one. Revoking a
        notebook's share cascades: it also removes every individual
        note-share under that notebook, not just the index page --
        otherwise those note pages would stay live and reachable with
        nothing pointing at them anymore, silently orphaned rather
        than actually taken down. Revoking one specific note's share
        never touches the notebook's own index share or any other
        note's, even if that note happens to be part of a shared
        notebook -- the two levels are independently revokable."""
        key = self._require_auth()
        if not key:
            return
        kind = (query.get("kind", [""])[0] or "").strip()
        notebook = (query.get("notebook", [""])[0] or "").strip()
        name = (query.get("name", [""])[0] or "").strip()
        if kind not in ("note", "notebook") or not notebook or (kind == "note" and not name):
            self._send_json(400, {"error": "Invalid request"})
            return

        with DATA_LOCK:
            shares = self._load_shares(key)
            removed_tokens = []

            if kind == "note":
                entry = shares["notes"].pop(f"{notebook}/{name}", None)
                if not entry:
                    self._send_json(404, {"error": "Share not found"})
                    return
                removed_tokens.append(entry["token"])
            else:
                entry = shares["notebooks"].pop(notebook, None)
                if not entry:
                    self._send_json(404, {"error": "Share not found"})
                    return
                removed_tokens.append(entry["token"])
                for share_key in list(shares["notes"].keys()):
                    note_entry = shares["notes"][share_key]
                    if note_entry.get("notebook") == notebook:
                        removed_tokens.append(note_entry["token"])
                        del shares["notes"][share_key]

            self._save_shares(key, shares)
            for token in removed_tokens:
                file_path = os.path.join(WORKSPACE_DIR, "shared", key, f"{token}.html")
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                except OSError:
                    pass  # registry entry is already gone -- a leftover file isn't harmful

        self._send_json(200, {"status": "ok", "removed": len(removed_tokens)})

    @staticmethod
    def _find_share_by_token(shares: dict, token: str):
        for bucket in (shares["notes"], shares["notebooks"]):
            for entry in bucket.values():
                if entry.get("token") == token:
                    return entry
        return None

    def _h_serve_shared(self, path):
        parts = path.split("/")
        if len(parts) != 4:
            self._send_json(404, {"error": "Not found"})
            return
        _, _, username, token = parts
        username_key = username.lower()
        if not USERNAME_RE.match(username_key) or not re.match(r"^[A-Za-z0-9_\-]{1,64}$", token or ""):
            self._send_json(404, {"error": "Not found"})
            return

        file_path = os.path.join(WORKSPACE_DIR, "shared", username_key, f"{token}.html")
        if not os.path.isfile(file_path):
            self._send_json(404, {"error": "Not found"})
            return

        with DATA_LOCK:
            shares = self._load_shares(username_key)
        entry = self._find_share_by_token(shares, token)
        if entry and entry.get("expires_days"):
            try:
                created_epoch = calendar.timegm(time.strptime(entry["created"], "%Y-%m-%dT%H:%M:%SZ"))
                if time.time() > created_epoch + entry["expires_days"] * 86400:
                    self._send_json(404, {"error": "Not found"})
                    return
            except (ValueError, TypeError, KeyError):
                pass  # malformed stored data -- fail open rather than break an otherwise-valid share

        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send_json(500, {"error": str(e)})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_gui_instance = None  # set by ServerGUI so EditorRequestHandler.log_message can reach it


def _read_env_or_file(env_name: str) -> str | None:
    """Reads a config value either directly from an environment
    variable, or from the file named by '<env_name>_FILE' (Docker
    secrets convention -- lets the actual secret live in a mounted
    file instead of an env var, which shows up in `docker inspect` and
    process listings). The _FILE variant wins if both are set."""
    file_path = os.environ.get(f"{env_name}_FILE")
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            print(f"Couldn't read {env_name}_FILE ({file_path}): {e}", file=sys.stderr)
            return None
    value = os.environ.get(env_name)
    return value.strip() if value else None


def _load_admin_password() -> None:
    global ADMIN_PASSWORD
    value = _read_env_or_file("INKWELL_ADMIN_PASSWORD")
    ADMIN_PASSWORD = value or None
    if ADMIN_PASSWORD and len(ADMIN_PASSWORD) < 12:
        print(
            "Warning: INKWELL_ADMIN_PASSWORD is shorter than 12 characters. "
            "This password can reset any user's password -- use a long, random one.",
            file=sys.stderr,
        )


def _load_encryption_key() -> None:
    global ENCRYPTION_KEY_BYTES
    passphrase = _read_env_or_file("INKWELL_ENCRYPTION_KEY")
    if not passphrase:
        ENCRYPTION_KEY_BYTES = None
        return
    if not CRYPTO_AVAILABLE:
        print(
            "INKWELL_ENCRYPTION_KEY is set, but the 'cryptography' package isn't installed. "
            "Install it (pip install cryptography) or unset the key. Refusing to start, "
            "rather than silently storing hidden notes as plain text.",
            file=sys.stderr,
        )
        sys.exit(1)
    # A length check is a crude proxy for entropy, not a real measurement
    # of it -- but it catches the common mistake of typing something
    # short/memorable here, when what actually matters is a truly random
    # value at least as long as what `openssl rand -base64 32` produces
    # (44 characters). PBKDF2 always derives a fixed 256-bit AES key from
    # this regardless of input length, but it can't manufacture entropy
    # that wasn't in the input -- a short/guessable passphrase still
    # yields a weak key no matter how many iterations stretch it.
    if len(passphrase) < 32:
        print(
            "Warning: INKWELL_ENCRYPTION_KEY is shorter than 32 characters. "
            "Use a real random value, e.g. the output of `openssl rand -base64 32` "
            "(44 characters) -- not something typed/memorable.",
            file=sys.stderr,
        )
    ENCRYPTION_KEY_BYTES = _derive_encryption_key(passphrase)


def _load_stt_config() -> None:
    global STT_PROVIDER, STT_API_KEY, STT_LOCAL_MODEL_SIZE, STT_CUSTOM_URL, STT_CUSTOM_MODEL_NAME
    provider = (os.environ.get("INKWELL_STT_PROVIDER") or "disabled").strip().lower()
    if provider not in ("disabled", "local", "openai", "groq", "google", "custom"):
        print(
            f"Warning: unknown INKWELL_STT_PROVIDER '{provider}' -- must be "
            "disabled/local/openai/groq/google/custom. Speech-to-text disabled.",
            file=sys.stderr,
        )
        provider = "disabled"
    STT_PROVIDER = provider
    STT_LOCAL_MODEL_SIZE = (os.environ.get("INKWELL_STT_LOCAL_MODEL") or "base").strip()
    STT_API_KEY = _read_env_or_file("INKWELL_STT_API_KEY")
    STT_CUSTOM_URL = (os.environ.get("INKWELL_STT_URL") or "").strip() or None
    STT_CUSTOM_MODEL_NAME = (os.environ.get("INKWELL_STT_MODEL_NAME") or "whisper-1").strip()

    if provider == "local" and not FASTER_WHISPER_AVAILABLE:
        print(
            "INKWELL_STT_PROVIDER=local is set, but the 'faster-whisper' package isn't "
            "installed. Install it (pip install faster-whisper) or choose a different "
            "provider. POST /api/transcribe will return an error until this is fixed -- "
            "not treated as fatal at startup, unlike a missing encryption dependency, "
            "since nothing about existing data depends on this working.",
            file=sys.stderr,
        )
    if provider in ("openai", "groq", "google") and not STT_API_KEY:
        print(
            f"Warning: INKWELL_STT_PROVIDER={provider} is set but no INKWELL_STT_API_KEY "
            "(or _FILE) was provided -- speech-to-text requests will fail.",
            file=sys.stderr,
        )
    if provider == "custom":
        if not STT_CUSTOM_URL:
            print(
                "Warning: INKWELL_STT_PROVIDER=custom is set but INKWELL_STT_URL isn't -- "
                "speech-to-text requests will fail until it's pointed at your Whisper "
                "server, e.g. http://192.168.1.50:8000/v1",
                file=sys.stderr,
            )
        if not STT_API_KEY:
            print(
                "INKWELL_STT_PROVIDER=custom has no INKWELL_STT_API_KEY set -- that's fine "
                "if your self-hosted Whisper server doesn't require auth (many don't), "
                "otherwise set one.",
                file=sys.stderr,
            )


def start_http_server(workspace_dir: str, host: str, port: int, use_tls: bool, cert_path: str = None, key_path: str = None):
    """Shared bootstrap for both GUI and headless modes: sets
    WORKSPACE_DIR, creates the ThreadingHTTPServer, optionally wraps it
    in TLS, and returns the (unstarted) server plus the URL scheme it's
    serving on. Caller is responsible for calling serve_forever() (in
    its own thread, for the GUI; directly, for headless)."""
    global WORKSPACE_DIR
    os.makedirs(workspace_dir, exist_ok=True)
    WORKSPACE_DIR = workspace_dir

    httpd = http.server.ThreadingHTTPServer((host, port), EditorRequestHandler)
    scheme = "http"
    if use_tls:
        if not cert_path or not key_path or not os.path.isfile(cert_path) or not os.path.isfile(key_path):
            httpd.server_close()
            raise FileNotFoundError(
                f"TLS is enabled but cert/key not found (cert={cert_path}, key={key_path})"
            )
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    return httpd, scheme


def run_headless() -> None:
    """Entry point for Docker / any headless environment. Configured
    entirely through environment variables:

      INKWELL_WORKSPACE_DIR    Where accounts/notebooks/notes live.
                                Default: /data (mount this as a volume).
      INKWELL_HOST              Default: 0.0.0.0
      INKWELL_PORT              Default: 8060
      INKWELL_TLS               "1" to have this process terminate TLS
                                 itself (needs INKWELL_TLS_CERT /
                                 INKWELL_TLS_KEY). Default: "0" --
                                 the common Docker setup is a reverse
                                 proxy (Caddy, nginx, Traefik...) in
                                 front doing real-certificate TLS, with
                                 this container reachable only over
                                 plain HTTP on the internal network.
      INKWELL_TLS_CERT/_KEY     Paths to a cert/key pair, if
                                 INKWELL_TLS=1.
      INKWELL_ADMIN_PASSWORD    Enables POST /api/admin/reset-password.
                                 (or INKWELL_ADMIN_PASSWORD_FILE)
      INKWELL_ENCRYPTION_KEY    Enables at-rest encryption of hidden
                                 notes. (or INKWELL_ENCRYPTION_KEY_FILE)
      INKWELL_STT_PROVIDER      Enables POST /api/transcribe (speech-to-
                                 text). "local" (faster-whisper, on this
                                 same server), "openai", "groq", or
                                 "google" -- omit/leave unset to disable.
      INKWELL_STT_LOCAL_MODEL   faster-whisper model size, only used for
                                 provider=local. Default: "base".
      INKWELL_STT_API_KEY       Required for openai/groq/google.
                                 (or INKWELL_STT_API_KEY_FILE)
    """
    _load_admin_password()
    _load_encryption_key()
    _load_stt_config()

    workspace_dir = os.environ.get("INKWELL_WORKSPACE_DIR", "/data")
    host = os.environ.get("INKWELL_HOST", DEFAULT_ADDY)
    try:
        port = int(os.environ.get("INKWELL_PORT", str(DEFAULT_PORT)))
    except ValueError:
        print(f"Invalid INKWELL_PORT: {os.environ.get('INKWELL_PORT')!r}", file=sys.stderr)
        sys.exit(1)
    use_tls = os.environ.get("INKWELL_TLS", "0").strip().lower() in ("1", "true", "yes")
    cert_path = os.environ.get("INKWELL_TLS_CERT", os.path.join(SCRIPT_DIR, "cert.pem"))
    key_path = os.environ.get("INKWELL_TLS_KEY", os.path.join(SCRIPT_DIR, "key.pem"))

    try:
        httpd, scheme = start_http_server(workspace_dir, host, port, use_tls, cert_path, key_path)
    except (OSError, FileNotFoundError) as e:
        print(f"Couldn't start server: {e}", file=sys.stderr)
        sys.exit(1)

    run_startup_encryption_sync()

    if scheme == "http":
        print(
            "Serving plain HTTP. Browsers will only send the session cookie over HTTPS -- "
            "put a reverse proxy in front that terminates real TLS (or set INKWELL_TLS=1 "
            "to have this process do it directly), or logins will silently fail.",
            file=sys.stderr,
        )
    print(f"Inkwell serving {scheme}://{host}:{port}  (workspace: {workspace_dir})")
    print(f"Admin password reset: {'enabled' if ADMIN_PASSWORD else 'disabled (INKWELL_ADMIN_PASSWORD not set)'}")
    print(f"Hidden-note encryption: {'enabled' if ENCRYPTION_KEY_BYTES else 'disabled (INKWELL_ENCRYPTION_KEY not set)'}")
    print(f"Speech-to-text: {STT_PROVIDER if STT_PROVIDER != 'disabled' else 'disabled (INKWELL_STT_PROVIDER not set)'}")

    def _shutdown(signum, frame):
        print("Shutting down...")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    httpd.serve_forever()
    httpd.server_close()


if TKINTER_AVAILABLE:
    class ServerGUI:
        def __init__(self, root: tk.Tk):
            self.root = root
            self.root.title("Inkwell -- HTTPS Server")
            self.httpd: http.server.ThreadingHTTPServer | None = None
            self.server_thread: threading.Thread | None = None

            saved = load_gui_settings()
            default_dir = os.path.join(SCRIPT_DIR, "workspace")
            self.dir_var = tk.StringVar(value=saved.get("workspace_dir", default_dir))
            self.addy_var = tk.StringVar(value=saved.get("address", DEFAULT_ADDY))
            self.port_var = tk.StringVar(value=str(saved.get("port", DEFAULT_PORT)))
            self.status_var = tk.StringVar(value="Stopped")

            frame = tk.Frame(root, padx=12, pady=12)
            frame.pack(fill="both", expand=True)

            tk.Label(frame, text="Workspace directory (accounts, notebooks & notes):").grid(
                row=0, column=0, columnspan=3, sticky="w"
            )
            self.dir_entry = tk.Entry(frame, textvariable=self.dir_var, width=48)
            self.dir_entry.grid(row=1, column=0, columnspan=2, sticky="we")
            self.browse_button = tk.Button(frame, text="Browse...", command=self.browse_directory)
            self.browse_button.grid(row=1, column=2, padx=(6, 0))

            tk.Label(frame, text="Address:").grid(row=2, column=0, sticky="w", pady=(10, 0))
            tk.Label(frame, text="Port:").grid(row=2, column=1, sticky="w", pady=(10, 0))
            self.addy_entry = tk.Entry(frame, textvariable=self.addy_var, width=20)
            self.addy_entry.grid(row=3, column=0, sticky="w")
            self.port_entry = tk.Entry(frame, textvariable=self.port_var, width=10)
            self.port_entry.grid(row=3, column=1, sticky="w")

            self.start_stop_button = tk.Button(
                frame, text="Start Server", command=self.toggle_server, width=20
            )
            self.start_stop_button.grid(row=4, column=0, pady=(12, 4), sticky="w")

            tk.Label(frame, text="Status:").grid(row=5, column=0, sticky="w")
            tk.Label(frame, textvariable=self.status_var, fg="blue").grid(
                row=5, column=1, columnspan=2, sticky="w"
            )

            tk.Label(frame, text="Log:").grid(row=6, column=0, sticky="w", pady=(10, 0))
            self.log_text = tk.Text(frame, width=60, height=10, state="disabled")
            self.log_text.grid(row=7, column=0, columnspan=3, sticky="we")

            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        def log(self, message: str) -> None:
            def append():
                self.log_text.config(state="normal")
                self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            self.root.after(0, append)

        def browse_directory(self):
            chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or SERVE_DIR)
            if chosen:
                self.dir_var.set(chosen)

        def toggle_server(self):
            if self.httpd is None:
                self.start_server()
            else:
                self.stop_server()

        def start_server(self):
            workspace_dir = self.dir_var.get().strip() or os.path.join(SERVE_DIR, "workspace")
            addy = self.addy_var.get().strip() or DEFAULT_ADDY
            try:
                port = int(self.port_var.get().strip())
            except ValueError:
                messagebox.showerror("Invalid port", "Port must be a number.")
                return

            cert_path = os.path.join(SCRIPT_DIR, "cert.pem")
            key_path = os.path.join(SCRIPT_DIR, "key.pem")

            try:
                self.httpd, _scheme = start_http_server(workspace_dir, addy, port, True, cert_path, key_path)
            except FileNotFoundError:
                messagebox.showerror(
                    "Missing certificate",
                    "cert.pem and key.pem must be in the same folder as this script."
                )
                return
            except OSError as e:
                messagebox.showerror("Couldn't start server", str(e))
                self.httpd = None
                return

            run_startup_encryption_sync()

            self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.server_thread.start()

            save_gui_settings(WORKSPACE_DIR, addy, port)

            self.status_var.set(f"Running at https://{addy}:{port}")
            self.start_stop_button.config(text="Stop Server")
            self.dir_entry.config(state="disabled")
            self.browse_button.config(state="disabled")
            self.addy_entry.config(state="disabled")
            self.port_entry.config(state="disabled")
            self.log(f"Started -- serving app from {SERVE_DIR}, workspace at {WORKSPACE_DIR}")

        def stop_server(self):
            if self.httpd:
                httpd = self.httpd
                self.httpd = None

                def do_shutdown():
                    httpd.shutdown()
                    httpd.server_close()
                    self.log("Stopped.")

                threading.Thread(target=do_shutdown, daemon=True).start()

            self.status_var.set("Stopped")
            self.start_stop_button.config(text="Start Server")
            self.dir_entry.config(state="normal")
            self.browse_button.config(state="normal")
            self.addy_entry.config(state="normal")
            self.port_entry.config(state="normal")

        def on_close(self):
            self.stop_server()
            self.root.destroy()


if __name__ == "__main__":
    headless_requested = os.environ.get("INKWELL_HEADLESS", "").strip().lower() in ("1", "true", "yes")

    if headless_requested or not TKINTER_AVAILABLE:
        run_headless()
    else:
        _load_admin_password()
        _load_encryption_key()
        _load_stt_config()
        root = tk.Tk()
        _gui_instance = ServerGUI(root)
        root.mainloop()
