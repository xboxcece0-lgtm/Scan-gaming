"""Trafic en direct de la Livebox, directement sur l'iPhone (app a-Shell).

Affiche, rafraîchi toutes les 5 secondes, les appareils connectés à la box et
le débit qu'ils consomment à l'instant t. Fichier autonome : aucune
installation, il suffit de le copier dans a-Shell et de lancer
    python3 trafic.py
L'iPhone doit être connecté au Wi‑Fi de la maison. Ctrl‑C (ou fermer a-Shell)
pour arrêter. `python3 trafic.py --debug` affiche les réponses brutes de la box.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

HOST = os.environ.get("LIVEBOX_HOST", "192.168.1.1")
USER = os.environ.get("LIVEBOX_USER", "admin")
REFRESH = 5
PWD_FILE = os.path.expanduser("~/Documents/.livebox_mdp")
CT = "application/x-sah-ws-1-call+json"
WIDTH = 44  # largeur d'un iPhone en portrait dans a-Shell


class BoxError(Exception):
    pass


class Box:
    def __init__(self, password):
        self.password = password
        self.ctx = None
        self.cookies = {}

    def _http(self, path, body=None, headers=None):
        headers = dict(headers or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://{HOST}{path}", data=data, headers=headers,
                                     method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                for h in r.headers.get_all("Set-Cookie") or []:
                    k, sep, v = h.split(";")[0].partition("=")
                    if sep:
                        self.cookies[k.strip()] = v.strip()
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise BoxError(f"erreur HTTP {e.code}")
        except OSError as e:
            raise BoxError(f"Livebox injoignable ({HOST}). Êtes-vous sur le Wi‑Fi de la maison ? ({e})")
        return json.loads(raw) if data and raw else {}

    def login(self):
        self.cookies.clear()
        self._http("/")
        r = self._http("/ws", {"service": "sah.Device.Information", "method": "createContext",
                               "parameters": {"applicationName": "so_sdkut", "username": USER,
                                              "password": self.password}},
                       {"Content-Type": CT, "Authorization": "X-Sah-Login"})
        self.ctx = (r.get("data") or {}).get("contextID")
        if not self.ctx:
            raise BoxError("mot de passe refusé par la Livebox")

    def call(self, service, method, params=None, retry=True):
        if not self.ctx:
            self.login()
        r = self._http("/ws", {"service": service, "method": method, "parameters": params or {}},
                       {"Content-Type": CT, "X-Context": self.ctx, "Authorization": f"X-Sah {self.ctx}"})
        res = r.get("result", r)
        if res.get("errors"):
            if retry:
                self.ctx = None
                return self.call(service, method, params, False)
            raise BoxError(str(res["errors"]))
        return res.get("status")

    def logout(self):
        if self.ctx:
            try:
                self._http("/ws", {"service": "sah.Device.Information", "method": "releaseContext",
                                   "parameters": {"applicationName": "so_sdkut"}},
                           {"Content-Type": CT, "Authorization": f"X-Sah-Logout {self.ctx}"})
            except BoxError:
                pass


def active_devices(box):
    st = box.call("Devices", "get", {"expression": {
        "wifi": '.Active==true && wifi && (edev || hnid || wifi_bridge) and .PhysAddress!=""',
        "eth": '.Active==true && eth && (edev || hnid) and .PhysAddress!=""'}}) or {}
    out = {}
    for link, devs in (st.items() if isinstance(st, dict) else [("?", st)]):
        for d in devs or []:
            mac = (d.get("PhysAddress") or d.get("Key") or "").upper()
            if mac:
                out[mac] = {"name": d.get("Name") or d.get("IPAddress") or mac,
                            "link": "wifi" if link == "wifi" else "fil"}
    return out


def wifi_counters(box):
    """Compteurs cumulés par client Wi‑Fi : {MAC: (octets vers l'appareil, octets depuis)}."""
    st = box.call("NeMo.Intf.lan", "getMIBs", {"mibs": "wlanvap"}) or {}
    out = {}
    for vap in (st.get("wlanvap") or {}).values():
        for key, a in (vap.get("AssociatedDevice") or {}).items():
            if isinstance(a, dict):
                mac = (a.get("MACAddress") or key).upper()
                out[mac] = (int(a.get("TxBytes") or 0), int(a.get("RxBytes") or 0))
    return out


def wired_rates(box, macs):
    """Débit des appareils filaires via HomeLan (non documenté : au mieux)."""
    if not macs:
        return {}
    try:
        st = box.call("HomeLan", "getDeviceResults", {"DeviceName": macs, "NumberOfReadings": 1})
    except BoxError:
        return {}
    out = {}
    for key, v in (st or {}).items() if isinstance(st, dict) else []:
        traffic = (v or {}).get("Traffic") or []
        if traffic:
            t = traffic[-1]
            # compteurs en bits sur une fenêtre de 30 s (vu côté box : Rx = reçu de l'appareil)
            out[key.upper()] = (t.get("Tx_Counter", 0) / 30, t.get("Rx_Counter", 0) / 30)
    return out


def fmt(bps):
    if bps is None:
        return "   ?   "
    mbps = bps / 1e6
    if mbps >= 100:
        return f"{mbps:5.0f} M"
    if mbps >= 0.1:
        return f"{mbps:5.1f} M"
    return f"{bps / 1e3:5.0f} k"


def get_password():
    if os.path.exists(PWD_FILE):
        with open(PWD_FILE) as f:
            return f.read().strip()
    pwd = input("Mot de passe administrateur de la Livebox : ").strip()
    if input("Le mémoriser sur cet iPhone ? (o/n) ").strip().lower().startswith("o"):
        with open(PWD_FILE, "w") as f:
            f.write(pwd)
        os.chmod(PWD_FILE, 0o600)
    return pwd


def main():
    debug = "--debug" in sys.argv
    box = Box(get_password())
    try:
        box.login()
    except BoxError as e:
        if "refusé" in str(e) and os.path.exists(PWD_FILE):
            os.remove(PWD_FILE)
        sys.exit(f"Erreur : {e}")

    if debug:
        for args in (("Devices", "get", {"expression": {"eth": "eth && .Active==true"}}),
                     ("NeMo.Intf.lan", "getMIBs", {"mibs": "wlanvap"})):
            try:
                print(f"--- {args[0]}.{args[1]}\n" + json.dumps(box.call(*args), indent=1)[:4000])
            except BoxError as e:
                print(f"--- {args[0]}.{args[1]} : {e}")
        devs = active_devices(box)
        wired = [m for m, d in devs.items() if d["link"] == "fil"]
        try:
            print("--- HomeLan.getDeviceResults\n" + json.dumps(
                box.call("HomeLan", "getDeviceResults", {"DeviceName": wired, "NumberOfReadings": 1}),
                indent=1)[:4000])
        except BoxError as e:
            print(f"--- HomeLan.getDeviceResults : {e}")
        box.logout()
        return

    prev, prev_t = {}, None
    try:
        while True:
            try:
                devs = active_devices(box)
                now = time.time()
                counters = wifi_counters(box)
                wired = wired_rates(box, [m for m, d in devs.items() if d["link"] == "fil"])
            except BoxError as e:
                print(f"\nErreur : {e} — nouvel essai dans {REFRESH} s")
                time.sleep(REFRESH)
                continue
            rows = []
            for mac, d in devs.items():
                down = up = None
                if mac in counters and mac in prev and prev_t:
                    dt = now - prev_t
                    down = max(0, counters[mac][0] - prev[mac][0]) * 8 / dt
                    up = max(0, counters[mac][1] - prev[mac][1]) * 8 / dt
                elif mac in wired:
                    down, up = wired[mac]
                rows.append((down or 0, down, up, d))
            prev, prev_t = counters, now
            rows.sort(key=lambda r: r[0], reverse=True)

            out = ["\033[2J\033[H" + f"Livebox · {time.strftime('%H:%M:%S')} · {len(devs)} connectés",
                   f"{'Appareil':<22}  ↓ reçu  ↑ envoyé", "─" * WIDTH]
            for _, down, up, d in rows:
                icon = "📶" if d["link"] == "wifi" else "🔌"
                name = d["name"][:19]
                out.append(f"{icon}{name:<20}{fmt(down)} {fmt(up)}")
            if prev_t and not counters:
                out.append("\n(débits Wi‑Fi indisponibles sur cette box)")
            out.append("\n↓ = téléchargé par l'appareil · M = Mbit/s\n? = filaire sans statistique · Ctrl‑C pour quitter")
            if len(rows) and all(r[1] is None for r in rows):
                out.append("Calcul des débits… (premier relevé)")
            print("\n".join(out), flush=True)
            time.sleep(REFRESH)
    except KeyboardInterrupt:
        pass
    finally:
        box.logout()


if __name__ == "__main__":
    main()
