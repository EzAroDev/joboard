"""Notes, calendrier (faux Microsoft Graph, sans réseau) et résumés de l'historique IA.

Lancer : python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from organizer import Calendar, NotesStore  # noqa: E402
from sheets import UserError  # noqa: E402


class Response:
    def __init__(self, status, data=None):
        self.status_code = status
        self.data = data

    def json(self):
        if self.data is None:
            raise ValueError("vide")
        return self.data


class FakeGraph:
    """Imite les points d'entrée Microsoft utilisés : code de connexion, jeton, /me et les évènements."""

    def __init__(self):
        self.events = {}
        self.calls = []
        self.authorized = False

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        if url.endswith("/devicecode"):
            return Response(200, {"device_code": "dev", "user_code": "ABCD-1234", "verification_uri": "https://microsoft.com/devicelogin",
                                  "interval": 5, "expires_in": 900, "message": "Saisissez le code"})
        if data.get("grant_type", "").endswith("device_code"):
            if not self.authorized:
                return Response(400, {"error": "authorization_pending"})
            return Response(200, {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600})
        if data.get("grant_type") == "refresh_token":
            return Response(200, {"access_token": "tok2", "refresh_token": "ref2", "expires_in": 3600})
        return Response(400, {"error": "invalid_request"})

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, json))
        assert headers["Authorization"].startswith("Bearer ")
        assert 'outlook.timezone="Europe/Paris"' in headers["Prefer"]
        path = url.split("/v1.0", 1)[1]
        if path.startswith("/me?"):
            return Response(200, {"mail": "utilisateur@example.com"})
        if path.startswith("/me/calendarView"):
            return Response(200, {"value": list(self.events.values())})
        if method == "POST" and path == "/me/events":
            event = dict(json, id="AAMk" + str(len(self.events) + 1).zfill(8), webLink="https://outlook.live.com/x")
            self.events[event["id"]] = event
            return Response(201, event)
        event_id = path.rsplit("/", 1)[1]
        if method == "PATCH":
            self.events[event_id].update(json)
            return Response(200, self.events[event_id])
        if method == "DELETE":
            self.events.pop(event_id)
            return Response(204)
        return Response(404, {"error": {"message": "introuvable"}})


class NotesTests(unittest.TestCase):
    def test_create_update_pin_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NotesStore(Path(tmp) / "notes.json")
            note = store.save({"target": "offres:of 12", "label": "Acme · Data", "text": "  Demander le salaire  "})
            self.assertEqual(note["text"], "Demander le salaire")
            self.assertRegex(note["id"], r"^[a-f0-9]{32}$")
            again = store.save(dict(note, text="Demander la fourchette", pinned=True))
            self.assertEqual(again["id"], note["id"])
            self.assertTrue(NotesStore(Path(tmp) / "notes.json").list()[0]["pinned"])
            self.assertEqual(len(store.list()), 1)
            with self.assertRaises(UserError):
                store.save({"target": "general", "text": "   "})
            with self.assertRaises(UserError):
                store.save({"id": "../../x", "text": "a"})
            with self.assertRaises(UserError):
                store.delete("../../x")
            store.delete(note["id"])
            self.assertEqual(store.list(), [])


