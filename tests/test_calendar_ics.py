"""Lien ICS publié par Outlook.com : lecture, fuseaux, répétitions, lien d'ajout pré-rempli.

Lancer : python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calendar_ics import compose_link, expand, parse_calendar  # noqa: E402
from organizer import Calendar  # noqa: E402
from sheets import UserError  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
OUTLOOK_ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "METHOD:PUBLISH", "PRODID:Microsoft Exchange Server 2010", "VERSION:2.0",
    "BEGIN:VTIMEZONE", "TZID:Romance Standard Time", "BEGIN:STANDARD", "DTSTART:16010101T030000", "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100", "END:STANDARD", "END:VTIMEZONE",
    # Entretien ponctuel, heure de Paris, description repliée sur deux lignes.
    "BEGIN:VEVENT", "UID:entretien-1", "SUMMARY:Entretien RH \\, Acme", "DTSTART;TZID=Romance Standard Time:20261027T100000",
    "DTEND;TZID=Romance Standard Time:20261027T110000", "LOCATION:Teams", "DESCRIPTION:Ligne 1\\nLigne",
    "  2 suite", "BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY", "END:VALARM", "END:VEVENT",
    # Réunion en UTC (heure d'hiver à Paris : +1 h).
    "BEGIN:VEVENT", "UID:utc-1", "SUMMARY:Point réseau", "DTSTART:20261029T090000Z", "DTEND:20261029T093000Z", "END:VEVENT",
    # Journée entière.
    "BEGIN:VEVENT", "UID:jour-1", "SUMMARY:Salon de l'emploi", "DTSTART;VALUE=DATE:20261030", "DTEND;VALUE=DATE:20261031", "END:VEVENT",
    # Répétition hebdomadaire lundi et mercredi, 5 fois, un mercredi annulé, un lundi déplacé.
    "BEGIN:VEVENT", "UID:cours", "SUMMARY:Cours d'anglais", "DTSTART;TZID=Romance Standard Time:20261026T180000",
    "DTEND;TZID=Romance Standard Time:20261026T190000", "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO,WE",
    "EXDATE;TZID=Romance Standard Time:20261028T180000", "END:VEVENT",
    "BEGIN:VEVENT", "UID:cours", "SUMMARY:Cours d'anglais (déplacé)", "RECURRENCE-ID;TZID=Romance Standard Time:20261102T180000",
    "DTSTART;TZID=Romance Standard Time:20261103T180000", "DTEND;TZID=Romance Standard Time:20261103T190000", "END:VEVENT",
    # Mensuel : 2e mardi du mois.
    "BEGIN:VEVENT", "UID:mensuel", "SUMMARY:Club alumni", "DTSTART;TZID=Romance Standard Time:20261013T190000",
    "DTEND;TZID=Romance Standard Time:20261013T200000", "RRULE:FREQ=MONTHLY;BYDAY=2TU;UNTIL=20261231T000000Z", "END:VEVENT",
    # Annulé : ignoré.
    "BEGIN:VEVENT", "UID:annule", "SUMMARY:Annulé", "STATUS:CANCELLED", "DTSTART:20261027T120000Z", "DTEND:20261027T130000Z", "END:VEVENT",
    "END:VCALENDAR", ""])


class Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.content = text.encode("utf-8")
        self.text = text


class FakeHttp:
    def __init__(self, body=OUTLOOK_ICS, status=200):
        self.body, self.status, self.gets = body, status, 0

    def get(self, url, timeout=None, headers=None):
        self.gets += 1
        return Resp(self.status, self.body)


class ParseTests(unittest.TestCase):
    def events(self, start="2026-10-01", end="2026-12-31"):
        return expand(parse_calendar(OUTLOOK_ICS, PARIS), date.fromisoformat(start), date.fromisoformat(end), PARIS)

    def test_single_events_timezones_and_text(self):
        by = {e["title"]: e for e in self.events()}
        rh = by["Entretien RH , Acme"]
        self.assertEqual((rh["start"], rh["end"]), ("2026-10-27T10:00", "2026-10-27T11:00"))
        self.assertEqual(rh["description"], "Ligne 1\nLigne 2 suite")
        self.assertEqual(rh["location"], "Teams")
        self.assertEqual(rh["kind"], "entretien")
        self.assertEqual(rh["source"], "ics")
        self.assertEqual(by["Point réseau"]["start"], "2026-10-29T10:00")
        self.assertEqual(by["Point réseau"]["kind"], "outlook")
        day = by["Salon de l'emploi"]
        self.assertTrue(day["allDay"])
        self.assertEqual((day["start"], day["end"]), ("2026-10-30", "2026-10-31"))
        self.assertNotIn("Annulé", by)

    def test_recurrences_exceptions_and_overrides(self):
        courses = sorted(e["start"] for e in self.events() if e["title"].startswith("Cours"))
        # 26/10, (28/10 annulé), 02/11 déplacé au 03/11, 04/11, 09/11 : COUNT=5 inclut l'annulé.
        self.assertEqual(courses, ["2026-10-26T18:00", "2026-11-03T18:00", "2026-11-04T18:00", "2026-11-09T18:00"])
        club = [e["start"] for e in self.events() if e["title"] == "Club alumni"]
        self.assertEqual(club, ["2026-10-13T19:00", "2026-11-10T19:00", "2026-12-08T19:00"])
        ids = [e["id"] for e in self.events()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([e["id"] for e in self.events()], ids)  # identifiants stables d'une lecture à l'autre

    def test_range_filter(self):
        titles = {e["title"] for e in self.events("2026-10-27", "2026-10-27")}
        self.assertEqual(titles, {"Entretien RH , Acme"})

    def test_compose_link(self):
        url = compose_link({"title": "Entretien · Acme", "start": "2026-10-27T10:00", "end": "2026-10-27T11:00", "allDay": False,
                            "location": "Teams", "description": "Avec la RH"}, PARIS)
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "outlook.live.com")
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(q["subject"], "Entretien · Acme")
        self.assertEqual(q["startdt"], "2026-10-27T10:00:00+01:00")
        self.assertEqual(q["enddt"], "2026-10-27T11:00:00+01:00")
        self.assertEqual(q["allday"], "false")
        summer = compose_link({"title": "x", "start": "2026-06-01", "end": "2026-06-02", "allDay": True}, PARIS)
        self.assertIn("startdt=2026-06-01&", summer)


class CalendarIcsTests(unittest.TestCase):
    def test_link_setup_merge_and_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHttp()
            cal = Calendar(Path(tmp), {}, http=http)
            self.assertFalse(cal.status()["ics"]["set"])
            with self.assertRaises(UserError):
                cal.set_ics("http://exemple.fr/cal.ics")
            status = cal.set_ics("webcal://outlook.live.com/owa/calendar/abc/def/calendar.ics")
            self.assertTrue(status["ics"]["set"])
            self.assertEqual(status["ics"]["host"], "outlook.live.com")
            self.assertIn("https://outlook.live.com", (Path(tmp) / "outlook-ics.json").read_text(encoding="utf-8"))

            # Créé dans Joboard et envoyé vers Outlook : un lien pré-rempli est renvoyé.
            saved = cal.save({"title": "Entretien RH , Acme", "kind": "entretien", "start": "2026-10-27T10:00", "end": "2026-10-27T11:00",
                              "link": {"target": "offres:of 1", "label": "Acme"}, "sendToOutlook": True})
            self.assertEqual(saved["source"], "local")
            self.assertTrue(saved["compose"].startswith("https://outlook.live.com/calendar/0/deeplink/compose?"))
            # Il revient par le lien publié : une seule fois, avec le lien vers la candidature.
            listed = cal.events("2026-10-27", "2026-10-27")["events"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["id"], saved["id"])
            self.assertTrue(listed[0]["synced"])
            self.assertEqual(listed[0]["link"]["label"], "Acme")

            # Le lien publié n'est relu qu'après 5 minutes, sauf actualisation demandée.
            gets = http.gets
            cal.events("2026-10-01", "2026-10-31")
            self.assertEqual(http.gets, gets)
            cal.events("2026-10-01", "2026-10-31", True)
            self.assertEqual(http.gets, gets + 1)

            other = cal.save({"title": "Test technique", "kind": "test", "start": "2026-10-28T14:00", "end": "2026-10-28T15:00"})
            self.assertNotIn("compose", other)
            composed = cal.compose(other["id"])
            self.assertIn("subject=Test%20technique", composed["url"])
            self.assertTrue(composed["event"]["sentToOutlook"])

            # Lien devenu invalide : l'erreur est signalée sans perdre les évènements déjà lus.
            http.status = 404
            result = cal.events("2026-10-01", "2026-10-31", True)
            self.assertIn("404", result["error"])
            self.assertTrue(any(e["source"] == "ics" for e in result["events"]))
            self.assertFalse(cal.set_ics("")["ics"]["set"])

    def test_rejects_html_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal = Calendar(Path(tmp), {}, http=FakeHttp(body="<html>calendrier</html>"))
            with self.assertRaises(UserError) as ctx:
                cal.set_ics("https://outlook.live.com/owa/calendar/abc/def/calendar.html")
            self.assertIn("ICS", str(ctx.exception))
            self.assertFalse((Path(tmp) / "outlook-ics.json").exists())


if __name__ == "__main__":
    unittest.main()
