#!/usr/bin/env python3
"""Fetch UniFi Talk call logs via the undocumented internal API."""
from __future__ import annotations

import argparse
import csv
import http.cookiejar
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import db
import transcribe as tx

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# (connect, read) timeouts. Without these a single hung TCP connection to the
# controller stalls the sync loop forever with no log output.
API_TIMEOUT = (10, 60)
DOWNLOAD_TIMEOUT = (10, 300)

# Touched at every loop iteration / transcription so the compose healthcheck
# can tell a live worker from a wedged one. Empty (host CLI use) = no-op.
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "")


def beat() -> None:
    if not HEARTBEAT_FILE:
        return
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE) or ".", exist_ok=True)
        with open(HEARTBEAT_FILE, "a"):
            pass
        os.utime(HEARTBEAT_FILE, None)
    except OSError:
        pass


class LoginError(RuntimeError):
    """Recoverable login failure — the loop retries next cycle (fresh TOTP)."""


@dataclass
class Creds:
    host: str
    username: str
    password: str
    totp_secret: str | None = None


def op_read(ref: str) -> str:
    r = subprocess.run(["op", "read", ref], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _resolve(val_or_ref: str) -> str:
    """Return the literal value, or resolve via `op` if the value is an op:// ref.
    If `op` isn't installed, assumes the caller resolved it (e.g. via `op run`)."""
    if val_or_ref.startswith("op://"):
        if not shutil.which("op"):
            raise SystemExit(
                "Got an unresolved op:// reference but the `op` CLI is not "
                "available here. Start the stack with `make up` (which wraps "
                "docker compose in `op run` so refs resolve on the host) — "
                "plain `docker compose up` passes the raw op:// strings through."
            )
        return op_read(val_or_ref)
    return val_or_ref


def load_creds() -> Creds:
    host = os.environ.get("UNIFI_HOST", "https://192.168.1.1")
    user = os.environ.get("UNIFI_USER_REF") or os.environ.get("UNIFI_USER")
    pw = os.environ.get("UNIFI_PASS_REF") or os.environ.get("UNIFI_PASS")
    if not user or not pw:
        raise SystemExit("Set UNIFI_USER(_REF) and UNIFI_PASS(_REF)")
    totp = os.environ.get("UNIFI_TOTP_SECRET")
    return Creds(
        host=host.rstrip("/"),
        username=_resolve(user),
        password=_resolve(pw),
        totp_secret=_resolve(totp) if totp else None,
    )


class UniFiClient:
    def __init__(self, creds: Creds, session_file: str | None = None):
        self.creds = creds
        self.session_file = session_file
        self.session = requests.Session()
        self.session.verify = False
        # Retry transient connect failures + 5xx on GETs at the transport
        # layer; POSTs (login) only retry connection setup, never a response.
        retry = Retry(
            total=3, connect=3, read=2, backoff_factor=1.5,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.csrf: str | None = None

    def _url(self, path: str) -> str:
        return urljoin(self.creds.host + "/", path.lstrip("/"))

    def _load_session(self) -> bool:
        if not self.session_file or not os.path.exists(self.session_file):
            return False
        try:
            data = json.load(open(self.session_file))
        except (json.JSONDecodeError, OSError):
            return False
        for c in data.get("cookies", []):
            self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        self.csrf = data.get("csrf")
        # ping Talk info to see if the cookie is still valid
        try:
            r = self.session.get(
                self._url("/proxy/talk/api/info"),
                headers={"X-CSRF-Token": self.csrf or "", "Accept": "application/json"},
                timeout=API_TIMEOUT,
            )
        except requests.RequestException:
            return False
        return r.status_code == 200

    def _save_session(self) -> None:
        if not self.session_file:
            return
        os.makedirs(os.path.dirname(self.session_file) or ".", exist_ok=True)
        data = {
            "csrf": self.csrf,
            "cookies": [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                for c in self.session.cookies
            ],
        }
        with open(self.session_file, "w") as f:
            json.dump(data, f)

    def _current_totp(self) -> str | None:
        if not self.creds.totp_secret:
            return None
        import pyotp
        return pyotp.TOTP(self.creds.totp_secret).now()

    def login(self) -> None:
        if self._load_session():
            return  # reused persisted session
        body = {
            "username": self.creds.username,
            "password": self.creds.password,
            "rememberMe": False,
        }
        totp = self._current_totp()
        if totp:
            body["token"] = totp
        r = self.session.post(
            self._url("/api/auth/login"),
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=API_TIMEOUT,
        )
        if r.status_code == 499:
            # Recoverable: a rejected/expired TOTP lands here too, and the next
            # cycle generates a fresh code — so don't kill the loop over it.
            raise LoginError(
                "MFA challenge failed (HTTP 499). If this persists, set "
                "UNIFI_TOTP_SECRET to the BASE32 shared secret."
            )
        r.raise_for_status()
        self.csrf = r.headers.get("X-CSRF-Token") or r.headers.get("X-Updated-CSRF-Token")
        self._save_session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(
            self._url(path),
            params=params,
            headers={"Accept": "application/json", "X-CSRF-Token": self.csrf or ""},
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def call_log(self, per_page: int = 100):
        page = 0
        while True:
            data = self._get("/proxy/talk/api/call_log",
                             {"page": page, "per_page": per_page})
            records = data.get("records", [])
            if not records:
                break
            for rec in records:
                yield rec
            total = int(data.get("total_count", 0))
            if (page + 1) * per_page >= total:
                break
            page += 1

    def info(self) -> dict:
        return self._get("/proxy/talk/api/info")

    def transcript(self, uuid: str):
        return self._get(f"/proxy/talk/api/transcript/{uuid}")

    def download_recording(self, uuid: str, dest: str) -> str:
        r = self.session.get(
            self._url(f"/proxy/talk/api/call_log/recording/{uuid}"),
            headers={"X-CSRF-Token": self.csrf or ""},
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
        )
        r.raise_for_status()
        # Write to a temp file and rename: an interrupted download must not
        # leave a truncated MP3 that poisons every future transcription pass.
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return dest


# ---------- commands ----------

# Per-uuid transcription failure counts. A file that keeps blowing up (corrupt
# audio, codec edge case) gets skipped after MAX_TRANSCRIBE_ATTEMPTS instead of
# burning a full large-v3 pass every cycle forever. Resets on restart.
_transcribe_failures: dict[str, int] = {}
MAX_TRANSCRIBE_ATTEMPTS = 3


def cmd_sync(client: UniFiClient, args) -> int:
    """Fetch new calls into SQLite, optionally transcribe pending recordings."""
    conn = db.connect(args.db)
    records = list(client.call_log())
    inserted = db.upsert_calls(conn, records)
    print(f"[sync] fetched {len(records)} calls, {inserted} new")

    if args.transcribe:
        pending = db.calls_needing_transcript(conn)
        print(f"[sync] {len(pending)} calls pending transcription")
        os.makedirs(args.recordings_dir, exist_ok=True)
        for row in pending:
            uuid = row["uuid"]
            beat()
            if _transcribe_failures.get(uuid, 0) >= MAX_TRANSCRIBE_ATTEMPTS:
                continue
            try:
                native = client.transcript(uuid)
            except Exception:
                native = None
            if native:
                text = native if isinstance(native, str) else json.dumps(native)
                db.save_transcript(conn, uuid, text, source="native")
                print(f"  ✓ {uuid}  native")
                continue

            mp3 = os.path.join(args.recordings_dir, f"{uuid}.mp3")
            if not os.path.exists(mp3):
                try:
                    client.download_recording(uuid, mp3)
                except requests.HTTPError as e:
                    print(f"  - {uuid}  no recording on server ({e.response.status_code})")
                    continue
                except requests.RequestException as e:
                    print(f"  ! {uuid}  download failed: {e}")
                    continue
            try:
                text, source = tx.transcribe(mp3, model=args.whisper_model, prefer=args.whisper_backend)
            except Exception as e:
                # Broad on purpose: backend/codec errors (not just
                # TranscribeError) must not abort the whole sync cycle.
                _transcribe_failures[uuid] = _transcribe_failures.get(uuid, 0) + 1
                n = _transcribe_failures[uuid]
                give_up = " — giving up until restart" if n >= MAX_TRANSCRIBE_ATTEMPTS else ""
                print(f"  ! {uuid}  transcription failed (attempt {n}/{MAX_TRANSCRIBE_ATTEMPTS}): {e}{give_up}")
                continue
            db.save_transcript(conn, uuid, text, source=source, model=args.whisper_model)
            print(f"  ✓ {uuid}  {source}  ({len(text)} chars)")

    if args.prune_recordings_days > 0:
        pruned = _prune_recordings(conn, args.recordings_dir, args.prune_recordings_days)
        if pruned:
            print(f"[sync] pruned {pruned} MP3s older than {args.prune_recordings_days} days")

    s = db.stats(conn)
    print(f"[sync] stats: {s}")
    return 0


def _prune_recordings(conn, rec_dir: str, days: int) -> int:
    """Delete MP3s for calls older than N days. Keeps DB rows; only audio is removed."""
    pruned = 0
    for uuid in db.recordings_to_prune(conn, days):
        path = os.path.join(rec_dir, f"{uuid}.mp3")
        if os.path.exists(path):
            try:
                os.remove(path)
                pruned += 1
            except OSError:
                pass
    return pruned


def cmd_info(client: UniFiClient, args) -> int:
    print(json.dumps(client.info(), indent=2))
    return 0


def cmd_clean_transcripts(client: UniFiClient, args) -> int:
    """Re-apply the filler/hallucination cleaner to every stored transcript."""
    conn = db.connect(args.db)
    rows = list(conn.execute("SELECT uuid, text FROM transcripts"))
    changed = 0
    for row in rows:
        new = tx.clean_transcript(row["text"])
        if new != row["text"]:
            conn.execute("UPDATE transcripts SET text = ? WHERE uuid = ?", (new, row["uuid"]))
            changed += 1
    conn.commit()
    print(f"[clean] {changed}/{len(rows)} transcripts updated")
    return 0


def cmd_list(client: UniFiClient, args) -> int:
    records = list(client.call_log())
    if args.format == "json":
        out = json.dumps(records, indent=2)
        (open(args.output, "w").write(out) if args.output else print(out))
    elif args.format == "csv":
        if not args.output:
            print("--output required for csv", file=sys.stderr); return 2
        _write_csv(records, args.output)
    else:
        print(f"{'Time (UTC)':<22} {'Dir':<4} {'From':<16} {'To':<16} {'Status':<12} {'Dur':>4} Rec")
        for r in records:
            t = (r.get("time") or "")[:19]
            rec = "Y" if r.get("recording_filename") else "-"
            print(f"{t:<22} {r.get('direction',''):<4} {r.get('from',''):<16} "
                  f"{r.get('to',''):<16} {r.get('status',''):<12} "
                  f"{str(r.get('duration','')):>4} {rec}")
        print(f"\n{len(records)} records")
    return 0


def _write_csv(records, path):
    fields = ["time","direction","from","to","answered_by","status","duration",
              "is_video_call","recording_filename"]
    extras = sorted({k for r in records for k in r.keys()} - set(fields))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields + extras, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)


# ---------- main ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--loop", type=int, metavar="SECONDS",
                   help="run repeatedly, sleeping N seconds between runs")
    sub = p.add_subparsers(dest="cmd")

    p_sync = sub.add_parser("sync", help="fetch into SQLite, optionally transcribe")
    p_sync.add_argument("--db", default="data/calls.db")
    p_sync.add_argument("--transcribe", action="store_true",
                        help="transcribe recordings that don't have a transcript yet")
    p_sync.add_argument("--whisper-model", default=os.environ.get("WHISPER_MODEL", "tiny"))
    p_sync.add_argument("--whisper-backend", default=os.environ.get("WHISPER_BACKEND", "auto"),
                        choices=["auto", "faster-whisper", "whisper"])
    p_sync.add_argument("--recordings-dir", default="data/recordings")
    p_sync.add_argument("--prune-recordings-days",
                        type=int,
                        default=int(os.environ.get("PRUNE_RECORDINGS_DAYS", "30")),
                        help="delete MP3s for calls older than N days (0 disables; default 30)")
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="print calls (no DB)")
    p_list.add_argument("--format", choices=["table", "json", "csv"], default="table")
    p_list.add_argument("--output")
    p_list.set_defaults(func=cmd_list)

    p_info = sub.add_parser("info", help="dump /proxy/talk/api/info")
    p_info.set_defaults(func=cmd_info)

    p_clean = sub.add_parser("clean-transcripts",
                             help="re-apply filler/hallucination cleaner to stored transcripts")
    p_clean.add_argument("--db", default="data/calls.db")
    p_clean.set_defaults(func=cmd_clean_transcripts, offline=True)

    return p


def _handle_sigterm(signum, frame):
    print("[loop] SIGTERM — shutting down", flush=True)
    sys.exit(0)


def main() -> int:
    args = build_parser().parse_args()
    if not getattr(args, "func", None):
        build_parser().print_help(); return 2

    # As container PID 1, Python never gets the kernel's default SIGTERM
    # disposition — without a handler, `docker stop` hangs 10s then SIGKILLs
    # (exit 137). Exit cleanly so restart policies behave.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    session_file = os.environ.get("SESSION_FILE", "data/session.json")
    needs_login = not getattr(args, "offline", False)
    while True:
        beat()
        try:
            if needs_login:
                creds = load_creds()
                client = UniFiClient(creds, session_file=session_file)
                client.login()
            else:
                client = None
            rc = args.func(client, args)
        except (LoginError, requests.RequestException) as e:
            print(f"[error] {e}", file=sys.stderr)
            rc = 1
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            traceback.print_exc()
            rc = 1
        if not args.loop:
            return rc
        beat()
        print(f"[loop] sleeping {args.loop}s…")
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
