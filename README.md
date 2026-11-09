# Scan Gaming

Mesure le temps de connexion des consoles et PC de la maison (filaire ou Wi‑Fi),
avec une interface à installer sur l'écran d'accueil de l'iPhone.

- **Temps** : temps de connexion du jour par appareil, frise horaire des sessions, limite quotidienne optionnelle (barre orange à 80 %, rouge en cas de dépassement).
- **Semaine** : histogramme sur 7 jours, total et moyenne par jour.
- **Appareils** : tous les appareils détectés sur le réseau. Vous les nommez, choisissez leur type et cochez ceux à suivre.

## Pourquoi pas une application qui tourne seulement sur l'iPhone ?

iOS met les applications en pause dès qu'elles passent en arrière-plan et leur
interdit de lire la table ARP du réseau. Une app iPhone seule ne verrait donc les
consoles que lorsqu'elle est ouverte à l'écran : impossible de mesurer une
soirée de jeu.

Scan Gaming est donc découpé en deux :

```
 ┌──────────────────────────┐   scan toutes les 60 s    ┌──────────────┐
 │ Collecteur (Raspberry Pi,│ ───── ping + ARP ───────▶ │ PS5, Xbox,   │
 │ NAS, vieux PC… allumé    │                           │ Switch, PC…  │
 │ en permanence)           │                           └──────────────┘
 │  • SQLite (historique)   │
 │  • serveur web :8080     │ ◀──── Safari / icône écran d'accueil ── iPhone
 └──────────────────────────┘
```

## Livebox Orange (recommandé)

Avec une Livebox (5, 6, 7…), le collecteur peut demander directement à la box
la liste des appareils connectés, au lieu de scanner le réseau. C'est plus fiable :
la box sait exactement qui est associé au Wi‑Fi ou branché sur ses ports. Elle
fournit aussi le nom de chaque appareil (ex. `PS5-123`, `DESKTOP-HUGO`), son
type, et indique si la connexion est en Wi‑Fi ou filaire.

Il suffit de renseigner le **mot de passe administrateur** de la Livebox, celui
qui sert à se connecter à http://192.168.1.1. Si vous ne l'avez jamais changé,
ce sont les **8 premiers caractères de la clé Wi‑Fi** imprimée sous la box.

```bash
# test rapide : affiche les appareils connectés vus par la box
LIVEBOX_PASSWORD=xxxxxxxx python3 server/livebox.py
```

Puis décommentez `LIVEBOX_PASSWORD` dans `docker-compose.yml`, ou lancez
`LIVEBOX_PASSWORD=xxxxxxxx python3 server/app.py`.

Si la box ne répond pas (redémarrage, mauvais mot de passe…), le collecteur
repasse automatiquement en scan réseau et l'indique en bas de l'écran.

> En mode Livebox, le collecteur n'a plus besoin de lire la table ARP. Il peut
> donc tourner sur **n'importe quel ordinateur allumé en permanence**, y compris
> un PC Windows ou un Mac (Python installé, ou Docker Desktop).

## Trafic en direct sur l'iPhone, sans collecteur

`iphone/trafic.py` affiche les appareils connectés à la Livebox et leur débit
à l'instant t, directement sur l'iPhone, en attendant que le collecteur soit en place :

1. Installez l'app gratuite **a-Shell** (App Store).
2. Enregistrez `trafic.py` dans l'app Fichiers → Sur mon iPhone → **a-Shell**.
3. Dans a-Shell, tapez `python3 trafic.py`, puis le mot de passe administrateur de la box.

Le débit des appareils Wi‑Fi est calculé à partir des compteurs de la box. Pour
les appareils filaires, la statistique n'est pas documentée par Orange : la box
peut ne pas en fournir (affiché « ? »). `python3 trafic.py --debug` affiche les
réponses brutes de la box pour diagnostic.

## Installation

Prérequis : une machine **Linux** reliée au réseau de la maison et allumée en
permanence (Raspberry Pi, NAS Synology/QNAP avec Docker, mini‑PC…). Le
collecteur consomme très peu de ressources.

### Sur un Raspberry Pi (recommandé)

