"""Détection des appareils présents sur le réseau local.

Principe : on envoie un ping à chaque adresse du sous-réseau (ce qui force
la résolution ARP, même si l'appareil ignore les pings — c'est souvent le cas
des consoles), puis on lit la table des voisins du système (`ip neigh` sous
Linux, `arp -an` sous macOS). Un appareil est considéré « en ligne » s'il a
répondu à la résolution ARP. Aucun privilège root n'est nécessaire.
"""
from __future__ import annotations

import ipaddress
import platform
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

MAC_RE = re.compile(r"([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})", re.I)
ONLINE_STATES = {"REACHABLE", "DELAY", "PROBE", "PERMANENT", "NOARP"}


def normalize_mac(mac: str) -> str:
    return ":".join(part.zfill(2) for part in mac.lower().split(":"))


def is_random_mac(mac: str) -> bool:
    """Adresse MAC « privée » (aléatoire), utilisée par les téléphones récents."""
    return bool(int(mac.split(":")[0], 16) & 0x02)


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def detect_network() -> ipaddress.IPv4Network:
    """Devine le sous-réseau local à partir de l'interface de la route par défaut."""
    out = _run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    route = _run(["ip", "-4", "route", "show", "default"])
    dev = None
    m = re.search(r"dev (\S+)", route)
    if m:
        dev = m.group(1)
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if m and (dev is None or m.group(1) == dev):
            return ipaddress.ip_interface(m.group(2)).network
    # Repli (macOS, etc.) : on suppose un /24 autour de l'IP locale.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # aucun paquet n'est réellement envoyé
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_interface(f"{local_ip}/24").network


def _ping(ip: str) -> None:
    if platform.system() == "Darwin":
        cmd = ["ping", "-c", "1", "-t", "1", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    _run(cmd, timeout=3)


def _udp_poke(ip: str) -> None:
    """Repli si `ping` est indisponible : un datagramme UDP déclenche aussi l'ARP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"", (ip, 9))
    except OSError:
        pass


def sweep(network: ipaddress.IPv4Network, workers: int = 64) -> None:
    hosts = [str(h) for h in network.hosts()]
    if len(hosts) > 1024:
        raise ValueError(f"Réseau trop grand ({network}), précisez SCAN_NETWORK (ex. 192.168.1.0/24)")
    have_ping = bool(_run(["ping", "-c", "1", "-W", "1", "127.0.0.1"], timeout=3))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_ping if have_ping else _udp_poke, hosts))


def read_neighbors(network: ipaddress.IPv4Network) -> dict[str, str]:
    """Renvoie {mac: ip} pour les voisins actuellement joignables."""
    found: dict[str, str] = {}
    out = _run(["ip", "-4", "neigh", "show"])
    if out:
        for line in out.splitlines():
            parts = line.split()
            if not parts or "lladdr" not in parts:
                continue
            ip = parts[0]
            mac = parts[parts.index("lladdr") + 1]
            state = parts[-1].upper()
            if state in ONLINE_STATES and ipaddress.ip_address(ip) in network:
                found[normalize_mac(mac)] = ip
        return found
    # macOS / BSD
    for line in _run(["arp", "-an"]).splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at (\S+)", line)
        if not m or "incomplete" in m.group(2):
            continue
        ip, mac = m.group(1), m.group(2)
        if MAC_RE.fullmatch(mac) and ipaddress.ip_address(ip) in network:
            found[normalize_mac(mac)] = ip
    return found


def reverse_dns(ip: str) -> str | None:
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return None
    return name.split(".")[0] if name and name != ip else None


def scan(network: ipaddress.IPv4Network) -> dict[str, str]:
    sweep(network)
    time.sleep(1.5)  # laisse le temps aux réponses ARP tardives
    return read_neighbors(network)


if __name__ == "__main__":
    net = detect_network()
    print(f"Scan de {net}…")
    for mac, ip in sorted(scan(net).items(), key=lambda x: ipaddress.ip_address(x[1])):
        print(f"{ip:16} {mac}  {reverse_dns(ip) or ''}")
