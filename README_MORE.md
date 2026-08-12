# Inkwell -- a flat-only Markdown editor with native clipboard support

A standalone, non-VR alternative to the Godot VR text editor project.
Plain HTML/CSS/JS, served by the same Python server design used there --
no Godot, no WebGL canvas, no WASM.

## Why this exists

The VR project needed a long series of workarounds to make cut/copy/
paste behave reasonably well, because Godot's Web export renders
everything -- including all of its "text" -- as pixels on a `<canvas>`.
A browser only knows how to deliver clipboard actions to genuinely
editable DOM elements (`<textarea>`, `<input>`, `contenteditable`);
a canvas isn't one, so every one of those workarounds existed purely to
bridge that gap.

This version uses a real `<textarea>` for the actual editing surface.
Native copy, cut, and paste -- Ctrl+C/X/V, right-click, all of it -- work
exactly the way they do on any other website, in every browser
including Firefox, with zero custom code. That's not a workaround; it's
just what a `<textarea>` already does.

Same trade for: text selection, undo/redo, spell-check, browser
find-on-page, screen readers, opening a local file (a real
`<input type="file">`, no trusted-activation issues at all), and
downloading a copy (a real `<a download>` link). All of it is free here
and none of it was free in the canvas-rendered version.

What's genuinely different, not just simpler: since this isn't
rendering into a 3D scene, links in Rich Text preview open in a real new
tab (`target="_blank"`) instead of being copied to the clipboard, which
was the VR version's own workaround for not being able to reliably open
a browser tab from inside a VR session.

**What's out of scope, on purpose:** no VR/WebXR support at all -- this
is the flat-mode-only alternative discussed as a "what if we didn't need
VR" hypothetical. If you need the VR headset experience, use the Godot
project instead.

## Setup

1. This folder needs `cert.pem` and `key.pem` (a TLS certificate/key
   pair) sitting next to `Python_HTTPS_Server.py`, same requirement as
   the Godot project's server. `run.sh` generates a self-signed pair
   automatically the first time it's run if they're not already there
   (requires `openssl` on your PATH) -- or make your own ahead of time:
   ```
   openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes
   ```
   (HTTPS matters here mainly so the Copy/Cut *menu buttons*, which use
   `navigator.clipboard.writeText()`, work reliably across browsers --
   native Ctrl+C/Ctrl+V work regardless, secure context or not.)

2. Run the server:
   ```
   ./run.sh
   ```
   (or `python Python_HTTPS_Server.py` directly, if you'd rather skip
   the cert check/generation step). Same GUI as before, but the
   directory picker now chooses a **workspace** directory (accounts,
   notebooks, and notes all live under it) instead of a flat files
   directory. Pick it, set address and port, then Start Server. It
   remembers those choices next time (`server_gui_settings.json`, next
   to the script).

3. Open `https://<address>:<port>/` in a browser and create an account
   (or sign in, if you already have one).

Want admin-password reset, at-rest encryption, or speech-to-text, in
either mode? The GUI window itself doesn't have fields for those yet
-- `run.sh` has a block of commented-out `export INKWELL_...=` lines
for exactly this; uncomment and fill in whichever you want, then run
the script as usual.

`run.sh` isn't only for the GUI, either -- it has its own commented
block of core connection settings (`INKWELL_HEADLESS`,
`INKWELL_WORKSPACE_DIR`, `INKWELL_HOST`, `INKWELL_PORT`, `INKWELL_TLS`,
`INKWELL_TLS_CERT`/`_KEY`) near the top. Uncomment `INKWELL_HEADLESS`
there and it switches into the same headless mode Docker uses, letting
you run Inkwell on a bare machine -- a spare Linux box, an always-on
Mac mini -- without Docker at all, still using this same script and
its cert-generation convenience.

## Running on Docker (public/internet-facing deployment)

The GUI mode above needs a display, so it won't run in a container --
there's a separate **headless mode** for that, configured entirely
through environment variables instead of the GUI's fields.

Build the image once:

```
docker build -t inkwell:latest .
```

then run it, either via one of the included compose files (see below
for which one fits your setup) --

```
docker compose up -d
```

-- or directly:

```
docker run -d \
  -p 8060:8060 \
  -v /home/user/docker/inkwell:/app \
  -v /home/user/docker/inkwell/data:/data \
  -e INKWELL_ADMIN_PASSWORD="$(openssl rand -base64 32)" \
  inkwell:latest
```

### Deploying through Portainer

The included compose files reference `image: inkwell:latest` rather
than `build: .`, specifically so they work when pasted into a
Portainer stack. Portainer's own backend is what would need to read
the build context (`Dockerfile`, `index.html`, etc.) to build an
image, and it usually doesn't have your host filesystem mounted into
itself to do that -- even though the Docker daemon does, and bind
mounts (which are just host-path strings handed to the daemon
directly) work fine through Portainer either way. So: `docker build`
that image once on the host first (see above), *then* paste the
compose file's contents into Portainer's stack editor. You'll rarely
need to rebuild after that, since the app code itself is bind-mounted
in at runtime -- only if you change `requirements.txt` or the
Dockerfile.

For `docker-compose.external-tunnel.yml`'s network step, Portainer can
do the `docker network create` / `docker network connect` equivalent
too, without touching a command line: Networks -> add `inkwell_net` ->
open it -> add your existing `cloudflared` container to it.

### Logs

