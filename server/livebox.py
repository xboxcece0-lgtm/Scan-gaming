"""Client minimal pour l'API locale des Livebox Orange (Livebox 5, 6, 7…).

La Livebox expose sur http://192.168.1.1/ws l'API « sysbus » utilisée par sa
propre interface web. Avec le mot de passe administrateur, on peut y lire la
liste des appareils connectés (Wi‑Fi et filaire), ce qui est plus fiable
qu'un scan réseau : la box sait exactement qui est associé au Wi‑Fi ou branché
sur ses ports.

Diagnostic :  LIVEBOX_PASSWORD=... python3 server/livebox.py
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request

CONTENT_TYPE = "application/x-sah-ws-1-call+json"
MAC_KEY = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")
READING_SECONDS = 30  # durée d'une fenêtre de relevé HomeLan

# Requêtes reprises de l'intégration Home Assistant « livebox » (cyr-ius).
ACTIVE_DEVICES = {
    "expression": {
        "wifi": '.Active==true && wifi && (edev || hnid || wifi_bridge) and .PhysAddress!=""',
        "eth": '.Active==true && eth && (edev || hnid) and .PhysAddress!=""',
    }
}

# Correspondance DeviceType Livebox -> type Scan Gaming (pré-remplissage).
KIND_HINTS = (
    ("game", "console"), ("console", "console"), ("playstation", "console"),
    ("xbox", "console"), ("nintendo", "console"), ("computer", "pc"),
    ("laptop", "pc"), ("desktop", "pc"), ("mobile", "telephone"),
    ("phone", "telephone"), ("tablet", "tablette"), ("tv", "tv"), ("stb", "tv"),
)


def clean_name(name: str | None) -> str | None:
    """Décode les noms échappés par la box (« \\040none\\041 » = « none! »)."""
    name = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), name or "").strip(" !")
    if not name or re.fullmatch(r"none(-\d+)?|unknown.*|\??", name, re.I):
        return None
    return name


class LiveboxError(Exception):
    pass


def guess_kind(device_type: str | None, name: str | None) -> str | None:
    text = f"{device_type or ''} {name or ''}".lower()
    for needle, kind in KIND_HINTS:
        if needle in text:
            return kind
    return None


class Livebox:
    def __init__(self, password: str, host: str = "192.168.1.1", username: str = "admin",
                 timeout: float = 15):
        self.base = f"http://{host}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self._context: str | None = None
        self._cookies: dict[str, str] = {}
        self._lock = threading.RLock()  # partagé entre la boucle de scan et celle du trafic

    # --- transport ------------------------------------------------------------------
    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def _store_cookies(self, headers) -> None:
        # Les noms de cookies de la Livebox contiennent des « / » : on les lit à la main.
        for hdr in headers.get_all("Set-Cookie") or []:
            key, sep, value = hdr.split(";")[0].partition("=")
            if sep:
                self._cookies[key.strip()] = value.strip()

    def _http(self, method: str, path: str, body: dict | None, headers: dict) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        if self._cookies:
            headers = {**headers, "Cookie": self._cookie_header()}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._store_cookies(resp.headers)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise LiveboxError(f"Livebox : erreur HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LiveboxError(f"Livebox injoignable ({self.base}) : {exc}") from exc
        if method == "GET":
            return {}
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise LiveboxError("Réponse inattendue de la Livebox") from exc

    def login(self) -> None:
        self._cookies.clear()
        self._http("GET", "/", None, {})  # récupère le cookie de session initial
        res = self._http("POST", "/ws", {
            "service": "sah.Device.Information",
            "method": "createContext",
            "parameters": {"applicationName": "so_sdkut", "username": self.username,
                           "password": self.password},
        }, {"Content-Type": CONTENT_TYPE, "Authorization": "X-Sah-Login"})
        ctx = (res.get("data") or {}).get("contextID")
        if not ctx:
            raise LiveboxError("Connexion à la Livebox refusée : vérifiez le mot de passe administrateur")
        self._context = ctx

    def call(self, service: str, method: str, params: dict | None = None, _retry: bool = True):
        with self._lock:
            return self._call(service, method, params, _retry)

    def _call(self, service: str, method: str, params: dict | None, _retry: bool):
        if not self._context:
            self.login()
        res = self._http("POST", "/ws", {"service": service, "method": method, "parameters": params or {}}, {
            "Content-Type": CONTENT_TYPE,
            "X-Context": self._context,
            "Authorization": f"X-Sah {self._context}",
        })
        result = res.get("result", res)
        if result.get("errors"):
            if _retry:  # contexte expiré : on se reconnecte une fois
                self._context = None
                return self._call(service, method, params, False)
            raise LiveboxError(f"Livebox : {result['errors']}")
        return result.get("status")

    # --- fonctions utiles -----------------------------------------------------------
    def active_devices(self) -> dict[str, dict]:
        """Appareils actuellement connectés : {mac: {ip, name, type, link}}."""
        status = self.call("Devices", "get", ACTIVE_DEVICES) or {}
        groups = status.items() if isinstance(status, dict) else [("?", status)]
        found: dict[str, dict] = {}
        for link, devices in groups:
            for d in devices or []:
                mac = (d.get("PhysAddress") or d.get("Key") or "").lower()
                if not mac or not d.get("Active", True):
                    continue
                found[mac] = {
                    "ip": d.get("IPAddress") or "",
                    "name": clean_name(d.get("Name")),
                    "type": d.get("DeviceType") or None,
                    "link": "wifi" if link == "wifi" else "filaire" if link == "eth" else None,
                    "port": (d.get("InterfaceName") or "").upper() if link == "eth" else None,
                    "iface": d.get("InterfaceName") or None,
                }
        return found

    def wifi_counters(self) -> dict[str, tuple[int, int]]:
        """Compteurs d'octets cumulés des clients Wi‑Fi associés à la box.

        {mac: (octets envoyés à l'appareil, octets reçus de l'appareil)}.
        Les appareils filaires ou connectés via un répéteur n'y figurent pas.
        """
        status = self.call("NeMo.Intf.lan", "getMIBs", {"mibs": "wlanvap"}) or {}
        out: dict[str, tuple[int, int]] = {}
        for vap in (status.get("wlanvap") or {}).values():
            for key, a in ((vap or {}).get("AssociatedDevice") or {}).items():
                if isinstance(a, dict):
                    mac = (a.get("MACAddress") or key).lower()
                    out[mac] = (int(a.get("TxBytes") or 0), int(a.get("RxBytes") or 0))
        return out

    # --- statistiques HomeLan (relevés de 30 s, compteurs en bits par fenêtre) ----------
    def _last_readings(self, status) -> dict[str, dict]:
        out = {}
        for key, v in (status or {}).items() if isinstance(status, dict) else []:
            traffic = (v or {}).get("Traffic") or []
            if traffic and isinstance(traffic[-1], dict):
                out[key] = traffic[-1]
        return out

    def device_readings(self) -> dict[str, dict]:
        """Dernier relevé HomeLan par appareil : {mac: {Timestamp, Rx_Counter, Tx_Counter}}."""
        status = self.call("HomeLan", "getDeviceResults", {"NumberOfReadings": 1})
        return {k.lower(): v for k, v in self._last_readings(status).items() if MAC_KEY.fullmatch(k)}

    def port_readings(self) -> dict[str, dict]:
        """Dernier relevé HomeLan par port, indexé par nom d'interface (ETH0, ETH1…)."""
        names = {}
        for item in (self.call("HomeLan.Interface", "get") or {}).values():
            if isinstance(item, dict) and item.get("Name") and item.get("FriendlyName"):
                names.setdefault(item["FriendlyName"], item["Name"].upper())
        readings = self._last_readings(self.call("HomeLan", "getResults", {"NumberOfReadings": 1}))
        return {names[f]: r for f, r in readings.items() if f in names}

    def device_traffic(self, macs: list[str], readings: int = 4):
        """Statistiques de trafic par appareil (HomeLan), si la box les fournit.

        Expérimental : le format n'est pas documenté par Orange. Renvoie la
        réponse brute, ou None si la fonction n'est pas disponible.
        """
        try:
            return self.call("HomeLan", "getDeviceResults",
                             {"DeviceName": [m.upper() for m in macs], "NumberOfReadings": readings})
        except LiveboxError:
            return None

    def logout(self) -> None:
        if not self._context:
            return
        try:
            self._http("POST", "/ws", {"service": "sah.Device.Information", "method": "releaseContext",
                                       "parameters": {"applicationName": "so_sdkut"}},
                       {"Content-Type": CONTENT_TYPE, "Authorization": f"X-Sah-Logout {self._context}"})
        except LiveboxError:
            pass
        self._context = None


if __name__ == "__main__":
    pwd = os.environ.get("LIVEBOX_PASSWORD")
    if not pwd:
        raise SystemExit("Définissez LIVEBOX_PASSWORD (mot de passe administrateur de la Livebox).")
    box = Livebox(pwd, host=os.environ.get("LIVEBOX_HOST", "192.168.1.1"),
                  username=os.environ.get("LIVEBOX_USER", "admin"))
    try:
        devices = box.active_devices()
        print(f"{len(devices)} appareil(s) connecté(s) :")
        for mac, d in sorted(devices.items(), key=lambda x: x[1]["ip"]):
            print(f"  {d['ip']:16} {mac}  {d['link'] or '':8} {d['name'] or '':28} {d['type'] or ''}")
        print("\nStatistiques de trafic par appareil (expérimental, réponse brute) :")
        print(json.dumps(box.device_traffic(list(devices)[:3], readings=2), indent=2)[:3000])
    finally:
        box.logout()
