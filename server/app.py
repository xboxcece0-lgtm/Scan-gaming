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
  LIVEBOX_PASSWORD mot de passe administrateur de la Livebox : active la
                 détection via la box (recommandé pour les Livebox Orange)
  LIVEBOX_HOST   adresse de la Livebox (défaut 192.168.1.1)
  LIVEBOX_USER   identifiant administrateur (défaut admin)
  LIVE_INTERVAL  secondes entre deux relevés de trafic Livebox (défaut 10)
  ACTIVE_KBPS    débit (kbit/s) à partir duquel un appareil compte comme
                 « actif », c.-à-d. réellement utilisé (défaut 300)
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
from livebox import Livebox, LiveboxError, guess_kind  # noqa: E402
from db import Store, day_bounds, overlap_seconds  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
INTERVAL = float(os.environ.get("SCAN_INTERVAL", 60))
GRACE = float(os.environ.get("SCAN_GRACE", 180))
PORT = int(os.environ.get("PORT", 8080))
PASSWORD = os.environ.get("APP_PASSWORD", "")
RETENTION = int(os.environ.get("RETENTION_DAYS", 400))
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "data" / "scan-gaming.db"))
LIVEBOX_PASSWORD = os.environ.get("LIVEBOX_PASSWORD", "")
LIVE_INTERVAL = float(os.environ.get("LIVE_INTERVAL", 10))
ACTIVE_BPS = float(os.environ.get("ACTIVE_KBPS", 300)) * 1000

log = logging.getLogger("scan-gaming")
state = {"last_scan": None, "last_error": None, "network": None, "source": None, "scanning": False}
live: dict[str, dict] = {}  # {mac: {"down": bit/s, "up": bit/s}} — clients Wi‑Fi mesurés par la box
scan_now = threading.Event()


class Detector:
    """Choisit la source de détection : Livebox si configurée, sinon scan ARP."""

    def __init__(self, store: Store, box: Livebox | None):
        self.store = store
        self.box = box
        net_env = os.environ.get("SCAN_NETWORK")
        self._network = ipaddress.ip_network(net_env, strict=False) if net_env else None

    @property
    def network(self) -> ipaddress.IPv4Network:
        if self._network is None:
            self._network = scanner.detect_network()
        return self._network

    def detect(self) -> tuple[dict[str, dict], str | None]:
        """Renvoie ({mac: infos}, avertissement éventuel)."""
        warning = None
        if self.box:
            try:
                found = self.box.active_devices()
                state["source"] = "Livebox"
                return {mac: {"ip": d["ip"], "hostname": d["name"], "link": d["link"],
                              "kind": guess_kind(d["type"], d["name"])}
                        for mac, d in found.items()}, None
            except LiveboxError as exc:
                warning = f"{exc} — repli sur le scan réseau"
                log.warning(warning)
        state["source"], state["network"] = "scan réseau", str(self.network)
        known = self.store.known_hostnames()
        return {mac: {"ip": ip, "hostname": known.get(mac) or scanner.reverse_dns(ip)}
                for mac, ip in scanner.scan(self.network).items()}, warning


def traffic_loop(store: Store, box: Livebox) -> None:
    """Relève les compteurs Wi‑Fi de la box pour le débit en direct et le temps actif."""
    prev: dict[str, tuple[int, int]] = {}
    prev_t = 0.0
    while True:
        try:
            counters = box.wifi_counters()
            now = time.time()
            dt = now - prev_t
            rates = {}
            if prev and 0 < dt < 5 * LIVE_INTERVAL:  # ignore les trous (box redémarrée…)
                for mac, (tx, rx) in counters.items():
                    if mac in prev:
                        rates[mac] = {"down": max(0, tx - prev[mac][0]) * 8 / dt,
                                      "up": max(0, rx - prev[mac][1]) * 8 / dt}
                active = [m for m, r in rates.items() if r["down"] + r["up"] >= ACTIVE_BPS]
                if active:
                    store.add_activity(active, date.today().isoformat(), dt)
            live.clear()
            live.update(rates)
            prev, prev_t = counters, now
        except Exception:  # la boucle principale signale déjà les erreurs de box
            log.debug("relevé de trafic impossible", exc_info=True)
            live.clear()
            prev = {}
        time.sleep(LIVE_INTERVAL)


def scan_loop(store: Store, box: Livebox | None) -> None:
    detector = Detector(store, box)
    last_purge = 0.0
    while True:
        started = time.time()
        state["scanning"] = True
        try:
            seen, warning = detector.detect()
            store.record_scan(seen, time.time(), GRACE)
            state["last_scan"], state["last_error"] = time.time(), warning
            log.info("scan (%s) : %d appareil(s) en ligne", state["source"], len(seen))
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


def with_live(devices: list[dict]) -> list[dict]:
    """Ajoute le débit instantané et l'état « actif » aux appareils mesurés."""
    for dev in devices:
        r = live.get(dev["mac"]) if dev["online"] else None
        dev["rate_down"] = round(r["down"]) if r else None
        dev["rate_up"] = round(r["up"]) if r else None
        dev["active_now"] = bool(r and r["down"] + r["up"] >= ACTIVE_BPS)
    return devices


def parse_day(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


def day_usage(store: Store, d: date) -> dict:
    start, end = day_bounds(d)
    by_mac: dict[str, list] = {}
    for s in store.sessions_between(start, end):
        by_mac.setdefault(s["mac"], []).append(s)
    active = store.activity(d.isoformat(), d.isoformat())
    devices = []
    for dev in with_live(store.devices()):
        sessions = by_mac.get(dev["mac"], [])
        devices.append({
            **dev,
            "seconds": round(overlap_seconds(sessions, start, end, INTERVAL)),
            "active_seconds": round(active[dev["mac"]][d.isoformat()]) if dev["mac"] in active else None,
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
    act = store.activity(labels[0], labels[-1])
    active = {mac: [round(days_map.get(day, 0)) for day in labels] for mac, days_map in act.items()}
    return {"days": labels, "usage": result, "active": active}


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
                return self._json({**state, "interval": INTERVAL, "grace": GRACE,
                                   "live": bool(live), "active_kbps": ACTIVE_BPS / 1000})
            if url.path == "/api/devices":
                return self._json(with_live(self.store.devices()))
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
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    Handler.store = store
    box = Livebox(LIVEBOX_PASSWORD, os.environ.get("LIVEBOX_HOST", "192.168.1.1"),
                  os.environ.get("LIVEBOX_USER", "admin")) if LIVEBOX_PASSWORD else None
    threading.Thread(target=scan_loop, args=(store, box), daemon=True).start()
    if box:
        threading.Thread(target=traffic_loop, args=(store, box), daemon=True).start()
    log.info("détection via %s — interface sur http://0.0.0.0:%d",
             "la Livebox" if LIVEBOX_PASSWORD else "scan réseau", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
