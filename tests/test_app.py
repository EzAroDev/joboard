"""Tests sans réseau : une fausse API Google Sheets en mémoire.

Lancer : python -m unittest discover -s tests -v
"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sheets  # noqa: E402
from sheets import Backend, UserError, to_serial  # noqa: E402


def text(v):
    return {"userEnteredValue": {"stringValue": v}, "effectiveValue": {"stringValue": v}, "formattedValue": v}


def num(v):
    return {"userEnteredValue": {"numberValue": v}, "effectiveValue": {"numberValue": v}}


def day(d, formula=None):
    c = {"effectiveValue": {"numberValue": to_serial(d)}, "effectiveFormat": {"numberFormat": {"type": "DATE"}},
         "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}}
    c["userEnteredValue"] = {"formulaValue": formula} if formula else {"numberValue": to_serial(d)}
    return c


def strict_list(values):
    return {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]}, "strict": True}


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


class FakeSheetsApi:
    """Reproduit le sous-ensemble de l'API utilisé par sheets.py."""

    def __init__(self, sheets_spec):
        self.sheets = {}
        for i, (title, rows) in enumerate(sheets_spec.items()):
            self.sheets[title] = {"sheetId": 100 + i, "rows": [list(r) for r in rows], "rowCount": 40}
        self.meta = []
        self.batches = 0

    def _by_id(self, sid):
        return next(s for s in self.sheets.values() if s["sheetId"] == sid)

    def _cell(self, sheet, r, c):
        rows = sheet["rows"]
        while len(rows) <= r:
            rows.append([])
        while len(rows[r]) <= c:
            rows[r].append({})
        return rows[r][c]

    def request(self, method, url, params=None, json=None, timeout=None):
        path = urlparse(url).path
        if method == "POST" and path.endswith(":batchUpdate"):
            self.batches += 1
            for req in json["requests"]:
                self._apply(req)
            return Resp(200, {})
        params = params or {}
        if params.get("includeGridData"):
            out = []
            for rng in params["ranges"]:
                title = rng.split("!")[0].strip("'").replace("''", "'")
                s = self.sheets[title]
                out.append({"properties": {"sheetId": s["sheetId"], "title": title, "gridProperties": {"rowCount": s["rowCount"]}},
                            "data": [{"rowData": [{"values": r} for r in s["rows"]]}]})
            return Resp(200, {"sheets": out})
        if "/values/" in path:
            return Resp(200, {"values": []})
        return Resp(200, {"properties": {"title": "Test", "timeZone": "Europe/Paris"},
                          "sheets": [{"properties": {"sheetId": s["sheetId"], "title": t}} for t, s in self.sheets.items()],
                          "developerMetadata": [dict(m) for m in self.meta]})

    def _apply(self, req):
        if "updateCells" in req:
            u = req["updateCells"]
            s = self._by_id(u["start"]["sheetId"])
            cell = self._cell(s, u["start"]["rowIndex"], u["start"]["columnIndex"])
            new = u["rows"][0]["values"][0]
            if "formulaValue" in cell.get("userEnteredValue", {}):
                raise AssertionError("écriture dans une cellule à formule")
            uev = new.get("userEnteredValue", {})
            cell["userEnteredValue"] = uev
            cell["effectiveValue"] = dict(uev)
            if "userEnteredFormat" in new:
                cell["userEnteredFormat"] = new["userEnteredFormat"]
                cell["effectiveFormat"] = {"numberFormat": {"type": new["userEnteredFormat"]["numberFormat"]["type"]}}
        elif "copyPaste" in req:
            cp = req["copyPaste"]
            src, dst = cp["source"], cp["destination"]
            s = self._by_id(src["sheetId"])
            a = self._cell(s, src["startRowIndex"], src["startColumnIndex"])
            b = self._cell(s, dst["startRowIndex"], dst["startColumnIndex"])
            if cp["pasteType"] == "PASTE_FORMULA":
                b["userEnteredValue"] = {"formulaValue": a["userEnteredValue"]["formulaValue"] + " (copie)"}
            elif cp["pasteType"] == "PASTE_DATA_VALIDATION":
                b["dataValidation"] = a.get("dataValidation")
        elif "appendDimension" in req:
            self._by_id(req["appendDimension"]["sheetId"])["rowCount"] += req["appendDimension"]["length"]
        elif "createDeveloperMetadata" in req:
            m = dict(req["createDeveloperMetadata"]["developerMetadata"])
            m["metadataId"] = len(self.meta) + 1
            self.meta.append(m)
        elif "updateDeveloperMetadata" in req:
            mid = req["updateDeveloperMetadata"]["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
            next(m for m in self.meta if m["metadataId"] == mid)["metadataValue"] = req["updateDeveloperMetadata"]["developerMetadata"]["metadataValue"]
        else:
            raise AssertionError(f"requête non gérée : {req}")


CONTACT_STATUSES = ["À contacter", "Invitation envoyée", "Connecté", "Message envoyé", "Email envoyé",
                    "Relancé", "Réponse positive", "Entretien", "Pas de réponse", "Refus / pas de VIE"]


def make_api():
    status_dv = strict_list(CONTACT_STATUSES)
    contacts = [
        [text(h) for h in ["Statut", "Projet", "Priorité", "Entreprise", "Nom", "Date 1er contact",
                           "Relance prévue (formule)", "Notes / réponse"]],
        [dict(text("Message envoyé"), dataValidation=status_dv), text("VIE New York"), text("P1"), text("Acme"),
         text("Jean Dupont"), day(date(2026, 10, 1)), day(date(2026, 10, 8), "=F2+7"), {}],
        [dict(text("À contacter"), dataValidation=status_dv), text("VIE New York"), text("P2"), text("Beta"),
         text("Ann Lee"), {}, {"userEnteredValue": {"formulaValue": "=F3+7"}}, {}],
    ]
    offres = [
        [text(h) for h in ["Statut", "Lien pour postuler", "ID offre", "Entreprise", "Poste", "Correspondance CV",
                           "Date candidature", "Date relance"]],
        [text("À postuler"), text("https://x/1"), text("JOB-007"), text("Foo"), text("Data Eng"), num(0.8), {}, {}],
        [text("Refus"), text("https://x/2"), text("JOB-012"), text("Bar"), text("Dev"), num(0.5), {}, {}],
    ]
    return FakeSheetsApi({"Offres": offres, "Contacts": contacts})


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.api = make_api()
        self.b = Backend(self.api, "SHEET")
        self.data = self.b.get_all_data()

    def test_read(self):
        c = self.data["tabs"]["contacts"]
        self.assertEqual(len(c["rows"]), 2)
        self.assertEqual(c["cols"]["Relance prévue"], "Relance prévue (formule)")
        self.assertEqual(c["formulaCols"], ["Relance prévue (formule)"])
        self.assertEqual(c["rows"][0]["Relance prévue (formule)"], "2026-10-08")
        self.assertEqual(c["statuses"], CONTACT_STATUSES)
        self.assertFalse(self.data["tabs"]["spont"]["exists"])
        self.assertIn("Localisation", self.data["tabs"]["offres"]["missing"])

    def test_message_sent_sets_first_contact_and_keeps_formula(self):
        r = self.b.set_status("contacts", 3, {"Entreprise": "Beta", "Nom": "Ann Lee"}, "Message envoyé")
        self.assertEqual(r["row"]["Statut"], "Message envoyé")
        self.assertEqual(r["row"]["Date 1er contact"], date.today().isoformat())
        self.assertEqual(self.api.sheets["Contacts"]["rows"][2][6]["userEnteredValue"]["formulaValue"], "=F3+7")

    def test_formula_column_skipped_and_text_not_formula(self):
        r = self.b.update_row("contacts", 3, {"Entreprise": "Beta", "Nom": "Ann Lee"},
                              {"Relance prévue": "2026-01-01", "Notes / réponse": "=SUM(1)"})
        self.assertEqual(r["skipped"], ["Relance prévue (formule) (formule protégée)"])
        self.assertEqual(self.api.sheets["Contacts"]["rows"][2][7]["userEnteredValue"], {"stringValue": "=SUM(1)"})

    def test_invalid_status_rejected(self):
        with self.assertRaisesRegex(UserError, "non autorisée"):
            self.b.set_status("contacts", 3, {"Entreprise": "Beta", "Nom": "Ann Lee"}, "N'importe quoi")

    def test_row_found_after_sort(self):
        r = self.b.update_row("contacts", 2, {"Entreprise": "Beta", "Nom": "Ann Lee"}, {"Priorité": "P1"})
        self.assertEqual(r["row"]["_row"], 3)

    def test_unknown_row_rejected(self):
        with self.assertRaisesRegex(UserError, "déplacée"):
            self.b.update_row("contacts", 3, {"Entreprise": "Nope", "Nom": "X"}, {"Priorité": "P1"})

    def test_duplicate_contact_blocked(self):
        with self.assertRaisesRegex(UserError, "Doublon"):
            self.b.add_row("contacts", {"Nom": "jean  DUPONT", "Entreprise": "acmé"})

    def test_add_contact_copies_formula_and_validation(self):
        r = self.b.add_row("contacts", {"Nom": "Zoé", "Entreprise": "Gamma"})
        self.assertEqual(r["row"]["_row"], 4)
        self.assertEqual(r["row"]["Statut"], "À contacter")
        row = self.api.sheets["Contacts"]["rows"][3]
        self.assertEqual(row[6]["userEnteredValue"]["formulaValue"], "=F3+7 (copie)")
        self.assertEqual(row[0]["dataValidation"]["condition"]["type"], "ONE_OF_LIST")

    def test_reply_is_appended_with_date(self):
        r = self.b.add_reply("contacts", 2, {"Entreprise": "Acme", "Nom": "Jean Dupont"}, "Merci !", "Réponse positive")
        self.assertRegex(r["row"]["Notes / réponse"], r"^\[\d\d/\d\d/\d{4}\] Merci !$")
        self.assertEqual(r["row"]["Statut"], "Réponse positive")

    def test_application_sent_fills_dates_as_real_dates(self):
        r = self.b.set_status("offres", 2, {"ID offre": "JOB-007", "Entreprise": "Foo", "Poste": "Data Eng"}, "Candidature envoyée")
        self.assertEqual(r["row"]["Date candidature"], date.today().isoformat())
        self.assertEqual(r["row"]["Date relance"], (date.today() + timedelta(days=7)).isoformat())
        cell = self.api.sheets["Offres"]["rows"][1][6]
        self.assertEqual(cell["userEnteredFormat"]["numberFormat"], {"type": "DATE", "pattern": "dd/mm/yyyy"})

    def test_ids_never_reused_and_duplicates_blocked(self):
        # JOB-012 effacée à la main dans le Sheet après la lecture : le max mémorisé (12) empêche sa réutilisation.
        self.api.sheets["Offres"]["rows"][2] = [{} for _ in range(8)]
        r = self.b.add_row("offres", {"Entreprise": "New", "Poste": "P", "Lien pour postuler": "https://x/3"})
        self.assertEqual(r["id"], "JOB-013")
        r = self.b.add_row("offres", {"Entreprise": "New2", "Poste": "P2"})
        self.assertEqual(r["id"], "JOB-014")
        with self.assertRaisesRegex(UserError, "Doublon"):
            self.b.add_row("offres", {"Entreprise": "Other", "Poste": "Q", "Lien pour postuler": "https://x/3"})

    def test_archive(self):
        r = self.b.archive_row("offres", 2, {"ID offre": "JOB-007"})
        self.assertEqual(r["row"]["Statut"], "Retirée")

    def test_statuts_tab_in_rows_gives_lists_and_help(self):
        api = make_api()
        api.sheets["Statuts"] = {"sheetId": 900, "rowCount": 40, "rows": [
            [text("Onglet"), text("Statut"), text("Nombre"), text("Quand l’utiliser")],
            [text("Offres"), text("À postuler"), num(0), text("Dossier à envoyer")],
            [text("Offres"), text("Refus"), num(3), text("Candidature refusée")],
        ]}
        data = Backend(api, "SHEET").get_all_data()
        offres = data["tabs"]["offres"]
        self.assertEqual(offres["statuses"], ["À postuler", "Refus"])
        self.assertEqual(offres["statusHelp"]["Refus"], "Candidature refusée")

    def test_mark_spont_sent_sets_date_and_preparation(self):
        prep_dv = strict_list(["Prête à relire", "À qualifier", "Canal à vérifier", "Envoyée"])
        api = make_api()
        api.sheets["Spontanées"] = {"sheetId": 901, "rowCount": 40, "rows": [
            [text(h) for h in ["Statut", "ID", "Entreprise", "Préparation", "Date d’envoi", "Relance prévue"]],
            [text("À préparer"), text("SP-001"), text("Acme"), dict(text("Prête à relire"), dataValidation=prep_dv), {},
             {"userEnteredValue": {"formulaValue": "=IF(E2=\"\";\"\";E2+14)"}}],
        ]}
        b = Backend(api, "SHEET")
        b.get_all_data()
        r = b.mark_spont_sent(2, {"ID": "SP-001", "Entreprise": "Acme"})
        self.assertEqual(r["row"]["Statut"], "Candidature envoyée")
        self.assertEqual(r["row"]["Préparation"], "Envoyée")
        self.assertEqual(r["row"]["Date d’envoi"], date.today().isoformat())
        self.assertIn("Relance prévue", r["row"]["_f"])

    def test_bad_date_rejected(self):
        with self.assertRaisesRegex(UserError, "Date invalide"):
            self.b.update_row("offres", 2, {"ID offre": "JOB-007"}, {"Date relance": "demain"})


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        server.backend = Backend(make_api(), "SHEET")
        cls.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        server.PORT = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{server.PORT}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, fn, args, token=None):
        req = urllib.request.Request(self.base + "/api/call", data=json.dumps({"fn": fn, "args": args}).encode(),
                                     headers={"Content-Type": "application/json", "X-SC-Token": token or self.server.TOKEN})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_has_token_and_assets_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            html = r.read().decode()
        self.assertIn(self.server.TOKEN, html)
        self.assertIn('href="https://docs.google.com/spreadsheets/d/SHEET/edit"', html)
        for asset in ("/app.js", "/styles.css"):
            with urllib.request.urlopen(self.base + asset) as r:
                self.assertEqual(r.status, 200)

    def test_ping(self):
        with urllib.request.urlopen(self.base + "/api/ping") as r:
            self.assertEqual(json.loads(r.read())["session"], self.server.SESSION)

    def test_api_call_and_errors(self):
        status, body = self.call("getAllData", [])
        self.assertEqual(status, 200)
        self.assertEqual(len(body["result"]["tabs"]["offres"]["rows"]), 2)
        self.assertEqual(self.call("getAllData", [], token="bad")[0], 403)
        self.assertEqual(self.call("os.system", [])[0], 400)
        status, body = self.call("setStatus", ["contacts", 3, {"Entreprise": "Beta", "Nom": "Ann Lee"}, "Inconnu"])
        self.assertEqual(status, 400)
        self.assertIn("non autorisée", body["error"])

    def test_path_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/..%2Fserver.py")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
