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

Three services share a `./data` volume:

- **worker** — pulls the call log every 5 min, transcribes new recordings.
  No network ports.
- **pusher** — reads unpushed rows from SQLite and POSTs them to your
  webhook (`PUSH_URL`). No network ports. Idles if `PUSH_URL` is unset.
- **api** — *optional* read-only Flask API over the local SQLite DB.
  Off by default (opt-in via the `api` profile). Binds to `127.0.0.1` only.

Use the Makefile (wraps `op run` so 1P refs in `.env` resolve on the host):

```sh
cp .env.example .env   # edit

make up                # worker + pusher (no exposed ports at all)
make up-api            # worker + pusher + local API on 127.0.0.1:${API_PORT}

make logs              # tail logs from running services
make sync              # run one sync cycle in a throwaway container
make down              # stop everything
```

Or raw compose:

```sh
op run --env-file .env -- docker compose up -d --build                 # no API
op run --env-file .env -- docker compose --profile api up -d --build   # with API
```

What the stack does:

- Pulls the call log every 5 minutes (`--loop 300`).
- Writes calls to `/data/calls.db` (SQLite).
- Saves MP3s to `/data/recordings/<uuid>.mp3` (auto-pruned after 30 days).
- Transcribes new recordings with `faster-whisper` (`large-v3` by default).
- Persists session cookies to `/data/session.json` so MFA isn't needed after
  the first successful login.
- Pushes everything to your webhook if `PUSH_URL` is configured.

`./data` on the host is the single source of truth — back that up and you
have every call, every recording, every transcript.

### Expose the local API on your LAN

Local API is `127.0.0.1`-only by default. To open it up:

```
# in .env
API_BIND=0.0.0.0
API_PORT=8000
```

Then `make up-api`. Put it behind auth (reverse proxy, Tailscale, or
similar) before exposing it publicly — the API has no built-in
authentication.

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

---

## Recording retention

MP3s accumulate — about 20 MB per hour of recorded audio. The sync loop
deletes MP3s for calls older than `PRUNE_RECORDINGS_DAYS` (default **30**).
Transcripts and call metadata stay forever; only the audio file is removed.

Set `PRUNE_RECORDINGS_DAYS=0` to disable pruning entirely.

---

## Pushing data to your own backend

`utalkn2me` is the local producer. It collects calls and transcripts into
SQLite. A third container service — **pusher** — reads unsent rows from
that local DB and POSTs them to a webhook **you operate**. That's where
you write your own receiver and persist to MySQL, Postgres, DynamoDB,
whatever makes sense for you.

```
[UniFi controller] → [utalkn2me worker] → [SQLite outbox] → [pusher] → [YOUR /ingest webhook] → [your DB]
```

### Config

In `.env`:

```
PUSH_URL=https://api.yourcompany.com/ingest/utalkn2me
PUSH_TOKEN=op://Private/utalkn2me-webhook/secret
PUSH_SITE_ID=home-office-01
PUSH_BATCH=10          # rows per HTTP request
PUSH_INTERVAL=5        # seconds between batches
PUSH_TIMEOUT=30
```

Leave `PUSH_URL` empty to run in local-only mode (pusher idles, everything
else still works — use the REST API or query SQLite directly).

### Payload contract

Every row is delivered as a single JSON POST. Two event types:

**`call.upserted`** — sent when a new call appears, resent when the row
changes (e.g., a late transcript arrives on a Pro-plan install in the future):

```json
{
  "schema_version": 1,
  "event": "call.upserted",
  "site_id": "home-office-01",
  "uuid": "00000000-0000-0000-0000-000000000001",
  "time": "2026-04-22T16:30:51.300Z",
  "direction": "in",
  "from": "+15555550123",
  "to":   "+15555550199",
  "from_caller_name": "EXAMPLE CALLER",
  "answered_by": "0008",
  "status": "accepted",
  "duration": 132,
  "is_video_call": false,
  "recording_available": true,
  "recording_filename": "20260422_113120_15555550123_101_abcdef.mp3",
  "quality_score": 100,
  "to_smart_attendant_title": "Front Desk",
  "first_seen": "2026-04-22 16:31:05",
  "transcript": {
    "text": "Thanks for calling — how can I help?…",
    "source": "faster-whisper",
    "model": "large-v3",
    "created_at": "2026-04-22 16:31:40"
  },
  "raw": {
    "…the complete original Talk API record, including call_events timeline…"
  }
}
```

