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
import urllib.error
import urllib.request

CONTENT_TYPE = "application/x-sah-ws-1-call+json"

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
                return self.call(service, method, params, _retry=False)
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
                    "name": d.get("Name") or None,
                    "type": d.get("DeviceType") or None,
                    "link": "wifi" if link == "wifi" else "filaire" if link == "eth" else None,
                }
        return found

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
