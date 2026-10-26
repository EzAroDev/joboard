"""Joboard — serveur local.

Double-clic sur « Joboard.exe » :
  1. ce terminal s'ouvre et se connecte au Google Sheet ;
  2. le serveur démarre sur http://localhost:8765 ;
  3. le lien s'ouvre dans un onglet de votre navigateur.
Fermer ce terminal (ou Ctrl+C) arrête l'application.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import socket
import sys
import time
import traceback
import urllib.request
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from organizer import Calendar, NotesStore
from usage import UsageTracker
from sheets import SCOPES, ApiError, Backend, UserError

APP_NAME = "Joboard"
APP_ID = "joboard"
DEFAULT_CONFIG = {
    "sheet_id": "",
    "port": 8765,
    "open_browser": True,
}


def app_dir() -> Path:
    """Dossier du .exe une fois compilé (config, identifiants), sinon dossier du script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Dossier des fichiers web embarqués (extraits par PyInstaller dans _MEIPASS)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


BASE = app_dir()
WEB = resource_dir() / "web"
TOKEN = secrets.token_urlsafe(24)    # exigé sur chaque appel API (protège contre les autres sites)
SESSION = secrets.token_hex(6)       # change à chaque lancement : la page se recharge si le serveur redémarre


# ---------------------------------------------------------------------------
# Affichage console
# ---------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def pause_and_exit(code: int) -> None:
    """Garde le terminal ouvert pour que le message reste lisible."""
    try:
        input("\nAppuyez sur Entrée pour fermer cette fenêtre…")
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)


# ---------------------------------------------------------------------------
# Configuration et connexion Google
# ---------------------------------------------------------------------------

def load_config() -> dict:
    path = BASE / "config.json"
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("le contenu n'est pas un objet JSON")
            config.update(data)
        except (OSError, ValueError) as exc:
            say(f"[ERREUR] config.json illisible ({path}) : {exc}")
            pause_and_exit(1)
    else:
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        say(f"config.json créé : {path}")
    return config


SETUP_HELP = """
[CONFIGURATION REQUISE] Aucun identifiant Google trouvé.

Placez UN de ces fichiers dans ce dossier :
  {base}

  • credentials.json      (recommandé) identifiant OAuth « Application de bureau »
                          → au premier lancement, une page Google vous demande d'autoriser l'accès.
  • service_account.json  clé d'un compte de service, avec qui vous avez partagé le Sheet (Éditeur).

La procédure pas à pas est dans README.md, section « Connexion à Google ».
"""


def load_credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    sa_file = BASE / "service_account.json"
    if sa_file.exists():
        from google.oauth2 import service_account
        say("Connexion avec le compte de service (service_account.json)…")
        return service_account.Credentials.from_service_account_file(str(sa_file), scopes=SCOPES)

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_file = BASE / "token.json"
    client_file = BASE / "credentials.json"
    creds = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except (ValueError, OSError):
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError:
            say("L'autorisation Google a expiré ou a été révoquée : nouvelle connexion nécessaire.")
    if not client_file.exists():
        say(SETUP_HELP.format(base=BASE))
        pause_and_exit(1)
    say("Une page Google va s'ouvrir : choisissez le compte propriétaire du Sheet et autorisez l'accès.")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        authorization_prompt_message="Si la page ne s'ouvre pas, copiez ce lien dans votre navigateur :\n{url}\n",
        success_message="Connexion réussie. Vous pouvez fermer cet onglet et revenir à Joboard.",
    )
    token_file.write_text(creds.to_json(), encoding="utf-8")
    say(f"Autorisation enregistrée dans {token_file.name} (ne partagez pas ce fichier).")
    return creds


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

backend: Backend | None = None
tracker: UsageTracker | None = None
notes: NotesStore | None = None
calendar: Calendar | None = None
PORT = 0
DRIVE_URL = "https://drive.google.com/drive/my-drive"

