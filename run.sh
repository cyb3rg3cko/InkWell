#!/usr/bin/env bash
# Launches the Inkwell server. Two modes, both driven by the settings
# block below:
#
#   GUI mode (default) -- opens a small window for picking the
#   workspace directory, address, and port. Always serves HTTPS, so
#   this script makes sure a cert/key pair exists first, generating a
#   self-signed one if not.
#
#   Headless mode -- no window at all, fully configured by the
#   INKWELL_* variables below (or your shell's environment/a Docker
#   container, if you're not using this script for that). This is the
#   same mode `docker compose up` runs -- this script just lets you
#   run it directly on a machine without Docker, e.g. a spare Linux
#   box or an always-on Mac mini. Uncomment `INKWELL_HEADLESS` below to
#   switch into it.

set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------
# Core connection settings -- uncomment and fill in whichever you
# need. These have to be set before the TLS-certificate check further
# down, since INKWELL_HEADLESS/INKWELL_TLS change whether that check
# even applies -- that's why this block comes first, not grouped with
# the "optional features" block later in this file.
# ---------------------------------------------------------------

# export INKWELL_HEADLESS=1                      # uncomment to skip the GUI entirely (see the mode explanation above)
# export INKWELL_WORKSPACE_DIR="./workspace"      # headless mode defaults to /data, meant for Docker -- set this explicitly for a bare-metal run, or it'll likely fail with a permissions error
# export INKWELL_HOST="0.0.0.0"                   # default: 0.0.0.0 (every interface) -- set to "127.0.0.1" for loopback-only
# export INKWELL_PORT="8060"                      # default: 8060
# export INKWELL_TLS="1"                          # headless mode defaults to plain HTTP (0), expecting a reverse proxy in front -- set to "1" to have this process terminate real TLS itself instead
# export INKWELL_TLS_CERT="cert.pem"              # only read if INKWELL_TLS=1 -- defaults to cert.pem next to this script either way
# export INKWELL_TLS_KEY="key.pem"                # only read if INKWELL_TLS=1 -- defaults to key.pem next to this script either way

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Couldn't find python3 or python on your PATH. Install Python 3 and try again." >&2
  exit 1
fi

# GUI mode always needs a cert/key pair. Headless mode only needs one
# if INKWELL_TLS=1 was explicitly set above (its default is plain HTTP,
# expecting something else -- a reverse proxy -- to handle real TLS).

NEED_TLS_CERT=0
if [[ -z "${INKWELL_HEADLESS:-}" ]]; then
  NEED_TLS_CERT=1
elif [[ "${INKWELL_TLS:-0}" == "1" ]]; then
  NEED_TLS_CERT=1
fi

if [[ "$NEED_TLS_CERT" == "1" ]]; then
  CERT_PATH="${INKWELL_TLS_CERT:-cert.pem}"
  KEY_PATH="${INKWELL_TLS_KEY:-key.pem}"
  if [[ ! -f "$CERT_PATH" || ! -f "$KEY_PATH" ]]; then
    echo "No $CERT_PATH or $KEY_PATH found -- generating a self-signed certificate..."
    if ! command -v openssl >/dev/null 2>&1; then
      echo "openssl is required to generate one automatically. Install openssl, or supply your own cert/key at those paths." >&2
      exit 1
    fi
    openssl req -x509 -newkey rsa:2048 -keyout "$KEY_PATH" -out "$CERT_PATH" -days 365 -nodes -subj "/CN=localhost"
    echo "Certificate generated (self-signed -- browsers will warn about it, that's expected)."
  fi
fi

# ---------------------------------------------------------------
# Optional features -- short-term way to configure these without the
# GUI having dedicated fields for them yet (GUI mode) or without
# reaching for Docker/your shell's environment (headless mode).
# Admin-password reset, at-rest encryption, and speech-to-text are all
# env-var-only under the hood regardless of GUI vs. headless. Uncomment
# and fill in whichever of these you want; `exec` at the bottom carries
# every exported value in this file straight into the Python process.
# See README.md for what each one does and the full list of
# speech-to-text provider options.
# ---------------------------------------------------------------

# export INKWELL_ADMIN_PASSWORD="a long, random password -- this can reset ANY account's password"

# export INKWELL_ENCRYPTION_KEY="a long, random value -- generate with: openssl rand -base64 32"

# export INKWELL_STT_PROVIDER="local"   # local | openai | groq | google | custom
# export INKWELL_STT_LOCAL_MODEL="base"          # only used if STT_PROVIDER=local
# export INKWELL_STT_API_KEY=""                  # required for openai/groq/google; optional for custom; unused for local
# export INKWELL_STT_URL="http://192.168.1.50:8000/v1"   # only used if STT_PROVIDER=custom
# export INKWELL_STT_MODEL_NAME="whisper-1"               # only used if STT_PROVIDER=custom

exec "$PYTHON_BIN" Python_HTTPS_Server.py
