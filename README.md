# utalkn2me

Pull UniFi Talk call logs + recordings via the undocumented internal API,
store them in SQLite, transcribe new recordings with local Whisper, and serve
it all over a small REST API.

The official UniFi API (`/proxy/network/integration/v1/*`) only exposes Network
data. Talk has no public API or webhook. This tool authenticates against the
UniFi OS SSO endpoint (including MFA), captures the session cookie + CSRF
token, and calls `/proxy/talk/api/*`. Verified against UniFi Talk **5.1.2** on
a UDM Pro Max.

---

## Security — store credentials in 1Password

**Do not put your UniFi username, password, or TOTP seed in plain text in
`.env`.** This account has full control of your firewall, phones, and voicemail.

The recommended pattern:

1. Put your UniFi credential in 1Password (one item with `email`, `password`,
   and a `one-time password` field).
2. Reference it in `.env` with `op://` refs (see `.env.example`).
3. Run the script with `op run` so 1Password resolves the refs **on the host**
   before anything else sees them:

   ```sh
   op run --env-file .env -- python unifi_talk.py sync --transcribe
   ```

4. For the container: same thing, the resolved values get passed to Docker:

   ```sh
   op run --env-file .env -- docker compose up -d
   ```

This keeps plaintext secrets out of `.env`, out of your shell history, and out
of the container image. The container itself never sees the 1Password CLI or
service-account token — just the already-resolved values at startup.

### Session persistence (so MFA isn't needed every run)

UniFi SSO requires a TOTP only on the **initial** login. After that the
session cookie is good for a long time. The script persists the cookie to
`data/session.json` and reuses it across runs. You'll need a fresh TOTP only
when:

- you first start the container, or
- the cookie expires (UniFi rotates it occasionally).

A stale TOTP in `.env` is harmless — the script only uses it if the saved
session fails to validate.

---

## Local usage (Mac / Linux)

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit as needed

# One-off table of every call on the controller
op run --env-file .env -- python unifi_talk.py list

# Sync into SQLite + transcribe any new recordings
op run --env-file .env -- python unifi_talk.py sync --transcribe

# Run forever, syncing every 5 minutes
op run --env-file .env -- python unifi_talk.py --loop 300 sync --transcribe
```

Whisper runs locally on both macOS and Linux. On Mac with Apple Silicon, both
`openai-whisper` (brew) and `faster-whisper` (pip, default here) work with no
GPU. `faster-whisper` is ~3× quicker on CPU.

---

## Docker

Two services share a `./data` volume:

- **worker** — runs `sync --transcribe` every 5 minutes in a loop
- **api**    — Flask + gunicorn REST API on port 8000

Use the Makefile (wraps `op run` so 1P refs in `.env` resolve on the host):

```sh
cp .env.example .env   # edit
make up                # build + start both services
make logs              # tail logs
make stats             # GET /stats via the running API
make sync              # run one sync cycle in a throwaway container
make down              # stop
```

Or the raw commands:

```sh
op run --env-file .env -- docker compose up -d --build
docker compose logs -f
```

What the container does:

- Pulls the call log every 5 minutes (`--loop 300`).
- Writes calls to `/data/calls.db` (SQLite).
- Saves MP3s to `/data/recordings/<uuid>.mp3`.
- Transcribes new recordings with `faster-whisper` (`tiny` model by default).
- Persists session cookies to `/data/session.json` so MFA isn't needed after
  the first successful login.

`./data` on the host is the single source of truth — back that up and you
have every call, every recording, every transcript.

### Change the whisper model / polling interval

In `.env`:

```
WHISPER_MODEL=small      # tiny | base | small | medium | large
```

Override the worker's polling interval in `compose.yml`:

```yaml
    command: ["--loop", "60", "sync", "--transcribe",
              "--db", "/data/calls.db",
              "--recordings-dir", "/data/recordings"]
```

---

## REST API

Base URL: `http://localhost:8000` (set `API_PORT` in `.env` to change).

