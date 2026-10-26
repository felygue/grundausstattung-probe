#!/usr/bin/env python3
"""Migrations-Zeitstempel: kein numerischer Präfix darf doppelt vergeben sein.

Zusammengeführt aus drei Fassungen: servicewelt scripts/check-migration-timestamps.mjs (Supabase,
Dateien, Issue #266: der Präfix ist dort Primärschlüssel, zwei gleiche brechen `supabase db reset`),
Verwalterwerkstatt_OS scripts/check-migration-timestamps.mjs (Prisma, Ordner, bekannte Altfälle als
Datenliste, weil Umbenennen angewendeter Migrationen nicht additiv ist) und hvtools pr-checks.yml
(Shell, nur innerhalb des PR). Das Präfixmuster ist ein Parameter: 14 Stellen sind Standard,
wbhv-os nummeriert vierstellig (migrations/0001_grundschema.sql).

Gezählt werden Unterordner und *.sql-Dateien des Verzeichnisses. Fehlt ein Verzeichnis, wird das
sichtbar gemeldet und ist kein Fehler: ein Prüfer über einer leeren Grundmenge darf nicht still
grün sein (FEGAS pruefskript-leere-grundmenge).

Aufruf:  python3 migration_zeitstempel.py [VERZEICHNIS] [--praefix REGEX] [--bekannt P1,P2]
                                          [--konfiguration PFAD] [--liste DATEI] [--wurzel PFAD]
         Ohne VERZEICHNIS: konfiguration.json (migrationen.verzeichnis) oder die bekannten Orte.
Exit:    0 eindeutig oder nur bekannte Altfälle · 1 neue Dopplung oder Name ohne Präfix · 2 Fehlbedienung
"""
import argparse
import json
import re
import sys
from pathlib import Path

BEKANNTE_ORTE = ("supabase/migrations", "apps/api/prisma/migrations", "prisma/migrations", "migrations", "database/migrations")


