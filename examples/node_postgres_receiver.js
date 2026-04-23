// Node.js receiver → Postgres for utalkn2me webhooks.
//
// Demonstrates that the producer is language-agnostic — any HTTP server
// that can parse JSON works. Here: Express + `pg`, upsert by uuid.
//
//   npm install express pg
//   node node_postgres_receiver.js
//
// Env:
//   EXPECTED_TOKEN  — bearer token the pusher must send
//   PG_URL          — e.g. postgres://user:pw@host:5432/dbname
//   PORT            — default 9000
//
// Postgres schema:
//
//   CREATE TABLE calls (
//     uuid              UUID PRIMARY KEY,
//     call_time         TIMESTAMPTZ,
//     direction         TEXT,
//     from_number       TEXT,
//     to_number         TEXT,
//     from_caller_name  TEXT,
//     duration          INT,
//     status            TEXT,
//     transcript_text   TEXT,
//     raw               JSONB,
//     received_at       TIMESTAMPTZ DEFAULT now()
//   );
//   CREATE INDEX ON calls (call_time DESC);

const express = require('express');
const { Pool } = require('pg');

const app = express();
app.use(express.json({ limit: '5mb' }));

const pool = new Pool({ connectionString: process.env.PG_URL });
const EXPECTED = process.env.EXPECTED_TOKEN || '';

function authOk(req) {
  if (!EXPECTED) return true;
  return req.headers.authorization === `Bearer ${EXPECTED}`;
}

async function upsertCall(p) {
  const t = p.transcript || {};
  await pool.query(
    `INSERT INTO calls (uuid, call_time, direction, from_number, to_number,
                        from_caller_name, duration, status, transcript_text, raw)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
     ON CONFLICT (uuid) DO UPDATE SET
       call_time        = EXCLUDED.call_time,
       direction        = EXCLUDED.direction,
       from_number      = EXCLUDED.from_number,
       to_number        = EXCLUDED.to_number,
       from_caller_name = EXCLUDED.from_caller_name,
       duration         = EXCLUDED.duration,
       status           = EXCLUDED.status,
       transcript_text  = COALESCE(EXCLUDED.transcript_text, calls.transcript_text),
       raw              = EXCLUDED.raw`,
    [
      p.uuid,
      p.time,
      p.direction,
      p.from,
      p.to,
      p.from_caller_name,
      p.duration,
      p.status,
      t.text || null,
      p,
    ]
  );
}

async function upsertTranscript(p) {
  const t = p.transcript || {};
  await pool.query(
    `UPDATE calls SET transcript_text = $1 WHERE uuid = $2`,
    [t.text || null, p.uuid]
  );
}

app.post('/ingest/utalkn2me', async (req, res) => {
  if (!authOk(req)) return res.status(401).json({ error: 'unauthorized' });
  const p = req.body || {};
  try {
    if (p.event === 'call.upserted')        await upsertCall(p);
    else if (p.event === 'transcript.upserted') await upsertTranscript(p);
    else return res.status(400).json({ error: `unknown event ${p.event}` });
    res.json({ ok: true, uuid: p.uuid });
  } catch (err) {
    console.error('ingest error', err);
    res.status(500).json({ error: 'db error' });
  }
});

app.get('/health', (_req, res) => res.json({ ok: true }));

const port = process.env.PORT || 9000;
app.listen(port, () => console.log(`utalkn2me receiver on :${port}`));
