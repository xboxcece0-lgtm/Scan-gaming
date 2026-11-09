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
  TRAFFIC_RETENTION_DAYS conservation du débit minute par minute (défaut 62)
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
from livebox import READING_SECONDS, Livebox, LiveboxError, guess_kind  # noqa: E402
from db import Store, day_bounds, overlap_seconds  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
INTERVAL = float(os.environ.get("SCAN_INTERVAL", 60))
GRACE = float(os.environ.get("SCAN_GRACE", 180))
PORT = int(os.environ.get("PORT", 8080))
PASSWORD = os.environ.get("APP_PASSWORD", "")
RETENTION = int(os.environ.get("RETENTION_DAYS", 400))
TRAFFIC_RETENTION = int(os.environ.get("TRAFFIC_RETENTION_DAYS", 62))  # historique minute par minute
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "data" / "scan-gaming.db"))
LIVEBOX_PASSWORD = os.environ.get("LIVEBOX_PASSWORD", "")
LIVE_INTERVAL = float(os.environ.get("LIVE_INTERVAL", 10))
ACTIVE_BPS = float(os.environ.get("ACTIVE_KBPS", 300)) * 1000

log = logging.getLogger("scan-gaming")
state = {"last_scan": None, "last_error": None, "network": None, "source": None, "scanning": False}
live: dict[str, dict] = {}        # {mac: {"down": bit/s, "up": bit/s}} — clients Wi‑Fi (compteurs AP)
live_homelan: dict[str, dict] = {}  # idem, autres appareils (relevés HomeLan de 30 s)
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
                              "iface": d["iface"], "kind": guess_kind(d["type"], d["name"])}
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
                minute = int(now // 60)
                store.add_traffic([(m, minute, r["down"] * dt / 8, r["up"] * dt / 8)
                                   for m, r in rates.items() if r["down"] + r["up"] > 0])
            live.clear()
            live.update(rates)
            prev, prev_t = counters, now
        except Exception:  # la boucle principale signale déjà les erreurs de box
            log.debug("relevé de trafic impossible", exc_info=True)
            live.clear()
            prev = {}
        time.sleep(LIVE_INTERVAL)


def homelan_loop(store: Store, box: Livebox) -> None:
    """Débit via les relevés HomeLan de la box (fenêtres de 30 s, par adresse MAC).

    Couvre les appareils filaires, y compris derrière un switch. Les appareils connectés
    via un répéteur qui traduit les adresses MAC (TP-Link RE305…) ne sont pas mesurables :
    la box attribue leur trafic au répéteur. Les clients Wi‑Fi directs gardent la mesure plus réactive des
    compteurs Wi‑Fi (traffic_loop). Repli : compteur du port Ethernet si l'appareil
    est seul sur ce port.
    """
    seen_ts: dict[str, int] = {}
    while True:
        rates, active, samples = {}, [], []
        try:
            devs = box.active_devices()
            try:
                dev = box.device_readings()
            except LiveboxError:
                dev = {}
            # Un appareil inactif garde son dernier relevé (parfois vieux de plusieurs
            # jours) : on compare à l'horloge de la box, donnée par le relevé le plus récent.
            ref = max((r.get("Timestamp") or 0 for r in dev.values()), default=0)
            wired = {m: d for m, d in devs.items() if d["link"] == "filaire"}
            per_port: dict[str, int] = {}
            for d in wired.values():
                per_port[d["port"]] = per_port.get(d["port"], 0) + 1
            need_ports = any(m not in dev and per_port.get(d["port"]) == 1 for m, d in wired.items())
            ports = box.port_readings() if need_ports else {}
            for mac, d in devs.items():
                if mac in live:  # client Wi‑Fi direct : déjà mesuré toutes les 10 s
                    continue
                r = dev.get(mac)
                if r is None and mac in wired and per_port.get(d["port"]) == 1:
                    r = ports.get(d["port"])
                if r is None:
                    continue
                ts = r.get("Timestamp") or 0
                fresh = not ref or ref - ts <= 3 * READING_SECONDS
                down = (r.get("Tx_Counter") or 0) / READING_SECONDS if fresh else 0.0
                up = (r.get("Rx_Counter") or 0) / READING_SECONDS if fresh else 0.0
                rates[mac] = {"down": down, "up": up}
                if fresh and ts != seen_ts.get(mac):
                    if down + up >= ACTIVE_BPS:
                        active.append(mac)
                    if down + up > 0:  # bits sur 30 s → octets, à la minute du relevé
                        samples.append((mac, int(ts // 60), down * READING_SECONDS / 8, up * READING_SECONDS / 8))
                seen_ts[mac] = ts
            if active:
                store.add_activity(active, date.today().isoformat(), READING_SECONDS)
            if samples:
                store.add_traffic(samples)
        except Exception:
            log.debug("relevé HomeLan impossible", exc_info=True)
        live_homelan.clear()
        live_homelan.update(rates)
        time.sleep(READING_SECONDS)


def enforce_limits(store: Store, box: Livebox) -> None:
    """Coupe Internet aux appareils « coupure automatique » qui ont atteint leur limite,
    et le rétablit le lendemain pour ceux coupés par la limite (pas les coupures manuelles)."""
    today = date.today()
    start_today, _ = day_bounds(today)
    usage = {d["mac"]: d for d in day_usage(store, today)["devices"]}
    for dev in store.devices():
        mac = dev["mac"]
        if dev["blocked"] and dev["blocked_by"] == "limite" and (dev["blocked_at"] or 0) < start_today:
            try:
                box.set_blocked(mac, False)
                store.set_blocked(mac, False, None)
                log.info("nouveau jour : accès Internet rétabli pour %s", dev["name"] or mac)
            except LiveboxError as exc:
                log.warning("rétablissement impossible pour %s : %s", mac, exc)
            continue
        if dev["blocked"] or not (dev["auto_block"] and dev["tracked"] and dev["daily_limit"]):
            continue
        u = usage.get(mac) or {}
        used = u.get("active_seconds") if u.get("active_seconds") is not None else u.get("seconds", 0)
        if used >= dev["daily_limit"] * 60:
            try:
                box.set_blocked(mac, True)
                store.set_blocked(mac, True, "limite")
                log.info("limite atteinte : accès Internet coupé pour %s", dev["name"] or mac)
            except LiveboxError as exc:
                log.warning("coupure impossible pour %s : %s", mac, exc)


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
            if box:
                enforce_limits(store, box)
            if time.time() - last_purge > 86400:
                store.purge_older_than(RETENTION)
                store.purge_traffic_older_than(TRAFFIC_RETENTION)
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
        r = (live.get(dev["mac"]) or live_homelan.get(dev["mac"])) if dev["online"] else None
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


def device_detail(store: Store, mac: str, d: date) -> dict | None:
    """Fiche d'un appareil pour un jour : temps, sessions, débit par tranches de 5 min."""
    usage = day_usage(store, d)
    dev = next((x for x in usage["devices"] if x["mac"] == mac), None)
    if dev is None:
        return None
    start, end = usage["start"], usage["end"]
    bins = [[0.0, 0.0] for _ in range(288)]  # 288 tranches de 5 minutes
    peak, peak_at = 0.0, None
    for minute, down, up in store.traffic_between(mac, start, end):
        i = int((minute * 60 - start) // 300)
        if 0 <= i < 288:
            bins[i][0] += down
            bins[i][1] += up
        rate = (down + up) * 8 / 60
        if rate > peak:
            peak, peak_at = rate, minute * 60
    return {**dev, "day": usage["day"], "start": start, "end": end,
            "bins": [[round(a), round(b)] for a, b in bins],
            "total_down": round(sum(b[0] for b in bins)), "total_up": round(sum(b[1] for b in bins)),
            "peak_bps": round(peak), "peak_at": peak_at}


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
    box: Livebox | None = None
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
        self._safe(self._do_GET)

    def _do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/status":
                return self._json({**state, "interval": INTERVAL, "grace": GRACE,
                                   "live": bool(live or live_homelan), "active_kbps": ACTIVE_BPS / 1000,
                                   "can_block": self.box is not None})
            if url.path == "/api/devices":
                return self._json(with_live(self.store.devices()))
            if url.path == "/api/usage":
                return self._json(day_usage(self.store, parse_day(q.get("day"))))
            if url.path == "/api/device":
                mac = (q.get("mac") or "").lower()
                if self.box:  # resynchronise avec la box (coupure faite depuis l'appli Orange…)
                    real = self.box.is_blocked(mac)
                    dev = next((x for x in self.store.devices() if x["mac"] == mac), None)
                    if dev and bool(dev["blocked"]) != real:
                        self.store.set_blocked(mac, real, "manuel" if real else None)
                detail = device_detail(self.store, mac, parse_day(q.get("day")))
                return self._json(detail) if detail else self._json({"error": "appareil inconnu"}, 404)
            if url.path == "/api/week":
                return self._json(week_usage(self.store, parse_day(q.get("day"))))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._static(url.path)

    def do_POST(self):
        self._safe(self._do_POST)

    def _do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path == "/api/scan":
            scan_now.set()
            return self._json({"ok": True})
        if path.startswith("/api/devices/") and path.endswith("/block"):
            if not self.box:
                return self._json({"error": "la coupure nécessite la Livebox (LIVEBOX_PASSWORD)"}, 400)
            mac = unquote(path.split("/")[3]).lower()
            blocked = bool(self._body().get("blocked"))
            try:
                self.box.set_blocked(mac, blocked)
            except LiveboxError as exc:
                return self._json({"error": str(exc)}, 502)
            self.store.set_blocked(mac, blocked, "manuel")
            log.info("accès Internet %s manuellement pour %s", "coupé" if blocked else "rétabli", mac)
            return self._json({"ok": True, "blocked": blocked})
        self._json({"error": "introuvable"}, 404)

    def do_PATCH(self):
        self._safe(self._do_PATCH)

    def _do_PATCH(self):
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
        self._safe(self._do_DELETE)

    def _do_DELETE(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path.startswith("/api/devices/"):
            self.store.delete_device(unquote(path.rsplit("/", 1)[1]).lower())
            return self._json({"ok": True})
        self._json({"error": "introuvable"}, 404)

    def _safe(self, handler) -> None:
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass  # le navigateur a fermé la connexion
        except Exception as exc:
            log.exception("erreur sur %s %s", self.command, self.path)
            try:
                self._json({"error": f"erreur interne : {exc}"}, 500)
            except OSError:
                pass

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
    Handler.box = box
    threading.Thread(target=scan_loop, args=(store, box), daemon=True).start()
    if box:
        threading.Thread(target=traffic_loop, args=(store, box), daemon=True).start()
        threading.Thread(target=homelan_loop, args=(store, box), daemon=True).start()
    log.info("détection via %s — interface sur http://0.0.0.0:%d",
             "la Livebox" if LIVEBOX_PASSWORD else "scan réseau", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
