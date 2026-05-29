# Claude Code – Installierte Tools & Skills
## Project of Bernd Lorenzer
Übersicht über alles, was in dieser Session eingerichtet wurde, und wie du es mit Claude Code (VS Code) verwendest.

---

## 1. oh-my-claudecode (OMC)

**Was es ist:** Multi-Agenten-Orchestrierungsschicht für Claude Code. Koordiniert spezialisierte Agenten, Skills und Tools automatisch.

**Installation:** `npm i -g oh-my-claude-sisyphus@latest` + `omc setup`

### Skills (Aufruf: `/oh-my-claudecode:<name>` oder `/name` falls verfügbar)

| Skill | Befehl | Beschreibung |
|-------|--------|--------------|
| Autopilot | `/oh-my-claudecode:autopilot "Aufgabe"` | Vollautomatische Ausführung von Idee bis fertiger Code |
| Ultrawork | `/oh-my-claudecode:ultrawork` | Parallele Ausführung für hohen Durchsatz |
| Ralph | `/oh-my-claudecode:ralph` | Selbstreferenzieller Loop bis Aufgabe erledigt |
| Team | `/oh-my-claudecode:team` | N koordinierte Agenten auf einer geteilten Aufgabenliste |
| Ralplan | `/oh-my-claudecode:ralplan` | Konsens-Planung vor Ausführung (gut bei unklaren Aufgaben) |
| Deep Interview | `/oh-my-claudecode:deep-interview` | Sokrates-Interview zur Anforderungsklärung |
| Deep Dive | `/oh-my-claudecode:deep-dive` | 2-stufige Pipeline: Trace → Deep Interview |
| UltraQA | `/oh-my-claudecode:ultraqa` | QA-Zyklus: testen, verifizieren, fixen, wiederholen |
| CCG | `/oh-my-claudecode:ccg` | Claude + Codex + Gemini Tri-Modell-Orchestrierung |
| OmC Plan | `/oh-my-claudecode:omc-plan` | Strategische Planung mit optionalem Interview |
| OMC Doctor | `/oh-my-claudecode:omc-doctor` | Installation diagnostizieren und reparieren |
| OMC Reference | `/oh-my-claudecode:omc-reference` | Agenten-Katalog, Tools, Team-Routing |
| Self Improve | `/oh-my-claudecode:self-improve` | Autonome Code-Verbesserungs-Engine |
| Wiki | `/oh-my-claudecode:wiki` | Persistente Markdown-Wissensbasis über Sessions hinweg |
| HUD | `/oh-my-claudecode:hud` | Statuszeile konfigurieren |
| Skill | `/oh-my-claudecode:skill` | Skills verwalten (list, add, remove, search) |
| Cancel | `/oh-my-claudecode:cancel` | Aktiven OMC-Modus abbrechen |
| Release | `/oh-my-claudecode:release` | Release-Workflow für OMC |
| Sciomc | `/oh-my-claudecode:sciomc` | Parallele Scientist-Agenten für Analysen |

### Eingebaute Agenten (werden automatisch geroutet)

`analyst`, `architect`, `code-reviewer`, `code-simplifier`, `critic`, `debugger`, `designer`, `document-specialist`, `executor`, `git-master`, `planner`, `qa-tester`, `scientist`, `security-reviewer`, `test-engineer`, `tracer`, `verifier`, `writer`

### Beispiele

```
# Vollautomatisch eine REST API bauen
/oh-my-claudecode:autopilot "Erstelle eine REST API für Aufgabenverwaltung"

# Code-Review durch Experten-Agent
/oh-my-claudecode:team "Reviewe meinen Code und fixe alle Bugs"

# Planung vor komplexer Aufgabe
/oh-my-claudecode:ralplan "Refaktoriere das gesamte Auth-System"
```

---

## 2. Deep Research Skills

