#!/usr/bin/env python3
"""Destruktive Migrationen: DROP TABLE oder TRUNCATE als Anweisung.

Übernommen aus servicewelt scripts/check-migration-destruktiv.mjs (Fassung vom 25.09.2026):
Kommentare werden vorher entfernt (#265: TRUNCATE in einer Erklärung blockierte den Deploy),
`drop table` trifft breit (auch in Strings, etwa execute format('drop table %I')), `truncate`
als ganzes Wort außerhalb von Rechtelisten, also nicht in 'truncated' (#1767:
jsonb_build_object(…,'truncated',…)), nicht in `grant select, truncate on` und nicht in
`revoke truncate on`. Seit 26.10.2026 mit Zeichen-Scanner statt Regex für Kommentare und Strings.

In der Grundprüfung läuft dieser Prüfer nur über die im PR geänderten Dateien und mit --warnung:
Die Sperre selbst ist Sache des Deploy-Wegs je Repo (servicewelt deploy-stufe.yml), eine
gemergte Contract-Migration darf einen Merge-Riegel nicht dauerhaft rot färben.

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


def ohne_kommentare(sql):
    """Kommentare (-- bis Zeilenende, /* … */) durch ein Leerzeichen ersetzen; Stringliterale ('…' mit '' als
    Escape, "…", $tag$…$tag$) bleiben unverändert, ein -- oder /* darin ist kein Kommentar. Ein Zeichen-Scanner
    statt einer Regex, weil die Regex-Fassung drei echte Fälle übersah (GA-C1-20): DROP/*c*/TABLE klebte zu
    DROPTABLE, SELECT '--'; DROP TABLE wurde ab dem -- im String weggekürzt."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(" ")
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" ")
            i = j
            continue
        if c in ("'", '"'):
            # E'…' erlaubt Backslash-Escapes (E'it\'s'); sonst ist nur '' ein Escape (GA-C1-20, Runde 2)
            escapes = c == "'" and i > 0 and sql[i - 1] in "Ee" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = i + 1
            while j < n:
                if escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        m = DOLLAR_RE.match(sql, i)
        if m:
            # ein Dollar-Block ist meist ausführbarer PL/pgSQL-Rumpf mit eigenen Kommentaren: innen dieselbe Bereinigung
            tag = m.group(0)
            j = sql.find(tag, m.end())
            if j < 0:
                out.append(sql[i:])
                i = n
            else:
                out.append(tag + ohne_kommentare(sql[m.end():j]) + tag)
                i = j + len(tag)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def ist_destruktiv(sql):
    """DROP TABLE überall (auch in Strings wie execute format('drop table %I')); TRUNCATE als Anweisung: nicht
    'truncated' (Wortgrenze), nicht in Rechtelisten (grant select, truncate on …) und nicht revoke truncate on."""
    text = ohne_kommentare(sql)
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
