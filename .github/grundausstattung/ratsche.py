#!/usr/bin/env python3
"""Ratsche: konfiguration.json und .gitleaks.toml dürfen gegenüber dem Basisstand nur strenger werden.

Die Deckel in konfiguration.json sind das beim Einrichten gemessene Ist und dürfen nur sinken; die Allowlist
in .gitleaks.toml nimmt Altfälle aus und darf nicht wachsen. Beides prüfte bisher nur pruefen.sh --vergleich
nach dem Merge; im Pflicht-Check verglich workflows_haertung.py den Bestand mit dem Deckel desselben PR, und
ein PR konnte Deckel und Verstoß zusammen anheben (Codex GA-C1-10, GA-C1-11). Dieser Prüfer vergleicht im PR
gegen den Stand des Zielbranches (git show <basis>:<pfad>, von grundpruefung.yml bereitgestellt).

Fehler (Exit 1): ein Deckel steigt · eine neue pull_request_target-Ausnahme ohne Begründung · neue Pfade, Regexe,
Regel-Allowlists oder .gitleaksignore-Einträge (für einen Fehlalarm: Kommentar gitleaks:allow an der Zeile) ·
Standardregeln nicht eingebunden (extend.useDefault = true fehlt: eine eigene Konfiguration ersetzt sie sonst) ·
disabledRules · eine Allowlist, die alles ausnimmt. Ist das Repo schon eingerichtet (konfiguration.json im
Basisstand), gilt eine fehlende .gitleaks.toml oder .gitleaksignore dort als leer; erst ein Repo ganz ohne
Grundausstattung ist der Erstinstallationsfall, in dem es nichts zu vergleichen gibt (GA-C2-4).
Warnung: eine neue begründete pull_request_target-Ausnahme · neue Altfälle in migrationen.bekannt oder
ohne_praefix_bekannt · geändertes Migrationsverzeichnis oder Präfixmuster.
Fehlt eine Basisdatei (erster PR mit der Grundausstattung), gibt es für sie nichts zu vergleichen.

Aufruf:  python3 ratsche.py --basis DIR [--wurzel DIR]
         DIR enthält konfiguration.json und/oder .gitleaks.toml des Basisstandes; --wurzel ist der PR-Stand (Standard .)
Exit:    0 nur strenger oder gleich · 1 lockerer · 2 Fehlbedienung
"""
import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ALLES_RE = {"", ".*", "^.*$", ".+", "^.+$", ".*$", "^.*", "^"}
PLATZHALTER = "BEGRÜNDUNG EINTRAGEN"


def deckel_wert(konf, name):
    eintrag = (konf.get("deckel") or {}).get(name)
    roh = eintrag.get("wert", 0) if isinstance(eintrag, dict) else (eintrag or 0)
    try:
        return int(roh)
    except (TypeError, ValueError):
        return 0


def lade_json(pfad):
    try:
        d = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{pfad}: nicht lesbar ({e.__class__.__name__})"
    return (d if isinstance(d, dict) else {}), None


def allowlist(pfad):
    """Wirksame Freistellungen einer .gitleaks.toml: Pfade und Regexe aus globaler Allowlist, allowlists-Blöcken und
    Regel-Allowlists, dazu extend.useDefault (None, wenn extend fehlt) und disabledRules. Fehler als Text."""
    try:
        d = tomllib.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{pfad}: nicht lesbar ({e.__class__.__name__}: {e})"
    bloecke = [d["allowlist"]] if isinstance(d.get("allowlist"), dict) else []
    bloecke += [a for a in (d.get("allowlists") or []) if isinstance(a, dict)]
    for r in d.get("rules") or []:
        if isinstance(r, dict):
            if isinstance(r.get("allowlist"), dict):
                bloecke.append(r["allowlist"])
            bloecke += [a for a in (r.get("allowlists") or []) if isinstance(a, dict)]
    pfade = {str(x) for a in bloecke for x in (a.get("paths") or [])}
    regexe = {str(x) for a in bloecke for x in (a.get("regexes") or [])}
    ext = d.get("extend") if isinstance(d.get("extend"), dict) else None
    use_default = ext.get("useDefault") if ext else None
    disabled = [str(x) for x in ((ext or {}).get("disabledRules") or [])]
    return {"pfade": pfade, "regexe": regexe, "use_default": use_default, "disabled": disabled, "regeln": len(d.get("rules") or [])}, None


def ignore_eintraege(pfad):
    """Fingerabdrücke aus einer .gitleaksignore (eine je Zeile, Kommentare und Leerzeilen zählen nicht)."""
    return {z.strip() for z in pfad.read_text(encoding="utf-8").splitlines() if z.strip() and not z.lstrip().startswith("#")}


LEER = {"pfade": set(), "regexe": set(), "use_default": True, "disabled": [], "regeln": 0}


