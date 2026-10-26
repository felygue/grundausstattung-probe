#!/usr/bin/env python3
"""Workflow-Härtung: prüft alle .github/workflows/*.yml eines Repos.

Fünf Regeln, die aus den realen Vorfällen der letzten Jahre stammen (Nx s1ngularity 2025,
Grafana 2025, Ultralytics 2024, tj-actions 2025, GhostAction 2026) und aus eigenen Fällen
(servicewelt #2315: wörtliche Zeilentrenner machten eine Workflow-Datei für GitHub ungültig):

  (1) permissions: steht auf Workflow- oder auf jeder Job-Ebene          (Deckel: permissions_fehlt)
  (2) kein pull_request_target, außer in der begründeten Ausnahmeliste   (Fehler)
  (3) jedes uses: ist auf einen 40-stelligen Commit-SHA gepinnt,          (Deckel: ungepinnt)
      ausgenommen lokale Verweise ./… und docker://…
  (4) kein U+0085, U+2028, U+2029 wörtlich in der Datei                   (Fehler)
  (5) kein secrets.* im env: auf Workflow- oder Job-Ebene                 (Deckel: env_secrets)

Deckel und Ausnahmen stehen als Daten in .github/grundausstattung/konfiguration.json. Ein
Deckel ist das gemessene Ist beim Einrichten (Ratsche: darf nur sinken, nie steigen). Neue
Repos haben 0. YAML wird ausschließlich mit PyYAML gelesen, nie mit eigenen Mustern.

Aufruf:  python3 workflows_haertung.py [REPO-WURZEL] [--konfiguration PFAD] [--json]
Exit:    0 sauber oder innerhalb der Deckel · 1 Befund · 2 Fehlbedienung oder PyYAML fehlt
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("::error::PyYAML fehlt. Im Workflow legt grundpruefung.yml ein venv an; lokal: python3 -m venv .venv && .venv/bin/pip install --require-hashes -r vorlagen/.github/grundausstattung/anforderungen.txt")
    sys.exit(2)

ZEILENTRENNER = {"\u0085": "U+0085", "\u2028": "U+2028", "\u2029": "U+2029"}
SHA_RE = re.compile(r"@[0-9a-f]{40}$")
SECRETS_RE = re.compile(r"\bsecrets\b")  # secrets.X, secrets['X'] und toJSON(secrets) im Rumpf eines Ausdrucks


def ausdruecke(text):
    """Die Rümpfe aller ${{ … }}-Ausdrücke. Das Ende ist das erste }} außerhalb eines Stringliterals ('…', '' als
    Escape): format('{{{0}}}', secrets.TOKEN) trägt ein }} im Literal, eine Regex bis zum ersten }} übersah das
    Secret dahinter (GA-C1-15, Runde 2)."""
    rumpf = []
    i, n = 0, len(text)
    while True:
        a = text.find("${{", i)
        if a < 0:
            return rumpf
        j = a + 3
        start = j
        while j < n and not text.startswith("}}", j):
            if text[j] == "'":
                j += 1
                while j < n:
                    if text[j] == "'":
                        if j + 1 < n and text[j + 1] == "'":
                            j += 2
                            continue
                        break
                    j += 1
            j += 1
        rumpf.append(text[start:j])
        i = j + 2
PLATZHALTER = "BEGRÜNDUNG EINTRAGEN"
DECKEL_NAMEN = ("permissions_fehlt", "permissions_schreibend", "ungepinnt", "env_secrets")


def lade_konfiguration(pfad):
    if pfad is None or not pfad.exists():
        return {}
    with pfad.open(encoding="utf-8") as f:
        return json.load(f)


def deckel_wert(konf, name):
    """Deckel aus konfiguration.json; ein unlesbarer Wert gilt sichtbar als 0, der strengsten Stufe."""
    eintrag = (konf.get("deckel") or {}).get(name)
    roh = eintrag.get("wert", 0) if isinstance(eintrag, dict) else (eintrag or 0)
    try:
        return int(roh)
    except (TypeError, ValueError):
        print(f"::warning::deckel.{name} in konfiguration.json ist kein Zahlwert ({roh!r}); es gilt 0")
        return 0


def trigger_von(doc):
    """Die Trigger eines Workflows als Liste von Namen. PyYAML liest den Schlüssel `on` nach
    YAML 1.1 als Wahrheitswert True, deshalb beide Schreibweisen nachsehen."""
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


def env_mit_secrets(env):
    """Schlüssel eines env-Blocks, deren Wert im Ausdruck auf den Kontext secrets zugreift."""
    if not isinstance(env, dict):
        return []
    return [k for k, v in env.items() if isinstance(v, str) and any(SECRETS_RE.search(r) for r in ausdruecke(v))]


def permissions_schreibend(perm):
    """True für `write-all` oder ein Mapping mit einem write-Wert auf Workflow-Ebene."""
    if perm == "write-all":
        return True
    return isinstance(perm, dict) and any(v == "write" for v in perm.values())


def pruefe_datei(pfad, konf):
    """Liefert den Befund einer Workflow-Datei als Wörterbuch."""
    text = pfad.read_text(encoding="utf-8")
    befund = {"datei": str(pfad), "fehler": [], "permissions_fehlt": False, "permissions_schreibend": False,
              "ungepinnt": [], "env_secrets": [], "ausnahme_unbegruendet": []}

    # (4) Zeilentrenner: zeilenweise zählen, damit die Zeile im Befund steht
    for nr, zeile in enumerate(text.split("\n"), start=1):
        for zeichen, name in ZEILENTRENNER.items():
            if zeichen in zeile:
                befund["fehler"].append((nr, f"{name} steht wörtlich in der Datei; GitHub liest YAML 1.1 und verwirft den ganzen Workflow (servicewelt #2315)"))

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:  # pragma: no cover
        befund["fehler"].append((1, f"YAML nicht lesbar: {str(e).splitlines()[0]}"))
        return befund
    if not isinstance(doc, dict):
        befund["fehler"].append((1, "Workflow-Datei ist kein YAML-Mapping"))
        return befund

    # (2) pull_request_target: nur mit begründeter Ausnahme; ein leerer oder Platzhalter-Grund ist keine Ausnahme
    erlaubt = (konf.get("erlaubt") or {}).get("pull_request_target") or {}
    if "pull_request_target" in trigger_von(doc):
        grund = erlaubt.get(pfad.name) if isinstance(erlaubt, dict) else None
        if grund is None:
            befund["fehler"].append((1, "pull_request_target ohne Eintrag in erlaubt.pull_request_target der konfiguration.json; der Trigger gibt Fremdcode Zugriff auf Secrets (Nx 2025, Grafana 2025)"))
        elif not isinstance(grund, str) or not grund.strip() or grund.strip().startswith(PLATZHALTER):
            befund["ausnahme_unbegruendet"].append(pfad.name)

    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        jobs = {}

    # (1) permissions auf Workflow-Ebene oder auf jeder Job-Ebene; write-all oder write auf Workflow-Ebene ist eigener Befund
    if "permissions" not in doc:
        ohne = [jid for jid, job in jobs.items() if not (isinstance(job, dict) and "permissions" in job)]
        if ohne or not jobs:
            befund["permissions_fehlt"] = True
    elif permissions_schreibend(doc.get("permissions")):
        befund["permissions_schreibend"] = True

    # (5) env mit secrets auf Workflow-Ebene
    for k in env_mit_secrets(doc.get("env")):
        befund["env_secrets"].append(f"workflow env {k}")

    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for k in env_mit_secrets(job.get("env")):
            befund["env_secrets"].append(f"job {jid} env {k}")
        # (3) uses: am Job (wiederverwendbarer Workflow) und an jedem Schritt
        verweise = []
        if isinstance(job.get("uses"), str):
            verweise.append(job["uses"])
        for schritt in job.get("steps") or []:
            if isinstance(schritt, dict) and isinstance(schritt.get("uses"), str):
                verweise.append(schritt["uses"])
        for u in verweise:
            if u.startswith("./") or u.startswith("docker://"):
                continue
            if not SHA_RE.search(u):
                befund["ungepinnt"].append(f"job {jid}: {u}")
    return befund


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wurzel", nargs="?", default=".", help="Repo-Wurzel (Standard: .)")
    ap.add_argument("--konfiguration", help="Pfad zur konfiguration.json (Standard: <wurzel>/.github/grundausstattung/konfiguration.json)")
    ap.add_argument("--json", action="store_true", help="Zusammenfassung als JSON ausgeben (für den Installer, der die Deckel misst)")
    args = ap.parse_args(argv)

    wurzel = Path(args.wurzel)
    konf_pfad = Path(args.konfiguration) if args.konfiguration else wurzel / ".github" / "grundausstattung" / "konfiguration.json"
    konf = lade_konfiguration(konf_pfad)
    ordner = wurzel / ".github" / "workflows"
    dateien = sorted(list(ordner.glob("*.yml")) + list(ordner.glob("*.yaml"))) if ordner.is_dir() else []

    befunde = [pruefe_datei(p, konf) for p in dateien]
    zaehler = {
        "workflows": len(dateien),
        "fehler": sum(len(b["fehler"]) for b in befunde),
        "permissions_fehlt": sum(1 for b in befunde if b["permissions_fehlt"]),
        "permissions_schreibend": sum(1 for b in befunde if b["permissions_schreibend"]),
        "ungepinnt": sum(len(b["ungepinnt"]) for b in befunde),
        "env_secrets": sum(len(b["env_secrets"]) for b in befunde),
        "ausnahme_unbegruendet": sum(len(b["ausnahme_unbegruendet"]) for b in befunde),
    }
    deckel = {n: deckel_wert(konf, n) for n in DECKEL_NAMEN}
    ueberschritten = [n for n in DECKEL_NAMEN if zaehler[n] > deckel[n]]

    if args.json:
        print(json.dumps({"zaehler": zaehler, "deckel": deckel, "ueberschritten": ueberschritten,
                          "dateien": [{"datei": b["datei"], "fehler": [f"{z}: {m}" for z, m in b["fehler"]],
                                       "permissions_fehlt": b["permissions_fehlt"], "permissions_schreibend": b["permissions_schreibend"],
                                       "ungepinnt": b["ungepinnt"], "env_secrets": b["env_secrets"],
                                       "ausnahme_unbegruendet": b["ausnahme_unbegruendet"]} for b in befunde]}, ensure_ascii=False, indent=2))
    else:
        for b in befunde:
            rel = b["datei"]
            for zeile, meldung in b["fehler"]:
                print(f"::error file={rel},line={zeile}::{meldung}")
            if b["permissions_fehlt"]:
                print(f"::warning file={rel}::kein permissions: auf Workflow-Ebene oder an jedem Job (GITHUB_TOKEN bekommt sonst die Standardrechte)")
            if b["permissions_schreibend"]:
                print(f"::warning file={rel}::permissions auf Workflow-Ebene mit write oder write-all; Schreibrechte gehören an den einzelnen Job")
            for a in b["ausnahme_unbegruendet"]:
                print(f"::warning file={rel}::pull_request_target-Ausnahme ohne Begründung in konfiguration.json ({a}); bis ein Mensch den Grund einträgt, meldet pruefen.sh das als Abweichung")
            for u in b["ungepinnt"]:
                print(f"::warning file={rel}::uses nicht auf Commit-SHA gepinnt: {u} (tj-actions 2025: verschobene Tags)")
            for e in b["env_secrets"]:
                print(f"::warning file={rel}::secrets im env auf Workflow- oder Job-Ebene erreichen jeden Schritt, auch Tests mit PR-Code: {e}")
        for n in DECKEL_NAMEN:
            stand = "ÜBERSCHRITTEN" if n in ueberschritten else "innerhalb"
            print(f"::notice::{n}: {zaehler[n]} gemessen, Deckel {deckel[n]} ({stand})")
        if not dateien:
            print(f"::notice::0 Workflows geprüft: {ordner} fehlt oder ist leer")
        print(f"SUMMARY workflows={zaehler['workflows']} fehler={zaehler['fehler']} permissions_fehlt={zaehler['permissions_fehlt']}/{deckel['permissions_fehlt']} permissions_schreibend={zaehler['permissions_schreibend']}/{deckel['permissions_schreibend']} ungepinnt={zaehler['ungepinnt']}/{deckel['ungepinnt']} env_secrets={zaehler['env_secrets']}/{deckel['env_secrets']} ausnahme_unbegruendet={zaehler['ausnahme_unbegruendet']} ueberschritten={len(ueberschritten)}")

    return 1 if zaehler["fehler"] or ueberschritten else 0


if __name__ == "__main__":
    sys.exit(main())
