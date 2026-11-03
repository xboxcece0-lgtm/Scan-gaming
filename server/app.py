"""Scan Gaming — collecteur réseau + interface web (installable sur iPhone).

Lancement :  python3 server/app.py
Variables d'environnement (toutes optionnelles) :
  SCAN_NETWORK   sous-réseau à scanner, ex. 192.168.1.0/24 (détecté sinon)
  SCAN_INTERVAL  secondes entre deux scans (défaut 60)
  SCAN_GRACE     secondes d'absence avant de clore une session (défaut 180)
  PORT           port HTTP (défaut 8080)
  DB_PATH        fichier SQLite (défaut data/scan-gaming.db)
  APP_PASSWORD   mot de passe de l'interface (authentification HTTP basique)
  RETENTION_DAYS durée de conservation de l'historique (défaut 400)
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import scanner  # noqa: E402
from db import Store, day_bounds, overlap_seconds  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
INTERVAL = float(os.environ.get("SCAN_INTERVAL", 60))
GRACE = float(os.environ.get("SCAN_GRACE", 180))
PORT = int(os.environ.get("PORT", 8080))
PASSWORD = os.environ.get("APP_PASSWORD", "")
RETENTION = int(os.environ.get("RETENTION_DAYS", 400))
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "data" / "scan-gaming.db"))

log = logging.getLogger("scan-gaming")
state = {"last_scan": None, "last_error": None, "network": None, "scanning": False}
scan_now = threading.Event()


def scan_loop(store: Store, network: ipaddress.IPv4Network) -> None:
    last_purge = 0.0
    while True:
        started = time.time()
        state["scanning"] = True
        try:
            seen = scanner.scan(network)
            known = store.known_hostnames()
            hosts = {mac: (known.get(mac) or scanner.reverse_dns(ip)) for mac, ip in seen.items()}
            store.record_scan(seen, hosts, time.time(), GRACE)
            state["last_scan"], state["last_error"] = time.time(), None
            log.info("scan: %d appareil(s) en ligne", len(seen))
            if time.time() - last_purge > 86400:
                store.purge_older_than(RETENTION)
                last_purge = time.time()
        except Exception as exc:  # on ne veut jamais arrêter la boucle
            state["last_error"] = str(exc)
            log.exception("échec du scan")
        finally:
            state["scanning"] = False
        scan_now.wait(max(1.0, INTERVAL - (time.time() - started)))
        scan_now.clear()


def parse_day(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


def day_usage(store: Store, d: date) -> dict:
    start, end = day_bounds(d)
    by_mac: dict[str, list] = {}
    for s in store.sessions_between(start, end):
        by_mac.setdefault(s["mac"], []).append(s)
    devices = []
    for dev in store.devices():
        sessions = by_mac.get(dev["mac"], [])
        devices.append({
            **dev,
            "seconds": round(overlap_seconds(sessions, start, end, INTERVAL)),
            "sessions": [
                {"start": max(s["start"], start), "end": min(s["end"] + INTERVAL, end, time.time()),
                 "open": bool(s["open"])}
                for s in sessions
            ],
        })
    return {"day": d.isoformat(), "start": start, "end": end, "devices": devices}


def week_usage(store: Store, last: date, days: int = 7) -> dict:
    first = last - timedelta(days=days - 1)
    start, _ = day_bounds(first)
    _, end = day_bounds(last)
    by_mac: dict[str, list] = {}
    for s in store.sessions_between(start, end):
        by_mac.setdefault(s["mac"], []).append(s)
    labels = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    bounds = [day_bounds(first + timedelta(days=i)) for i in range(days)]
    result = {mac: [round(overlap_seconds(ss, ds, de, INTERVAL)) for ds, de in bounds]
              for mac, ss in by_mac.items()}
    return {"days": labels, "usage": result}


class Handler(BaseHTTPRequestHandler):
    store: Store
    server_version = "ScanGaming/1.0"

    def log_message(self, fmt, *args):  # journal HTTP discret
        log.debug(fmt, *args)

    # --- utilitaires ----------------------------------------------------------------
    def _authorized(self) -> bool:
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                return False
            return hmac.compare_digest(pwd, PASSWORD)
        return False

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def _guard(self) -> bool:
        if self._authorized():
            return True
        self._send(401, b"Authentification requise", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="Scan Gaming"'})
        return False

    # --- routes ---------------------------------------------------------------------
    def do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/status":
                return self._json({**state, "interval": INTERVAL, "grace": GRACE})
            if url.path == "/api/devices":
                return self._json(self.store.devices())
            if url.path == "/api/usage":
                return self._json(day_usage(self.store, parse_day(q.get("day"))))
            if url.path == "/api/week":
                return self._json(week_usage(self.store, parse_day(q.get("day"))))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._static(url.path)

    def do_POST(self):
        if not self._guard():
            return
        if urlparse(self.path).path == "/api/scan":
            scan_now.set()
            return self._json({"ok": True})
        self._json({"error": "introuvable"}, 404)

    def do_PATCH(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/devices/"):
            mac = unquote(path.rsplit("/", 1)[1]).lower()
            try:
                dev = self.store.update_device(mac, self._body())
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json(dev) if dev else self._json({"error": "appareil inconnu"}, 404)
        self._json({"error": "introuvable"}, 404)

    def do_DELETE(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/devices/"):
            self.store.delete_device(unquote(path.rsplit("/", 1)[1]).lower())
            return self._json({"ok": True})
        self._json({"error": "introuvable"}, 404)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (WEB / rel).resolve()
        if WEB not in file.parents or not file.is_file():
            return self._send(404, b"Introuvable", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix == ".webmanifest":
            ctype = "application/manifest+json"
        self._send(200, file.read_bytes(), ctype)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    net_env = os.environ.get("SCAN_NETWORK")
    network = ipaddress.ip_network(net_env, strict=False) if net_env else scanner.detect_network()
    state["network"] = str(network)
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    Handler.store = store
    threading.Thread(target=scan_loop, args=(store, network), daemon=True).start()
    log.info("réseau scanné : %s — interface sur http://0.0.0.0:%d", network, PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
