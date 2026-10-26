"""Consommation IA : quotas (fenêtres 5 h / 7 jours), heure de réinitialisation et historique.

Quotas mesurés automatiquement, sans envoyer de message ni consommer de quota :
  - en direct : la page de consommation de chaque compte (celle qu'affichent `/usage` dans Claude Code
    et `/status` dans Codex), lue avec la connexion déjà enregistrée par le CLI, toutes les 30 s
    tant que l'application est ouverte et juste après chaque réponse ;
  - à défaut : l'évènement `rate_limit_event` renvoyé par Claude Code à chaque message (mémorisé ici)
    et les fichiers de session Codex ~/.codex/sessions/**/rollout-*.jsonl (évènements `token_count`).
Historique : ~/.claude/projects/**/*.jsonl et les sessions Codex.
Seuls des compteurs (horodatage, nombre de tokens, pourcentages) sont lus : jamais le contenu des conversations.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HISTORY_DAYS = 7
APP_LOG_MAX = 500
LIVE_INTERVAL = 30       # secondes entre deux mesures en direct
LIVE_IDLE = 120          # sans consultation depuis 2 min, la mesure se met en pause
LIVE_MIN_GAP = 5         # une réponse peut déclencher une mesure, pas plus d'une toutes les 5 s
LIVE_BACKOFF = 300       # le service limite les requêtes : on attend 5 min
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_LABELS = {"five_hour": "Fenêtre 5 h", "seven_day": "Semaine", "seven_day_opus": "Semaine (Opus)", "seven_day_sonnet": "Semaine (Sonnet)"}

try:  # le magasin de certificats de Python sous Windows peut contenir une racine expirée
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10, context=_SSL) as r:
        return json.loads(r.read())


def _window_status(utilization) -> str:
    if not isinstance(utilization, (int, float)):
        return "allowed"
    return "rejected" if utilization >= 1 else "allowed_warning" if utilization >= 0.75 else "allowed"


def _codex_label(minutes) -> str:
    return "Fenêtre 5 h" if minutes == 300 else "Semaine" if minutes == 10080 else f"Fenêtre {round((minutes or 0) / 60)} h"


def _iso_to_epoch(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class UsageTracker:
    def __init__(self, state_file: Path, home: Path | None = None):
        self.state_file = state_file
        self.home = home or Path.home()
        self.lock = threading.Lock()
        self._file_cache: dict = {}  # chemin -> (mtime, taille, [(epoch, tokens)])
        self._quota_cache: dict = {}
        self._history_cache: dict = {}
        self._codex_file_list: tuple = (0, [])
        self._live: dict = {}        # provider -> (mesuré à, fenêtres, offre)
        self._live_error: dict = {}  # provider -> message lisible
        self._live_next: dict = {}   # provider -> prochaine mesure autorisée
        self._viewed = 0.0
        self._wake = threading.Event()
        self.state = {"claude": {"limits": None, "measuredAt": None}, "app": []}
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.state.update({k: v for k, v in data.items() if k in ("claude", "app")})
        except (OSError, ValueError):
            pass

    # ---------- Enregistrement depuis l'application ----------

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def record_claude_limits(self, info: dict) -> None:
        """info = rate_limit_info de Claude Code (contient unifiedWindows)."""
        if not isinstance(info, dict):
            return
        with self.lock:
            self.state["claude"] = {"limits": info, "measuredAt": time.time()}
            self._save()

    def record_call(self, provider: str, model: str, tokens: dict, duration_ms: int = 0, cost: float | None = None) -> None:
        with self.lock:
            self.state["app"].append({
                "ts": time.time(), "provider": provider, "model": model or "",
                "input": int(tokens.get("input", 0)), "output": int(tokens.get("output", 0)),
                "cache": int(tokens.get("cache", 0)), "ms": int(duration_ms or 0),
                "cost": round(cost, 5) if isinstance(cost, (int, float)) else None,
            })
            self.state["app"] = self.state["app"][-APP_LOG_MAX:]
            self._history_cache.pop(provider, None)
            self._save()

    # ---------- Quotas en direct ----------

    def _fetch_claude(self):
        creds = json.loads((self.home / ".claude" / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
        if (creds.get("expiresAt") or 0) / 1000 < time.time():
            raise PermissionError("connexion Claude Code à renouveler : envoyez un message à Claude ou lancez « claude »")
        data = _get_json(CLAUDE_USAGE_URL, {"Authorization": "Bearer " + creds["accessToken"],
                                            "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code"})
        windows = []
        for key, label in CLAUDE_LABELS.items():
            w = data.get(key)
            if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
                continue
            used = w["utilization"] / 100
            windows.append({"key": key, "label": label, "utilization": used,
                            "resetsAt": _iso_to_epoch(w.get("resets_at")), "status": _window_status(used)})
        return windows, None

    def _fetch_codex(self):
        auth = json.loads((self.home / ".codex" / "auth.json").read_text(encoding="utf-8"))["tokens"]
        data = _get_json(CODEX_USAGE_URL, {"Authorization": "Bearer " + auth["access_token"],
                                           "ChatGPT-Account-Id": auth.get("account_id") or "", "User-Agent": "codex_cli_rs"})
        rl = data.get("rate_limit") or {}
        windows = []
        for key in ("primary_window", "secondary_window"):
            w = rl.get(key)
            if not isinstance(w, dict):
                continue
            used = w.get("used_percent")
            used = used / 100 if isinstance(used, (int, float)) else None
            windows.append({"key": key, "label": _codex_label((w.get("limit_window_seconds") or 0) // 60), "utilization": used,
                            "resetsAt": w.get("reset_at"), "status": _window_status(used)})
        return windows, data.get("plan_type")

    def refresh_live(self, force: bool = False) -> None:
        """Mesure les quotas des deux comptes ; une erreur garde la dernière mesure et l'explique."""
        for provider, fetch in (("claude", self._fetch_claude), ("chatgpt", self._fetch_codex)):
            now = time.time()
            if not force and now < self._live_next.get(provider, 0):
                continue
            self._live_next[provider] = now + LIVE_MIN_GAP
            try:
                windows, plan = fetch()
                with self.lock:
                    self._live[provider] = (time.time(), windows, plan)
                    self._live_error.pop(provider, None)
            except (FileNotFoundError, KeyError):
                self._live_error[provider] = "CLI non connecté sur ce PC"
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self._live_next[provider] = now + LIVE_BACKOFF
                    self._live_error[provider] = "mesure en pause quelques minutes (trop de requêtes)"
                else:
                    self._live_error[provider] = ("connexion à renouveler dans le CLI" if exc.code in (401, 403)
                                                  else f"service indisponible ({exc.code})")
            except PermissionError as exc:
                self._live_error[provider] = str(exc)
            except (OSError, ValueError, TypeError) as exc:
                self._live_error[provider] = "mesure impossible : " + (str(exc)[:120] or "erreur réseau")

    def wake_live(self) -> None:
        """Après une réponse : mesure tout de suite plutôt qu'au prochain passage."""
        self._wake.set()

    def start_live(self, interval: int = LIVE_INTERVAL) -> None:
        def loop():
            while True:
                if time.time() - self._viewed < LIVE_IDLE:
                    self.refresh_live()
                self._wake.wait(interval)
                self._wake.clear()
        threading.Thread(target=loop, name="mesure-quotas", daemon=True).start()

    # ---------- Quotas ----------

    def _claude_windows(self):
        claude = self.state.get("claude") or {}
        live = self._live.get("claude")
        if live and live[0] >= (claude.get("measuredAt") or 0):
            return [dict(w) for w in live[1]], live[0], None
        info = claude.get("limits") or {}
        windows = []
        labels = CLAUDE_LABELS
        for key, w in (info.get("unifiedWindows") or {}).items():
            if not isinstance(w, dict):
                continue
            windows.append({
                "key": key, "label": labels.get(key, key.replace("_", " ")),
                "utilization": w.get("utilization"), "resetsAt": w.get("resetsAt"),
                "status": w.get("status") or "allowed",
            })
        if not windows and info.get("rateLimitType"):
            windows.append({"key": info["rateLimitType"], "label": labels.get(info["rateLimitType"], info["rateLimitType"]),
                            "utilization": info.get("utilization"), "resetsAt": info.get("resetsAt"), "status": info.get("status") or "allowed"})
        windows.sort(key=lambda w: 0 if w["key"] == "five_hour" else 1)
        return windows, claude.get("measuredAt"), None

    def _codex_files(self, days: int):
        root = self.home / ".codex" / "sessions"
        if not root.is_dir():
            return []
        now = time.time()
        limit = now - days * 86400
        at, cached = self._codex_file_list
        if now - at < 1:
            return [p for m, p in cached if m >= limit]
        files = []
        for p in root.rglob("rollout-*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= now - 30 * 86400:
                files.append((m, p))
        files.sort(reverse=True)
        self._codex_file_list = (now, files)
        return [p for m, p in files if m >= limit]

    def _latest_codex_limits(self, path: Path):
        """Read appended quota events only; never parse conversation content."""
        try:
            stat = path.stat()
            old = self._quota_cache.get(str(path))
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if old and old[0] == stamp:
                return old[2]
            offset, latest = (old[1], old[2]) if old else (0, None)
            if old and (old[0][2] != stat.st_ino or stat.st_size <= offset):
                offset, latest = 0, None
            with path.open('rb') as f:
                f.seek(offset)
                while True:
                    start = f.tell()
                    line = f.readline()
                    if not line or not line.endswith(b'\n'):
                        offset = start  # unfinished event: read it on the next update
                        break
                    offset = f.tell()
                    if b'"rate_limits"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    payload = e.get('payload') or {}
                    limits = payload.get('rate_limits') or (payload.get('info') or {}).get('rate_limits')
                    if limits:
                        latest = (limits, _iso_to_epoch(e.get('timestamp')) or stat.st_mtime)
            self._quota_cache[str(path)] = (stamp, offset, latest)
            return latest
        except OSError:
            return None

    def _codex_windows(self):
        candidates = [self._latest_codex_limits(path) for path in self._codex_files(30)[:15]]
        candidates = [entry for entry in candidates if entry]
        live = self._live.get("chatgpt")
        if live and (not candidates or live[0] >= max(entry[1] for entry in candidates)):
            return [dict(w) for w in live[1]], live[0], live[2]
        if not candidates:
            return [], None, None
        rl, measured = max(candidates, key=lambda entry: entry[1])
        windows = []
        for slot in ("primary", "secondary"):
            w = rl.get(slot) or {}
            if not w:
                continue
            minutes = w.get("window_minutes") or 0
            used = w.get("used_percent")
            reached = rl.get("rate_limit_reached_type")
            windows.append({
                "key": slot,
                "label": _codex_label(minutes),
                "utilization": (used / 100) if isinstance(used, (int, float)) else None,
                "resetsAt": w.get("resets_at") or (measured + w["resets_in_seconds"] if w.get("resets_in_seconds") else None),
                "status": "rejected" if (isinstance(used, (int, float)) and used >= 100) or (reached and slot in str(reached)) else
                          "allowed_warning" if isinstance(used, (int, float)) and used >= 75 else "allowed",
            })
        return windows, measured, rl.get("plan_type")

    # ---------- Historique (toutes sessions, pas seulement l'application) ----------

    def _scan(self, path: Path, parser):
        try:
            st = path.stat()
        except OSError:
            return []
        cached = self._file_cache.get(str(path))
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]
        points = parser(path)
        self._file_cache[str(path)] = (st.st_mtime, st.st_size, points)
        return points

    @staticmethod
    def _parse_claude_file(path: Path):
        points, seen = [], set()
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    msg = e.get("message") or {}
                    usage = msg.get("usage") or {}
                    key = msg.get("id") or e.get("uuid")
                    if not usage or key in seen:
                        continue
                    seen.add(key)
                    ts = _iso_to_epoch(e.get("timestamp"))
                    if ts:
                        tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
                        points.append((ts, tokens))
        except OSError:
            pass
        return points

    @staticmethod
    def _parse_codex_file(path: Path):
        points = []
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"token_count"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    last = ((e.get("payload") or {}).get("info") or {}).get("last_token_usage") or {}
                    ts = _iso_to_epoch(e.get("timestamp"))
                    if ts and last:
                        tokens = (last.get("input_tokens") or 0) - (last.get("cached_input_tokens") or 0) + (last.get("output_tokens") or 0)
                        points.append((ts, max(tokens, 0)))
        except OSError:
            pass
        return points

    def _history(self, provider: str):
        cached = self._history_cache.get(provider)
        if cached and time.time() - cached[0] < 10:
            return cached[1]
        limit = time.time() - HISTORY_DAYS * 86400
        if provider == "claude":
            root = self.home / ".claude" / "projects"
            files = []
            if root.is_dir():
                for p in root.rglob("*.jsonl"):
                    try:
                        if p.stat().st_mtime >= limit:
                            files.append(p)
                    except OSError:
                        continue
            points = [pt for p in files for pt in self._scan(p, self._parse_claude_file)]
        else:
            points = [pt for p in self._codex_files(HISTORY_DAYS + 1) for pt in self._scan(p, self._parse_codex_file)]
        now = datetime.now()
        hour0 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        day0 = (now - timedelta(days=HISTORY_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        hourly, daily = [0] * 24, [0] * HISTORY_DAYS
        messages_today = 0
        for ts, tokens in points:
            dt = datetime.fromtimestamp(ts)
            h = int((dt - hour0).total_seconds() // 3600)
            if 0 <= h < 24:
                hourly[h] += tokens
            d = (dt.date() - day0.date()).days
            if 0 <= d < HISTORY_DAYS:
                daily[d] += tokens
                if d == HISTORY_DAYS - 1:
                    messages_today += 1
        result = {
            "hourly": hourly, "hourStart": hour0.timestamp(),
            "daily": daily, "dayStart": day0.timestamp(),
            "today": daily[-1], "week": sum(daily), "messagesToday": messages_today,
        }
        self._history_cache[provider] = (time.time(), result)
        return result

    # ---------- Synthèse ----------

    def summary(self, available: dict) -> dict:
        out = {}
        first_view = time.time() - self._viewed >= LIVE_IDLE
        self._viewed = time.time()
        if first_view:
            self._wake.set()  # retour sur l'application après une pause : mesure immédiate
        with self.lock:
            app = list(self.state.get("app") or [])
            sources = {"claude": self._claude_windows, "chatgpt": self._codex_windows}
            for provider, read in sources.items():
                windows, measured, plan = read()
                now = time.time()
                for w in windows:
                    if isinstance(w.get("resetsAt"), (int, float)) and w["resetsAt"] <= now:
                        w.update(utilization=0, status="reset")  # fenêtre déjà réinitialisée depuis la mesure
                blocked = [w for w in windows if w["status"] == "rejected" or (isinstance(w.get("utilization"), (int, float)) and w["utilization"] >= 1)]
                today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                mine = [r for r in app if r.get("provider") == provider and r.get("ts", 0) >= today0]
                out[provider] = {
                    "available": bool(available.get(provider)),
                    "plan": plan,
                    "windows": windows,
                    "measuredAt": measured,
                    "live": provider in self._live and not self._live_error.get(provider),
                    "liveError": self._live_error.get(provider),
                    "blocked": bool(blocked),
                    "availableAt": max((w["resetsAt"] for w in blocked if w.get("resetsAt")), default=None),
                    "history": self._history(provider),
                    "app": {
                        "messagesToday": len(mine),
                        "tokensToday": sum(r.get("input", 0) + r.get("output", 0) + r.get("cache", 0) for r in mine),
                        "lastModel": mine[-1]["model"] if mine else "",
                    },
                }
        return out


def _fmt_debug(summary: dict) -> str:  # pour un contrôle rapide en ligne de commande
    return json.dumps(summary, ensure_ascii=False, indent=1)[:3000]


if __name__ == "__main__":
    tracker = UsageTracker(Path(os.environ.get("TEMP", ".")) / "sc-usage-test.json")
    print(_fmt_debug(tracker.summary({"claude": True, "chatgpt": True})))
