"""Notes et calendrier des entretiens.

  - Notes : une note est rattachée à une ligne du Sheet (offre, spontanée, contact, entreprise),
    à un évènement du calendrier, ou libre. Elles sont enregistrées dans `joboard-donnees/notes.json`,
    jamais dans le Sheet.
  - Calendrier, deux modes :
      * lien publié (par défaut, sans compte développeur) : Joboard lit le lien ICS que publie
        Outlook.com ; les entretiens créés ici sont gardés sur ce PC et ouverts dans Outlook,
        pré-remplis, pour y être enregistrés d'un clic ;
      * Microsoft Graph (si « outlook_client_id » est renseigné) : lecture et écriture directes,
        connexion par code (« device code ») sans mot de passe stocké.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import requests

from calendar_ics import compose_link, expand, parse_calendar, zone
from sheets import UserError

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access User.Read Calendars.ReadWrite"
NOTE_ID = re.compile(r"^[a-f0-9]{32}$")
EVENT_ID = re.compile(r"^[A-Za-z0-9_=+/-]{8,400}$")
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
KINDS = ("entretien", "test", "relance", "autre")
TIMEOUT = 20
ICS_TTL = 300          # le lien publié est relu au plus toutes les 5 minutes (Outlook le met à jour moins souvent)
ICS_MAX = 15_000_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _text(value, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

class NotesStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def list(self) -> list:
        with self.lock:
            notes = _read_json(self.path, [])
        return notes if isinstance(notes, list) else []

    def save(self, note) -> dict:
        if not isinstance(note, dict):
            raise UserError("Note invalide.")
        text = _text(note.get("text"), 20_000)
        if not text:
            raise UserError("La note est vide.")
        target = _text(note.get("target") or "general", 200)
        with self.lock:
            notes = _read_json(self.path, [])
            notes = notes if isinstance(notes, list) else []
            note_id = str(note.get("id") or "")
            current = next((n for n in notes if n.get("id") == note_id), None) if note_id else None
            if note_id and not current and not NOTE_ID.fullmatch(note_id):
                raise UserError("Note invalide.")
            now = _now_ms()
            if current is None:
                current = {"id": note_id or uuid.uuid4().hex, "createdAt": now}
                notes.append(current)
            current.update({"target": target, "label": _text(note.get("label"), 200), "text": text,
                            "pinned": bool(note.get("pinned")), "updatedAt": now})
            _write_json(self.path, notes)
            return dict(current)

    def delete(self, note_id) -> dict:
        if not isinstance(note_id, str) or not NOTE_ID.fullmatch(note_id):
            raise UserError("Note invalide.")
        with self.lock:
            notes = [n for n in _read_json(self.path, []) if n.get("id") != note_id]
            _write_json(self.path, notes)
        return {"deleted": note_id}


# ---------------------------------------------------------------------------
# Calendrier
# ---------------------------------------------------------------------------

def _clean_event(event) -> dict:
    """Valide un évènement envoyé par l'interface (heures locales « AAAA-MM-JJTHH:MM »)."""
    if not isinstance(event, dict):
        raise UserError("Évènement invalide.")
    title = _text(event.get("title"), 250)
    if not title:
        raise UserError("Donnez un titre à l'évènement.")
    all_day = bool(event.get("allDay"))
    start, end = str(event.get("start") or ""), str(event.get("end") or "")
    if not STAMP.fullmatch(start) or not STAMP.fullmatch(end):
        raise UserError("Date ou heure invalide.")
    if all_day:
        start, end = start[:10], end[:10]
        if end <= start:
            end = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
    else:
        if len(start) == 10 or len(end) == 10:
            raise UserError("Indiquez une heure de début et de fin.")
        if end <= start:
            raise UserError("La fin doit être après le début.")
    kind = event.get("kind") if event.get("kind") in KINDS else "entretien"
    reminder = event.get("reminder")
    reminder = int(reminder) if isinstance(reminder, (int, float)) and 0 <= reminder <= 10_080 else None
    link = event.get("link") if isinstance(event.get("link"), dict) else {}
    return {"id": _text(event.get("id"), 400), "source": event.get("source") if event.get("source") in ("outlook", "local") else "",
            "title": title, "start": start, "end": end, "allDay": all_day, "kind": kind,
            "location": _text(event.get("location"), 500), "description": _text(event.get("description"), 8_000),
            "reminder": reminder,
            "link": {"target": _text(link.get("target"), 200), "label": _text(link.get("label"), 200)} if link.get("target") else None}


