#!/usr/bin/env python3
"""Destruktive Migrationen: DROP TABLE oder TRUNCATE als Anweisung.

Übernommen aus servicewelt scripts/check-migration-destruktiv.mjs (Fassung vom 25.09.2026):
Kommentare werden vorher entfernt (#265: TRUNCATE in einer Erklärung blockierte den Deploy),
`drop table` trifft breit (auch in Strings, etwa execute format('drop table %I')), `truncate`
nur als Anweisung (Zeilenanfang, nach ; oder am Anfang eines Strings, ganzes Wort), also nicht
in 'truncated' (#1767: jsonb_build_object(…,'truncated',…)), nicht in Rechtelisten und nicht in
`revoke truncate on`. hvtools pr-checks.yml trifft `truncated` noch als Teilwort.

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

KOMMENTARE_RE = re.compile(r"/\*[\s\S]*?\*/|--[^\n]*")
DESTRUKTIV_RE = re.compile(r"drop\s+table|(^|;|'|\")\s*truncate\b", re.IGNORECASE | re.MULTILINE)


def ohne_kommentare(sql):
    return KOMMENTARE_RE.sub("", sql)


def ist_destruktiv(sql):
    return bool(DESTRUKTIV_RE.search(ohne_kommentare(sql)))


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
            continue
        if ist_destruktiv(p.read_text(encoding="utf-8", errors="replace")):
            treffer.append(d)
    art = "warning" if args.warnung else "error"
    for d in treffer:
        print(f"::{art} file={d}::enthält DROP TABLE oder TRUNCATE als Anweisung; destruktive Migrationen gehen nicht automatisch live (expand/contract, Sicherung vor dem Löschen)")
    print(f"SUMMARY dateien={len(args.dateien)} destruktiv={len(treffer)}")
    return 0 if (args.warnung or not treffer) else 1


if __name__ == "__main__":
    sys.exit(main())
