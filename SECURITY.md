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
