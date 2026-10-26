"""Synthetic data shared by the browser checks. No live API or Google Sheet is used."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sheets import TABS  # noqa: E402


def fixture(empty=False):
    data = {"today": "2026-10-24", "tabs": {}, "dashboard": {"rows": [["Synthèse", 42]]}}
    for key, spec in TABS.items():
        tab = dict(spec, exists=True, missing=[], cols={c: c for c in spec["columns"]},
                   headers=spec["columns"], options={}, formulaCols=[], checkboxCols=[], statusHelp={}, rows=[])
        for i in range(36 if not empty else 0):
            row = {c: "" for c in spec["columns"]}
            row.update({"_row": i + 2, "_links": {}, "_f": [], "Entreprise": ['Apple', 'Microsoft'][i] if i < 2 else f"Entreprise de démonstration {i + 1}",
                        "Statut": spec["statuses"][i % len(spec["statuses"])],
                        "Poste": "Ingénieur données et développement logiciel",
                        "Poste / rôle proposé": "Ingénieur données", "Nom": f"Contact {i + 1}",
                        "Type de contact": "Recruteur", "Contrat": "VIE" if i % 3 else "CDI",
                        "Localisation": "New York" if i % 2 else "Paris", "Pays": "France",
                        "Correspondance CV": 78, "Date candidature": "2026-10-15",
                        "Date 1er contact": "2026-10-15", "Date d'envoi": "2026-10-15",
                        "Relance prévue": "2026-10-20", "Date relance": "2026-10-20"})
            tab["rows"].append(row)
        data["tabs"][key] = tab
    return data


def usage_fixture():
    now = time.time()
    return {p: {"available": True, "plan": "plus" if p == "chatgpt" else None,
                "windows": [{"key": str(i), "label": label, "utilization": .2 + i / 10,
                             "resetsAt": now + 3600 * (i + 1), "status": "allowed"}
                            for i, label in enumerate(["Fenêtre 5 h", "Semaine", "Semaine (Opus)", "Semaine (Sonnet)"] if p == "claude" else ["Fenêtre 5 h", "Semaine"])],
                "blocked": False, "availableAt": None, "measuredAt": now,
                "history": {"hourly": list(range(24)), "daily": list(range(7)), "dayStart": now - 6 * 86400,
                            "week": 12345, "today": 2500, "messagesToday": 18},
                "app": {"messagesToday": 3, "tokensToday": 2500}}
            for p in ("claude", "chatgpt")}