| Method | Path                             | Description                                  |
|--------|----------------------------------|----------------------------------------------|
| GET    | `/health`                        | Liveness                                     |
| GET    | `/stats`                         | Row counts: calls, with_recording, transcripts, pending |
| GET    | `/calls`                         | List calls (newest first). See filters below |
| GET    | `/calls/<uuid>`                  | Full record + raw Talk JSON + transcript     |
| GET    | `/calls/<uuid>/transcript`       | Just the transcript. `?format=text` for plain text |
| GET    | `/calls/<uuid>/recording`        | MP3 audio stream (only if cached locally)    |

`GET /calls` query params:

- `limit` (default 50, max 500), `offset`
- `direction` = `in` | `out`
- `status`    = `accepted` | `refused` | `voicemail`
- `from_date`, `to_date` (ISO-8601, compared against `time`)
- `has_transcript` = `true` | `false`
- `q` — substring search across `from`, `to`, caller name, transcript text

Examples:

```sh
# last 10 inbound calls with transcripts
curl -s 'http://localhost:8000/calls?direction=in&has_transcript=true&limit=10'

# search transcripts for "invoice"
curl -s 'http://localhost:8000/calls?q=invoice'

# grab a transcript as plain text
curl -s http://localhost:8000/calls/<uuid>/transcript?format=text

# download the MP3
curl -o call.mp3 http://localhost:8000/calls/<uuid>/recording
```

---

## SQLite schema

Two tables in `data/calls.db` — easy to join into another system.

```sql
CREATE TABLE calls (
    uuid                     TEXT PRIMARY KEY,
    time                     TEXT NOT NULL,         -- ISO-8601 UTC
    direction                TEXT,                   -- 'in' | 'out'
    from_number              TEXT,
    to_number                TEXT,
    from_caller_name         TEXT,                   -- CNAM lookup
    answered_by              TEXT,
    status                   TEXT,                   -- accepted | refused | voicemail
    duration                 INTEGER,                -- seconds
    is_video_call            INTEGER,
    recording                INTEGER,                -- 1 if a recording exists
    recording_filename       TEXT,
    quality_score            INTEGER,                -- 0–100
    to_smart_attendant_title TEXT,
    raw                      TEXT NOT NULL,          -- full JSON from Talk
    first_seen               TEXT NOT NULL
);

CREATE TABLE transcripts (
    uuid        TEXT PRIMARY KEY REFERENCES calls(uuid),
    text        TEXT NOT NULL,
    source      TEXT NOT NULL,      -- 'native' | 'faster-whisper' | 'whisper'
    model       TEXT,               -- whisper model name
    created_at  TEXT NOT NULL
);
```

Example queries:

```sql
-- recent calls with transcripts
SELECT c.time, c.from_caller_name, c.from_number, c.duration, t.text
  FROM calls c JOIN transcripts t USING (uuid)
 ORDER BY c.time DESC LIMIT 20;

-- calls that still need transcribing
SELECT uuid, time, from_number, duration
  FROM calls WHERE recording = 1
   AND uuid NOT IN (SELECT uuid FROM transcripts);

-- raw JSON for deep dives
SELECT json_extract(raw, '$.call_events') FROM calls WHERE uuid = ?;
```

---

## Endpoint notes (undocumented Talk API)

All paths prefixed with `/proxy/talk/api/`.

| Path                                | Notes |
|-------------------------------------|-------|
| `GET call_log?page=N&per_page=M`    | **page is 0-indexed**. Returns `{records, total_count}` |
| `GET call_log/recording/<uuid>`     | Streams the MP3 recording |
| `GET transcript/<uuid>`             | Native transcript. Returns `null` on 5.1.2 — feature stubbed but not shipped, even on Pro |
| `GET info`                          | Controller/Talk version, host device, feature flags |
| `GET users`                         | Talk users |
| `GET devices`                       | Registered phones |
| `GET contacts`                      | Contacts |

Ubiquiti can break any of this at any time.

---

## Quick CLI commands

```sh
op run --env-file .env -- python unifi_talk.py list            # table of all calls
op run --env-file .env -- python unifi_talk.py sync            # fetch-only, no transcription
op run --env-file .env -- python unifi_talk.py sync --transcribe
op run --env-file .env -- python unifi_talk.py info            # controller info
```