Headless mode logs to stdout -- `docker logs inkwell` (or `-f` to
follow live), or Portainer's own log view for the container. Every
connection gets an explicit `Connection opened: <ip>:<port>` /
`Connection closed: <ip>:<port>` pair, plus one line per HTTP request
handled on it (method, path, response code) -- with keep-alive
enabled, a single connection typically carries several of these rather
than one apiece. That connection-level detail is exactly what made
diagnosing a "connection reset by peer" issue against a reverse proxy
straightforward -- if you ever need to debug connectivity again,
this is the first place to look, alongside whatever's proxying to
Inkwell (cloudflared's own logs, Caddy's, etc.).

The request line is logged with its **query string stripped** --
notebook and note names travel as query parameters
(`?notebook=X&name=Y`), and logging those in plain text would defeat
the point of hiding something if anyone with log access could just
read the name there instead. Request bodies (which is where a
password would be, on login/register) were never logged in the first
place.

### App code and data as host directories, not Docker-managed volumes

All four included compose files (`docker-compose.yml`,
`docker-compose.cloudflare.yml`, `docker-compose.external-tunnel.yml`,
`docker-compose.standalone.yml`) bind-mount two host directories in,
set via `INKWELL_APP_DIR` and
`INKWELL_DATA_DIR` in `.env`, rather than baking the app code into the
image or storing data in a Docker-managed volume:

