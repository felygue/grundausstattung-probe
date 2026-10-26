#!/usr/bin/env python3
"""Destruktive Migrationen: DROP TABLE oder TRUNCATE als Anweisung.

Übernommen aus servicewelt scripts/check-migration-destruktiv.mjs (Fassung vom 25.09.2026):
Kommentare werden vorher entfernt (#265: TRUNCATE in einer Erklärung blockierte den Deploy),
`truncate` nur als ganzes Wort außerhalb von Rechtelisten, also nicht in 'truncated' (#1767:
jsonb_build_object(…,'truncated',…)), nicht in `grant select, truncate on` und nicht in
`revoke truncate on`. Seit 26.10.2026 mit Zeichen-Scanner statt Regex (Codex GA-C1-20, WB-C1-6/-8/-15):
Blockkommentare mit Verschachtelungstiefe, zitierte Bezeichner "…" werden wie Texte ausgeblendet (sie
sind keine Anweisungen), einfache Literale werden ausgeblendet, außer sie laufen: hinter DO als ganzer Rumpf, hinter
EXECUTE, format( oder || als dynamisches SQL, das mit einem Befehl beginnt (führende Kommentare im
Literal zählen nicht). Dollar-Blöcke sind nur Rümpfe, wenn sie hinter DO, AS, EXECUTE, format(, || oder
am Anfang stehen; hinter IS oder RAISE sind sie Texte und werden ausgeblendet. Gemessen 26.10.2026 in
wbhv-os: 7 von 7 Treffern über 289 Commits waren Wörter in Literalen.

In der Grundprüfung läuft dieser Prüfer nur über die im PR geänderten Dateien und mit --warnung:
Die Sperre selbst ist Sache des Deploy-Wegs je Repo (servicewelt deploy-stufe.yml), eine
gemergte Contract-Migration darf einen Merge-Riegel nicht dauerhaft rot färben. Im Deploy nutzt
freigabe_einstufen.py denselben Scanner mit breiteren Regeln für die Freigabe-Schranke.

Aufruf:  python3 migration_destruktiv.py [--warnung] DATEI [DATEI ...]
Exit:    0 keine Treffer (oder --warnung) · 1 Treffer · 2 Fehlbedienung
"""
import argparse
import re
import sys
from pathlib import Path

DROP_RE = re.compile(r"\bdrop\s+table\b", re.IGNORECASE)
TRUNCATE_RE = re.compile(r"\btruncate\b", re.IGNORECASE)
RECHTE_DAVOR_RE = re.compile(r"(\bgrant|\brevoke|,)\s*$", re.IGNORECASE)
DOLLAR_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
# ein Literal, das selbst eine Anweisung trägt: Befehl am Anfang (nach Kommentaren), danach Leerraum und ein Objekt
# oder ein offenes Ende für die Verkettung ('truncate ' || t); ein bloßes 'TRUNCATE' ist ein Wort, keine Anweisung
LITERAL_ANWEISUNG_RE = re.compile(r"^\s*(drop\s+(table|schema|role|user|group)|truncate|delete\s+from|alter\s+(table|role|user|group)|create\s+(role|user|group))(\s+\S|\s+$)", re.IGNORECASE)
FUEHRENDE_KOMMENTARE_RE = re.compile(r"^(\s*(/\*.*?\*/|--[^\n]*(\n|$)))*", re.DOTALL)
# nur hier wird ein Literal ausgeführt: DO '…' als ganzer Rumpf; EXECUTE '…', format('…') und || als dynamisches SQL
# der Sprachname kann zitiert sein und ist dann schon ausgeblendet ("       "); E für E'…'-Rümpfe
DO_DAVOR_RE = re.compile(r"\bdo(\s+language\s+(\"[^\"]*\"|\w+))?\s*E?$", re.IGNORECASE)
DYNAMISCH_DAVOR_RE = re.compile(r"(\bexecute|\bformat\s*\(|\|\|)\s*$", re.IGNORECASE)
# ein Dollar-Block ist ein Rumpf hinter DO, AS, EXECUTE, format(, || oder am Anfang; hinter IS oder RAISE ist er Text
DOLLAR_RUMPF_DAVOR_RE = re.compile(r"(^|\bdo(\s+language\s+(\"[^\"]*\"|\w+))?|\bas|\bexecute|\bformat\s*\(|\|\|)\s*$", re.IGNORECASE)


def _literal_ende(sql, i, c, escapes):
    """Index des schließenden Anführungszeichens eines Literals, das bei i beginnt ('' ist Escape, bei E'…' auch \\)."""
    n = len(sql)
    j = i + 1
    while j < n:
        if escapes and sql[j] == "\\":
            j += 2
            continue
        if sql[j] == c:
            if j + 1 < n and sql[j + 1] == c:
                j += 2
                continue
            return j
        j += 1
    return n - 1


def _blockkommentar_ende(sql, i):
    """Index hinter dem passenden */ eines bei i beginnenden Blockkommentars; PostgreSQL verschachtelt Kommentare."""
    n = len(sql)
    tiefe = 1
    j = i + 2
    while j < n and tiefe > 0:
        if sql.startswith("/*", j):
            tiefe += 1
            j += 2
        elif sql.startswith("*/", j):
            tiefe -= 1
            j += 2
        else:
            j += 1
    return j


