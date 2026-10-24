#!/usr/bin/env python3
"""Zugangsdaten: keine versionierte .env-Datei im Repo.

Übernommen aus wbhv-os pr-checks.yml (Job keine-zugangsdaten). Erlaubt sind Vorlagen, deren
Name auf example, beispiel, sample, template, vorlage oder dist endet; gemessene Fälle:
.env.example und .env.playwright.example (servicewelt), ops/telefonie-testanlage/.env.beispiel
(servicewelt), .env.prod.example (dev.app). Die Suche nach Token-Mustern übernimmt gitleaks.

Aufruf:  python3 zugangsdaten.py [REPO-WURZEL] [--liste DATEI]
         --liste liest die versionierten Pfade aus einer Datei statt aus `git ls-files` (Tests).
Exit:    0 sauber · 1 versionierte .env gefunden · 2 Fehlbedienung
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ENV_RE = re.compile(r"^\.env(\..+)?$")
ERLAUBTE_ENDUNGEN = (".example", ".beispiel", ".sample", ".template", ".vorlage", ".dist")


def ist_verdaechtig(pfad):
    name = pfad.rsplit("/", 1)[-1]
    if not ENV_RE.match(name):
        return False
    return not name.lower().endswith(ERLAUBTE_ENDUNGEN)


def versionierte_pfade(wurzel):
    out = subprocess.run(["git", "-C", str(wurzel), "ls-files", "-z"], check=True, capture_output=True)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wurzel", nargs="?", default=".")
    ap.add_argument("--liste", help="Datei mit einem versionierten Pfad je Zeile (statt git ls-files)")
    args = ap.parse_args(argv)

    if args.liste:
        pfade = [z.strip() for z in Path(args.liste).read_text(encoding="utf-8").splitlines() if z.strip()]
    else:
        pfade = versionierte_pfade(Path(args.wurzel))

    treffer = [p for p in pfade if ist_verdaechtig(p)]
    for p in treffer:
        print(f"::error file={p}::versionierte .env-Datei; Zugangsdaten gehören in Secrets, Vorlagen heißen .env.example")
    print(f"SUMMARY pfade={len(pfade)} env_treffer={len(treffer)}")
    return 1 if treffer else 0


if __name__ == "__main__":
    sys.exit(main())
