# AI_INSIGHTS.md — Eigene Beobachtungen des technischen Leads

> Diese Datei gehört dem AI-Lead. Eigenständig gepflegt, ohne explizite
> Aufforderung aktualisiert, sobald beim Arbeiten etwas auffällt.
> Initialer Scan: 2026-05-16, nach Lesen von PLAN.md, ASSUMPTIONS.md,
> docs/wiki/, results/phase4_*.json, config/, und dem gesamten src-Baum.

---

## VERDIKT — ORB 15-min NQ — NOT VIABLE (2026-05-17)

Der vollständige Walk-Forward + die Tradeify-SELECT-Simulation + beide
Robustheits-Checks sind gelaufen. Ergebnis: **NOT VIABLE.**

Belegt durch drei unabhängige Linien:

1. **Gelocktes Kriterium.** Tradeify 180-Tage-WIN-Rate = **28.6%** (< 40%).
   71.4% der 35 rollierenden Fenster killen den Account am $2.000-EOD-
   Trailing-DD, bevor +$3.000 erreicht wird. → NOT VIABLE per Definition.
2. **Bootstrap-CI (CHECK A).** Das 95%-Konfidenzintervall des mittleren
   net-R pro Trade spannt für JEDEN OOS-Split UND das Aggregat durch Null
   (Aggregat: mean +0.011, CI [-0.159, +0.177]). Der Edge ist statistisch
   nicht von Null unterscheidbar — nicht verifiziert.
3. **Null-Model (CHECK B).** Aggregat-p-Wert = **0.169** (> 0.05). Das
   ORB-Entry-Timing schlägt zufälliges Entern innerhalb von 30 Minuten
   nach dem Signal NICHT. Das Timing trägt keine verwertbare Information.
   (Einzig Split C zeigte p=0.016 — ein einzelnes 7-Monats-Fenster, das
   das Aggregat trägt; A und B: p=0.49 / 0.55.)

Der OOS-Aggregat-Avg-Net von +$17.92/Trade wirkt positiv, ist aber
vollständig von Split C getragen (WR 53%, +$12.404); Split A und B
verlieren beide (WR 36%/39%, -$5.863 / -$2.939). Bootstrap und Null-Model
bestätigen: das ist Rauschen, kein Edge.

ORB ist damit zwar mechanisch sauber gebaut (459 Trades, R:R exakt 1.5,
ehrliche $19-Kosten, look-ahead-frei) und besser als die NasdaqAle-ICT-
Strategie (avg net +$17.92 vs -$2.65; Tradeify-WR 28.6% vs 2.9%) — aber
weiterhin **nicht** profitabel genug für eine Tradeify-SELECT-Challenge.

Volldaten: `results/phase4_orb_tradeify_sim.json`,
`results/phase4_orb_vs_nasdaqale.json`. Konsequenzen werden separat
besprochen (Owner-Entscheidung).

---

