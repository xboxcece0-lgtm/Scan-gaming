"""Stockage SQLite des appareils et de leurs sessions de connexion."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac         TEXT PRIMARY KEY,
    ip          TEXT,
    hostname    TEXT,
    name        TEXT,
    kind        TEXT NOT NULL DEFAULT 'autre',   -- console, pc, telephone, tv, autre
    link        TEXT,                             -- wifi / filaire (si connu, via la Livebox)
    iface       TEXT,                             -- interface de la box (ETH0, vap5g0priv0…)
    via         TEXT,                             -- saisi : switch / répéteur / appareil parent
    tracked     INTEGER NOT NULL DEFAULT 0,       -- affiché dans le suivi des temps
    daily_limit INTEGER,                          -- minutes par jour, optionnel
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    mac     TEXT NOT NULL REFERENCES devices(mac) ON DELETE CASCADE,
    start   REAL NOT NULL,
    end     REAL NOT NULL,
    open    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_mac_end ON sessions(mac, end);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(open);
-- Temps « actif » : secondes par jour où l'appareil consommait du trafic
CREATE TABLE IF NOT EXISTS activity (
    mac     TEXT NOT NULL REFERENCES devices(mac) ON DELETE CASCADE,
    day     TEXT NOT NULL,   -- AAAA-MM-JJ, heure locale
    seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (mac, day)
);
-- Historique de trafic : octets échangés par minute et par appareil
CREATE TABLE IF NOT EXISTS traffic (
    mac    TEXT NOT NULL REFERENCES devices(mac) ON DELETE CASCADE,
    minute INTEGER NOT NULL,          -- horodatage Unix / 60
    down   REAL NOT NULL DEFAULT 0,   -- octets reçus par l'appareil
    up     REAL NOT NULL DEFAULT 0,   -- octets envoyés par l'appareil
    PRIMARY KEY (mac, minute)
);
"""

EDITABLE = {"name", "kind", "tracked", "daily_limit", "via"}
KINDS = {"console", "pc", "telephone", "tablette", "tv", "autre"}