- **`INKWELL_APP_DIR` -> `/app`** -- should contain `index.html` and
  `Python_HTTPS_Server.py` (this same folder works, since that's
  exactly what's already here). Editing `index.html` on the host takes
  effect on the very next browser request -- it's a static file the
  app reads from disk each time, no restart needed. Editing
  `Python_HTTPS_Server.py` needs a restart to take effect
  (`docker compose restart inkwell`, or Portainer's "Restart" button
  on the container) since Python only reads it once, at startup.
- **`INKWELL_DATA_DIR` -> `/data`** -- accounts, notebooks, and notes.
  Created automatically on first run if it doesn't already exist. A
  host path you can back up, inspect, or move directly, rather than
  something buried in Docker's own volume storage.

Both are required (the compose files fail fast with a clear error if
either is unset in `.env`, rather than silently falling back to
something else).

**Put something in front of it that terminates real TLS** -- a
self-hosted reverse proxy (Caddy, nginx, Traefik -- the included
`docker-compose.yml`/`Caddyfile` use Caddy, since it gets you a free
auto-renewing Let's Encrypt certificate just by naming your domain), or
a **Cloudflare Tunnel** (`docker-compose.cloudflare.yml`, see below) --
either is fine, and both work the same way from the app's point of
view: it serves plain HTTP internally by default (`INKWELL_TLS=0`),
and whatever's in front of it handles HTTPS to the browser. Don't
expose the container's port straight to the internet with *nothing* in
front of it; login will also silently break in that case, since the
session cookie is marked `Secure` and browsers refuse to send `Secure`
cookies over plain HTTP.

| Environment variable | Default | Purpose |
|---|---|---|
| `INKWELL_HEADLESS` | unset | Set to `1` to skip the GUI (the Docker image sets this for you) |
| `INKWELL_WORKSPACE_DIR` | `/data` | Where accounts/notebooks/notes live -- a *container-internal* path; see `INKWELL_DATA_DIR` below for where that's actually stored on the host |
| `INKWELL_HOST` | `0.0.0.0` | Bind address |
| `INKWELL_PORT` | `8060` | Bind port |
| `INKWELL_TLS` | `0` | `1` to have this process terminate TLS itself instead of relying on something in front of it |
| `INKWELL_TLS_CERT` / `INKWELL_TLS_KEY` | `cert.pem`/`key.pem` next to the script | Only used if `INKWELL_TLS=1` |
| `INKWELL_ADMIN_PASSWORD` (or `_FILE`) | unset | Enables password-reset recovery -- see below |
| `INKWELL_ENCRYPTION_KEY` (or `_FILE`) | unset | Enables at-rest encryption of hidden notes -- see below |
| `INKWELL_STT_PROVIDER` | unset | Enables `POST /api/transcribe` (speech-to-text) -- `local`/`openai`/`groq`/`google`/`custom`, see below |
| `INKWELL_STT_LOCAL_MODEL` | `base` | faster-whisper model size, only used if `INKWELL_STT_PROVIDER=local` |
| `INKWELL_STT_API_KEY` (or `_FILE`) | unset | Required for `openai`/`groq`/`google`; optional for `custom`; unused for `local` |
| `INKWELL_STT_URL` | unset | Base URL of your own Whisper server, only used if `INKWELL_STT_PROVIDER=custom` |
| `INKWELL_STT_MODEL_NAME` | `whisper-1` | The `model` field sent in the request, only used if `INKWELL_STT_PROVIDER=custom` |

`INKWELL_APP_DIR` and `INKWELL_DATA_DIR` (in `.env`, not the table
above) are a different thing -- they're read by **Docker Compose
itself**, not the app, to know which host directories to bind-mount in
as `/app` and `/data` respectively. The app only ever sees the
container-internal paths.

Any `INKWELL_*_PASSWORD`/`_KEY` variable also has an `_FILE` variant
(e.g. `INKWELL_ADMIN_PASSWORD_FILE=/run/secrets/admin_password`) that
reads the value from a file instead -- the Docker/Swarm secrets
convention, so the actual secret doesn't sit in an env var visible to
`docker inspect` or `/proc`.

### Running behind a Cloudflare Tunnel instead of a reverse proxy

This works fine, and is a perfectly good alternative to a self-hosted
Caddy/nginx -- from the app's perspective they play the same role.
Cloudflare's edge is what terminates real TLS for your public
hostname; the browser's connection is genuinely `https://`, so the
`Secure` session cookie is sent correctly, exactly as it would be
behind Caddy. `cloudflared` (the tunnel daemon) makes an *outbound*
connection to Cloudflare, so there's nothing to open on this host's
firewall at all -- arguably a nicer property than the reverse-proxy
setup, which needs 80/443 published.

**If `cloudflared` isn't running yet**, `docker-compose.cloudflare.yml`
sets both it and Inkwell up together, on a shared Docker network with
no ports published to the host at all -- configure the tunnel from a
token (create one in the Cloudflare Zero Trust dashboard -- Networks ->
Tunnels -> Docker connector) and point its public hostname at
`http://inkwell:8060`, using the compose service name since the two
containers talk to each other over the internal Docker network, not
`localhost`.

**If you already have a `cloudflared` container running** (a common
setup if it's already tunneling other services) -- don't run a second
one. Use `docker-compose.external-tunnel.yml` instead, which starts
only Inkwell, joined to a Docker network you create and also attach
your existing `cloudflared` container to:
```
docker network create inkwell_net
docker network connect inkwell_net <your-cloudflared-container-name>
docker compose -f docker-compose.external-tunnel.yml up -d
```
`docker network connect` works on a running container regardless of
whether it was started via `docker run` or its own compose file, and
doesn't disturb whatever other network(s) it's already on -- a
container can be attached to more than one at a time. Once both
containers share `inkwell_net`, point that tunnel's public hostname at
`http://inkwell:8060` the same way as above (Docker's built-in DNS
resolves `inkwell` to the right container automatically; the
`container_name: inkwell` in that compose file is what makes that name
predictable).

Leave `INKWELL_TLS` at its default (`0`) either way -- `cloudflared`
talks plain HTTP to the app internally, same as Caddy would, and
that's expected and fine; the tunnel's own connection back to
Cloudflare is already encrypted independent of that.

One thing worth double-checking if requests start getting rejected
with "Cross-site request blocked": the CSRF check above compares the
browser's `Origin` header against the `Host` header the app receives.
Cloudflare forwards the original `Host` by default, so this should
just work, but a non-default tunnel config that rewrites the Host
header (e.g. a custom `httpHostHeader` override) could make the two
stop matching. If you hit that, it means Cloudflare/cloudflared isn't
forwarding your public hostname through as `Host` -- worth confirming
in the tunnel's public hostname settings.

If you instead see `cloudflared` fail with **"Unable to reach the
origin service"**, check its logs (`docker logs <cloudflared-container>`)
for the specific underlying error before assuming the network path is
broken -- `dial tcp ...: connection refused` or `i/o timeout` really
is a connectivity problem (wrong address/port, firewall, or the
same-host hairpin-NAT issue the network-based setup above avoids), but
`read: connection reset by peer` on an otherwise-successful connection
points somewhere else entirely: two separate causes, both fixed as of
this version, can produce exactly that symptom on a same-host
connection to a plain-HTTP origin:

- The server didn't speak real HTTP/1.1 keep-alive -- it closed the
  TCP connection after every single response (Python's `http.server`
  default), while a reverse proxy expects to reuse connections. When
  it tried to reuse one this server had already closed, the OS sent
  back a hard RST rather than a clean close.
- Request bodies were only ever read by `Content-Length` -- any
  request sent with `Transfer-Encoding: chunked` instead (which
  Cloudflare Tunnel's forwarding can do, even for a request whose
  original sender used `Content-Length`) left its actual body bytes
  sitting unread on the socket. Those leftover bytes then got
  misparsed as the start of the *next* request on the same
  connection, corrupting that connection's framing -- which, once bad
  enough, also surfaces as a reset.

If the reset persists even after both of those are fixed (confirm
you're actually running the rebuilt image), and your `cloudflared`
container only runs with host networking (`network_mode: host`) --
which also means it *can't* be attached to an extra custom Docker
network the way the shared-network approach above needs --
`docker-compose.host-network.yml` puts Inkwell on host networking too.
This completely bypasses Docker's bridge network and port-publishing
layer for the connection between them, which doubles as a decisive
diagnostic: if that fixes it, the cause was something in Docker's
networking layer, not the app; if it doesn't, that's ruled out and the
issue is somewhere else entirely (at that point, a packet capture on
the host for port 8060 during a tunnel request, e.g.
`tcpdump -i any port 8060 -w capture.pcap`, would show exactly what's
being exchanged right before the reset). There's no `ports:` section
in that file at all -- meaningless under host networking, since the
app binds directly to whatever `INKWELL_HOST`/`INKWELL_PORT` say on
the real host; set `INKWELL_HOST` to your host's specific LAN IP
(rather than `0.0.0.0`) if you don't want it also listening on any
other interface the host might have.

### Skipping a Docker network entirely (direct published port)

If whatever's reaching Inkwell -- an existing tunnel container, a
reverse proxy, or a trusted LAN -- would rather connect to the host's
own address and a published port than a container name over a shared
network, `docker-compose.standalone.yml` covers that: no `networks:`
key, no bundled proxy/tunnel, just Inkwell with its port published
directly. A few things it handles that are easy to get wrong by hand:

- Runs as a specific host UID/GID instead of root (so files created
  under the data directory are owned by you), using real numbers
  rather than `${UID}`/`${GID}` -- those aren't reliably available for
  interpolation through Portainer, or even a plain shell unless
  explicitly exported first. Replace the placeholder numbers with your
  own (`id -u` / `id -g`), and make sure that UID/GID actually has
  write access to the data directory on the host first
  (`sudo chown -R 1000:1000 /path/to/it`), or the container will fail
  with permission errors the first time it tries to create its own
  files.
- Binds the published port to `127.0.0.1` only by default, not every
  interface on the host -- `"8060:8060"` on its own is shorthand for
  `"0.0.0.0:8060:8060"`, which would make the plain-HTTP port directly
  reachable by anything that can reach the host's network at all,
  bypassing Cloudflare (or any other proxy) entirely for whoever finds
  it. Change it if you specifically want broader reachability.

**Wanting a LAN fallback for when the tunnel/internet is down** --
reaching Inkwell from another device on your own network, not just the
Docker host itself -- needs the port bound beyond loopback (change it
to `"8060:8060"`, or to the host's specific LAN IP for a narrower
version of the same thing). That gets you a reachable page, but
**not** working login on its own: the session cookie is `Secure`, so
browsers refuse to send/store it over plain HTTP, and a direct LAN-IP
connection is a different origin from your tunneled domain anyway (no
session carries over either way). `docker-compose.standalone.yml` has
a commented-out block for exactly this -- enabling `INKWELL_TLS=1`
with a self-signed certificate (fine for LAN access you already trust;
browsers show a one-time warning to click through) so login actually
works on that fallback path too.

## Security

**Accounts.** Passwords are hashed with PBKDF2-HMAC-SHA256 (600,000
iterations, OWASP's current baseline recommendation, per-account salt)
-- never stored in plain text. The iteration count is saved alongside
each hash specifically so it can be raised again in the future without
invalidating everyone's existing password. Minimum password length is
10 characters; there's no complexity requirement beyond that (length
matters more than forced complexity, per current guidance). Login is
rate-limited (5 attempts, then a short lockout, per username) and the
session cookie is `HttpOnly` + `Secure` + `SameSite=Lax`. `SameSite=Lax`
already blocks the classic cross-site-form CSRF attack in any modern
browser; there's also a lightweight Origin/Referer check on every
state-changing request as a second, independent layer.

**What this setup does *not* include**, worth knowing before putting it
on the open internet: no email verification, no 2FA, no built-in
brute-force protection beyond the per-account lockout above (a
determined attacker distributing attempts across many IPs isn't
slowed by it -- put this behind a reverse proxy with its own
rate-limiting for real protection against that), and sessions are
in-memory only (everyone gets logged out on a server restart -- not a
security weakness, but worth knowing). None of this is unusual for a
small self-hosted tool, but it's not the same bar as a service that
needs to withstand sustained, resourced attack.

**Hiding is not encryption.** A hidden notebook/note is a UI declutter
toggle, gated back open by a 4-digit PIN. On its own that's *not* real
access control -- anyone who can reach your account through the API at
all can still request hidden content, and the PIN's 4-digit space is
far too small to resist real brute-forcing anyway (rate-limited here,
but still). If you want hidden notes actually protected on disk, see
at-rest encryption below.

### Resetting a forgotten password

There's no email system, so password reset works differently: whoever
runs the server sets an **admin password** (`INKWELL_ADMIN_PASSWORD`),
which can reset *any* account's password. A "Forgot password?" link on
the sign-in screen asks for the account's username, a new password,
and the admin password.

- Use a long, random admin password (`openssl rand -base64 32`) --
  it's more sensitive than any one user's password, since it can reset
  every account. Store it as a secret, not a plain env var, if your
  deployment supports that (`INKWELL_ADMIN_PASSWORD_FILE`).
- Attempts are rate-limited by IP (5 tries, then a 5-minute lockout) --
  harsher than the per-account login lockout, since this endpoint is a
  bigger target.
- A successful reset invalidates every existing session for that
  account, in case the reset is happening because it was compromised.
- Leave `INKWELL_ADMIN_PASSWORD` unset to disable this feature
  entirely (the endpoint returns "not configured" rather than doing
  anything) -- there's currently no other way to recover a forgotten
  password, so make sure at least one recovery path exists before
  relying on this for real accounts.

### Encrypting hidden notes at rest

If `INKWELL_ENCRYPTION_KEY` is set, any note that's effectively hidden
(directly, or because its notebook is hidden) is encrypted on disk with
AES-256-GCM, using a key derived from that passphrase. It decrypts
transparently through the app -- you won't notice it's happening day
to day.

**What this protects against:** someone who gets the raw files under
your workspace directory *without* going through the running app --
a stolen backup, a misconfigured volume/bucket, another tenant on
shared storage -- can't read a hidden note's content without also
having the key.

**What this does *not* protect against:** anyone who can reach the
running app as you (or who compromises the server process itself)
still gets the plaintext on request, same as any other note. The key
lives in the server's environment and is used automatically -- it is
**not** derived from your account password or your PIN, and
deliberately so:

- Deriving it from your *password* would mean the server needs your
  plaintext password again at read/write time, which either means
  storing it server-side (defeats the point of only ever storing a
  hash) or asking you to re-enter it constantly.
- Deriving it from your 4-digit *PIN* would be security theater -- a
  PIN only has 10,000 possible values, instantly brute-forced offline
  against a stolen encrypted file, key-derivation function or not.

So instead it's one operator-set, higher-entropy key for the whole
server -- a real protection for the "stolen backup" threat, but it
doesn't turn hidden notes into something even the app operator can't
read.

Generate a real random value (`openssl rand -base64 32`), not something
you'd type from memory, and store it as a secret if your deployment
supports that (`INKWELL_ENCRYPTION_KEY_FILE`). **Losing this value
makes already-encrypted hidden notes unrecoverable** -- there's no
recovery path built in, back it up somewhere as carefully as you'd
back up the notes themselves. This feature needs the `cryptography`
Python package (included in the Docker image; `pip install
cryptography` for local non-Docker use) -- the server refuses to start
with a key configured but that package missing, rather than silently
falling back to storing hidden notes as plain text.

**Existing notes get swept up automatically on startup**, not just
new ones going forward -- if you set the key for the first time (or
copy data in from an unencrypted instance and restart with the key
already configured), every note that's already hidden gets encrypted
right away as part of starting up, rather than sitting in plain text
until it happens to be opened or have its hidden state toggled through
the API. The server logs how many notes it updated, if any. This only
runs one way, though: a note already encrypted with a key that's since
been removed from the config stays encrypted (and unreadable via the
API) rather than getting corrupted or silently dropped back to plain
text -- there's no way to decrypt it without that key, by definition.

### Speech-to-text

`POST /api/transcribe` exists mainly for clients where typing is
awkward -- a VR editor being the motivating case -- but it's just a
normal authenticated API endpoint like everything else, so anything
that can already talk to your Inkwell server can use it: send audio
bytes in the request body, get `{"text": "..."}` back. Unset
`INKWELL_STT_PROVIDER` (the default) and the endpoint just returns 503
-- nothing about the rest of the app depends on this.

Every request goes through *this server first*, never straight from a
client to a third-party STT provider. That's deliberate: it means
provider API keys only ever need to exist in this server's own
environment, never inside a distributed client (an app binary, an APK,
whatever) where they'd be extractable and someone else could run up
your bill.

Five provider options, picked per-deployment via `INKWELL_STT_PROVIDER`:

- **`local`** -- runs on this same server via
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper), no
  external API, no per-request cost, and nothing about what you say
  ever leaves your infrastructure. Not installed by default -- it's a
  much heavier dependency (`ctranslate2`, `numpy`, `tokenizers`) than
  anything else this app needs, so building it in is opt-in:
  ```
  docker build --build-arg INSTALL_STT=1 -t inkwell:latest .
  ```
  (or `pip install -r requirements-stt.txt` for local non-Docker use).
  `INKWELL_STT_LOCAL_MODEL` picks the model size -- `tiny` through
  `large-v3`, trading accuracy for speed and download size (roughly
  75MB to 3GB); `base` is a reasonable starting point on a CPU-only
  server. The actual model weights are a separate download from
  Hugging Face that happens automatically on the *first* transcription
  request (needs outbound internet access that one time), cached under
  `workspace/stt-models/` after that so a restart doesn't re-download.
  Given this app's whole approach to privacy elsewhere (hidden notes,
  PIN, at-rest encryption), this is the option most in keeping with
  that -- worth the extra setup step if your server's hardware can
  keep transcription feeling reasonably snappy without a GPU.
- **`openai`** / **`groq`** -- both speak the same request/response
  shape (Groq's API is deliberately OpenAI-compatible), just different
  base URLs and models under the hood. Groq's hosted Whisper is
  notably inexpensive and fast if you want a remote provider without
  running inference yourself.
- **`google`** -- Google Cloud Speech-to-Text. Expects 16kHz mono
  16-bit PCM WAV audio specifically -- Google's API needs the sample
  rate and encoding declared up front rather than reading them from
  the file, so whatever client you're sending audio from needs to
  record/export in that format for this to work correctly.
- **`custom`** -- your own Whisper server, running anywhere reachable
  from this machine -- a separate PC on your network with better
  hardware than the InkWell server itself, say. Works with anything
  exposing the same OpenAI-shaped `/v1/audio/transcriptions` endpoint
  (e.g. [faster-whisper-server/Speaches](https://github.com/speaches-ai/speaches),
  `whisper-asr-webservice`) -- set `INKWELL_STT_URL` to its base URL,
  including scheme and any path prefix it uses:
  ```
  INKWELL_STT_URL=http://192.168.1.50:8000/v1
  ```
  `INKWELL_STT_API_KEY` is optional here, unlike the other remote
  providers -- a lot of self-hosted Whisper servers don't require any
  auth at all, and this server simply omits the `Authorization` header
  entirely if no key is set, rather than sending an empty/malformed
  one a server that doesn't expect it might reject.
  `INKWELL_STT_MODEL_NAME` (default `whisper-1`) is sent as the
  `model` field -- most self-hosted servers ignore it, but it's
  configurable in case yours cares.

`openai`/`groq`/`google` all need `INKWELL_STT_API_KEY` (or `_FILE`)
set to that provider's key; `local` doesn't need any key at all.
Audio clips are capped at 25MB per request, matching common provider
limits.

## Accounts, notebooks & notes

Inkwell now has real accounts. The first thing you see is a sign-in /
create-account screen -- notebooks and notes are private per account,
stored server-side under `workspace/users/<username>/`.

The left sidebar is a small file tree:
- It opens on your **notebooks**. Click one to see its notes.
- Notes are listed without their `.txt` extension (it's still there
  underneath -- renaming a note only asks for the part before it). The
  status bar at the bottom, which shows the currently open note, does
  the same.
- A **back arrow** in the sidebar header returns from a notebook's
  notes to the notebook list.
- A **collapse arrow** shrinks the sidebar down to a thin strip (click
  it again to expand).
- The "⋯" button on any notebook or note opens Rename / Hide / Delete.
- "+ New Notebook" / "+ New Note" (label changes depending on which
  list you're looking at) creates a new one.

Passwords are never stored in plain text -- they're hashed
(PBKDF2-HMAC-SHA256, per-account salt) same as the privacy PIN below.
Sign-in state is a random session token in an HttpOnly cookie, kept in
the server's memory (so it resets if the server restarts).

A note that was hidden the last time you had it open won't silently
load itself back up the next time you sign in (or if the browser
reloads the tab) -- the point of hiding something is that it doesn't
just show up on its own, so that check applies to session-draft
recovery too, not only the sidebar list.

### Search, and finding what's hidden

There's a search box at the bottom of the sidebar, always available --
type part of a notebook or note's name and matching (non-hidden)
results show up from anywhere in your account, not just the notebook
you're currently looking at. Click a result to open it; the search box
and its results clear themselves right after, so it's ready for the
next search rather than sitting there with a stale query.

Hiding is a *declutter* toggle, not real access control -- a hidden
notebook or note just stops showing up in the normal sidebar list, and
by default it's also left out of that search.

To search hidden items too: click the small eye icon next to the
search box. It'll ask for your **4-digit privacy PIN** (set/changed
from Settings -> Privacy PIN, which also asks for your account
password as a confirmation step) every time it's needed -- unlocking
doesn't persist across a page reload/refresh, only within the current
page load, so leaving the tab open unattended and someone reloading it
doesn't leave hidden search sitting unlocked. Once unlocked for that
page load, the same search box also surfaces hidden matches, tagged
"hidden," and gains an "Unhide" button on those results to bring one
back into the normal list for good. Click the eye icon again to
re-lock without waiting for a reload.

PIN attempts are rate-limited (5 tries, then a short lockout) to keep
someone from just guessing at it. The search box also has browser
autofill suggestions turned off, so a browser's own memory of past
searches can't surface a previously-searched name later, independent
of anything server-side.

### Moving over old files

If this server used to run in flat-file mode, any `.txt` files sitting
directly in the workspace directory are left exactly where they are --
nothing is touched automatically. Once signed in, open
Settings -- an "Import Files From Old Server" section appears
automatically if any are found, letting you pick which ones to bring
in and which notebook to drop them into.

### Sharing a notebook or note

The "⋯" menu on any notebook or note (in the normal sidebar tree --
never on a hidden one, since hidden items don't appear there in the
first place) has a **Share** option. It builds a plain, standalone
HTML page -- no login, no app, nothing but that one page -- and copies
a link to it to your clipboard (or, if the browser won't allow that,
shows the link in a dialog to copy manually).

- **Sharing a note** renders it through the same engine as Rich Text
  preview, so it looks the same as it does in the app, with a "Shared
  from InkWell by `<username>`" line at the bottom. Any checkboxes on
  the page are there for readability, not interaction -- there's no
  app behind a shared page to save a change to, so they're rendered
  disabled.
- **Sharing a notebook** shares every non-hidden note in it
  individually this same way, then builds one more page: an index
  titled with the notebook's name, listing links to each note in the
  same order they're in in your sidebar (respects however you've
  drag-reordered them), also with the "Shared from InkWell by" line at
  the bottom.

Each shared page lives at its own random, hard-to-guess URL
(`/shared/<username>/<a long random token>`) -- reachable by anyone
with that exact link, without needing an account or being signed in,
same as how a "Share" link normally works in most apps.

**Sharing something a second time reuses the same link** rather than
minting a new one -- the old URL just starts showing the newer
content, instead of the old link going stale while a second, separate
one floats around too. Re-sharing a notebook carries this through to
its notes individually as well: any note in it that already had its
own share (whether from a previous notebook share, or from having
been shared on its own) keeps that same link too, rather than every
re-share of the notebook creating a fresh batch of duplicate links for
notes that were already shared.

**Saving a note also silently pushes the update to its share, if it
has one** -- you don't need to open the "⋯" menu and hit Share again
every time you make an edit for the live link to reflect it. This
only ever *updates* an existing share; saving a note that's never been
shared doesn't create one on its own, and this covers content edits
specifically -- adding, removing, reordering, or renaming notes in an
already-shared notebook doesn't automatically rebuild that notebook's
index page. Re-sharing the notebook (Share again from its "⋯" menu)
rebuilds it fully, reusing existing per-note links as described above.

**File > Manage Shares** lists everything currently shared on this
account, grouped by notebook -- a notebook's own index share (if it
has one) listed first, followed by its individual notes' shares --
each with the link (click to copy) and a **Revoke** button.

- **Revoking a note's share** removes just that one link. Nothing
  else is affected, even if that note is also part of a shared
  notebook.
- **Revoking a notebook's share cascades**: it takes down the index
  page *and* every individual note-share under that notebook in one
  action, rather than leaving those note pages still live and
  reachable with nothing pointing at them anymore. The confirmation
  dialog says so before it happens.

Revoking anything deletes both the share's file and its registry entry
immediately, so the old link stops working right away rather than just
disappearing from this list.

There's no expiration on a share right now -- once created, a link
stays live indefinitely until revoked by hand. The underlying data
format already has a slot for a future per-share expiration
(`expires_days`, currently always `null`/unused) and the server
already checks it on every request to a shared page, so turning
expiration on later is a small, additive change (a UI control to set
it, basically) rather than a redesign -- it's just not wired up to
anything yet.

## Settings

File > Settings now opens with **Account** (who you're signed in as,
plus Log Out) and **Privacy PIN** (set/change the 4-digit PIN used to
reveal hidden notebooks/notes -- see above) at the top, followed by the
same appearance settings as before: accent color (the brand mark,
active-toggle highlight, and top border all share this one setting),
editor background color, Markdown text color, Rich Text text color, a
scroll speed multiplier (intercepts mouse-wheel scrolling only when set
away from its default of `1.0`, so native smooth-scroll feel is left
completely alone unless you actually change it), and "Auto-continue
lists" (off by default -- see below).

These appearance settings are now saved **per account** (under
`workspace/settings/<username>.json`) rather than shared by everyone
who opens the server.

### Changing your password

Settings -> **Change Password** asks for your current password plus a
new one (min. 8 characters, entered twice). Changing it doesn't sign
out your current session, but any *other* signed-in session for that
account keeps working too -- sessions aren't tied to the password hash,
so log out elsewhere yourself if that matters to you.

### Caps Lock indicator

Every password-entry field (sign in, create account, change password,
the account-password confirmation on the PIN form, and the PIN unlock
field) shows a small "⇧ Caps Lock is on" warning right under the field
while Caps Lock is active, so a mistyped password shows up before you
submit it rather than as a confusing "incorrect password" afterward.

### Reordering notebooks and notes

A small grip handle (⋮⋮) on the left of each row in the sidebar tree
lets you drag notebooks (in the notebooks list) or notes (within a
notebook) into whatever order you want. Works with a mouse or with
touch -- it's built on pointer events rather than the browser's native
HTML5 drag-and-drop, since that has no reliable touch support in most
mobile browsers.

The order is saved to your account on the server the moment you drop
an item, so it's the same on every device you sign in from, not just
remembered locally in one browser. New notebooks/notes are added at
the end of the current order; renaming, hiding, or unhiding something
doesn't change its position.

## Mobile UI

A small inline script at the very top of `<head>` checks, once, before
anything else loads: the browser's user-agent string (for
Android/iPhone/iPad/etc.) and, as a fallback for anything that string
missed, whether the device's primary pointer is touch (`pointer:
coarse`) combined with a narrow viewport. If either says "mobile," it
adds a `mobile-ui` class to `<html>` before the page paints, so there's
no flash of the desktop layout first. It's checked once at load, not
on every resize -- so an on-screen keyboard popping up (which resizes
the viewport on many mobile browsers) can't flip the layout mid-edit.

What changes under `mobile-ui`:
- The sidebar becomes an off-canvas **drawer** instead of a persistent
  panel: it's hidden by default, opened via a hamburger button that
  appears at the left of the top bar, and closes itself again after
  you tap a note (or via the backdrop / the same button that's
  "Collapse" on desktop). It opens automatically on first landing in
  the app if nothing's open yet, so there's something to tap right
  away.
- Buttons, menu items, and tree rows get larger touch targets.
- Text inputs render at 16px, which stops iOS Safari's automatic
  zoom-on-focus.
- The editor gets tighter side padding to leave more room for text.

There's no separate settings toggle for this -- it's tied to device
detection, not a preference, since it's meant to just work correctly
on a phone or tablet without extra setup. (If you ever want to force
one layout or the other for testing, editing the `mobile-ui` class
check near the top of `<head>` is the place to do it.)

## Import Local File

File > Import Local File (was "Open Local File") reads a file from
your computer and saves it straight into a notebook, rather than just
loading it into an unsaved buffer -- it becomes a real note right
away, the same as anything typed and saved in the app. It picks the
notebook the same way Save does when nothing's open yet:
- If you're currently browsing inside a notebook, it imports there.
- Otherwise it asks which notebook to use -- type an existing name to
  add it there, or a new one to create it.

## Auto-save

Off by default. Settings -> Auto-save turns it on, with a companion
field for how often (in minutes, 1-120). When on, it silently saves
the current note in the background on that interval -- but only if
there's actually something unsaved (`state.modified`) *and* the note
already has a home (it's been saved or opened before, so both a
filename and a notebook are already known). It deliberately never
prompts for a notebook the way a manual Save does when nothing's open
yet -- that would mean an unattended timer popping up a dialog, which
auto-save should never do. A brand new, never-saved document just
doesn't get auto-saved until you save it once yourself; after that,
the timer picks it up like anything else.

The status bar (bottom of the window, next to the notebook/note name)
shows **"Saved HH:MM"** for whenever the note was last written to the
server -- a manual Save updates it exactly the same as an auto-save
does, so there's one place to check either way. Auto-saves specifically
update it quietly, without the transient "Saved to X / Y" popup a
manual save shows -- that popup appearing every few minutes on a timer
would get noisy fast; the persistent status-bar text is enough to know
it's happening.

## Markdown / Rich Text toggle

The top bar's Markdown/Rich Text switch is now a single icon button
(an eye) instead of two labeled buttons, to save space -- click it to
flip between editing the raw Markdown and viewing the Rich Text
preview; it highlights when you're in the preview. Its tooltip always
names the mode a click will take you *to* ("Show Rich Text preview" /
"Back to Markdown source").

## Checklists (`- [ ]`)

A line written as `- [ ] task` (or `- [x] task` for one already
checked) renders in Rich Text preview as an actual clickable checkbox,
not just styled text -- checking or unchecking it flips the `[ ]`/`[x]`
in the underlying Markdown directly, so the source stays in sync with
what you see, and it doesn't just apply visually on your machine, it's
saved the next time you save the note.

There's also a **Checklist** button in the bottom quick-format row
(next to Bullet List) that inserts or removes `- [ ]` the same way the
Bullet button handles `- ` -- acts on the current selection if there's
one, or just the current line otherwise; toggles the whole block
on/off together (existing checked/unchecked state on lines that are
already checklist items is left alone either way).

Both checklists and plain bullets respect indentation: 2 leading
spaces (or 1 tab) in the Markdown source is one nesting level, same
step size as the Indent/Outdent buttons and Tab key use, so indenting
a checkbox or bullet with Tab and then switching to Rich Text preview
shows it nested under the line above it, not flattened to the same
level.

A plain bullet directly next to a checklist item (no blank line
between them) gets its own separate list in the rendered preview,
rather than the two sharing one -- otherwise a checklist item ending
up inside a plain bullet's list (or vice versa) would show a stray
bullet marker next to its checkbox, since it'd have inherited the
wrong list's styling.

## Quick-format buttons

Four icon-only buttons sit in the bottom-right of the status bar:
**Indent**, **Outdent**, **Bullet List**, and **Checklist**. Each acts on the current
selection if there is one, or just the line the cursor is on if there
isn't -- same behavior as the Tab/Shift+Tab keyboard shortcuts below,
except these always work regardless of whether "Auto-continue lists"
is turned on (that setting only governs the *keyboard* Tab/Enter
behavior; the buttons are a separate, always-available shortcut).

The Bullet List button toggles: if every non-blank line in the
selection already starts with a `-` marker, it removes them all;
otherwise it adds a marker to whichever lines don't already have one.
Blank lines in a selection are left alone either way.

Settings -> "Show quick-format buttons" hides or shows this row (on by
default).

## Auto-continue lists (Enter/Tab)

Off by default, since it changes how Tab and Enter behave in the editor
in a way not everyone wants -- Tab normally moves focus away from a
textarea entirely; this feature intentionally takes it over instead,
only once turned on.

When enabled:
- **Enter** on a line starting with `-` or `*` continues the list on the
  next line, keeping the same indentation and marker -- the same idea as
  a word processor continuing a bullet point after pressing Enter.
- **Enter** on a checklist line (`- [ ] text` or `- [x] text`) continues
  with a full `- [ ] ` marker on the next line -- not just a bare `- `
  -- so a checklist stays a checklist as you keep typing, regardless of
  whether the line you pressed Enter on was checked or not (new items
  always start unchecked).
- **Enter on an already-empty bullet or checklist item** ends the list
  instead of continuing it forever -- removes that empty marker and
  drops to a plain blank line, matching the common word-processor
  convention.
- **Tab** indents the current line (or every line touched by the
  selection, if there is one) by one level (2 spaces).
- **Shift+Tab** outdents by one level -- removes up to 2 spaces of
  leading whitespace, or however much a given line actually has if it's
  less than a full level, so it never errors out on partially-indented
  lines.

All of it persists through the same `/api/settings` endpoint the Godot
project's server already exposes, written to `editor_settings.json`
next to the script. The three color fields use the *exact same key
names* as the Godot build (`markdown_text_color`, `rich_text_color`,
`flat_background_color` for editor background) -- if you point both
versions at the same server, their color preferences genuinely stay in
sync. `scroll_multiplier` is a new key specific to this version: the
Godot build's "scroll speed" is a VR thumbstick concept (scrollbar-units
per second of thumbstick deflection) with no real equivalent for a
normally-scrollable page, so this uses a distinct key rather than
overloading that one with a different meaning.

## What's not here

No border/laser/reticle/keyboard/VR-environment color settings -- none
of those visual elements exist outside VR, so they're not offered here
at all, rather than showing settings that would do nothing.

## Double-click to select a word

Confirmed working on desktop already (this is completely native
`<textarea>` behavior, no code involved) -- but reported broken
specifically in a VR browser, where a native selection toolbar/paste
bubble would appear and get stuck, with nothing actually selected.
Root cause: VR browsers emulate clicks through a controller raycast
rather than a real mouse or touchscreen, and their built-in double-click/
selection-gesture detection doesn't reliably handle that.

Fixed by not depending on the browser's own detection at all --
double-click is now detected manually, by timing and position between
consecutive clicks, then the word boundaries are found and selected
directly via `editor.selectionStart`/`selectionEnd`. This works
identically regardless of what kind of pointer produced the clicks,
real mouse or VR controller raycast alike. Verified with concrete test
cases (clicking mid-word, at either edge of a word, on a hyphenated
"word", and genuinely in the middle of whitespace) rather than just
trusting the logic by inspection.

## Surviving an unexpected reload

The current document is continuously mirrored into `sessionStorage` as
you type (and on every load/new/save action, so it always reflects
whatever's actually in the editor) -- if the browser reloads this tab
unexpectedly (a crash-recovery reload, an accidental refresh, etc.), the
next load notices the leftover draft and restores it automatically, with
a status message confirming that's what happened.

Chosen specifically over `localStorage`: `sessionStorage` clears itself
the moment the tab actually closes, which matches "survive a reload
without the tab closing" exactly, rather than lingering indefinitely
after you're done with the tab entirely. If you'd rather have it survive
closing the tab and reopening later too (a stronger, different
trade-off), or want an additional server-side autosave layer that
survives even a fully cleared/disabled browser storage, both are
straightforward to add on top of this -- just ask.

## Quick-save button

A save icon in the middle of the top bar, for one-click saving without
opening the File menu. Its action is configurable in Settings ("Top bar
save button"): "Save to Server" (the default) or "Download a Copy" --
whichever you pick, it calls the exact same underlying code as the
matching File menu item (both were refactored into named functions,
`saveToServer()`/`downloadCopy()`, shared between the menu and the
button rather than duplicated).