### Pfad- und Owner-Diskrepanz im Projekt-Setup
**Typ:** Risiko
**Priorität:** Niedrig
**Beobachtung:** Der Kontext-Transfer nennt `c:/Users/Bernd/Workspace/nasdaq_ale_bot/`
als Projektpfad. Tatsächlich liegt das Projekt unter
`c:\Users\loren\Private\Workspace\Workspace\nasdaq_ale_bot\`. Die Datei
`.claude/settings.local.json` führt `additionalDirectories` auf
`C:\Users\Bernd\.claude` und `C:\Users\Bernd\Workspace\nasdaq_ale_bot\.omc` —
beide existieren am aktuellen Rechner nicht. Es gab offenbar einen
Rechner-/User-Wechsel (Bernd → loren); die alten Pfad-Einträge sind tot.
**Empfehlung:** Einen kanonischen Pfad festlegen. Tote `Bernd`-Einträge aus
`settings.local.json` entfernen. Beim ORB-Build keine absoluten Pfade
hardcoden — `Path(__file__).resolve().parents[N]` verwenden (so wie die
phase3/phase4-Scripts es bereits tun).
**Betroffene Dateien:** `.claude/settings.local.json`, Kontext-Handoff
**Datum:** 2026-05-16

---

### news_events.csv Staleness ist ein stiller Live-Trading-Kill-Switch
**Status:** RESOLVED (2026-05-16) — Für die ORB-Baseline wird KEIN
NewsBlackoutGate verwendet (spec §9.2). Die ORB-State-Machine erzwingt ihr
Trading-Fenster strukturell (Zustand `WAITING_FOR_BREAKOUT` läuft nur
09:45-12:00) und verdrahtet bewusst keine GateList. Damit ist die Baseline
ein bekannter, ehrlicher Zustand statt eines stillen Stale-CSV-Verhaltens.
Ein News-Filter ist nur ein Kandidat, falls die Baseline MARGINAL ausfällt.
**Typ:** Risiko
**Priorität:** Hoch
**Beobachtung:** `NewsBlackoutGate` ist fail-closed: wenn
`data/news_events.csv` älter als 24h ist (`assert_fresh`, `filters/news.py`),
wird über `NewsFeedStale` *jeder* Trade blockiert. In allen Backtests dieser
Session war ein `touch data/news_events.csv` nötig, sonst 0 Trades. Die CSV
ist nur ein Platzhalter-Stub (1 Zeile, 2020-01-01). Im Live-Betrieb heißt
das: wird der News-Feed nicht täglich aktualisiert, hört der Bot lautlos auf
zu traden und wirkt dabei "gesund".
**Empfehlung:** Vor dem ORB-Build explizit entscheiden, ob ORB überhaupt ein
News-Gate nutzt. 15-min ORB ist klassischerweise robust gegen die Cash-Open-
Volatilität — ein News-Blackout ist evtl. gar nicht erwünscht. Falls doch:
echten Feed anbinden oder den Staleness-Mechanismus so bauen, dass er im
Backtest nicht greift und im Live-Betrieb hart alarmiert statt still zu
blockieren.
**Betroffene Dateien:** `src/nasdaq_ale_bot/filters/news.py`,
`src/nasdaq_ale_bot/execution/gates.py`, `data/news_events.csv`
**Datum:** 2026-05-16

---

### Kostenmodell-Inkonsistenz: MockBroker vs. realistische Sims
**Status:** RESOLVED (2026-05-16) — `config/cost_model.yaml` ist die Single
Source of Truth. `execution/cost_model.py` (`CostModel`, `load_cost_model`)
lädt sie. `MockBroker` nimmt einen optionalen `cost_model`-Parameter: ist er
gesetzt, werden auf JEDEM Fill Kommission ($4.50/Seite) UND 1-Tick-Slippage
angewandt; ist er `None`, gilt das Legacy-Verhalten (NasdaqAle-Tests
unverändert, 359 grün). Test `test_cost_model.py` beweist: ein flacher
1-NQ-Round-Trip kostet exakt $19.00. Alle ORB-Pfade verwenden den
cost_model — keine getrennte Kostenaddition in Scripts.
**Typ:** Risiko
**Priorität:** Hoch
**Beobachtung:** `MockBroker` zieht intern nur `COMMISSION_PER_CONTRACT =
$4.50` pro Kontrakt ab — einmalig beim Exit, behandelt als gesamte
Round-Trip-Kommission (`mock_broker.py`). Die Apex/Tradeify-Viability-Sims
verwenden dagegen das realistische Modell: $9 Kommission + $10 Slippage =
$19 round-trip pro NQ. Differenz: ~$14.50/Trade. Genau diese Lücke hat das
Cost-Break-Even-Problem der NasdaqAle-Strategie 8 Monate lang verdeckt — die
Zwischen-Backtests sahen profitabel aus, der finale Viability-Test mit
realistischen Kosten zeigte -$2.65/Trade Net.
**Empfehlung:** Das ORB-Backtest-Setup muss das volle $19-Round-Trip-Modell
ab dem allerersten Lauf anwenden — entweder `COMMISSION_PER_CONTRACT` auf
realistische Werte heben + einen Slippage-Term ergänzen, oder eine
einheitliche Kosten-Schicht im Runner. Sonst überschätzt jeder ORB-
Walk-Forward den Edge. Das kanonische Kostenmodell steht in TECH_STACK.md.
**Betroffene Dateien:** `src/nasdaq_ale_bot/execution/mock_broker.py`,
`src/nasdaq_ale_bot/backtest/runner.py`
**Datum:** 2026-05-16

---

### ORB Look-Ahead-Risiko: die Opening Range muss VOR dem Breakout-Check schließen
**Status:** RESOLVED (2026-05-16) — `OpeningRange` friert strukturell ein:
die Range schließt beim ersten Bar mit NY-Zeit >= 09:45; der 09:44-Bar ist
der letzte Range-Bar. `BreakoutDetector` prüft vor jedem 5-Min-Close, dass
die OR `FROZEN` ist, sonst `OpeningRangeNotFrozenError` (fail-closed). Der
5-Min-Confirmation-Bar ist beim Signal voll geschlossen; der Entry läuft
auf dem OPEN des NÄCHSTEN 1-Min-Bars. Test
`test_lookahead_guard_raises_when_or_not_frozen` deckt den Guard ab.
**Typ:** Risiko
**Priorität:** Hoch
**Beobachtung:** Die Opening Range (OR-High/OR-Low der ersten 15 Minuten)
ist ein stateful Akkumulator — strukturell identisch zu
`Bucketed4HAggregator` / `DailyAggregator` in `bias/timeframe.py`. Wenn ein
Breakout-Check Bars liest, die noch innerhalb der sich bildenden Range
liegen, ODER die Range mit der Breakout-Bar selbst finalisiert wird, ist das
Look-Ahead-Bias. `CandleView` schützt nur Detection-Funktionen, nicht
Engine-Level-State wie diesen Akkumulator.
**Empfehlung:** Die OR exakt nach dem Aggregator-Muster bauen: die Range gilt
erst als "geschlossen", wenn die erste Bar NACH dem 15-min-Fenster eintrifft
(`on_1m_bar` gibt die geschlossene Range zurück, nicht vorher). Breakout-
Evaluierung erst ab dieser Bar. Ein dedizierter Test wie der Look-Ahead-Audit
aus Check 1 sollte das pro Trade verifizieren.
**Betroffene Dateien:** geplant `src/nasdaq_ale_bot/strategies/orb/`,
Referenzmuster in `src/nasdaq_ale_bot/bias/timeframe.py`
**Datum:** 2026-05-16

---

### "95% Infrastruktur-Reuse" für ORB ist zu optimistisch
**Typ:** Architektur
**Priorität:** Mittel
**Beobachtung:** Der Kontext nennt 95% Reuse. Tatsächlich:
WIEDERVERWENDBAR (strategie-agnostisch) — `backtest/` (runner, metrics,
grid, walk_forward), `core/{candle, candle_view, account_ledger, leg,
liquidity, logging_sink}`, `execution/{broker, mock_broker, gates}`,
`filters/killzone`, `settings`. NICHT WIEDERVERWENDBAR (ICT-spezifisch) —
alle 6 Dateien in `detection/`, `core/state_machine.py` (die 6-State-ICT-
Engine), `bias/htf_bias.py` (4H-FVG-Bias). Realistischer Reuse: ~60-70% nach
Infrastruktur-/Harness-Schicht. Die Strategie-Logik selbst ist ein voller
Neuschrieb — was auch korrekt ist, ORB ist eine andere Strategie.
**Empfehlung:** Beim Multi-Strategy-Refactor die ICT-State-Machine mit unter
`strategies/nasdaqale/` ziehen, nicht in `core/` lassen. ORB bekommt eine
eigene, einfachere State-Machine (z.B. `PRE_RANGE → RANGE_BUILDING →
RANGE_SET → BREAKOUT_ARMED → IN_TRADE → DONE`). Nicht versuchen, ORB in die
6-State-ICT-Engine zu pressen — das wäre eine erzwungene Abstraktion.
`core/` sollte nur strategie-neutrale Primitive enthalten.
**Betroffene Dateien:** `src/nasdaq_ale_bot/core/state_machine.py`,
`src/nasdaq_ale_bot/detection/`, `src/nasdaq_ale_bot/bias/`
**Datum:** 2026-05-16

---

### PLAN.md ist veraltet — Phasen-Nummerierung von der Realität abgekoppelt
**Typ:** Architektur
**Priorität:** Mittel
**Beobachtung:** PLAN.md beschreibt durchgehend ein QQQ/SPY/Alpaca-Projekt:
"Phase 4 — Paper Live Runner" (Alpaca WebSocket), "Phase 3.5 — vectorbt
cross-check", "Phase 5 — Apex compliance". Realität: NQ/ES-Futures auf
Databento, `live/` ist leer (nur `__init__.py`), Apex wurde durch Tradeify
ersetzt, und das tatsächliche "Phase 3.5" war die Databento-Datenmigration.
Der Begriff "Phase 3.5" hat in der Projektgeschichte zwei verschiedene
Bedeutungen. Das Wiki `docs/wiki/04-assumptions-index.md` deckt nur A1-A24
ab, ASSUMPTIONS.md hat A1-A36.
**Empfehlung:** PLAN.md als historisches Dokument markieren (Header
"SUPERSEDED — siehe .context/STRATEGY.md") statt es zu löschen — die
ADRs §9/§9.A/§9.B haben noch dokumentarischen Wert. `.context/` ist ab jetzt
die Source of Truth. Beim ORB-Build keine Phasen-Nummern aus PLAN.md mehr
referenzieren.
**Betroffene Dateien:** `PLAN.md`, `docs/wiki/04-assumptions-index.md`,
`docs/wiki/index.md`
**Datum:** 2026-05-16

---

### Kleinere Hygiene-Punkte (gesammelt)
**Typ:** Optimierung
**Priorität:** Niedrig
**Beobachtung:** (a) `config/instruments.yaml` hat einen `correlated:`-Block
(ES) UND einen separaten `es:`-Block mit identischem Inhalt — Duplikat,
Drift-Risiko. Ebenso ein `mnq:`-Block, der für die NQ/Tradeify-Richtung nicht
mehr gebraucht wird. (b) `pyproject.toml` setzt `fail_under = 90` für
Coverage, aber `addopts = "-ra --strict-markers"` enthält kein `--cov` —
d.h. ein blankes `pytest` prüft die 90%-Schwelle gar nicht; sie greift nur
bei explizitem `--cov`. (c) `scikit-learn` wird von
`scripts/phase35_ml_session_regime.py` importiert, ist aber keine deklarierte
Dependency in `pyproject.toml` (nur ad-hoc installiert).
**Empfehlung:** (a) `instruments.yaml` auf einen kanonischen ES-Block
reduzieren. (b) Entscheiden, ob Coverage Teil des Standard-Testlaufs sein
soll; falls ja, `--cov` in `addopts` oder ein CI-Schritt. (c) `scikit-learn`
entweder als optionale Dependency deklarieren oder das ML-Script als
Wegwerf-Experiment markieren (es wurde laut Historie ohnehin verworfen).
Keiner dieser Punkte blockiert den ORB-Build — vor dem Refactor in einem
Rutsch aufräumen.
**Betroffene Dateien:** `config/instruments.yaml`, `pyproject.toml`,
`scripts/phase35_ml_session_regime.py`
**Datum:** 2026-05-16

---

### ORB Spec-Widerspruch: $500 Risk-Budget vs. Opposite-Edge-Stop auf NQ
**Status:** TEILWEISE GELÖST (2026-05-16) — Owner-Entscheidung: Stop von der
Gegenkante auf den OR-MITTELPUNKT verlegt (`compute_stop_target`, config
`placement: or_midpoint_plus_buffer`). Damit halbieren sich die Stop-
Distanzen grob. Restproblem: bei $500 Budget sind im Jan 2024 trotzdem nur
2/17 Signalen sizeable (stop_dist ≤ 25 pt). Budget-Eligibility: $500→2,
$750→9, $1000→17. Budget-Entscheidung beim Owner offen. Siehe auch den
folgenden Eintrag zum R:R.
**Typ:** Risiko
**Priorität:** Hoch
**Beobachtung:** Der ORB-Spec definiert (Part 2) den Stop = gegenüberliegende
OR-Kante ± Buffer, Sizing = `floor(risk_budget / (stop_dist × $20))`,
risk_budget = $500 (Part 5). Damit eine Position ≥ 1 Kontrakt ergibt, muss
`stop_dist ≤ 25` Punkte sein ($500 / ($20 × 25) = 1.0). Für einen
Breakout-Trade liegt der Entry aber jenseits einer OR-Kante, der Stop an der
GEGENÜBERLIEGENDEN Kante — also mindestens ~OR_range entfernt. NQ-Opening-
Ranges im Jan 2024 (Signal-Tage) lagen bei 14-76 Punkten, im Mittel ~50.
Ergebnis im Jan-2024-Sanity-Check: 17 Breakout-Signale, aber **16 davon
per Sizing übersprungen** (stop_dist 40-50 → qty 0), nur **1 Trade**. Part 7
erwartete 15-20 Trades. Das ist ein interner Widerspruch im Spec selbst:
Part 2 (Sizing-Regel) macht Part 7 (Trade-Erwartung) strukturell unmöglich.
Die Implementierung ist spec-treu — kein Bug. Der 50-Punkt-Stop-Cap hilft
nicht: er begrenzt den VERLUST, nicht die Sizing-Eligibility (50 × $20 =
$1000 > $500 → trotzdem qty 0).
**Empfehlung:** Vor den Steps 2-4 muss der Owner entscheiden — Optionen
(KEINE habe ich eigenmächtig gewählt; das wäre Iteration vor dem Test):
(a) risk_budget anheben (z.B. $1500-2000) — ändert das Risikoprofil;
(b) Stop nicht an der vollen Gegenkante, sondern enger (z.B. fixe Distanz
oder Bruchteil der OR) — Abweichung vom Spec-Wortlaut;
(c) MNQ statt NQ (1/10 Point-Value → $500 trägt 10× die Stop-Distanz) —
Spec sagt aber NQ.
Bis zur Klärung sind die Decision Criteria (avg net/trade, WR) nicht sinnvoll
messbar — bei ~1 Trade/Monat ist keine Statistik möglich.
**Betroffene Dateien:** `config/orb_strategy.yaml`,
`src/nasdaq_ale_bot/strategies/orb/state_machine.py` (`compute_position_size`)
**Datum:** 2026-05-16

---

### ORB Part-4-Abweichung: GateList nicht verdrahtet (strukturelle Enforcement)
**Typ:** Architektur
**Priorität:** Mittel
**Beobachtung:** Part 4 verlangt, die bestehende `GateList` für ORB
wiederzuverwenden (KillzoneGate auf 09:45-12:00, MaxTradesGate auf 1/Tag,
DailyLossGate -$1000, TrendRegimeGate aus, NewsBlackoutGate aus per §9.2,
MaxStop/SMT nicht nötig). Tatsächlich erzwingt die ORB-State-Machine das
Trading-Fenster (Zustand `WAITING_FOR_BREAKOUT` läuft nur 09:45-12:00) und
das 1-Trade/Tag-Limit (First-Signal-only → `DAY_DONE`) bereits STRUKTURELL.
Eine zusätzliche GateList-Schicht für genau diese Gates wäre redundant und
würde das Verhalten nicht ändern. DailyLossGate ist für eine 1-Trade/Tag-
Strategie ohnehin wirkungslos (es gibt keinen zweiten Entry zu blockieren).
**Empfehlung:** Für die Baseline keine GateList verdrahten — strukturelle
Enforcement ist ausreichend und vermeidet Over-Engineering (Pattern 7).
Das ist eine bewusste Abweichung von Part 4, hier offengelegt. Vom Owner
am 2026-05-16 ausdrücklich bestätigt.
**Betroffene Dateien:** `src/nasdaq_ale_bot/strategies/orb/state_machine.py`
**Datum:** 2026-05-16

---

### ORB R:R ist strukturell < 1:1 — die "2 Ticks past edge"-Annahme stimmt nicht
**Typ:** Risiko
**Priorität:** Hoch
**Beobachtung:** Die Owner-Begründung zum Mid-Stop nahm an, der Entry liege
"knapp jenseits einer OR-Kante (2 Ticks)" → Stop-Distanz ≈ OR_range/2 →
R:R ≈ 1:1. Die Jan-2024-Daten widerlegen das. Der Entry ist der OPEN des
1-Min-Bars NACH einem 5-Min-Confirmation-Bar. Dieser 5-Min-Bar schließt oft
viele Punkte jenseits der OR-Kante (Breakout-Extension), und der Entry-Bar
öffnet entsprechend weit draußen. Damit gilt:
  stop_dist = OR_range/2 + Breakout-Extension + Buffer
  target_dist = OR_range/2  (fix)
→ R:R = target_dist / stop_dist = (OR_range/2) / (OR_range/2 + Extension).
Auf den 17 Jan-2024-Signalen: R:R von 0.47 bis 0.95, Mittel ~0.71 — KEIN
einziges Signal erreichte 1:1. Bei R:R 0.71 liegt die Breakeven-WR (vor
Kosten) bei 1/(1+0.71) ≈ 58.5%; nach $19/Kontrakt-Kosten höher. Das ist eine
anspruchsvolle Hürde.
**Empfehlung:** Kein Blocker für den Walk-Forward — der misst genau die
tatsächliche WR. Aber wichtige Kontext-Zahl: ORB-NQ braucht ~58-62% WR, um
die Kosten zu schlagen. Falls der Walk-Forward das nicht liefert, ist die
Ursache strukturell (Extension-getriebenes Sub-1:1-R:R), nicht
Parameter-Tuning. Eine spätere Iteration müsste am Entry-Mechanik- oder
Target-Design ansetzen, nicht an risk_budget.
**Betroffene Dateien:** `src/nasdaq_ale_bot/strategies/orb/state_machine.py`
(`compute_stop_target`), `config/orb_strategy.yaml`
**Datum:** 2026-05-16
