# claude-matrix-bot

[![release](https://img.shields.io/github/v/release/marcobockelbrink/claude-matrix-bot)](https://github.com/marcobockelbrink/claude-matrix-bot/releases)
[![docker](https://github.com/marcobockelbrink/claude-matrix-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/marcobockelbrink/claude-matrix-bot/actions/workflows/docker.yml)
[![CodeQL](https://github.com/marcobockelbrink/claude-matrix-bot/actions/workflows/codeql.yml/badge.svg)](https://github.com/marcobockelbrink/claude-matrix-bot/actions/workflows/codeql.yml)
[![Trivy](https://github.com/marcobockelbrink/claude-matrix-bot/actions/workflows/trivy.yml/badge.svg)](https://github.com/marcobockelbrink/claude-matrix-bot/actions/workflows/trivy.yml)
[![signierte Commits](https://img.shields.io/badge/commits-signiert-blue)](https://github.com/marcobockelbrink/claude-matrix-bot/commits/main)

🇬🇧 [English version](README.md)

Ein Matrix-Chatbot auf Basis des [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk),
der eine [Home Assistant](https://www.home-assistant.io/)-Instanz vom Handy aus bedient.
Schreib ihm in einem privaten Matrix-Raum und er liest Zustände, bearbeitet Automationen,
ruft Dienste auf, prüft Logs und startet Home Assistant Core neu — dieselbe Arbeit wie in
einer Claude-Code-Session, erreichbar überall dort, wo dein Handy Empfang hat.

> ⚠️ **Vor dem Betrieb lesen.** Dieses Projekt gibt einem Chatbot die Schlüssel zu deinem
> Zuhause. Die meisten Tool-Aufrufe (Shell-Befehle, HA-Dienste) laufen **ohne Einzelfreigabe** —
> der Hauptschutz ist die Absender-Allowlist: Der Bot reagiert nur auf die von dir
> eingetragenen Matrix-IDs (und Signal-Nummern) und ignoriert alle anderen. Mit
> `CONFIRM_DESTRUCTIVE=true` (Standard) erfordern destruktive Befehle (Löschen, HA-Neustart/
> Stopp, Backup-Löschung) zusätzlich eine Ja/Nein-Bestätigung im Chat. Nutze ein starkes,
> einmaliges Passwort für den Matrix-Account des Bots, halte den Raum privat und
> verschlüsselt, und betrachte den Host des Containers als vertrauenswürdige Infrastruktur.
> Das Ganze ist absichtlich mächtig — stelle sicher, dass du das willst.

## Funktionen

- **Chat-Ops für Home Assistant** — Zustände lesen, Dienste aufrufen, Automationen
  bearbeiten, Logs prüfen, Core neu starten — alles über die HA-HTTP-API.
- **Sprachnachrichten** — schick eine Matrix-Sprachnachricht; der Bot transkribiert lokal
  (faster-whisper, keine Cloud-Spracherkennung) und behandelt sie als Prompt.
- **Bilder & Dateien zurück** — der Agent legt Kamera-Schnappschüsse, Diagramme oder
  Config-Dateien in eine Outbox, die in den Raum hochgeladen wird (E2E-verschlüsselt, wo der
  Raum es ist).
- **Proaktive Benachrichtigungen** — HA-Automationen POSTen an den Webhook des Bots; die
  Nachricht wird wörtlich gepostet oder (mit `"smart": true`) vom Agenten recherchiert und
  formuliert.
- **Tägliches Briefing** — ein optionaler geplanter Agent-Lauf (PV-Prognose, Wetter,
  Müllabfuhr, Spritpreise, HA-Fehler — siehe `system_prompt.md`).
- **Persistentes Gedächtnis** — der Agent führt eine Notizdatei auf einem gemounteten
  Volume, die Neustarts überlebt.
- **Bestätigung destruktiver Aktionen** — gefährliche Shell-Befehle warten auf ein
  **Ja/Nein** (oder eine 👍/👎-Reaktion) von dir im Chat.
- **Zweisprachig** — Bot-Meldungen auf Deutsch oder Englisch (`BOT_LANG`); der Agent
  spiegelt immer die Sprache, in der du schreibst.
- **Mehrbenutzer & optionaler Signal-Kanal** — mehrere Matrix-Nutzer freischalten (Familie)
  und/oder Signal als zweite Chat-Oberfläche über ein `signal-cli-rest-api`-Sidecar
  aktivieren.

## Funktionsweise

```
Handy (Element / beliebiger Matrix-Client)
        │  Matrix-Protokoll (E2E-verschlüsselter Raum)
        ▼
  ha-matrix-bot (Docker-Container, nur ausgehende Verbindungen)
   ├─ matrix-nio: tritt einem Raum bei, reagiert nur auf Allowlist-Nutzer
   └─ Claude Agent SDK: persistente Session mit Bash- / Read- / Write- / Edit- /
      WebFetch- / WebSearch-Tools; spricht HA per curl über die HTTP-API an
        │  ausgehendes HTTPS
        ▼
  https://dein-home-assistant/api/...   (z.B. über einen Cloudflare Tunnel)
```

Der Bot braucht weder eingehende Ports noch LAN-Zugriff — er baut nur ausgehende
Verbindungen auf (zu matrix.org und zur öffentlichen URL deines HA). Er spricht Home
Assistant über die **REST-/WebSocket-API** der öffentlichen Instanz-URL an und funktioniert
damit von überall. (Er nutzt *kein* SSH; Datei-Änderungen in `custom_components/` sind
außerhalb des Funktionsumfangs.)

## Einrichtung

### 1. Zwei Matrix-Accounts anlegen

Registriere zwei Accounts auf [matrix.org](https://matrix.org) (oder einem beliebigen
Homeserver):

- **Dein** Account — von dem du am Handy chattest (z.B. via [Element](https://element.io/)).
- **Der Bot-Account** — ein separater Account, mit dem sich der Container anmeldet.

Die matrix.org-Registrierung hat ein Captcha, dieser Schritt ist also manuell.

### 2. Privaten, verschlüsselten Raum erstellen

Mit deinem Account in Element: neuen Raum erstellen, in den Einstellungen **Verschlüsselung**
aktivieren, den Raum **privat** halten und **den Bot-Account einladen**. Der Bot nimmt
Einladungen nur von Allowlist-Nutzern an — einfach einladen, er tritt beim nächsten Sync bei.

### 3. Konfigurieren

```bash
cp .env.example .env
```

In der `.env` ausfüllen:

- `CLAUDE_CODE_OAUTH_TOKEN` (von `claude setup-token`, nutzt dein Claude-Abo) **oder**
  `ANTHROPIC_API_KEY` (Pay-per-Use, aus der [Claude Console](https://platform.claude.com/)).
- `HA_BASE_URL` / `HA_TOKEN` — die öffentliche URL deines HA und ein langlebiger
  Zugriffstoken (Home Assistant → Profil → Sicherheit → Langlebige Zugangstoken).
- `MATRIX_HOMESERVER` / `MATRIX_USER` / `MATRIX_PASSWORD` — der **Bot**-Account.
- `MATRIX_ALLOWED_USERS` — kommagetrennte Matrix-IDs (z.B. `@du:matrix.org`). Nur auf diese
  Absender reagiert der Bot, nur von ihnen nimmt er Einladungen an.

Optionale Funktionen (siehe Kommentare in `.env.example`): `BOT_LANG` (de/en),
`WEBHOOK_TOKEN` (aktiviert den Benachrichtigungs-Webhook), `BRIEFING_TIME` (tägliches
Briefing, z.B. `07:00`), `WHISPER_MODEL` (Sprachtranskription, `off` zum Deaktivieren),
`CONFIRM_DESTRUCTIVE`.

Die `.env` steht in der `.gitignore` — sie wird nie committet.

### 4. Starten

```bash
docker compose up -d --build
docker compose logs -f        # Login und Sync-Start beobachten
```

Dann vom Handy aus in den Raum schreiben. Die erste Antwort kann ein paar Sekunden dauern,
während der Agent Kontext sammelt.

Das Verzeichnis `store/` (neben der Compose-Datei, in den Container gemountet) enthält die
Matrix-Geräteidentität und Verschlüsselungs-Schlüssel des Bots — aufheben, sonst legt sich
der Bot bei jedem Neustart neue Schlüssel an.

## Optional: Signal als zweiter Kanal

Wenn (ein Teil der) Familie Signal gegenüber Element bevorzugt, kann der Bot beide Kanäle
gleichzeitig bedienen. Die Signal-Seite läuft als Sidecar-Container
([signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)) mit einer eigenen
Telefonnummer (eine einmalig zur Registrierung genutzte Prepaid-SIM reicht).

1. **Sidecar starten** (liegt hinter einem Compose-Profil, ist also standardmäßig aus):

   ```bash
   docker compose --profile signal up -d
   ```

2. **Bot-Nummer registrieren** (einmalig). Die API liegt auf `127.0.0.1:8380`:

   ```bash
   # SMS-Verifizierung anfordern (bei Captcha-Fehler eines auf
   # https://signalcaptchas.org/registration/generate.html lösen und mitgeben):
   curl -X POST http://127.0.0.1:8380/v1/register/+49XXXXXXXXX
   curl -X POST http://127.0.0.1:8380/v1/register/+49XXXXXXXXX \
     -H 'Content-Type: application/json' -d '{"captcha": "<token>"}'

   # Mit dem SMS-Code bestätigen:
   curl -X POST http://127.0.0.1:8380/v1/register/+49XXXXXXXXX/verify/<code>
   ```

3. **Bot konfigurieren** (`.env`) und neu starten:

   ```bash
   SIGNAL_API_URL=http://signal:8080
   SIGNAL_NUMBER=+49XXXXXXXXX
   SIGNAL_ALLOWED_NUMBERS=+49...,+49...   # Nummern der Familienmitglieder
   SIGNAL_NOTIFY=+49...                   # oder group.<id>, für Briefing/Webhook-Pushes
   ```

Familienmitglieder schreiben dann einfach der Bot-Nummer auf Signal (oder du holst den Bot
in eine Signal-Gruppe — Gruppen-IDs via `GET /v1/groups/<SIGNAL_NUMBER>`). Sprachnachrichten,
Bilder vom Agenten und die Bestätigung destruktiver Befehle funktionieren auch auf Signal.
Der Registrierungszustand liegt in `signal-data/` (gitignored) — aufheben, sonst muss die
Nummer neu registriert werden.

## Home-Assistant-Benachrichtigungen anbinden

Der Webhook lauscht auf Port `8321` (Mapping in `docker-compose.yml`). In Home Assistant
einen `rest_command` auf den Bot-Host definieren:

```yaml
# configuration.yaml
rest_command:
  matrix_bot_notify:
    url: "http://<bot-host-ip>:8321/notify"
    method: post
    headers:
      X-Token: !secret matrix_bot_webhook_token
    content_type: application/json
    payload: '{"message": {{ message | tojson }}, "smart": {{ smart | default(false) | tojson }}}'
```

Und aus einer beliebigen Automation aufrufen:

```yaml
actions:
  - action: rest_command.matrix_bot_notify
    data:
      message: "Batterie unter 30 % — Poolpumpe abgeschaltet."
      smart: true   # Agent recherchiert und formuliert die Push-Nachricht selbst
```

Mit `smart: false` (Standard) wird die Nachricht wörtlich gepostet, mit 🔔 vorangestellt.

## Wo der Bot laufen kann

Jeder Host mit ausgehendem Internet funktioniert (eingehend braucht nur der optionale
Benachrichtigungs-Webhook):

- **Eine kleine Always-on-Box** (Raspberry Pi, Mini-PC, NAS mit Docker/Container Manager,
  günstiger VPS).
- **Dieser Rechner**, zum Testen (`docker compose up`) — aber nur erreichbar, solange er an ist.
- **Kubernetes** — siehe unten.

### Fertiges Image

Jeder Push auf `main` baut per GitHub Actions ein Multi-Arch-Image (amd64 + arm64):

```
ghcr.io/marcobockelbrink/claude-matrix-bot:latest
```

Tags (`vX.Y.Z`) bekommen passende Image-Tags. Um es mit Compose statt eines lokalen Builds
zu nutzen: `build: .` durch `image: ghcr.io/marcobockelbrink/claude-matrix-bot:latest`
ersetzen.

### Kubernetes (Plain Manifests)

```bash
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml   # ausfüllen, nicht committen
kubectl apply -f deploy/k8s/secret.yaml -f deploy/k8s/bot.yaml
# optionales Signal-Sidecar:
kubectl apply -f deploy/k8s/signal.yaml
```

Nur ein Replica (der Bot hält eine Matrix-Geräteidentität und eine persistente
Agent-Session); zwei PVCs persistieren den Matrix-E2E-Store und Gedächtnis/Whisper-Cache
des Agenten.

### Kubernetes (Helm)

```bash
helm install ha-matrix-bot deploy/helm/ha-matrix-bot \
  --namespace ha-matrix-bot --create-namespace \
  --values my-values.yaml
```

Minimale `my-values.yaml`:

```yaml
secrets:
  values:
    CLAUDE_CODE_OAUTH_TOKEN: "sk-ant-oat01-..."
    HA_BASE_URL: "https://dein-home-assistant.example.com"
    HA_TOKEN: "..."
    MATRIX_HOMESERVER: "https://matrix.org"
    MATRIX_USER: "@deinbot:matrix.org"
    MATRIX_PASSWORD: "..."
    MATRIX_ALLOWED_USERS: "@du:matrix.org"
    WEBHOOK_TOKEN: "langer-zufalls-string"
config:
  briefingTime: "07:00"
signal:
  enabled: false   # einschalten + SIGNAL_*-Secrets ergänzen für den Signal-Kanal
```

Alternativ `secrets.existingSecret` auf ein selbst verwaltetes Secret zeigen lassen (z.B.
via sealed-secrets / SOPS). Alle Optionen: `deploy/helm/ha-matrix-bot/values.yaml`.

## Hinweise / Grenzen

- **Nur Allowlist.** Der Bot spricht mit den eingetragenen Matrix-IDs / Signal-Nummern,
  sonst niemandem. Webhook-/Briefing-Nachrichten gehen an `NOTIFY_ROOM` (falls gesetzt),
  sonst in den Raum, in dem zuletzt jemand von der Allowlist geschrieben hat (plus
  `SIGNAL_NOTIFY`, falls konfiguriert).
- **Frisches Gespräch nach Neustart.** Das Chat-Transkript lebt im Speicher; die
  Notizdatei `memory.md` des Agenten (im `data/`-Volume) bleibt erhalten.
- **Nur REST/WS, kein SSH.** Automationen, Dienstaufrufe, Config-Flows und Neustarts sind
  abgedeckt; Datei-Änderungen in `custom_components/` nicht.
- **Seriell.** Ein Agent-Lauf zur Zeit — für einen Haushalts-Chat völlig ausreichend.
- **Sprachtranskription ist CPU-Arbeit.** Die erste Sprachnachricht lädt das Whisper-Modell
  (Cache in `data/`); eine kurze Nachricht braucht auf einer modernen CPU wenige Sekunden.

## Sicherheit

- Commits und Release-Tags sind signiert.
- Die CI führt bei jedem Push [CodeQL](https://github.com/marcobockelbrink/claude-matrix-bot/security/code-scanning)
  und Trivy aus (Dateisystem-, IaC- und Container-Image-Scans); Dependabot überwacht pip-,
  Docker- und GitHub-Actions-Abhängigkeiten. Secret Scanning mit Push-Schutz ist aktiv.
- Schwachstelle gefunden? Bitte die
  [private Schwachstellenmeldung](https://github.com/marcobockelbrink/claude-matrix-bot/security/advisories/new)
  nutzen — siehe [SECURITY.md](SECURITY.md).

## Entwicklung

`bot.py` ist eine Ein-Datei-Brücke — auf der einen Seite der matrix-nio-Event-Loop, auf der
anderen ein persistenter `ClaudeSDKClient`. `system_prompt.md` ist das Home-Assistant-Runbook
für den Agenten.

**Container-Image.** Multi-Stage-Build auf `python:3.14-slim`: Der Builder kompiliert die
Wheels, die eine Toolchain brauchen (python-olm), das Runtime-Image enthält nur `libolm3`,
`curl`, `git` und die installierten Pakete — keinen Compiler. **Alpine wurde geprüft und
verworfen**: `ctranslate2` (die Engine hinter faster-whisper) veröffentlicht überhaupt keine
musl-Wheels, d.h. entweder fällt die Sprachtranskription weg oder die komplette C++-Engine
müsste aus dem Quelltext gebaut werden. Ohne Lösung dafür nicht erneut versuchen.

🤖 Gebaut mit [Claude Code](https://claude.com/claude-code)