**`transcript.upserted`** — sent when a transcript is produced for a call
that was already pushed earlier. Small, targeted update:

```json
{
  "schema_version": 1,
  "event": "transcript.upserted",
  "site_id": "home-office-01",
  "uuid": "00000000-0000-0000-0000-000000000001",
  "call_time": "2026-04-22T16:30:51.300Z",
  "transcript": {
    "text": "…",
    "source": "faster-whisper",
    "model": "large-v3",
    "created_at": "2026-04-22 16:31:40"
  }
}
```

### Guarantees

- **Idempotent** — every payload has a stable `uuid`. Upsert by it. Retries
  are safe.
- **At-least-once** — a 5xx or network failure retries indefinitely until it
  acks. Your receiver may see the same `uuid` twice.
- **In order per-uuid** — a `call.upserted` for a uuid always precedes any
  `transcript.upserted` for the same uuid (because the call row exists
  before the transcript does).
- **Eventually consistent** — during downtime the outbox just grows; when
  the endpoint comes back, everything catches up within `PUSH_BATCH /
  PUSH_INTERVAL` per second.
- **4xx = permanent** — malformed payloads, auth failures, etc. are not
  retried. The row gets marked as pushed with `push_error` set, and you
  look in SQLite to debug.

### Receiver examples

Working templates in [`examples/`](examples/). Same contract, four runtimes:

- **[`lambda_receiver.py`](examples/lambda_receiver.py)** — AWS Lambda
  behind API Gateway → DynamoDB. Zero-ops serverless pattern.
- **[`flask_mysql_receiver.py`](examples/flask_mysql_receiver.py)** —
  Flask + PyMySQL → MySQL. Classic setup.
- **[`node_postgres_receiver.js`](examples/node_postgres_receiver.js)** —
  Node.js + Express + `pg` → Postgres. Shows the producer is
  language-agnostic.
- **[`php_mysql_receiver.php`](examples/php_mysql_receiver.php)** —
  single-file PHP + PDO → MySQL. Drop into any LAMP host.

All follow the same recipe:

1. Check `Authorization: Bearer <token>` against your expected value.
2. Parse the JSON body.
3. Switch on `event`: `call.upserted` → upsert by `uuid`;
   `transcript.upserted` → update the row.
4. Return `200 OK`.

Pick the columns you care about from the top-level fields (`from`, `to`,
`duration`, `from_caller_name`, `status`, `transcript.text`), and stash
the rest in a JSON column via `raw` if you want to query it later.

### Cookbook — useful consumer queries

```sql
-- Call volume by day, last 30 days
SELECT DATE(call_time) AS day, COUNT(*) AS calls
  FROM calls WHERE call_time > NOW() - INTERVAL 30 DAY
 GROUP BY day ORDER BY day DESC;

-- Full-text search across transcripts (MySQL 5.7+)
ALTER TABLE calls ADD FULLTEXT INDEX transcript_ft (transcript_text);
SELECT uuid, call_time, from_caller_name, transcript_text
  FROM calls
 WHERE MATCH(transcript_text) AGAINST('invoice overdue' IN NATURAL LANGUAGE MODE);

-- Top callers this week
SELECT from_number, from_caller_name, COUNT(*) AS n, SUM(duration) AS total_sec
  FROM calls WHERE call_time > NOW() - INTERVAL 7 DAY AND direction='in'
 GROUP BY from_number ORDER BY n DESC LIMIT 20;

-- Long voicemails that never got called back (join with your outbound side)
SELECT call_time, from_caller_name, duration, transcript_text
  FROM calls
 WHERE status = 'voicemail' AND duration > 30
   AND from_number NOT IN (SELECT to_number FROM calls WHERE direction = 'out')
 ORDER BY call_time DESC;
```
