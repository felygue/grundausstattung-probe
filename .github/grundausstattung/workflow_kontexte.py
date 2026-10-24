#!/usr/bin/env python3
"""Workflow-Kontexte: welche Check-Kontexte melden die Workflows eines Repos, und bei welchen Triggern?

GitHub meldet als Kontext eines Pflicht-Checks den `name:` eines Jobs, falls gesetzt, sonst die
Job-ID (wbhv-os meldet IDs wie `pruefen`, hvtools Anzeigenamen wie „Lint + TypeScript“). Ein Name
mit `${{ … }}` ist zur Laufzeit veränderlich und zählt hier nicht als fester Kontext.

Installer und Prüfer fragen damit, ob ein Pflicht-Kontext (`pruefung`, `grundpruefung`) überhaupt
existiert, in einem Workflow mit `pull_request`-Trigger liegt und, bei einer Merge Queue, ob dieser
Workflow auch `merge_group` trägt. Ein Pflicht-Kontext, den kein Job meldet, blockiert jeden Merge
dauerhaft („Waiting for status to be reported“).

Aufruf:  python3 workflow_kontexte.py [REPO-WURZEL | --dateien DATEI ...] [--json]
         python3 workflow_kontexte.py WURZEL --kontext pruefung [--merge-group]
Exit:    0 · mit --kontext: 0 gefunden (und ggf. merge_group vorhanden), 1 nicht gefunden · 2 Fehlbedienung oder PyYAML fehlt
"""
import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("::error::PyYAML fehlt (siehe workflows_haertung.py)")
    sys.exit(2)


def trigger_von(doc):
    on = doc.get("on", doc.get(True))
    if on is None:
        return []
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(x) for x in on]
    if isinstance(on, dict):
        return [str(k) for k in on.keys()]
    return []


def kontexte_einer_datei(pfad):
    try:
        doc = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {"datei": str(pfad), "trigger": [], "jobs": [], "fehler": "YAML nicht lesbar"}
    if not isinstance(doc, dict):
        return {"datei": str(pfad), "trigger": [], "jobs": [], "fehler": "kein Mapping"}
    jobs = []
    for jid, job in (doc.get("jobs") or {}).items():
        name = job.get("name") if isinstance(job, dict) else None
        if isinstance(name, str) and "${{" not in name:
            kontext = name
        elif name is None:
            kontext = str(jid)
        else:
            kontext = None  # Ausdruck, zur Laufzeit veränderlich
        jobs.append({"id": str(jid), "name": name if isinstance(name, str) else None, "kontext": kontext})
    return {"datei": str(pfad), "trigger": trigger_von(doc), "jobs": jobs}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wurzel", nargs="?", default=".")
    ap.add_argument("--dateien", nargs="*", help="einzelne Workflow-Dateien statt <wurzel>/.github/workflows")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kontext", help="Kontextname, dessen Vorhandensein in einem Workflow mit pull_request-Trigger geprüft wird")
    ap.add_argument("--merge-group", action="store_true", help="zusätzlich verlangen, dass der liefernde Workflow merge_group trägt")
    args = ap.parse_args(argv)

    if args.dateien:
        pfade = [Path(d) for d in args.dateien]
    else:
        ordner = Path(args.wurzel) / ".github" / "workflows"
        pfade = sorted(list(ordner.glob("*.yml")) + list(ordner.glob("*.yaml"))) if ordner.is_dir() else []
    ergebnis = [kontexte_einer_datei(p) for p in pfade]

    if args.kontext:
        treffer = [e for e in ergebnis if "pull_request" in e["trigger"] and any(j["kontext"] == args.kontext for j in e["jobs"])]
        if not treffer:
            print(f"KONTEXT {args.kontext}: fehlt (kein Job mit diesem Kontext in einem Workflow mit pull_request-Trigger)")
            return 1
        ohne_queue = [e["datei"] for e in treffer if "merge_group" not in e["trigger"]]
        if args.merge_group and ohne_queue:
            print(f"KONTEXT {args.kontext}: vorhanden in {', '.join(e['datei'] for e in treffer)}, aber ohne merge_group-Trigger in {', '.join(ohne_queue)}")
            return 1
        print(f"KONTEXT {args.kontext}: vorhanden in {', '.join(e['datei'] for e in treffer)}" + (" (mit merge_group)" if args.merge_group else ""))
        return 0

    if args.json:
        print(json.dumps(ergebnis, ensure_ascii=False, indent=2))
    else:
        for e in ergebnis:
            print(f"{e['datei']}: trigger={','.join(e['trigger']) or '-'} kontexte={', '.join(j['kontext'] or '<ausdruck>' for j in e['jobs']) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