**Quelle:** [Weizhena/Deep-Research-skills](https://github.com/Weizhena/Deep-Research-skills)
**Installiert in:** `~/.claude/skills/` und `~/.claude/agents/`

### Skills

| Skill | Befehl | Beschreibung |
|-------|--------|--------------|
| Research | `/research "Thema"` | Vorläufige Recherche + Gliederung erstellen |
| Research Add Fields | `/research-add-fields` | Felder zur Recherche-Gliederung hinzufügen |
| Research Add Items | `/research-add-items` | Objekte zur Recherche-Gliederung hinzufügen |
| Research Deep | `/research-deep` | Tiefe Recherche: pro Item einen unabhängigen Agenten starten |
| Research Report | `/research-report` | Ergebnisse in Markdown-Bericht zusammenfassen |

### Agent

- **web-search-agent** – Wird automatisch für Web-Recherche-Aufgaben eingesetzt

### Beispiel-Workflow

```
/research "Vergleich von React vs Vue 2024"
# → Gliederung wird erstellt

/research-deep
# → Für jeden Punkt wird ein Agent gestartet

/research-report
# → Fertiger Markdown-Bericht
```

**Voraussetzung:** `pip install pyyaml` (bereits installiert, v6.0.1)

---

## 3. Obsidian Skills

**Quelle:** [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills)
**Installiert:** Als Plugin (settings.json) + Skills direkt in `~/.claude/skills/`

### Skills

| Skill | Befehl | Beschreibung |
|-------|--------|--------------|
| Obsidian CLI | `/obsidian-cli` | Obsidian Vault lesen, erstellen, suchen, Notizen verwalten |
| Obsidian Markdown | `/obsidian-markdown` | Obsidian-Markdown mit Wikilinks, Embeds, Callouts erstellen |
| Obsidian Bases | `/obsidian-bases` | Obsidian Bases (.base Dateien) erstellen und bearbeiten |
| Defuddle | `/defuddle "URL"` | Webseiten zu sauberem Markdown extrahieren (Token sparen) |
| JSON Canvas | `/json-canvas` | JSON Canvas Dateien (.canvas) erstellen und bearbeiten |

### Beispiele

```
# Notiz in Obsidian erstellen
/obsidian-markdown "Erstelle eine Notiz über Python Best Practices"

# Webseite zu Markdown konvertieren
/defuddle "https://example.com/artikel"

# Canvas-Diagramm erstellen
/json-canvas "Erstelle ein Mind-Map für mein Projekt"
```

---

## 4. n8n Skills

**Quelle:** [czlonkowski/n8n-skills](https://github.com/czlonkowski/n8n-skills)
**Installiert in:** `~/.claude/skills/`

### Skills

| Skill | Befehl | Beschreibung |
|-------|--------|--------------|
| n8n JavaScript | `/n8n-code-javascript` | JavaScript in n8n Code-Nodes schreiben |
| n8n Python | `/n8n-code-python` | Python in n8n Code-Nodes schreiben |
| n8n Expressions | `/n8n-expression-syntax` | n8n Expression-Syntax validieren und Fehler fixen |
| n8n MCP Tools | `/n8n-mcp-tools-expert` | n8n-MCP-Tools effektiv nutzen |
| n8n Node Config | `/n8n-node-configuration` | Nodes korrekt konfigurieren |
| n8n Validation | `/n8n-validation-expert` | Validierungsfehler interpretieren und beheben |
| n8n Patterns | `/n8n-workflow-patterns` | Bewährte Workflow-Architekturmuster |

### Beispiele

```
# n8n JavaScript-Code für einen HTTP-Request schreiben
/n8n-code-javascript "Fetch Daten von einer API und filtere nach Status = active"

# Workflow-Pattern für Fehlerbehandlung
/n8n-workflow-patterns "Zeig mir ein Retry-Pattern mit Error-Handling"

# Validierungsfehler lösen
/n8n-validation-expert "Mein HTTP Request Node zeigt 'Parameter required' Fehler"
```

---

## 5. Playwright MCP Server

**Was es ist:** Browser-Automatisierung direkt aus Claude Code heraus (Screenshots, Formulare ausfüllen, Web-Scraping, Tests).

**Konfiguriert in:** `~/.claude/mcp.json`

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

### Verwendung

Der Playwright-Server wird automatisch als Tool verfügbar, sobald Claude Code gestartet ist. Du kannst direkt in der Chat-Eingabe sagen:

```
"Öffne https://example.com und mach einen Screenshot"
"Fülle das Formular auf Seite X aus"
"Scrape alle Produktpreise von dieser Seite"
"Führe End-to-End-Tests für meine Login-Seite aus"
```

---

## 6. Frontend Design Plugin

**Quelle:** [anthropics/claude-code](https://github.com/anthropics/claude-code) → `plugins/frontend-design`
**Installiert in:** `~/.claude/plugins/frontend-design`

**Was es ist:** Spezialisiertes Plugin für UI/UX-Design und Frontend-Entwicklung.

### Verwendung

```
# Automatisch aktiviert für Frontend-Aufgaben
"Erstelle eine moderne React Landing Page"
"Designe ein Dashboard mit Tailwind CSS"
"Verbessere die UX meiner Formular-Komponente"
```

---

## Schnellreferenz: Alle Befehle auf einen Blick

```
# OMC Orchestrierung
/oh-my-claudecode:autopilot "..."     # Vollautomatisch
/oh-my-claudecode:ultrawork           # Parallele Ausführung
/oh-my-claudecode:ralph               # Loop bis fertig
/oh-my-claudecode:team                # Team-Modus
/oh-my-claudecode:ralplan "..."       # Erst planen, dann ausführen

# Recherche
/research "Thema"                     # Recherche starten
/research-deep                        # Tiefe Analyse
/research-report                      # Bericht erstellen

# Obsidian
/obsidian-cli                         # Vault verwalten
/obsidian-markdown                    # Notizen erstellen
/defuddle "URL"                       # Web → Markdown

# n8n
/n8n-code-javascript                  # JS Code schreiben
/n8n-workflow-patterns                # Patterns nachschlagen
/n8n-validation-expert                # Fehler lösen

# Browser (Playwright MCP)
"Mach einen Screenshot von ..."
"Scrape Daten von ..."
```

---

## Neustart erforderlich?

Nach Änderungen an `settings.json` oder neuen Skills:
- **VS Code:** `Ctrl+Shift+P` → `Developer: Reload Window`
- **Oder:** `/reset` im Chat-Fenster für neue Session

---

## Dateipfade

| Datei/Ordner | Inhalt |
|---|---|
| `~/.claude/settings.json` | Hauptkonfiguration (Hooks, Plugins, Statuszeile) |
| `~/.claude/mcp.json` | MCP-Server (Playwright) |
| `~/.claude/skills/` | Alle installierten Skills |
| `~/.claude/agents/` | Agenten-Definitionen |
| `~/.claude/plugins/` | Installierte Plugins |
| `~/.claude/CLAUDE.md` | OMC Systemprompt-Erweiterung |
#   W o r k s p a c e 
 
 