def _literal_laeuft(davor, inhalt):
    """True, wenn ein einfaches Literal ausgeführt wird: ganzer DO-Rumpf oder dynamisches SQL mit Befehl am Anfang."""
    if DO_DAVOR_RE.search(davor):
        return True
    if DYNAMISCH_DAVOR_RE.search(davor):
        ohne_kopf = FUEHRENDE_KOMMENTARE_RE.sub("", inhalt, count=1)
        return bool(LITERAL_ANWEISUNG_RE.match(ohne_kopf))
    return False


def _scanner(sql, literale_ausblenden):
    """Kommentare durch Leerraum ersetzen; einfache Literale je nach Schalter ausblenden (außer sie laufen) oder
    unverändert lassen; zitierte Bezeichner bleiben; Dollar-Blöcke sind Rümpfe oder Texte je nach Kontext und
    werden als Rumpf innen genauso behandelt. Längen bleiben erhalten."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if sql.startswith("/*", i):
            j = _blockkommentar_ende(sql, i)
            out.append(" " * (j - i))
            i = j
            continue
        if c == '"':
            # zitierter Bezeichner: kein String, aber auch keine Anweisung; der Inhalt wird ausgeblendet, die Anführungszeichen
            # bleiben, damit Muster wie alter table "x" drop column weiter einen Namen sehen
            j = _literal_ende(sql, i, c, False)
            inhalt = sql[i + 1:j]
            out.append(sql[i:j + 1] if not literale_ausblenden else c + " " * len(inhalt) + c)
            i = j + 1
            continue
        if c == "'":
            escapes = i > 0 and sql[i - 1] in "Ee" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = _literal_ende(sql, i, c, escapes)
            inhalt = sql[i + 1:j]
            if not literale_ausblenden or _literal_laeuft("".join(out), inhalt):
                out.append(sql[i:j + 1])
            else:
                out.append(c + " " * len(inhalt) + c)
            i = j + 1
            continue
        m = DOLLAR_RE.match(sql, i)
        if m:
            tag = m.group(0)
            j = sql.find(tag, m.end())
            ende = n if j < 0 else j
            rumpf = sql[m.end():ende]
            if not literale_ausblenden or DOLLAR_RUMPF_DAVOR_RE.search("".join(out)):
                out.append(tag + _scanner(rumpf, literale_ausblenden) + (tag if j >= 0 else ""))
            else:
                out.append(tag + " " * len(rumpf) + (tag if j >= 0 else ""))
            i = n if j < 0 else j + len(tag)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def ohne_kommentare(sql):
    """Nur Kommentare entfernt, Literale unverändert."""
    return _scanner(sql, literale_ausblenden=False)


def bereinigt(sql):
    """Kommentare entfernt, Texte ausgeblendet, laufende Literale und Rümpfe erhalten."""
    return _scanner(sql, literale_ausblenden=True)


def ist_destruktiv(sql):
    """DROP TABLE als Anweisung oder in dynamischem SQL; TRUNCATE als Anweisung: nicht 'truncated' (Wortgrenze), nicht
    in Rechtelisten (grant select, truncate on …), nicht revoke truncate on, nicht als Wort in einem Text."""
    text = bereinigt(sql)
    if DROP_RE.search(text):
        return True
    for m in TRUNCATE_RE.finditer(text):
        if RECHTE_DAVOR_RE.search(text[:m.start()]):
            continue
        if re.match(r"\s+on\b", text[m.end():], re.IGNORECASE):
            continue
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dateien", nargs="*")
    ap.add_argument("--warnung", action="store_true", help="Treffer als ::warning:: melden und mit 0 enden")
    args = ap.parse_args(argv)
    if not args.dateien:
        print("::notice::0 Migrationsdateien geprüft (keine geänderte .sql im PR)")
        print("SUMMARY dateien=0 destruktiv=0")
        return 0
    treffer = []
    for d in args.dateien:
        p = Path(d)
        if not p.is_file():
            # eine übergebene, aber nicht lesbare Datei still zu überspringen, machte die Prüfung scheinbar grün (GA-C1-21)
            print(f"::error::{d}: Datei nicht lesbar oder nicht vorhanden; Prüfung nicht möglich")
            print(f"SUMMARY dateien={len(args.dateien)} destruktiv=-1")
            return 2
        if ist_destruktiv(p.read_text(encoding="utf-8", errors="replace")):
            treffer.append(d)
    art = "warning" if args.warnung else "error"
    for d in treffer:
        print(f"::{art} file={d}::enthält DROP TABLE oder TRUNCATE als Anweisung; destruktive Migrationen gehen nicht automatisch live (expand/contract, Sicherung vor dem Löschen)")
    print(f"SUMMARY dateien={len(args.dateien)} destruktiv={len(treffer)}")
    return 0 if (args.warnung or not treffer) else 1


if __name__ == "__main__":
    sys.exit(main())
