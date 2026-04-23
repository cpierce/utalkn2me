#!/usr/bin/env python3
"""Fetch UniFi Talk call logs via the undocumented internal API."""
from __future__ import annotations

import argparse
import csv
import http.cookiejar
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import urljoin, unquote

import requests
import urllib3

import db
import transcribe as tx

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass
class Creds:
    host: str
    username: str
    password: str
    totp_ref: str | None = None
    totp: str | None = None


def op_read(ref: str) -> str:
    r = subprocess.run(["op", "read", ref], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def op_totp(ref: str) -> str:
    if not ref.startswith("op://"):
        raise ValueError(f"not a 1Password ref: {ref}")
    parts = ref[len("op://"):].split("/")
    if len(parts) < 2:
        raise ValueError(f"ref must be op://<vault>/<item>[/<field>]: {ref}")
    vault, item = unquote(parts[0]), unquote(parts[1])
    r = subprocess.run(
        ["op", "item", "get", item, "--vault", vault, "--otp"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _resolve(val_or_ref: str) -> str:
    """Return the literal value, or resolve via `op` if the value is an op:// ref.
    If `op` isn't installed, assumes the caller resolved it (e.g. via `op run`)."""
    if val_or_ref.startswith("op://"):
        return op_read(val_or_ref)
    return val_or_ref


def load_creds() -> Creds:
    host = os.environ.get("UNIFI_HOST", "https://192.168.1.1")
    user = os.environ.get("UNIFI_USER_REF") or os.environ.get("UNIFI_USER")
    pw = os.environ.get("UNIFI_PASS_REF") or os.environ.get("UNIFI_PASS")
    # UNIFI_TOTP_REF points to a 1P ref (for host-side `op` use).
    # UNIFI_TOTP is a literal 6-digit code (for `op run` style injection).
    totp_ref = os.environ.get("UNIFI_TOTP_REF")
    totp = os.environ.get("UNIFI_TOTP")
    if not user or not pw:
        raise SystemExit("Set UNIFI_USER(_REF) and UNIFI_PASS(_REF)")
    return Creds(
        host=host.rstrip("/"),
        username=_resolve(user),
        password=_resolve(pw),
        totp_ref=totp_ref,
        totp=totp,
    )


class UniFiClient:
    def __init__(self, creds: Creds, session_file: str | None = None):
        self.creds = creds
        self.session_file = session_file
        self.session = requests.Session()
        self.session.verify = False
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
        r = self.session.get(
            self._url("/proxy/talk/api/info"),
            headers={"X-CSRF-Token": self.csrf or "", "Accept": "application/json"},
        )
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

    def login(self) -> None:
        if self._load_session():
            return  # reused persisted session
        body = {
            "username": self.creds.username,
            "password": self.creds.password,
            "rememberMe": False,
        }
        # TOTP: literal wins over ref (literal is fresher).
        totp = self.creds.totp or (op_totp(self.creds.totp_ref) if self.creds.totp_ref else None)
        if totp:
            body["token"] = totp
        r = self.session.post(
            self._url("/api/auth/login"),
            json=body,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code == 499:
            raise SystemExit(
                "MFA required. Pass UNIFI_TOTP (6-digit code) or UNIFI_TOTP_REF."
            )
        r.raise_for_status()
        self.csrf = r.headers.get("X-CSRF-Token") or r.headers.get("X-Updated-CSRF-Token")
        self._save_session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(
            self._url(path),
            params=params,
            headers={"Accept": "application/json", "X-CSRF-Token": self.csrf or ""},
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
        )
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        return dest


# ---------- commands ----------

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
            try:
                text, source = tx.transcribe(mp3, model=args.whisper_model, prefer=args.whisper_backend)
            except tx.TranscribeError as e:
                print(f"  ! {uuid}  transcription failed: {e}")
                continue
            db.save_transcript(conn, uuid, text, source=source, model=args.whisper_model)
            print(f"  ✓ {uuid}  {source}  ({len(text)} chars)")

    s = db.stats(conn)
    print(f"[sync] stats: {s}")
    return 0


def cmd_info(client: UniFiClient, args) -> int:
    print(json.dumps(client.info(), indent=2))
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
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="print calls (no DB)")
    p_list.add_argument("--format", choices=["table", "json", "csv"], default="table")
    p_list.add_argument("--output")
    p_list.set_defaults(func=cmd_list)

    p_info = sub.add_parser("info", help="dump /proxy/talk/api/info")
    p_info.set_defaults(func=cmd_info)

    return p


def main() -> int:
    args = build_parser().parse_args()
    if not getattr(args, "func", None):
        build_parser().print_help(); return 2

    session_file = os.environ.get("SESSION_FILE", "data/session.json")
    while True:
        try:
            creds = load_creds()
            client = UniFiClient(creds, session_file=session_file)
            client.login()
            rc = args.func(client, args)
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            rc = 1
        if not args.loop:
            return rc
        print(f"[loop] sleeping {args.loop}s…")
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
