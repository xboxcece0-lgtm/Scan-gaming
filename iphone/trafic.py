"""Trafic en direct de la Livebox, directement sur l'iPhone (app a-Shell).

Affiche, rafraîchi toutes les 5 secondes, les appareils connectés à la box et
le débit qu'ils consomment à l'instant t. Fichier autonome : aucune
installation, il suffit de le copier dans a-Shell et de lancer
    python3 trafic.py
L'iPhone doit être connecté au Wi‑Fi de la maison. Ctrl‑C (ou fermer a-Shell)
pour arrêter.
    python3 trafic.py --liste   liste complète des appareils (nom, IP, MAC)
    python3 trafic.py --debug   réponses brutes de la box, pour diagnostic
    python3 trafic.py --mesures quels appareils la box mesure, avec IPv4/IPv6
    python3 trafic.py --activer MAC  demande à la box de mesurer un appareil
    python3 trafic.py --topologie    carte du réseau reconstruite par la box
"""
import json
import os
import re
import shutil
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
ACTIVE_BPS = 50_000  # en dessous de 50 kbit/s, l'appareil est considéré au repos
CLEAR = "\033[3J\033[H\033[2J"  # efface l'écran ET l'historique (sinon a-Shell empile)


def clean_name(name, ip, mac):
    """Décode les noms échappés par la box (« \\040none\\041 ») et nomme les anonymes."""
    name = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), name or "").strip(" !")
    if not name or re.fullmatch(r"none(-\d+)?|unknown.*|\??", name, re.I):
        suffix = f".{ip.rsplit('.', 1)[-1]}" if ip else mac[-5:]
        return f"inconnu {suffix}"
    return name


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
    expr = {"wifi": '.Active==true && wifi && (edev || hnid || wifi_bridge) and .PhysAddress!=""',
            "eth": '.Active==true && eth && (edev || hnid) and .PhysAddress!=""',
            "lan": '.Active==true && lan && physical and .PhysAddress!=""'}
    try:
        st = box.call("Devices", "get", {"expression": expr}) or {}
    except BoxError:
        expr.pop("lan")
        st = box.call("Devices", "get", {"expression": expr}) or {}
    if isinstance(st, dict) and "lan" in st:  # « wifi » / « eth » d'abord, « lan » complète
        st = {k: st[k] for k in sorted(st, key=lambda k: k == "lan")}
    out = {}
    for link, devs in (st.items() if isinstance(st, dict) else [("?", st)]):
        for d in devs or []:
            mac = (d.get("PhysAddress") or d.get("Key") or "").upper()
            if mac and mac not in out and not set((d.get("Tags") or "").split()) & {"self", "hgw"}:
                ip = d.get("IPAddress") or ""
                out[mac] = {"name": clean_name(d.get("Name"), ip, mac), "ip": ip,
                            "type": d.get("DeviceType") or "",
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


def diagnose(box):
    """Cherche des statistiques de trafic pour les appareils filaires (non documentées)."""
    lines = []

    def show(title, fn, limit=1500):
        try:
            res = json.dumps(fn(), ensure_ascii=False)
        except BoxError as e:
            res = f"ERREUR {e}"
        lines.append(f"--- {title}\n{res[:limit]}{' […]' if len(res) > limit else ''}")
        print(lines[-1], flush=True)

    devs = active_devices(box)
    wired = [m for m, d in devs.items() if d["link"] == "fil"]
    lines.append(f"--- {len(devs)} appareils, dont {len(wired)} filaires : " +
                 ", ".join(f"{devs[m]['name']} ({m[-5:]})" for m in wired))
    print(lines[-1])
    if wired:
        def ports():
            st = box.call("Devices", "get", {"expression": {"eth": ".Active==true && eth && edev"}}) or {}
            return [(d.get("Name"), d.get("InterfaceName"), d.get("Layer2Interface"))
                    for d in (st.get("eth") or [])]
        show("Ports des appareils filaires (nom, InterfaceName, Layer2Interface)", ports)
        m = wired[0]
        show("HomeLan.getDeviceResults sans filtre", lambda: box.call(
            "HomeLan", "getDeviceResults", {"NumberOfReadings": 1}), 2500)
        show(f"HomeLan.getDeviceResults {m[-5:]} sur 10 min", lambda: box.call(
            "HomeLan", "getDeviceResults", {"DeviceName": [m], "Seconds": 600}))
        show(f"HomeLan.getDeviceResults {m[-5:]} (texte)", lambda: box.call(
            "HomeLan", "getDeviceResults", {"DeviceName": m, "NumberOfReadings": 1}))
    show("HomeLan.Interface.get", lambda: {k: (v.get("FriendlyName"), v.get("NumberOfStoredMeasures"))
                                           for k, v in (box.call("HomeLan.Interface", "get") or {}).items()
                                           if isinstance(v, dict)}, 2500)
    show("HomeLan.Device.get", lambda: box.call("HomeLan.Device", "get"), 2500)
    show("HomeLan.getResults (interfaces)", lambda: box.call("HomeLan", "getResults", {"NumberOfReadings": 1}))
    path = os.path.expanduser("~/diagnostic-livebox.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n=> Résultat enregistré dans {path}")


def diag_mesures(box):
    """Pour chaque appareil connecté : adresses IPv4/IPv6 et mesures HomeLan de la box."""
    st = box.call("Devices", "get", {"expression": {
        "wifi": '.Active==true && wifi && (edev || hnid || wifi_bridge) and .PhysAddress!=""',
        "eth": '.Active==true && eth && (edev || hnid) and .PhysAddress!=""'}}) or {}
    def as_dict(service, method, params=None):
        try:
            res = box.call(service, method, params)
        except BoxError as e:
            print(f"({service}.{method} : {e})")
            return {}
        return res if isinstance(res, dict) else {}

    stored = {k.upper(): v.get("NumberOfStoredMeasures")
              for k, v in as_dict("HomeLan.Device", "get").items() if isinstance(v, dict)}
    readings = {}
    for k, v in as_dict("HomeLan", "getDeviceResults", {"NumberOfReadings": 1}).items():
        t = (v or {}).get("Traffic") or [] if isinstance(v, dict) else []
        if t:
            readings[k.upper()] = t[-1]
    ref = max((r.get("Timestamp") or 0 for r in readings.values()), default=0)
    lines = [f"{'appareil':<20} lien IPv6 mesures dernier relevé"]
    for link, devs in st.items() if isinstance(st, dict) else []:
        for d in devs or []:
            mac = (d.get("PhysAddress") or "").upper()
            v6 = [a.get("Address") for a in d.get("IPv6Address") or [] if isinstance(a, dict)]
            r = readings.get(mac)
            last = "aucun"
            if r:
                age = ref - (r.get("Timestamp") or 0)
                kbps = ((r.get("Tx_Counter") or 0) + (r.get("Rx_Counter") or 0)) / 30 / 1000
                last = f"il y a {age // 60} min, {kbps:.0f} kbit/s"
            name = clean_name(d.get("Name"), d.get("IPAddress") or "", mac)
            lines.append(f"{name[:20]:<20} {'wifi' if link == 'wifi' else 'fil ':4} "
                         f"{len(v6):>4} {str(stored.get(mac, '-')):>8} {last}")
            lines.append(f"    {d.get('IPAddress') or '-'}  {mac}")
    print("\n".join(lines))
    path = os.path.expanduser("~/diagnostic-mesures.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n=> Résultat enregistré dans {path}")


def activer(box, mac):
    """Demande à la box d'(ré)inscrire un appareil dans ses relevés de trafic HomeLan."""
    mac = mac.upper().replace("-", ":")
    def stored():
        res = box.call("HomeLan.Device", "get") or {}
        v = res.get(mac) if isinstance(res, dict) else None
        return v.get("NumberOfStoredMeasures") if isinstance(v, dict) else "absent"
    print(f"Mesures enregistrées pour {mac} : {stored()}")
    for method in ("addDevice",):
        try:
            print(f"HomeLan.{method} -> {json.dumps(box.call('HomeLan', method, {'macaddress': mac}))}")
        except BoxError as e:
            print(f"HomeLan.{method} -> ERREUR {e}")
    print("Attente de 75 s (deux relevés de 30 s)…", flush=True)
    time.sleep(75)
    print(f"Mesures enregistrées pour {mac} : {stored()}")


def diag_topologie(box):
    """Affiche l'arbre brut du réseau reconstruit par la box (buildTopology)."""
    print("Construction de la carte par la box (jusqu'à 30 s)…", flush=True)
    st = box.call("TopologyDiagnostics", "buildTopology", {"Timeout": 30, "LLTDIcon": False, "SendXmlFile": False})
    lines = []

    def walk(n, depth=0):
        if not isinstance(n, dict) or depth > 12:
            return
        tags = " ".join(t for t in (n.get("Tags") or "").split()
                        if t in ("self", "hgw", "lan", "wifi", "eth", "vap", "bridge", "edev", "hnid", "ssw_sta", "physical"))
        ssw = n.get("SSW", {}).get("CurrentMode") if isinstance(n.get("SSW"), dict) else ""
        mac = (n.get("PhysAddress") or "")[-5:]
        lines.append(f"{'  ' * depth}- {n.get('Name')!s:24} [{n.get('DeviceType') or '-'}] {mac} "
                     f"{'actif' if n.get('Active') else 'inactif'} {ssw or ''} ({tags})")
        for c in n.get("Children") or []:
            walk(c, depth + 1)

    for root in st if isinstance(st, list) else [st]:
        walk(root)
    print("\n".join(lines))
    path = os.path.expanduser("~/diagnostic-topologie.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n=> Résultat enregistré dans {path}")


def get_password():
    if os.environ.get("LIVEBOX_PASSWORD"):
        return os.environ["LIVEBOX_PASSWORD"]
    if os.path.exists(PWD_FILE):
        with open(PWD_FILE) as f:
            return f.read().strip()
    pwd = input("Mot de passe administrateur de la Livebox : ").strip()
    if input("Le mémoriser sur cet appareil ? (o/n) ").strip().lower().startswith("o"):
        os.makedirs(os.path.dirname(PWD_FILE), exist_ok=True)
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

    if "--liste" in sys.argv:
        devs = active_devices(box)
        print(f"{len(devs)} appareils connectés :\n")
        for mac, d in sorted(devs.items(), key=lambda x: tuple(int(p) for p in x[1]["ip"].split(".") if p.isdigit())):
            icon = "📶" if d["link"] == "wifi" else "🔌"
            print(f"{icon}{d['name'][:26]}\n   {d['ip']}  {mac}\n   {d['type']}")
        box.logout()
        return

    if "--activer" in sys.argv:
        i = sys.argv.index("--activer")
        if i + 1 >= len(sys.argv):
            sys.exit("Usage : python3 trafic.py --activer 84:3A:5B:2C:74:3D")
        activer(box, sys.argv[i + 1])
        box.logout()
        return

    if "--topologie" in sys.argv:
        diag_topologie(box)
        box.logout()
        return

    if "--mesures" in sys.argv:
        diag_mesures(box)
        box.logout()
        return

    if debug:
        diagnose(box)
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

            first = not any(r[1] is not None for r in rows)
            active = [r for r in rows if (r[1] or 0) + (r[2] or 0) >= ACTIVE_BPS]
            unknown = [r[3]["name"] for r in rows if r[1] is None]
            idle = len(rows) - len(active) - len(unknown)
            height = shutil.get_terminal_size((WIDTH, 24)).lines
            room = max(3, height - 9)  # place pour les lignes d'appareils actifs

            out = [f"Livebox · {time.strftime('%H:%M:%S')} · {len(devs)} connectés",
                   f"{'Appareil':<22}  ↓ reçu  ↑ envoyé", "─" * WIDTH]
            if first:
                out.append("Calcul des débits… (5 s)")
            elif not active:
                out.append("Aucun appareil actif en ce moment")
            for _, down, up, d in active[:room]:
                icon = "📶" if d["link"] == "wifi" else "🔌"
                out.append(f"{icon}{d['name'][:20]:<20}{fmt(down)} {fmt(up)}")
            if len(active) > room:
                out.append(f"  … et {len(active) - room} autre(s) actif(s)")
            out.append("─" * WIDTH)
            if not first:
                out.append(f"💤 {idle} au repos (< 50 kbit/s)")
                if unknown:
                    out.append(f"❔ {len(unknown)} sans mesure : " + ", ".join(unknown)[:WIDTH * 2 - 20])
            out.append("M = Mbit/s · Ctrl‑C pour quitter")
            print(CLEAR + "\n".join(out), end="", flush=True)
            time.sleep(REFRESH)
    except KeyboardInterrupt:
        pass
    finally:
        box.logout()


if __name__ == "__main__":
    main()
