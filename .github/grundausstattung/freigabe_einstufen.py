#!/usr/bin/env python3
"""Freigabe einstufen: braucht diese Auslieferung einen Menschen?

FEGAS/CLAUDE.md nennt unter „Immer fragen“ vier Fälle: destruktive Migrationen (DROP, TRUNCATE), Passwörter oder
Zugangsdaten ändern, Löschen über Live-APIs auf Produktivdaten, externer Kontakt. Die Regel galt bisher für Claude
im Gespräch; dieser Prüfer macht sie zum Mechanismus im Deploy-Workflow: Er vergleicht den Bereich vom letzten
ausgelieferten Stand bis zum neuen und meldet heikel=true, wenn eines zutrifft. Dann wartet der Job `freigabe` in
der Umgebung mit Required Reviewer; sonst läuft die Auslieferung wie immer, ohne Klick.

Regeln (jede nennt den Grund mit Datei):
  (1) eine geänderte .sql ist als Sperre destruktiv: DROP TABLE/SCHEMA, ALTER TABLE … DROP [COLUMN] (nicht
      CONSTRAINT, DEFAULT, NOT NULL, IDENTITY), TRUNCATE als Anweisung, DELETE FROM außerhalb von Funktionsrümpfen
      (ein DO-Block läuft sofort und zählt, eine Funktion erst später); oder sie legt Rollen an, ändert oder löscht
      sie (CREATE/ALTER/DROP ROLE oder USER, auch ohne PASSWORD: eine Login-Rolle ist ein Zugang); oder sie führt
      dynamisches SQL mit unbekanntem Inhalt aus (EXECUTE <variable>). Breiter als die Warnregel der Grundprüfung,
      aber mit demselben Scanner: Wörter in Kommentaren und Stringliteralen zählen nicht, dynamische Literale
      hinter EXECUTE, format( oder || schon (Kaltleser und Codex 26.10.2026).
  (2) eine geänderte, gelöschte, verschobene oder im Typ geänderte Datei passt auf freigabe.zugangsdaten_pfade
      (fnmatch; ** wie *; ohne Umbenennungserkennung, damit der alte Pfad sichtbar bleibt)
  (3) in einer geänderten Datei unter freigabe.api_pfade berührt ein DELETE-Aufruf der Kopf-Fassung eine hinzugefügte
      Zeile (method: 'DELETE' in jeder Schreibweise, auch über Zeilenumbrüche oder mit zitiertem Schlüssel, -X DELETE,
      .delete('http…); Testdateien zählen nicht, sie rufen Attrappen); eine geänderte, gelöschte oder verschobene
      Datei aus freigabe.loeschschutz_pfade (etwa ein Test, der DELETE in Modulen verbietet) zählt ebenfalls
  (4) der Deploy-Workflow (.github/workflows/deploy*.yml), dieser Prüfer, sein Hilfsmodul migration_destruktiv.py
      oder die Konfiguration (freigabe.json, konfiguration.json) wurde geändert oder gelöscht; sonst könnte ein
      Commit die Schranke und den Grund für die Schranke zusammen ausbauen. Der Workflow lädt Prüfer und
      Konfiguration deshalb aus dem Basisstand, nicht aus dem geprüften Kopf.
  (5) ein zugehöriger Pull Request trägt das Label (freigabe.label, Standard freigabe-nötig); die Labels übergibt
      der Workflow mit --labels, damit der Prüfer ohne Netz testbar bleibt. Externen Kontakt sieht kein Diff; dafür
      ist das Label da (Regel in der CLAUDE.md des Repos).
  (6) --unsicher GRUND: der Workflow konnte den Bereich nicht sicher bestimmen (kein vermerkter Stand, Labels nicht
      lesbar, Basisdateien fehlen). Ein unsicherer Bereich sperrt, er öffnet nicht.

Konfiguration: .github/grundausstattung/freigabe.json, ersatzweise der Schlüssel freigabe in konfiguration.json.
Ohne Konfiguration gelten die Regeln (1), (4), (5) und (6) mit dem Standard-Label.

Aufruf:  python3 -I freigabe_einstufen.py --basis SHA --kopf SHA [--wurzel .] [--konfiguration PFAD]
                                           [--labels "a,b"] [--unsicher GRUND] [--json]
Ausgabe: ohne --json: Zeilen heikel=true|false und gruende=… (einzeilig, Gründe mit · getrennt) nach $GITHUB_OUTPUT
         und eine Zusammenfassung nach $GITHUB_STEP_SUMMARY, wenn gesetzt; immer nach stdout.
         mit --json: nur das JSON nach stdout, der Aufrufer setzt die Outputs selbst.
Exit:    0 Einstufung getroffen (auch heikel) · 2 Fehlbedienung oder Bereich nicht bestimmbar
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migration_destruktiv import RECHTE_DAVOR_RE, bereinigt  # noqa: E402

STANDARD_LABEL = "freigabe-nötig"
DELETE_RE = re.compile(r"""['"]?method['"]?\s*[:=]\s*[`'"]delete[`'"]|-X\s+DELETE\b|\.delete\(\s*[`'"]https?://""", re.IGNORECASE | re.DOTALL)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
DEPLOY_WORKFLOW_RE = re.compile(r"^\.github/workflows/deploy[^/]*\.ya?ml$")
SCHRANKEN_DATEIEN = {Path(__file__).name, "migration_destruktiv.py", "freigabe.json", "konfiguration.json"}
TESTDATEI_RE = re.compile(r"(^|/)(test|tests|__tests__|spec)/|\.(test|spec)\.[A-Za-z0-9]+$")
# ALTER TABLE: jede Teilaktion der Anweisung zählt (ADD COLUMN x, DROP COLUMN y), auch mit zitiertem Namen
SQL_DESTRUKTIV_RE = re.compile(
    r"\bdrop\s+(table|schema)\b"
    r"|\balter\s+table\b[^;]*?\bdrop\s+(column\s+)?(if\s+exists\s+)?(?!constraint\b|default\b|not\s+null\b|identity\b|expression\b)[\w\"]",
    re.IGNORECASE | re.DOTALL)
SQL_DELETE_RE = re.compile(r"\bdelete\s+from\b", re.IGNORECASE)
SQL_TRUNCATE_RE = re.compile(r"\btruncate\b", re.IGNORECASE)
SQL_ROLLEN_RE = re.compile(r"\b(create|alter|drop)\s+(role|user|group)\b", re.IGNORECASE)
# EXECUTE mit unbekanntem Inhalt ist heikel: eine Variable, ein geklammerter Ausdruck, format( mit einer Variablen
# statt eines Literals. EXECUTE FUNCTION/PROCEDURE (Trigger), EXECUTE '…' und EXECUTE format('…') sind lesbar und
# laufen durch die anderen Regeln (WB-C2-4)
# format( gilt als lesbar, wenn nach der Klammer (und Leerraum) ein Literal folgt; der Lookahead steht vor dem Leerraum,
# damit ein Rückschritt des \s* ihn nicht aushebelt (gemessen: format( 'create role …' galt sonst als Variable)
SQL_EXECUTE_RE = re.compile(r"\bexecute\s+(?:format\s*\((?!\s*[`'\"])|(?!function\b|procedure\b|format\b)(?:\(|[A-Za-z_]))", re.IGNORECASE)
# $$ … $$ ohne Tag getrennt von $tag$ … $tag$: eine leere Rückreferenz passt in Python auf nichts
DOLLAR_BLOCK_RE = re.compile(r"\$\$.*?\$\$|\$([A-Za-z_][A-Za-z0-9_]*)\$.*?\$\1\$", re.DOTALL)
DO_DAVOR_RE = re.compile(r"\bdo(\s+language\s+(\"[^\"]*\"|\w+))?\s*$", re.IGNORECASE)


def ohne_funktionsruempfe(text):
    """Dollar-Blöcke ausblenden, die eine Funktion definieren (laufen später); ein Block hinter DO läuft sofort und bleibt."""
    def ersetze(m):
        return m.group(0) if DO_DAVOR_RE.search(text[:m.start()]) else " " * len(m.group(0))
    return DOLLAR_BLOCK_RE.sub(ersetze, text)


def sql_gruende(sql):
    """Gründe einer Migration für die Sperre; leer, wenn sie weder Daten löscht noch Zugänge anfasst."""
    text = bereinigt(sql)
    gruende = []
    m = SQL_DESTRUKTIV_RE.search(text)
    if m:
        gruende.append("destruktive Anweisung ALTER TABLE … DROP" if m.group(0).lower().startswith("alter") else f"destruktive Anweisung {' '.join(m.group(0).split()).upper()}")
    if SQL_DELETE_RE.search(ohne_funktionsruempfe(text)):
        gruende.append("destruktive Anweisung DELETE FROM")
    for m in SQL_TRUNCATE_RE.finditer(text):
        if RECHTE_DAVOR_RE.search(text[:m.start()]) or re.match(r"\s+on\b", text[m.end():], re.IGNORECASE):
            continue
        gruende.append("destruktive Anweisung TRUNCATE")
        break
    m = SQL_ROLLEN_RE.search(text)
    if m:
        gruende.append(f"Rolle in SQL ({' '.join(m.group(0).split()).upper()})")
    if SQL_EXECUTE_RE.search(text):
        gruende.append("dynamisches SQL mit unbekanntem Inhalt (EXECUTE einer Variablen)")
    return gruende


def lade_konfiguration(wurzel, pfad=None):
    """freigabe.json oder konfiguration.json["freigabe"]; fehlt beides, leere Konfiguration."""
    kandidaten = [Path(pfad)] if pfad else [wurzel / ".github" / "grundausstattung" / "freigabe.json"]
    for k in kandidaten:
        if k.exists():
            d = json.loads(k.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    konf = wurzel / ".github" / "grundausstattung" / "konfiguration.json"
    if konf.exists():
        d = json.loads(konf.read_text(encoding="utf-8"))
        f = d.get("freigabe") if isinstance(d, dict) else None
        return f if isinstance(f, dict) else {}
    return {}


def git(wurzel, *args):
    # errors=replace: eine Migration in Latin-1 (gemessen in wbhv-os) darf die Einstufung nicht abbrechen
    r = subprocess.run(["git", "-C", str(wurzel), *args], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} fehlgeschlagen")
    return r.stdout


def geaenderte_dateien(wurzel, basis, kopf, filter_):
    """Pfade im Bereich nach Diff-Filter, NUL-getrennt gelesen (Umlaute, Leerzeichen). Ohne Umbenennungserkennung:
    ein verschobener Schutz-Test erscheint so als gelöscht (D) und als neu (A), nicht nur unter dem Zielpfad (WB-C1-9)."""
    roh = git(wurzel, "diff", "--name-only", "-z", "--no-renames", f"--diff-filter={filter_}", basis, kopf)
    return [p for p in roh.split("\0") if p]


def hinzugefuegte_zeilennummern(wurzel, basis, kopf, datei):
    """Zeilennummern der hinzugefügten Zeilen in der Kopf-Fassung der Datei (aus den Hunk-Köpfen von -U0)."""
    diff = git(wurzel, "diff", "-U0", "--no-renames", basis, kopf, "--", datei)
    nummern = set()
    for m in HUNK_RE.finditer(diff):
        start = int(m.group(1))
        anzahl = int(m.group(2)) if m.group(2) is not None else 1
        nummern.update(range(start, start + anzahl))
    return nummern


def delete_aufruf_neu(wurzel, basis, kopf, datei):
    """Ein DELETE-Aufruf in der Kopf-Fassung, der eine hinzugefügte Zeile berührt (eine Zeile Spiel: wer in einem
    vorhandenen fetch-Objekt nur 'GET' zu 'DELETE' ändert, fügt die Zeile mit method: nicht hinzu, WB-C1-11)."""
    neu = hinzugefuegte_zeilennummern(wurzel, basis, kopf, datei)
    if not neu:
        return None
    try:
        text = git(wurzel, "show", f"{kopf}:{datei}")
    except RuntimeError:
        return None
    for m in DELETE_RE.finditer(text):
        von = text.count("\n", 0, m.start()) + 1
        bis = text.count("\n", 0, m.end()) + 1
        if any(z in neu for z in range(von - 1, bis + 2)):
            return " ".join(m.group(0).split())[:60]
    return None


def passt(pfad, muster):
    """fnmatch mit ** wie *: GitHubs Unterschied bei Schrägstrichen ist für diese Listen ohne Belang."""
    return any(fnmatch.fnmatchcase(pfad, str(m).replace("**", "*")) or fnmatch.fnmatchcase(pfad, str(m)) for m in muster)


def einstufen(wurzel, basis, kopf, konf, labels=(), unsicher=None):
    """Liefert die Liste der Gründe; leer heißt heikel=false."""
    gruende = []
    if unsicher:
        gruende.append(f"Bereich nicht sicher bestimmbar: {unsicher}")
    # Pfadregeln sehen auch gelöschte (D) und im Typ geänderte (T) Dateien; Inhaltsregeln nur Dateien, die am Kopf da sind
    alle = geaenderte_dateien(wurzel, basis, kopf, "AMDT")
    mit_inhalt = set(geaenderte_dateien(wurzel, basis, kopf, "AM"))
    zugang = konf.get("zugangsdaten_pfade") or []
    api = konf.get("api_pfade") or []
    loeschschutz = konf.get("loeschschutz_pfade") or []
    label = konf.get("label") or STANDARD_LABEL
    for d in alle:
        if d.endswith(".sql") and d in mit_inhalt:
            try:
                text = git(wurzel, "show", f"{kopf}:{d}")
            except RuntimeError:
                text = ""
            for g in sql_gruende(text):
                gruende.append(f"{g}: {d}")
        if zugang and passt(d, zugang):
            gruende.append(f"Zugangsdaten oder Rollen geändert: {d}")
        if api and passt(d, api) and d in mit_inhalt and not TESTDATEI_RE.search(d):
            treffer = delete_aufruf_neu(wurzel, basis, kopf, d)
            if treffer:
                gruende.append(f"neuer DELETE-Aufruf an eine API: {d} ({treffer})")
        if loeschschutz and passt(d, loeschschutz):
            gruende.append(f"Schutz vor Löschaufrufen geändert: {d}")
        if DEPLOY_WORKFLOW_RE.match(d) or (d.startswith(".github/grundausstattung/") and Path(d).name in SCHRANKEN_DATEIEN):
            gruende.append(f"Deploy-Workflow, Freigabe-Prüfer oder seine Konfiguration geändert: {d}")
    for l in labels:
        if l.strip() == label:
            gruende.append(f"Label {label} an einem zugehörigen Pull Request")
            break
    # einzeilig: ein Dateiname mit Zeilenumbruch könnte sonst in einer Output-Datei einen zweiten Schlüssel setzen (WB-C1-12)
    return [" ".join(g.split()) for g in gruende]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--basis", required=True, help="Commit des letzten ausgelieferten Stands")
    ap.add_argument("--kopf", default="HEAD", help="auszuliefernder Commit")
    ap.add_argument("--wurzel", default=".")
    ap.add_argument("--konfiguration", help="Pfad zur freigabe.json (der Workflow gibt die Fassung des Basisstands)")
    ap.add_argument("--labels", default="", help="kommagetrennte Labels der zugehörigen Pull Requests")
    ap.add_argument("--unsicher", default="", help="Grund, warum der Bereich nicht sicher bestimmbar war; sperrt")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    wurzel = Path(args.wurzel)
    try:
        basis = git(wurzel, "rev-parse", "--verify", f"{args.basis}^{{commit}}").strip()
        kopf = git(wurzel, "rev-parse", "--verify", f"{args.kopf}^{{commit}}").strip()
        konf = lade_konfiguration(wurzel, args.konfiguration)
        gruende = einstufen(wurzel, basis, kopf, konf, [l for l in args.labels.split(",") if l.strip()], args.unsicher.strip() or None)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"::error::Einstufung nicht möglich: {e}")
        return 2
    heikel = bool(gruende)
    if args.json:
        print(json.dumps({"heikel": heikel, "basis": basis, "kopf": kopf, "gruende": gruende}, ensure_ascii=False, indent=2))
        return 0
    for g in gruende:
        print(f"::notice::Freigabe nötig: {g}")
    print(f"SUMMARY heikel={'true' if heikel else 'false'} gruende={len(gruende)} basis={basis[:7]} kopf={kopf[:7]}")
    einzeilig = " · ".join(gruende)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"heikel={'true' if heikel else 'false'}\n")
            f.write(f"gruende={einzeilig}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            if heikel:
                f.write(f"## Freigabe nötig ({basis[:7]}..{kopf[:7]})\n\n" + "\n".join(f"- {g}" for g in gruende) + "\n\nDer Job `freigabe` wartet auf einen Required Reviewer.\n")
            else:
                f.write(f"## Keine Freigabe nötig ({basis[:7]}..{kopf[:7]})\n\nKeiner der Fälle aus „Immer fragen“ im Diff; die Auslieferung läuft ohne Klick.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