class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(devices)")}
        for col in ("link", "iface", "via"):  # migration des bases plus anciennes
            if col not in cols:
                self._db.execute(f"ALTER TABLE devices ADD COLUMN {col} TEXT")

    # --- enregistrement d'un scan -------------------------------------------------
    def record_scan(self, seen: dict[str, dict], now: float, grace: float) -> None:
        """Met à jour les sessions à partir des appareils vus au temps `now`.

        `seen` : {mac: {"ip", "hostname", "link", "kind"}} (seule "ip" est obligatoire).
        Une session reste ouverte tant que l'appareil réapparaît avant `grace`
        secondes ; au-delà, elle est close à la dernière détection.
        """
        with self._lock, self._db:
            db = self._db
            for mac, info in seen.items():
                db.execute(
                    """INSERT INTO devices (mac, ip, hostname, link, iface, kind, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, COALESCE(?, 'autre'), ?, ?)
                       ON CONFLICT(mac) DO UPDATE SET ip = excluded.ip,
                         hostname = COALESCE(excluded.hostname, devices.hostname),
                         link = COALESCE(excluded.link, devices.link),
                         iface = COALESCE(excluded.iface, devices.iface),
                         last_seen = excluded.last_seen""",
                    (mac, info.get("ip"), info.get("hostname"), info.get("link"), info.get("iface"),
                     info.get("kind"), now, now),
                )
                row = db.execute(
                    "SELECT id, end FROM sessions WHERE mac = ? AND open = 1", (mac,)
                ).fetchone()
                if row and now - row["end"] <= grace:
                    db.execute("UPDATE sessions SET end = ? WHERE id = ?", (now, row["id"]))
                else:
                    if row:
                        db.execute("UPDATE sessions SET open = 0 WHERE id = ?", (row["id"],))
                    db.execute("INSERT INTO sessions (mac, start, end) VALUES (?, ?, ?)",
                               (mac, now, now))
            db.execute("UPDATE sessions SET open = 0 WHERE open = 1 AND ? - end > ?",
                       (now, grace))

    def add_activity(self, macs: list[str], day: str, seconds: float) -> None:
        with self._lock, self._db:
            for mac in macs:
                self._db.execute(
                    """INSERT INTO activity (mac, day, seconds) SELECT ?, ?, ?
                       WHERE EXISTS (SELECT 1 FROM devices WHERE mac = ?)
                       ON CONFLICT(mac, day) DO UPDATE SET seconds = seconds + excluded.seconds""",
                    (mac, day, seconds, mac))

    def activity(self, first_day: str, last_day: str) -> dict[str, dict[str, float]]:
        """{mac: {jour: secondes actives}} entre deux jours inclus."""
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for r in self._db.execute("SELECT * FROM activity WHERE day BETWEEN ? AND ?", (first_day, last_day)):
                out.setdefault(r["mac"], {})[r["day"]] = r["seconds"]
        return out

    def add_traffic(self, rows: list[tuple[str, int, float, float]]) -> None:
        """Ajoute des octets (mac, minute, reçus, envoyés) aux compteurs par minute."""
        with self._lock, self._db:
            self._db.executemany(
                """INSERT INTO traffic (mac, minute, down, up) SELECT ?, ?, ?, ?
                   WHERE EXISTS (SELECT 1 FROM devices WHERE mac = ?)
                   ON CONFLICT(mac, minute) DO UPDATE SET down = down + excluded.down, up = up + excluded.up""",
                [(m, minute, d, u, m) for m, minute, d, u in rows])

    def traffic_between(self, mac: str, start: float, end: float) -> list[tuple[int, float, float]]:
        with self._lock:
            return [tuple(r) for r in self._db.execute(
                "SELECT minute, down, up FROM traffic WHERE mac = ? AND minute >= ? AND minute < ? ORDER BY minute",
                (mac, int(start // 60), int(end // 60)))]

    def known_hostnames(self) -> dict[str, str | None]:
        with self._lock:
            return {r["mac"]: r["hostname"] for r in self._db.execute("SELECT mac, hostname FROM devices")}

    # --- lecture -------------------------------------------------------------------
    def devices(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """SELECT d.*, EXISTS(SELECT 1 FROM sessions s WHERE s.mac = d.mac AND s.open = 1) AS online
                   FROM devices d ORDER BY d.tracked DESC, online DESC, COALESCE(d.name, d.hostname, d.ip)"""
            ).fetchall()
        return [dict(r) for r in rows]

    def sessions_between(self, start: float, end: float, mac: str | None = None) -> list[dict]:
        sql = "SELECT * FROM sessions WHERE end >= ? AND start < ?"
        args: list = [start, end]
        if mac:
            sql += " AND mac = ?"
            args.append(mac)
        with self._lock:
            return [dict(r) for r in self._db.execute(sql + " ORDER BY start", args)]

    def update_device(self, mac: str, fields: dict) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in EDITABLE}
        if "kind" in fields and fields["kind"] not in KINDS:
            raise ValueError("type d'appareil inconnu")
        if "tracked" in fields:
            fields["tracked"] = 1 if fields["tracked"] else 0
        if "daily_limit" in fields:
            v = fields["daily_limit"]
            fields["daily_limit"] = int(v) if v not in (None, "", 0) else None
        for key in ("name", "via"):
            if key in fields:
                fields[key] = (fields[key] or "").strip()[:60] or None
        with self._lock, self._db:
            if fields:
                cols = ", ".join(f"{k} = ?" for k in fields)
                self._db.execute(f"UPDATE devices SET {cols} WHERE mac = ?", [*fields.values(), mac])
            row = self._db.execute("SELECT * FROM devices WHERE mac = ?", (mac,)).fetchone()
        return dict(row) if row else None

    def delete_device(self, mac: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM devices WHERE mac = ?", (mac,))

    def purge_older_than(self, days: int) -> None:
        cutoff = time.time() - days * 86400
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE open = 0 AND end < ?", (cutoff,))
            self._db.execute("DELETE FROM activity WHERE day < date(?, 'unixepoch', 'localtime')", (cutoff,))

    def purge_traffic_older_than(self, days: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM traffic WHERE minute < ?", (int((time.time() - days * 86400) // 60),))


# --- calculs de durée --------------------------------------------------------------
def day_bounds(d: date) -> tuple[float, float]:
    start = datetime.combine(d, datetime.min.time()).astimezone()
    end = datetime.combine(d + timedelta(days=1), datetime.min.time()).astimezone()
    return start.timestamp(), end.timestamp()


def overlap_seconds(sessions: list[dict], start: float, end: float, interval: float) -> float:
    """Durée cumulée des sessions dans [start, end[.

    Chaque détection couvre environ un intervalle de scan : on ajoute donc
    `interval` à la fin des sessions pour ne pas compter 0 s pour une session
    vue une seule fois.
    """
    total = 0.0
    for s in sessions:
        a = max(s["start"], start)
        b = min(s["end"] + interval, end, time.time())
        if b > a:
            total += b - a
    return total