def eintraege_aus_verzeichnis(verz):
    """Unterordner und *.sql-Dateien; Einträge mit führendem Punkt (.claude-backups in facilioo-toolie) sind keine Migrationen."""
    namen = []
    for p in sorted(verz.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir() or p.suffix == ".sql":
            namen.append(p.name)
    return namen


def pruefe(namen, praefix_re, bekannt, ohne_praefix_bekannt=()):
    """Liefert (fehlende_praefixe, neue_duplikate, bekannte_duplikate); neue und bekannte als {praefix: [namen]}.
    Namen in ohne_praefix_bekannt sind Altfälle (hvtools 20260523T143000_…), sie zählen nicht als Fehler.

    bekannt enthält die vollen Namen der Altfall-Dateien (20260902160000_a.sql, 20260902160000_b.sql): eine
    dritte Datei unter demselben Präfix ist dann eine neue Dopplung. Ein bloßer Präfix (Altformat) zählt nicht als
    Altfall, denn er erlaubte die Dopplung unbegrenzt (GA-C1-12, Runde 2); main meldet ihn als Fehler."""
    muster = re.compile(rf"^({praefix_re})[_-]")
    bekannt = set(bekannt)
    bekannt_namen = {b for b in bekannt if muster.match(b)}
    nach_praefix = {}
    ohne = []
    for name in namen:
        m = muster.match(name)
        if not m:
            if name not in set(ohne_praefix_bekannt):
                ohne.append(name)
            continue
        nach_praefix.setdefault(m.group(1), []).append(name)
    duplikate = {p: n for p, n in nach_praefix.items() if len(n) > 1}
    neue, alte = {}, {}
    for p, n in duplikate.items():
        if all(x in bekannt_namen for x in n):
            alte[p] = n
        else:
            # eine neue Datei unter einem bekannten Präfix ist eine neue Dopplung, auch wenn die alten bekannt sind
            neue[p] = [x for x in n if x not in bekannt_namen] if any(x in bekannt_namen for x in n) else n
    return ohne, neue, alte


def bekannte_praefixe_ohne_namen(bekannt, praefix_re):
    muster = re.compile(rf"^({praefix_re})[_-]")
    return sorted(b for b in set(bekannt) if not muster.match(b))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("verzeichnis", nargs="?")
    ap.add_argument("--wurzel", default=".", help="Repo-Wurzel für die bekannten Orte und die konfiguration.json")
    ap.add_argument("--praefix", help=r"Regex des numerischen Präfix (Standard \d{14})")
    ap.add_argument("--bekannt", help="kommagetrennte Präfixe, die als Altfall doppelt sein dürfen")
    ap.add_argument("--konfiguration", help="Pfad zur konfiguration.json")
    ap.add_argument("--liste", help="Datei mit einem Eintragsnamen je Zeile statt eines Verzeichnisses (Tests)")
    args = ap.parse_args(argv)

    wurzel = Path(args.wurzel)
    konf_pfad = Path(args.konfiguration) if args.konfiguration else wurzel / ".github" / "grundausstattung" / "konfiguration.json"
    konf = {}
    if konf_pfad.exists():
        konf = json.loads(konf_pfad.read_text(encoding="utf-8")).get("migrationen", {}) or {}

    praefix_re = args.praefix or konf.get("praefix") or r"\d{14}"
    bekannt = set()
    if args.bekannt:
        bekannt |= {x.strip() for x in args.bekannt.split(",") if x.strip()}
    bekannt |= set(konf.get("bekannt", []) or [])
    ohne_praefix_bekannt = set(konf.get("ohne_praefix_bekannt", []) or [])

    if args.liste:
        namen = [z.strip() for z in Path(args.liste).read_text(encoding="utf-8").splitlines() if z.strip()]
        ort = args.liste
    else:
        verz = None
        if args.verzeichnis:
            verz = Path(args.verzeichnis)
        elif konf.get("verzeichnis"):
            verz = wurzel / konf["verzeichnis"]
        else:
            for ort in BEKANNTE_ORTE:
                if (wurzel / ort).is_dir():
                    verz = wurzel / ort
                    break
        if verz is not None and not verz.is_dir():
            # ein ausdrücklich konfiguriertes Verzeichnis, das fehlt, schaltet die Prüfung nicht ab (GA-C1-13)
            print(f"::error::Migrationsverzeichnis {verz} fehlt, ist aber konfiguriert (Argument oder migrationen.verzeichnis in konfiguration.json)")
            print(f"SUMMARY eintraege=0 ohne_praefix=0 doppelt_neu=0 doppelt_bekannt=0 verzeichnis={verz}")
            return 1
        if verz is None:
            print(f"::notice::0 Einträge geprüft: kein Migrationsverzeichnis gefunden (geprüft: {', '.join(BEKANNTE_ORTE)}). Ohne Migrationen ist das richtig; sonst migrationen.verzeichnis in konfiguration.json setzen.")
            print("SUMMARY eintraege=0 ohne_praefix=0 doppelt_neu=0 doppelt_bekannt=0 verzeichnis=fehlt")
            return 0
        namen = eintraege_aus_verzeichnis(verz)
        ort = str(verz)

    ohne, neue, alte = pruefe(namen, praefix_re, bekannt, ohne_praefix_bekannt)
    nur_praefixe = bekannte_praefixe_ohne_namen(bekannt, praefix_re)
    if nur_praefixe:
        print(f"::error::migrationen.bekannt nennt {len(nur_praefixe)} Präfix(e) ohne Dateinamen ({', '.join(nur_praefixe)}); eine weitere Datei unter so einem Präfix fiele nicht auf. Volle Dateinamen eintragen, zum Beispiel: {', '.join(sorted(x for p in nur_praefixe for x in namen if re.match(rf'^{p}[_-]', x))[:6]) or 'siehe Verzeichnis'}")
    if ohne_praefix_bekannt:
        print(f"::notice::{len(ohne_praefix_bekannt)} bekannte Altfälle ohne Präfix werden nicht gezählt (migrationen.ohne_praefix_bekannt)")
    for name in ohne:
        print(f"::error::{name} hat keinen Präfix nach Muster {praefix_re} (Format <praefix>_name)")
    for p, n in sorted(neue.items()):
        print(f"::error::Präfix {p} doppelt vergeben: {', '.join(n)}. Umbenennen (Sekunden hochzählen), Inhalt nie ändern; ist die Datei schon angewendet, ihren vollen Namen als Altfall in migrationen.bekannt eintragen.")
    for p, n in sorted(alte.items()):
        print(f"::notice::bekannter Altfall {p}: {', '.join(n)}")
    print(f"SUMMARY eintraege={len(namen)} ohne_praefix={len(ohne)} doppelt_neu={len(neue)} doppelt_bekannt={len(alte)} verzeichnis={ort}")
    return 1 if ohne or neue or nur_praefixe else 0


if __name__ == "__main__":
    sys.exit(main())