class Calendar:
    def __init__(self, folder: Path, config: dict | None = None, http=None):
        config = config or {}
        self.folder = folder
        self.client_id = str(config.get("outlook_client_id") or "").strip()
        self.account = str(config.get("outlook_account") or "votre compte Outlook").strip()
        self.tenant = str(config.get("outlook_tenant") or "consumers").strip()
        self.timezone = str(config.get("outlook_timezone") or "Europe/Paris").strip()
        self.http = http or requests.Session()
        self.token_file = folder / "outlook-token.json"
        self.local_file = folder / "evenements.json"
        self.links_file = folder / "evenements-liens.json"
        self.ics_file = folder / "outlook-ics.json"
        self.ics_config = str(config.get("outlook_ics_url") or "").strip()
        self.tz = zone(self.timezone, zone("Europe/Paris", None))
        self.lock = threading.RLock()
        self.pending: dict | None = None  # connexion par code en cours
        self._ics = {"url": "", "at": 0.0, "raw": [], "error": None}

    # ---------- Lien publié (ICS) ----------

    def _ics_url(self) -> str:
        saved = _read_json(self.ics_file, {})
        return str((saved or {}).get("url") or self.ics_config or "")

    @staticmethod
    def _check_ics_url(url: str) -> str:
        url = str(url or "").strip()
        if url.lower().startswith("webcal://"):
            url = "https://" + url[9:]
        if not re.fullmatch(r"https://[^\s/]+/\S+", url) or len(url) > 2000:
            raise UserError("Collez le lien ICS complet publié par Outlook (il commence par https:// et finit par .ics).")
        return url

    def _fetch_ics(self, url: str) -> list:
        try:
            r = self.http.get(url, timeout=TIMEOUT, headers={"Accept": "text/calendar"})
        except requests.RequestException as exc:
            raise UserError(f"Calendrier Outlook injoignable : {exc}") from None
        if r.status_code != 200:
            raise UserError(f"Outlook a refusé le lien publié ({r.status_code}) : vérifiez qu'il est toujours publié.")
        content = r.content if isinstance(getattr(r, "content", None), (bytes, bytearray)) else str(r.text).encode("utf-8")
        if len(content) > ICS_MAX:
            raise UserError("Calendrier publié trop volumineux.")
        text = content.decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text[:2000]:
            raise UserError("Ce lien ne renvoie pas un calendrier (.ics). Copiez le lien « ICS », pas le lien « HTML ».")
        return parse_calendar(text, self.tz)

    def _ics_events(self, start: str, end: str, force: bool) -> list:
        url = self._ics_url()
        if not url:
            return []
        cache = self._ics
        if force or cache["url"] != url or time.time() - cache["at"] > ICS_TTL:
            try:
                raw = self._fetch_ics(url)
                self._ics = cache = {"url": url, "at": time.time(), "raw": raw, "error": None}
            except UserError as exc:
                cache["error"] = str(exc)
                if cache["url"] != url:
                    raise
        return expand(cache["raw"], date.fromisoformat(start), date.fromisoformat(end), self.tz)

    def set_ics(self, url) -> dict:
        """Enregistre (ou retire, si vide) le lien ICS publié, après l'avoir lu une première fois."""
        with self.lock:
            if not str(url or "").strip():
                self.ics_file.unlink(missing_ok=True)
                self._ics = {"url": "", "at": 0.0, "raw": [], "error": None}
                return self.status()
            url = self._check_ics_url(url)
            raw = self._fetch_ics(url)
            _write_json(self.ics_file, {"url": url})
            self._ics = {"url": url, "at": time.time(), "raw": raw, "error": None}
        return self.status()

    def compose(self, event_id) -> dict:
        """Lien Outlook pré-rempli pour un évènement gardé sur ce PC ; il est marqué comme envoyé."""
        with self.lock:
            events = self._local()
            event = next((x for x in events if x.get("id") == event_id), None)
            if not event:
                raise UserError("Évènement introuvable.")
            event["sentToOutlook"] = True
            _write_json(self.local_file, events)
        return {"url": compose_link(event, self.tz), "event": dict(event, source="local")}

    # ---------- Connexion Outlook (Microsoft Graph, device code) ----------

    def _login_url(self, path: str) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/{path}"

    def _token(self) -> dict | None:
        token = _read_json(self.token_file, None)
        return token if isinstance(token, dict) and token.get("refresh_token") else None

    def status(self) -> dict:
        token = self._token()
        pending = None
        if self.pending and self.pending["expiresAt"] > time.time():
            pending = {k: self.pending[k] for k in ("code", "url", "message", "expiresAt", "interval")}
        ics_url = self._ics_url()
        return {"configured": bool(self.client_id), "connected": bool(token and self.client_id),
                "account": (token or {}).get("account") or self.account, "pending": pending,
                "timezone": self.timezone,
                "ics": {"set": bool(ics_url), "fetchedAt": self._ics["at"] if self._ics["url"] == ics_url else 0,
                        "error": self._ics["error"] if self._ics["url"] == ics_url else None,
                        "host": re.sub(r"^https://([^/]+)/.*$", r"\1", ics_url) if ics_url else ""}}

    def connect(self) -> dict:
        if not self.client_id:
            raise UserError("Ajoutez « outlook_client_id » dans config.json (voir README, section Calendrier).")
        try:
            r = self.http.post(self._login_url("devicecode"), data={"client_id": self.client_id, "scope": SCOPES}, timeout=TIMEOUT)
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise UserError(f"Connexion à Microsoft impossible : {exc}") from None
        if r.status_code != 200 or "device_code" not in data:
            raise UserError("Microsoft a refusé la connexion : " + str(data.get("error_description") or data.get("error") or r.status_code).splitlines()[0])
        with self.lock:
            self.pending = {"device": data["device_code"], "code": data["user_code"], "url": data.get("verification_uri") or "https://microsoft.com/devicelogin",
                            "message": data.get("message", ""), "interval": int(data.get("interval") or 5),
                            "expiresAt": time.time() + int(data.get("expires_in") or 900)}
        return self.status()

    def poll(self) -> dict:
        """Vérifie si l'utilisateur a saisi le code sur la page Microsoft."""
        with self.lock:
            pending = self.pending
        if not pending:
            return self.status()
        if pending["expiresAt"] <= time.time():
            self.pending = None
            raise UserError("Le code a expiré : relancez la connexion.")
        try:
            r = self.http.post(self._login_url("token"), data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                                              "client_id": self.client_id, "device_code": pending["device"]}, timeout=TIMEOUT)
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise UserError(f"Connexion à Microsoft impossible : {exc}") from None
        error = data.get("error")
        if error in ("authorization_pending", "slow_down"):
            return self.status()
        if error:
            self.pending = None
            raise UserError("Connexion refusée par Microsoft." if error == "authorization_declined" else "Connexion Outlook échouée : " + str(data.get("error_description") or error).splitlines()[0])
        self._store_token(data)
        self.pending = None
        try:
            me = self._graph("GET", "/me?$select=mail,userPrincipalName")
            token = self._token()
            token["account"] = me.get("mail") or me.get("userPrincipalName") or self.account
            _write_json(self.token_file, token)
        except UserError:
            pass
        return self.status()

    def disconnect(self) -> dict:
        with self.lock:
            self.token_file.unlink(missing_ok=True)
            self.pending = None
        return self.status()

    def _store_token(self, data: dict) -> None:
        old = self._token() or {}
        token = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or old.get("refresh_token"),
                 "expires_at": time.time() + int(data.get("expires_in") or 3600) - 60, "account": old.get("account") or self.account}
        _write_json(self.token_file, token)

    def _access_token(self) -> str:
        with self.lock:
            token = self._token()
            if not token or not self.client_id:
                raise UserError("Outlook n'est pas connecté.")
            if token.get("expires_at", 0) > time.time():
                return token["access_token"]
            try:
                r = self.http.post(self._login_url("token"), data={"grant_type": "refresh_token", "client_id": self.client_id,
                                                                  "refresh_token": token["refresh_token"], "scope": SCOPES}, timeout=TIMEOUT)
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                raise UserError(f"Outlook injoignable : {exc}") from None
            if r.status_code != 200 or "access_token" not in data:
                self.token_file.unlink(missing_ok=True)
                raise UserError("L'autorisation Outlook a expiré : reconnectez votre compte.")
            self._store_token(data)
            return data["access_token"]

    def _graph(self, method: str, path: str, body=None):
        headers = {"Authorization": "Bearer " + self._access_token(), "Prefer": f'outlook.timezone="{self.timezone}"'}
        url = path if path.startswith("https://") else GRAPH + path
        try:
            r = self.http.request(method, url, headers=headers, json=body, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise UserError(f"Outlook injoignable : {exc}") from None
        if r.status_code == 204:
            return {}
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            message = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None
            raise UserError(f"Outlook ({r.status_code}) : {message or 'requête refusée'}")
        return data

    # ---------- Évènements ----------

    def _links(self) -> dict:
        links = _read_json(self.links_file, {})
        return links if isinstance(links, dict) else {}

    def _local(self) -> list:
        events = _read_json(self.local_file, [])
        return events if isinstance(events, list) else []

    def _from_graph(self, e: dict, links: dict) -> dict:
        all_day = bool(e.get("isAllDay"))
        cut = 10 if all_day else 16
        meta = links.get(e.get("id"), {})
        online = (e.get("onlineMeeting") or {}).get("joinUrl") or ""
        return {"id": e.get("id"), "source": "outlook", "title": e.get("subject") or "(Sans titre)",
                "start": str((e.get("start") or {}).get("dateTime") or "")[:cut], "end": str((e.get("end") or {}).get("dateTime") or "")[:cut],
                "allDay": all_day, "location": (e.get("location") or {}).get("displayName") or "", "online": online,
                "description": meta.get("description", e.get("bodyPreview") or ""), "webLink": e.get("webLink") or "",
                "kind": meta.get("kind") or "outlook", "link": meta.get("link"),
                "reminder": e.get("reminderMinutesBeforeStart") if e.get("isReminderOn") else None,
                "cancelled": bool(e.get("isCancelled"))}

    def events(self, start: str, end: str, force=False) -> dict:
        if not (STAMP.fullmatch(str(start)) and STAMP.fullmatch(str(end))):
            raise UserError("Période invalide.")
        start, end = str(start)[:10], str(end)[:10]
        local = [dict(e, source="local") for e in self._local() if e.get("start", "")[:10] <= end and e.get("end", "")[:10] >= start]
        status = self.status()
        out, error = local, None
        if not status["connected"]:
            try:
                ics = self._ics_events(start, end, bool(force))
                # Un entretien créé ici puis enregistré dans Outlook revient par le lien publié :
                # on garde la version de Joboard (lien vers la candidature, notes) et on la marque synchronisée.
                sent = {(e["title"].strip().lower(), e["start"]): e for e in local if e.get("sentToOutlook")}
                for e in ics:
                    twin = sent.get((e["title"].strip().lower(), e["start"]))
                    if twin is not None:
                        twin["synced"] = True
                    else:
                        out = out + [e]
            except UserError as exc:
                error = str(exc)
            status = self.status()
            error = error or status["ics"]["error"]
        if status["connected"]:
            try:
                links = self._links()
                select = "id,subject,start,end,isAllDay,location,onlineMeeting,bodyPreview,webLink,isCancelled,isReminderOn,reminderMinutesBeforeStart"
                url = f"/me/calendarView?startDateTime={start}T00:00:00&endDateTime={end}T23:59:59&$top=250&$orderby=start/dateTime&$select={select}"
                pages = 0
                while url and pages < 8:
                    data = self._graph("GET", url)
                    out = out + [self._from_graph(e, links) for e in data.get("value", [])]
                    url = data.get("@odata.nextLink")
                    pages += 1
            except UserError as exc:
                error = str(exc)
        return {"events": sorted(out, key=lambda e: (e.get("start") or "")), "status": self.status(), "error": error}

    def _graph_body(self, e: dict) -> dict:
        when = (lambda v: {"dateTime": v + "T00:00:00", "timeZone": self.timezone}) if e["allDay"] else (lambda v: {"dateTime": v + ":00", "timeZone": self.timezone})
        body = {"subject": e["title"], "start": when(e["start"]), "end": when(e["end"]), "isAllDay": e["allDay"],
                "location": {"displayName": e["location"]}, "body": {"contentType": "text", "content": e["description"]},
                "isReminderOn": e["reminder"] is not None}
        if e["reminder"] is not None:
            body["reminderMinutesBeforeStart"] = e["reminder"]
        return body

    def save(self, event) -> dict:
        e = _clean_event(event)
        connected = self.status()["connected"]
        with self.lock:
            if e["source"] == "outlook" or (not e["source"] and connected):
                if e["id"] and not EVENT_ID.fullmatch(e["id"]):
                    raise UserError("Évènement invalide.")
                body = self._graph_body(e)
                if not e["id"]:
                    body["categories"] = ["Joboard"]
                saved = self._graph("PATCH", f"/me/events/{e['id']}", body) if e["id"] else self._graph("POST", "/me/events", body)
                links = self._links()
                links[saved["id"]] = {"kind": e["kind"], "link": e["link"], "description": e["description"]}
                _write_json(self.links_file, links)
                return self._from_graph(saved, links)
            events = self._local()
            current = next((x for x in events if x.get("id") == e["id"]), None) if e["id"] else None
            if current is None:
                current = {"id": "local-" + uuid.uuid4().hex, "createdAt": _now_ms()}
                events.append(current)
            current.update({k: e[k] for k in ("title", "start", "end", "allDay", "kind", "location", "description", "reminder", "link")})
            current["updatedAt"] = _now_ms()
            _write_json(self.local_file, events)
            out = dict(current, source="local")
            if not connected and not current.get("sentToOutlook") and event.get("sendToOutlook"):
                current["sentToOutlook"] = out["sentToOutlook"] = True
                _write_json(self.local_file, events)
                out["compose"] = compose_link(current, self.tz)
            return out

    def delete(self, event_id, source) -> dict:
        event_id = str(event_id or "")
        with self.lock:
            if source == "outlook":
                if not EVENT_ID.fullmatch(event_id):
                    raise UserError("Évènement invalide.")
                self._graph("DELETE", f"/me/events/{event_id}")
                links = self._links()
                if links.pop(event_id, None) is not None:
                    _write_json(self.links_file, links)
            else:
                _write_json(self.local_file, [x for x in self._local() if x.get("id") != event_id])
        return {"deleted": event_id}

    def push_local(self, event_id) -> dict:
        """Copie un évènement enregistré sur ce PC dans le calendrier Outlook, puis le retire du stockage local."""
        if not self.status()["connected"]:
            raise UserError("Connectez d'abord Outlook.")
        local = next((x for x in self._local() if x.get("id") == event_id), None)
        if not local:
            raise UserError("Évènement introuvable.")
        saved = self.save(dict(local, id="", source="outlook"))
        self.delete(event_id, "local")
        return saved