def vergleiche(basis_dir, wurzel):
    fehler, warnungen, hinweise = [], [], []
    konf_pfad = wurzel / ".github" / "grundausstattung" / "konfiguration.json"
    basis_konf_pfad = basis_dir / "konfiguration.json"
    if basis_konf_pfad.exists() and konf_pfad.exists():
        alt, e1 = lade_json(basis_konf_pfad)
        neu, e2 = lade_json(konf_pfad)
        if e1 or e2:
            fehler.append(e1 or e2)
        else:
            for name in sorted(set(alt.get("deckel") or {}) | set(neu.get("deckel") or {})):
                a, n = deckel_wert(alt, name), deckel_wert(neu, name)
                if n > a:
                    fehler.append(f"Deckel {name} steigt von {a} auf {n}; die Ratsche läuft nur abwärts. Den Verstoß beheben statt den Deckel anheben.")
            alt_e = (alt.get("erlaubt") or {}).get("pull_request_target") or {}
            neu_e = (neu.get("erlaubt") or {}).get("pull_request_target") or {}
            for datei in sorted(set(neu_e) - set(alt_e if isinstance(alt_e, dict) else {})):
                grund = neu_e[datei]
                if not isinstance(grund, str) or not grund.strip() or grund.strip().startswith(PLATZHALTER):
                    fehler.append(f"neue pull_request_target-Ausnahme ohne Begründung: {datei}")
                else:
                    warnungen.append(f"neue pull_request_target-Ausnahme: {datei} ({grund.strip()[:80]})")
            alt_m, neu_m = (alt.get("migrationen") or {}), (neu.get("migrationen") or {})
            for schluessel in ("bekannt", "ohne_praefix_bekannt"):
                neue = sorted(set(neu_m.get(schluessel) or []) - set(alt_m.get(schluessel) or []))
                if neue:
                    warnungen.append(f"neue Altfälle in migrationen.{schluessel}: {', '.join(neue)} (nur für schon angewendete Migrationen zulässig)")
            for schluessel in ("verzeichnis", "praefix"):
                if alt_m.get(schluessel) != neu_m.get(schluessel):
                    warnungen.append(f"migrationen.{schluessel} geändert: {alt_m.get(schluessel)!r} → {neu_m.get(schluessel)!r}; der Prüfbereich der Zeitstempelprüfung ändert sich")
    elif konf_pfad.exists():
        hinweise.append("keine konfiguration.json im Basisstand; nichts zu vergleichen")

    eingerichtet = basis_konf_pfad.exists()
    gl_pfad = wurzel / ".gitleaks.toml"
    basis_gl_pfad = basis_dir / ".gitleaks.toml"
    if gl_pfad.exists():
        jetzt, e = allowlist(gl_pfad)
        if e:
            fehler.append(e)
        else:
            if jetzt["use_default"] is not True:
                fehler.append(".gitleaks.toml bindet die Standardregeln nicht ein (extend.useDefault = true fehlt oder ist false); eine eigene Konfiguration ersetzt sie sonst vollständig")
            if jetzt["disabled"]:
                fehler.append(f".gitleaks.toml schaltet Regeln ab (extend.disabledRules: {', '.join(jetzt['disabled'])})")
            for x in sorted((jetzt["pfade"] | jetzt["regexe"]) & ALLES_RE):
                fehler.append(f".gitleaks.toml nimmt alles aus: {x!r}")
            alt = None
            if basis_gl_pfad.exists():
                alt, e_alt = allowlist(basis_gl_pfad)
                if e_alt:
                    fehler.append(e_alt)
            elif eingerichtet:
                alt = LEER
            else:
                hinweise.append("keine .gitleaks.toml im Basisstand und keine Grundausstattung dort; nichts zu vergleichen")
            if alt is not None:
                neue = sorted(jetzt["pfade"] - alt["pfade"]) + sorted(jetzt["regexe"] - alt["regexe"])
                if neue:
                    fehler.append(f"{len(neue)} neue gitleaks-Ausnahme(n) in .gitleaks.toml: {', '.join(neue[:5])}{' …' if len(neue) > 5 else ''}. Für einen Fehlalarm den Kommentar gitleaks:allow an die Zeile setzen, ein echtes Geheimnis entfernen und rotieren.")
    ig_pfad = wurzel / ".gitleaksignore"
    basis_ig_pfad = basis_dir / ".gitleaksignore"
    if ig_pfad.exists():
        jetzt_ig = ignore_eintraege(ig_pfad)
        if basis_ig_pfad.exists():
            alt_ig = ignore_eintraege(basis_ig_pfad)
        elif eingerichtet:
            alt_ig = set()
        else:
            alt_ig = None
            hinweise.append("keine .gitleaksignore im Basisstand und keine Grundausstattung dort; nichts zu vergleichen")
        if alt_ig is not None and jetzt_ig - alt_ig:
            neue = sorted(jetzt_ig - alt_ig)
            fehler.append(f"{len(neue)} neue Einträge in .gitleaksignore: {', '.join(neue[:3])}{' …' if len(neue) > 3 else ''}; ein ignorierter Fingerabdruck ist eine Ausnahme wie ein Allowlist-Pfad")
    return fehler, warnungen, hinweise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--basis", required=True, help="Verzeichnis mit konfiguration.json und/oder .gitleaks.toml des Zielbranches")
    ap.add_argument("--wurzel", default=".", help="Repo-Wurzel des PR-Standes")
    args = ap.parse_args(argv)
    basis = Path(args.basis)
    if not basis.is_dir():
        print(f"::error::Basisverzeichnis fehlt: {basis}")
        return 2
    fehler, warnungen, hinweise = vergleiche(basis, Path(args.wurzel))
    for f in fehler:
        print(f"::error::{f}")
    for w in warnungen:
        print(f"::warning::{w}")
    for h in hinweise:
        print(f"::notice::{h}")
    print(f"SUMMARY fehler={len(fehler)} warnungen={len(warnungen)} hinweise={len(hinweise)}")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