API_FUNCTIONS = {
    "getAllData": lambda: backend.get_all_data(),
    "updateRow": lambda tab, row, key, changes: backend.update_row(tab, row, key, changes),
    "setStatus": lambda tab, row, key, status: backend.set_status(tab, row, key, status),
    "markSpontSent": lambda row, key: backend.mark_spont_sent(row, key),
    "archiveRow": lambda tab, row, key: backend.archive_row(tab, row, key),
    "addReply": lambda tab, row, key, text, status="": backend.add_reply(tab, row, key, text, status),
    "addRow": lambda tab, data: backend.add_row(tab, data),
    "listNotes": lambda: notes.list(),
    "saveNote": lambda note: notes.save(note),
    "deleteNote": lambda note_id: notes.delete(note_id),
    "calendarStatus": lambda: calendar.status(),
    "calendarConnect": lambda: calendar.connect(),
    "calendarPoll": lambda: calendar.poll(),
    "calendarDisconnect": lambda: calendar.disconnect(),
    "calendarEvents": lambda start, end, force=False: calendar.events(start, end, force),
    "calendarSetIcs": lambda url: calendar.set_ics(url),
    "calendarCompose": lambda event_id: calendar.compose(event_id),
    "calendarSave": lambda event: calendar.save(event),
    "calendarDelete": lambda event_id, source: calendar.delete(event_id, source),
    "calendarPush": lambda event_id: calendar.push_local(event_id),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SuiviCandidatures/1.0"

    def log_message(self, fmt, *args):  # journal géré dans _log
        pass

    def _log(self, label: str, status: int, started: float) -> None:
        ms = int((time.perf_counter() - started) * 1000)
        mark = "OK " if status < 400 else "ERR"
        say(f"{time.strftime('%H:%M:%S')}  {mark} {label:<16} {status}  {ms} ms")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, status: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/ping":
            return self._json(200, {"app": APP_ID, "session": SESSION})
        if path == "/api/usage":
            if self.headers.get("X-SC-Token") != TOKEN:
                return self._json(403, {"error": "Session expirée : rechargez la page."})
            available = {}
            return self._json(200, {"result": tracker.summary(available) if tracker else {}})
        if path in ("/", "/index.html"):
            html = (WEB / "index.html").read_text(encoding="utf-8")
            html = html.replace("{{SC_TOKEN}}", TOKEN).replace("{{SC_SESSION}}", SESSION)
            sheet_url = f"https://docs.google.com/spreadsheets/d/{quote(backend.sheet_id, safe='')}/edit" if backend else "https://sheets.google.com/"
            html = html.replace("{{SC_SHEET_URL}}", sheet_url).replace("{{SC_DRIVE_URL}}", escape(DRIVE_URL, quote=True))
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                              {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
        name = path.lstrip("/")
        target = (WEB / name).resolve()
        if name and target.is_file() and WEB.resolve() in target.parents:
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, target.read_bytes(), ctype)
        return self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        started = time.perf_counter()
        path = urlparse(self.path).path
        if not self._host_ok() or path != "/api/call":
            return self._send(404, b"Not found", "text/plain")
        if self.headers.get("X-SC-Token") != TOKEN:
            return self._json(403, {"error": "Session expirée : rechargez la page."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            fn = body.get("fn")
            args = body.get("args") or []
            if fn not in API_FUNCTIONS or not isinstance(args, list):
                raise UserError(f"Fonction inconnue : {fn}")
        except (ValueError, UserError) as exc:
            self._json(400, {"error": str(exc)})
            return self._log("requête invalide", 400, started)
        try:
            result = API_FUNCTIONS[fn](*args)
            self._json(200, {"result": result})
            self._log(fn, 200, started)
        except UserError as exc:
            self._json(400, {"error": str(exc)})
            self._log(fn, 400, started)
            say(f"          → {exc}")
        except ApiError as exc:
            self._json(502, {"error": str(exc)})
            self._log(fn, 502, started)
            say(f"          → {exc}")
        except TypeError as exc:
            self._json(400, {"error": f"Arguments invalides pour {fn}."})
            self._log(fn, 400, started)
            say(f"          → {exc}")
        except Exception as exc:  # erreur inattendue : affichée dans l'interface et détaillée ici
            self._json(500, {"error": f"Erreur interne : {exc}"})
            self._log(fn, 500, started)
            traceback.print_exc()



class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # sous Windows, SO_REUSEADDR laisserait deux instances partager le port


def existing_instance(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main() -> None:
    global backend, tracker, notes, calendar, PORT, DRIVE_URL
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} — fermez cette fenêtre pour arrêter")

    say("=" * 64)
    say(f"  {APP_NAME}")
    say("=" * 64)
    config = load_config()
    wanted = int(config.get("port") or DEFAULT_CONFIG["port"])

    if existing_instance(wanted):
        url = f"http://localhost:{wanted}/"
        say(f"L'application tourne déjà dans une autre fenêtre : ouverture de {url}")
        webbrowser.open(url, new=2)
        time.sleep(3)
        return

    sheet_id = str(config.get("sheet_id") or "").strip()
    drive_url = str(config.get("drive_url") or "").strip()
    if drive_url.startswith("https://"):
        DRIVE_URL = drive_url
    if not sheet_id:
        say("[CONFIGURATION REQUISE] Renseignez sheet_id dans config.json (voir config.example.json).")
        pause_and_exit(1)

    creds = load_credentials()
    from google.auth.transport.requests import AuthorizedSession
    backend = Backend(AuthorizedSession(creds), sheet_id)
    try:
        title = backend.spreadsheet_title()
    except ApiError as exc:
        say(f"\n[ERREUR] {exc}")
        pause_and_exit(1)
    say(f"Connecté au Sheet : {title}")
    tracker = UsageTracker(BASE / "assistant-ia" / "consommation.json")
    tracker.start_live()
    data_dir = BASE / "joboard-donnees"
    notes = NotesStore(data_dir / "notes.json")
    calendar = Calendar(data_dir, config)
    cal = calendar.status()
    say(f"Calendrier : {'Outlook connecté (' + cal['account'] + ')' if cal['connected'] else 'Outlook à connecter' if cal['configured'] else 'Outlook lu via le lien publié' if cal['ics']['set'] else 'lien Outlook à ajouter dans l’onglet Calendrier'}")

    PORT = wanted if port_free(wanted) else 0
    httpd = Server(("127.0.0.1", PORT), Handler)
    PORT = httpd.server_address[1]
    if PORT != wanted:
        say(f"Le port {wanted} est occupé : utilisation du port {PORT}.")
    url = f"http://localhost:{PORT}/"
    say("")
    say(f"  Interface : {url}")
    say("  Laissez cette fenêtre ouverte pendant l'utilisation.")
    say("  Fermez-la (ou Ctrl+C) pour arrêter l'application.")
    say("-" * 64)
    if config.get("open_browser", True):
        webbrowser.open(url, new=2)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        say("Application arrêtée.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        say("\n[ERREUR INATTENDUE]")
        traceback.print_exc()
        pause_and_exit(1)
