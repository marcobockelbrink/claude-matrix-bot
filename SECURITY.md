# Security Policy / Sicherheitsrichtlinie

## English

### Reporting a vulnerability

Please report vulnerabilities via
[GitHub private vulnerability reporting](https://github.com/marcobockelbrink/claude-matrix-bot/security/advisories/new)
— **not** via public issues. You should get a first response within a few days.

### Supported versions

Only the latest release (and `main`) receives fixes.

### Scope & threat model

This bot intentionally holds powerful credentials (Home Assistant admin token, Claude
credentials, Matrix/Signal accounts). Deployment assumptions:

- The container host is trusted infrastructure.
- `.env` / Kubernetes Secrets are the only place credentials live; nothing is committed.
- The only inbound surface is the optional webhook/status port (token-protected); everything
  else is outbound-only.
- The sender allowlist plus (by default) chat confirmation for destructive commands are the
  guard rails — review them before deploying.

Reports about weaknesses in these mechanisms (allowlist bypass, token leakage, webhook auth,
confirmation bypass, prompt-injection paths that cross a trust boundary) are very welcome.

### Known limitations (by design)

- **`CONFIRM_DESTRUCTIVE` is an accident guard, not a security boundary.** It pattern-matches
  shell commands; an adversarially-steered agent could evade it (e.g. write a script, then
  execute it). The trust anchor remains the sender allowlist.
- **Prompt injection is inherent.** Text that reaches the agent (smart notifications built
  from sensor/device names, transcribed audio) can attempt to steer it. Only feed the
  webhook from sources you trust, and keep `CONFIRM_DESTRUCTIVE` on.
- **The agent can read its own credentials.** `HA_TOKEN` etc. are in the agent's tool
  environment (required to call HA); the system prompt forbids echoing them, but a
  successfully injected agent could leak them. Rotate tokens if you suspect this.
- **Matrix devices are not verified** (`ignore_unverified_devices`). Anyone who obtains an
  allowlisted account's password can read and command the bot — use strong, unique passwords.
- **Unfixed base-image CVEs** (Debian packages such as `perl`) appear in image scans; they
  have no upstream fix and are not reachable through the bot's attack surface. The image
  scan reports fixable findings only (`ignore-unfixed`).

## Deutsch

### Schwachstelle melden

Bitte melde Schwachstellen über die
[private Schwachstellenmeldung auf GitHub](https://github.com/marcobockelbrink/claude-matrix-bot/security/advisories/new)
— **nicht** über öffentliche Issues. Eine erste Antwort gibt es in der Regel innerhalb
weniger Tage.

### Unterstützte Versionen

Nur das jeweils aktuelle Release (und `main`) erhält Korrekturen.

### Geltungsbereich & Bedrohungsmodell

Dieser Bot hält absichtlich mächtige Zugangsdaten (Home-Assistant-Admin-Token,
Claude-Zugang, Matrix-/Signal-Konten). Annahmen für den Betrieb:

- Der Container-Host ist vertrauenswürdige Infrastruktur.
- Zugangsdaten leben ausschließlich in `.env` / Kubernetes-Secrets; nichts wird committet.
- Die einzige eingehende Fläche ist der optionale Webhook-/Status-Port (Token-geschützt);
  alles andere ist rein ausgehend.
- Die Absender-Allowlist plus (standardmäßig) Chat-Bestätigung für destruktive Befehle sind
  die Leitplanken — vor dem Einsatz prüfen.

Meldungen zu Schwächen in diesen Mechanismen (Allowlist-Umgehung, Token-Leaks,
Webhook-Auth, Umgehung der Bestätigung, Prompt-Injection über eine Vertrauensgrenze hinweg)
sind sehr willkommen.

### Bekannte Grenzen (bewusste Design-Entscheidungen)

- **`CONFIRM_DESTRUCTIVE` ist ein Unfallschutz, keine Sicherheitsgrenze.** Es matcht
  Shell-Befehle per Muster; ein gezielt manipulierter Agent könnte es umgehen (z.B. Skript
  schreiben, dann ausführen). Der Vertrauensanker bleibt die Absender-Allowlist.
- **Prompt-Injection ist systemimmanent.** Text, der den Agenten erreicht (Smart-Meldungen
  aus Sensor-/Gerätenamen, transkribiertes Audio), kann versuchen, ihn zu steuern. Den
  Webhook nur aus vertrauenswürdigen Quellen füttern und `CONFIRM_DESTRUCTIVE` anlassen.
- **Der Agent kann seine eigenen Zugangsdaten lesen.** `HA_TOKEN` & Co. stehen in seiner
  Tool-Umgebung (nötig für HA-Aufrufe); der System-Prompt verbietet die Ausgabe, aber ein
  erfolgreich injizierter Agent könnte sie leaken. Bei Verdacht: Tokens rotieren.
- **Matrix-Geräte werden nicht verifiziert** (`ignore_unverified_devices`). Wer das Passwort
  eines Allowlist-Accounts erlangt, kann den Bot mitlesen und steuern — starke, einmalige
  Passwörter verwenden.
- **Ungefixte Basisimage-CVEs** (Debian-Pakete wie `perl`) tauchen in Image-Scans auf; es
  gibt keinen Upstream-Fix, und sie sind über die Angriffsfläche des Bots nicht erreichbar.
  Der Image-Scan meldet nur behebbare Funde (`ignore-unfixed`).