1. **Préparer la carte microSD** depuis un ordinateur avec [Raspberry Pi Imager](https://www.raspberrypi.com/software/) :
   système « Raspberry Pi OS Lite (64-bit) ». Dans les réglages (⚙️ / « Modifier les réglages ») :
   nom d'hôte `scangaming`, utilisateur `pi` + un mot de passe, fuseau `Europe/Paris`,
   et dans l'onglet **Services** : **activer SSH** (authentification par mot de passe).
2. Insérer la carte, **brancher le Pi à la Livebox avec un câble réseau**, puis l'alimenter. Attendre 2 minutes.
3. **Se connecter au Pi** depuis le PC (Windows : ouvrir « Terminal » ou « PowerShell » ;
   Mac : « Terminal ») ou depuis l'iPhone (app a-Shell) : `ssh pi@scangaming.local`
   (ou `ssh pi@<IP>` ; l'IP du Pi apparaît dans l'interface de la Livebox).
4. Sur le Pi :
   ```bash
   sudo apt-get update && sudo apt-get install -y git
   git clone -b claude/nifty-heisenberg-gs8amu https://github.com/xboxcece0-lgtm/Scan-gaming.git
   cd Scan-gaming && ./install.sh
   ```
   Le script demande le mot de passe de la Livebox (stocké dans `/etc/scan-gaming.env`,
   lisible par root uniquement), installe un service qui démarre avec le Pi, et affiche l'adresse.
5. Sur l'iPhone, ouvrir **http://scangaming.local** dans Safari → Partager → **Sur l'écran d'accueil**.

Mise à jour : `cd ~/Scan-gaming && git pull && ./install.sh`.
Journal en cas de souci : `journalctl -u scan-gaming -f`.

### Avec Docker

```bash
git clone <ce dépôt> scan-gaming && cd scan-gaming
docker compose up -d --build
```

### Sans Docker

Python 3.10 ou plus, sans aucune dépendance externe :

```bash
python3 server/app.py
```

### Sur l'iPhone

1. Ouvrez Safari à l'adresse `http://<IP-du-collecteur>:8080` (ex. `http://192.168.1.10:8080`).
2. Touchez **Partager** puis **Sur l'écran d'accueil**. L'app s'ouvre ensuite en plein écran, comme une application normale.
3. Dans l'onglet **Appareils**, touchez chaque console ou PC, donnez-lui un nom, cochez **Suivre le temps de connexion** et, si vous voulez, une limite quotidienne.

> Pour repérer quelle ligne correspond à quelle console : éteignez-la complètement, attendez 3 minutes, rallumez-la. La pastille verte apparaît au scan suivant.

## Couper Internet à un appareil

Avec une Livebox, la fiche d'un appareil propose **⛔ Couper Internet** / **✅ Rétablir Internet**.
Dans ses réglages, la case **« Couper Internet quand la limite est atteinte »** coupe
automatiquement l'accès dès que la limite quotidienne est dépassée. L'accès est rétabli
automatiquement à minuit. Une coupure manuelle, elle, dure jusqu'à ce que vous la leviez.

La coupure utilise le contrôle d'accès de la Livebox (le même que l'application Orange) :
elle bloque **l'accès Internet** de l'appareil, en Wi‑Fi comme en câble ; le réseau local reste
accessible. Un appareil qui change d'adresse MAC (téléphone avec « adresse Wi‑Fi privée »)
peut y échapper ; les consoles et les PC ne changent pas d'adresse.

## Réglages (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `SCAN_NETWORK` | auto | Sous-réseau à scanner, ex. `192.168.1.0/24` |
| `SCAN_INTERVAL` | `60` | Secondes entre deux scans |
| `SCAN_GRACE` | `180` | Absence tolérée (s) avant de clore une session |
| `APP_PASSWORD` | – | Protège l'interface par mot de passe (nom d'utilisateur libre) |
| `PORT` | `8080` | Port de l'interface web |
| `DB_PATH` | `data/scan-gaming.db` | Base SQLite |
| `RETENTION_DAYS` | `400` | Durée de conservation de l'historique |
| `LIVEBOX_PASSWORD` | – | Mot de passe admin de la Livebox : active la détection via la box |
| `LIVEBOX_HOST` | `192.168.1.1` | Adresse de la Livebox |
| `LIVEBOX_USER` | `admin` | Identifiant administrateur |
| `LIVE_INTERVAL` | `10` | Secondes entre deux relevés de débit (Livebox) |
| `ACTIVE_KBPS` | `300` | Débit au-delà duquel un appareil compte comme « utilisé » |

## Fonctionnement et limites à connaître

- **Détection** : à chaque scan, le collecteur pingue toutes les adresses du sous-réseau, puis lit la table ARP (`ip neigh`). Une console qui ignore les pings est quand même détectée, parce qu'elle répond toujours à l'ARP. Les appareils sont identifiés par leur **adresse MAC**, donc un changement d'IP ne pose pas de problème.
- **Précision** : environ ± 1 minute avec l'intervalle par défaut. Une coupure de moins de 3 minutes (`SCAN_GRACE`) ne coupe pas la session.
- **Temps utilisé** : avec une Livebox, le débit de chaque appareil **Wi‑Fi connecté directement à la box** est relevé toutes les 10 s. Une minute où l'appareil échange plus de 300 kbit/s (`ACTIVE_KBPS`) compte comme « utilisée ». C'est ce temps qui est affiché et comparé à la limite quotidienne, plutôt que le simple temps de connexion. Les appareils filaires sont mesurés toutes les 30 s (même derrière un switch). Les appareils connectés via un répéteur Wi‑Fi qui traduit les adresses (TP‑Link RE305…) n'ont que le temps de connexion : la box attribue leur trafic au répéteur. Un répéteur relié par câble en mode « point d'accès » n'a pas ce problème.
- **Connexion ≠ jeu** : une console en **mode repos** (PS5, Xbox « instant-on ») reste connectée au réseau pour les mises à jour. Elle est alors comptée comme « en ligne ». Pour une mesure fiable, désactivez la connexion Internet en mode repos dans les réglages d'économie d'énergie de la console, ou choisissez l'arrêt complet / le mode économie d'énergie.
- **Adresses MAC privées** : iPhone, iPad et Android récents changent régulièrement d'adresse MAC (« adresse Wi‑Fi privée »). Ils peuvent donc apparaître plusieurs fois. Les consoles et les PC ne sont pas concernés.
- **Réseau invité** : un appareil branché sur le Wi‑Fi invité de la box est souvent isolé et invisible depuis le réseau principal.
- Le collecteur doit être sur le **même réseau local** que les consoles. En mode scan réseau (sans Livebox), Docker doit tourner en `network_mode: host` (Linux uniquement).

## Structure

```
server/livebox.py   détection via l'API locale de la Livebox Orange
server/scanner.py   détection réseau (ping + table ARP), utilisée sans Livebox
server/db.py        stockage SQLite, sessions, calcul des durées
server/app.py       boucle de scan + API JSON + fichiers web
web/                interface (PWA installable sur iPhone)
```

API : `GET /api/devices`, `GET /api/usage?day=AAAA-MM-JJ`, `GET /api/week?day=…`,
`PATCH /api/devices/<mac>`, `DELETE /api/devices/<mac>`, `POST /api/scan`, `GET /api/status`.