class CalendarTests(unittest.TestCase):
    def make(self, tmp, client_id="client-test"):
        graph = FakeGraph()
        return Calendar(Path(tmp), {"outlook_client_id": client_id}, http=graph), graph

    def event(self, **kw):
        base = {"title": "Entretien RH · Acme", "kind": "entretien", "start": "2026-10-27T10:00", "end": "2026-10-27T11:00",
                "location": "Teams", "description": "Avec la RH", "reminder": 30,
                "link": {"target": "offres:of 12", "label": "Acme · Data"}}
        base.update(kw)
        return base

    def test_local_events_without_outlook(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal, graph = self.make(tmp, client_id="")
            status = cal.status()
            self.assertFalse(status["configured"])
            self.assertEqual(status["account"], "votre compte Outlook")
            saved = cal.save(self.event())
            self.assertEqual(saved["source"], "local")
            self.assertTrue(saved["id"].startswith("local-"))
            listed = cal.events("2026-10-01", "2026-10-31")
            self.assertEqual([e["id"] for e in listed["events"]], [saved["id"]])
            self.assertEqual(cal.events("2026-11-01", "2026-11-30")["events"], [])
            cal.save(dict(saved, title="Entretien technique"))
            self.assertEqual(cal.events("2026-10-01", "2026-10-31")["events"][0]["title"], "Entretien technique")
            cal.delete(saved["id"], "local")
            self.assertEqual(cal.events("2026-10-01", "2026-10-31")["events"], [])
            self.assertEqual(graph.calls, [])
            with self.assertRaises(UserError):
                cal.connect()

    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal, _ = self.make(tmp, client_id="")
            for bad in (self.event(title=" "), self.event(end="2026-10-27T09:00"), self.event(start="27/10/2026"), self.event(start="2026-10-27")):
                with self.assertRaises(UserError):
                    cal.save(bad)
            day = cal.save(self.event(allDay=True, start="2026-10-27", end="2026-10-27"))
            self.assertEqual((day["start"], day["end"]), ("2026-10-27", "2026-10-28"))
            with self.assertRaises(UserError):
                cal.events("x", "2026-10-31")

    def test_outlook_device_login_and_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal, graph = self.make(tmp)
            local = cal.save(self.event(title="Préparé hors ligne"))
            status = cal.connect()
            self.assertEqual(status["pending"]["code"], "ABCD-1234")
            self.assertNotIn("device", status["pending"])
            self.assertFalse(cal.poll()["connected"])
            graph.authorized = True
            status = cal.poll()
            self.assertTrue(status["connected"])
            self.assertEqual(status["account"], "utilisateur@example.com")
            self.assertIsNone(status["pending"])
            token = json.loads((Path(tmp) / "outlook-token.json").read_text(encoding="utf-8"))
            self.assertEqual(token["refresh_token"], "ref")

            saved = cal.save(self.event())
            self.assertEqual(saved["source"], "outlook")
            created = graph.events[saved["id"]]
            self.assertEqual(created["subject"], "Entretien RH · Acme")
            self.assertEqual(created["start"], {"dateTime": "2026-10-27T10:00:00", "timeZone": "Europe/Paris"})
            self.assertEqual(created["categories"], ["Joboard"])
            self.assertEqual(created["reminderMinutesBeforeStart"], 30)
            # Le lien vers l'offre et le type sont gardés sur ce PC.
            graph.events[saved["id"]].update({"start": {"dateTime": "2026-10-27T10:00:00.0000000"}, "end": {"dateTime": "2026-10-27T11:00:00.0000000"}})
            listed = cal.events("2026-10-01", "2026-10-31")
            outlook = [e for e in listed["events"] if e["source"] == "outlook"]
            self.assertEqual(outlook[0]["link"]["label"], "Acme · Data")
            self.assertEqual(outlook[0]["start"], "2026-10-27T10:00")
            self.assertEqual(len(listed["events"]), 2)

            pushed = cal.push_local(local["id"])
            self.assertEqual(pushed["source"], "outlook")
            self.assertEqual([e["source"] for e in cal.events("2026-10-01", "2026-10-31")["events"]], ["outlook", "outlook"])

            cal.save(dict(saved, title="Entretien final"))
            self.assertEqual(graph.events[saved["id"]]["subject"], "Entretien final")
            cal.delete(saved["id"], "outlook")
            self.assertNotIn(saved["id"], graph.events)
            with self.assertRaises(UserError):
                cal.delete("../../etc", "outlook")

            # Jeton expiré : rafraîchi automatiquement.
            token = json.loads((Path(tmp) / "outlook-token.json").read_text(encoding="utf-8"))
            token["expires_at"] = time.time() - 10
            (Path(tmp) / "outlook-token.json").write_text(json.dumps(token), encoding="utf-8")
            cal.events("2026-10-01", "2026-10-31")
            self.assertEqual(json.loads((Path(tmp) / "outlook-token.json").read_text(encoding="utf-8"))["access_token"], "tok2")
            self.assertFalse(cal.disconnect()["connected"])


if __name__ == "__main__":
    unittest.main()
