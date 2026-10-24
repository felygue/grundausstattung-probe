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
import fnmatch
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("::error::PyYAML fehlt (siehe workflows_haertung.py)")
    sys.exit(2)

# Dieselbe Trigger-Lesart wie der Härtungsprüfer (liegt immer daneben, beide werden zusammen kopiert)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflows_haertung import trigger_von  # noqa: E402


def pr_filter(doc):
    """Filter des pull_request-Triggers: paths/paths-ignore machen den Lauf bedingt, branches schränken ihn ein."""
    on = doc.get("on", doc.get(True))
    pr = on.get("pull_request") if isinstance(on, dict) else None
    if not isinstance(pr, dict):
        return {"paths": False, "branches": None}
    return {"paths": bool(pr.get("paths") or pr.get("paths-ignore")),
            "branches": pr.get("branches") if isinstance(pr.get("branches"), list) else None,
            "branches_ignore": pr.get("branches-ignore") if isinstance(pr.get("branches-ignore"), list) else None}


def laeuft_auf_default(filter_, default_branch):
    """True, wenn der Workflow bei PRs auf den Default-Branch sicher startet: keine Pfadfilter, Branchfilter passt."""
    if filter_["paths"]:
        return False
    if default_branch is None:
        return True
    muster = filter_.get("branches")
    if muster is not None and not any(fnmatch.fnmatchcase(default_branch, str(m)) for m in muster):
        return False
    ignore = filter_.get("branches_ignore")
    if ignore and any(fnmatch.fnmatchcase(default_branch, str(m)) for m in ignore):
        return False
    return True


def kontexte_einer_datei(pfad):
    try:
        doc = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {"datei": str(pfad), "trigger": [], "jobs": [], "fehler": "YAML nicht lesbar"}
    if not isinstance(doc, dict):
        return {"datei": str(pfad), "trigger": [], "jobs": [], "fehler": "kein Mapping"}
    roh = doc.get("jobs") or {}
    if not isinstance(roh, dict):
        return {"datei": str(pfad), "trigger": trigger_von(doc), "jobs": [], "fehler": "jobs ist kein Mapping"}
    jobs = []
    for jid, job in roh.items():
        job = job if isinstance(job, dict) else {}
        name = job.get("name")
        # Ein Job mit uses: (wiederverwendbarer Workflow) meldet „<job> / <innerer job>“, ein Matrix-Job
        # „<job> (<wert>)“: beides ist kein fester Kontext (gemessen: servicewelt „Produktion (FAC01) / deploy“, dev.app „api-e2e (1)“).
        veraenderlich = "uses" in job or (isinstance(job.get("strategy"), dict) and "matrix" in job["strategy"])
        if veraenderlich or (isinstance(name, str) and "${{" in name) or (name is not None and not isinstance(name, str)):
            kontext = None
        elif isinstance(name, str):
            kontext = name
        else:
            kontext = str(jid)
        jobs.append({"id": str(jid), "name": name if isinstance(name, str) else None, "kontext": kontext})
    return {"datei": str(pfad), "trigger": trigger_von(doc), "jobs": jobs, "pr_filter": pr_filter(doc)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wurzel", nargs="?", default=".")
    ap.add_argument("--dateien", nargs="*", help="einzelne Workflow-Dateien statt <wurzel>/.github/workflows")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kontext", help="Kontextname, dessen Vorhandensein in einem Workflow mit pull_request-Trigger geprüft wird")
    ap.add_argument("--merge-group", action="store_true", help="zusätzlich verlangen, dass der liefernde Workflow merge_group trägt")
    ap.add_argument("--default-branch", help="mit --kontext: nur Workflows zählen, die bei PRs auf diesen Branch ohne Pfadfilter starten")
    args = ap.parse_args(argv)

    if args.dateien:
        pfade = [Path(d) for d in args.dateien]
    else:
        ordner = Path(args.wurzel) / ".github" / "workflows"
        pfade = sorted(list(ordner.glob("*.yml")) + list(ordner.glob("*.yaml"))) if ordner.is_dir() else []
    ergebnis = [kontexte_einer_datei(p) for p in pfade]

    unlesbar = [e for e in ergebnis if e.get("fehler")]
    if args.kontext:
        if unlesbar:
            # eine unlesbare Datei darf nicht als „Kontext fehlt“ durchgehen; die Ursache muss sichtbar sein
            print(f"KONTEXT {args.kontext}: nicht entscheidbar, unlesbare Workflow-Dateien: " + ", ".join(f"{e['datei']} ({e['fehler']})" for e in unlesbar))
            return 2
        kandidaten = [e for e in ergebnis if "pull_request" in e["trigger"] and any(j["kontext"] == args.kontext for j in e["jobs"])]
        treffer = [e for e in kandidaten if laeuft_auf_default(e.get("pr_filter") or {"paths": False, "branches": None}, args.default_branch)]
        if kandidaten and not treffer:
            # ein Pflicht-Kontext aus einem Workflow mit paths- oder branches-Filter bleibt bei anderen PRs für immer ausstehend
            print(f"KONTEXT {args.kontext}: vorhanden in {', '.join(e['datei'] for e in kandidaten)}, meldet aber nicht verlässlich (paths-Filter oder branches-Filter ohne den Default-Branch)")
            return 1
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
            fehler = f" FEHLER={e['fehler']}" if e.get("fehler") else ""
            print(f"{e['datei']}: trigger={','.join(e['trigger']) or '-'} kontexte={', '.join(j['kontext'] or '<veraenderlich>' for j in e['jobs']) or '-'}{fehler}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
