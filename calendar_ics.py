"""Lecture d'un calendrier publié au format iCalendar (lien ICS d'Outlook.com) et lien d'ajout Outlook.

Outlook.com peut publier un calendrier sous forme de lien privé (.ics) : Joboard le lit sans
identifiant d'application Microsoft. Les heures sont converties dans le fuseau de l'utilisateur ;
les répétitions courantes (quotidienne, hebdomadaire, mensuelle, annuelle) sont développées.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode

try:  # Windows n'a pas de base de fuseaux : le paquet tzdata la fournit (et PyInstaller l'embarque).
    import tzdata  # noqa: F401
except ImportError:  # pragma: no cover
    pass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Noms de fuseaux Windows utilisés par Outlook → noms IANA.
WINDOWS_TZ = {
    "Romance Standard Time": "Europe/Paris", "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik", "UTC": "UTC",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev", "GTB Standard Time": "Europe/Bucharest",
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "Tokyo Standard Time": "Asia/Tokyo", "China Standard Time": "Asia/Shanghai", "India Standard Time": "Asia/Kolkata",
    "Singapore Standard Time": "Asia/Singapore", "AUS Eastern Standard Time": "Australia/Sydney",
}
DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MAX_INSTANCES = 2000


def zone(name: str | None, fallback: ZoneInfo) -> ZoneInfo:
    name = (name or "").strip().strip('"')
    for candidate in (WINDOWS_TZ.get(name), name):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return fallback


def unfold(text: str) -> list[str]:
    """Les lignes longues continuent sur la suivante quand celle-ci commence par une espace."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def parse_line(line: str):
    """« NOM;PARAM=x;PARAM2="y":valeur » → (NOM, {PARAM: x}, valeur)."""
    head, sep, value = line.partition(":")
    if not sep:
        return None
    # Un « : » peut apparaître dans un paramètre entre guillemets.
    while head.count('"') % 2:
        more, sep, value = value.partition(":")
        head += ":" + more
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_time(value: str, params: dict, tz: ZoneInfo):
    """Renvoie (datetime aware, False) pour une heure, (date, True) pour une journée entière."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z)?", value)
    if not m:
        raise ValueError(value)
    y, mo, d, h, mi, s, utc = m.groups()
    naive = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    if utc:
        return naive.replace(tzinfo=timezone.utc), False
    return naive.replace(tzinfo=zone(params.get("TZID"), tz)), False


def parse_calendar(text: str, tz: ZoneInfo) -> list[dict]:
    """Évènements bruts : début / fin (aware ou date), règles de répétition, exceptions."""
    events, current, depth = [], None, 0
    for line in unfold(text):
        parsed = parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN" and value.upper() == "VEVENT":
            current, depth = {"exdates": set(), "params": {}}, 0
            continue
        if current is None:
            continue
        if name == "BEGIN":  # VALARM imbriqué
            depth += 1
            continue
        if name == "END":
            if depth:
                depth -= 1
            elif value.upper() == "VEVENT":
                if "start" in current:
                    events.append(current)
                current = None
            continue
        if depth:
            continue
        try:
            if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                current[{"DTSTART": "start", "DTEND": "end", "RECURRENCE-ID": "recurrence"}[name]], allday = parse_time(value, params, tz)
                if name == "DTSTART":
                    current["allDay"] = allday
            elif name == "EXDATE":
                for v in value.split(","):
                    current["exdates"].add(parse_time(v, params, tz)[0])
        except (ValueError, IndexError):
            continue
        if name == "SUMMARY":
            current["title"] = unescape(value)
        elif name == "LOCATION":
            current["location"] = unescape(value)
        elif name == "DESCRIPTION":
            current["description"] = unescape(value)
        elif name == "UID":
            current["uid"] = value
        elif name == "RRULE":
            current["rrule"] = dict(p.partition("=")[::2] for p in value.upper().split(";") if "=" in p)
        elif name == "STATUS":
            current["status"] = value.upper()
        elif name == "DURATION":
            current["duration"] = value
    return events


def parse_duration(value: str) -> timedelta:
    m = re.fullmatch(r"(-)?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value or "")
    if not m:
        return timedelta(0)
    sign, w, d, h, mi, s = m.groups()
    delta = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -delta if sign else delta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date | None:
    if n > 0:
        first = date(year, month, 1)
        day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    else:
        nxt = date(year + month // 12, month % 12 + 1, 1)
        last = nxt - timedelta(days=1)
        day = last - timedelta(days=(last.weekday() - weekday) % 7 + 7 * (-n - 1))
    return day if day.month == month else None


def occurrences(start, rule: dict, until_day: date):
    """Débuts successifs d'un évènement répété (règles courantes d'Outlook)."""
    freq = rule.get("FREQ")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            until = parse_time(rule["UNTIL"], {}, timezone.utc)[0]
        except ValueError:
            until = None
    is_dt = isinstance(start, datetime)
    day0 = start.date() if is_dt else start

    def at(day: date):
        return datetime.combine(day, start.timetz()).replace(tzinfo=start.tzinfo) if is_dt else day

    def past_until(value) -> bool:
        if until is None:
            return False
        if isinstance(until, datetime) and is_dt:
            return value > until
        return (value.date() if is_dt else value) > (until.date() if isinstance(until, datetime) else until)

    produced = 0
    byday = [d for d in rule.get("BYDAY", "").split(",") if d]
    for step in range(MAX_INSTANCES):
        if freq == "DAILY":
            days = [day0 + timedelta(days=step * interval)]
        elif freq == "WEEKLY":
            week = day0 - timedelta(days=day0.weekday()) + timedelta(weeks=step * interval)
            wanted = [DAYS.index(d[-2:]) for d in byday if d[-2:] in DAYS] or [day0.weekday()]
            days = [week + timedelta(days=w) for w in sorted(set(wanted))]
        elif freq in ("MONTHLY", "YEARLY"):
            months = step * interval * (12 if freq == "YEARLY" else 1)
            y, m = day0.year + (day0.month - 1 + months) // 12, (day0.month - 1 + months) % 12 + 1
            if byday:
                days = []
                for d in byday:
                    mm = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", d)
                    if mm:
                        n = int(rule.get("BYSETPOS") or mm.group(1) or 1)
                        hit = _nth_weekday(y, m, DAYS.index(mm.group(2)), n)
                        if hit:
                            days.append(hit)
            else:
                try:
                    days = [date(y, m, int(rule.get("BYMONTHDAY") or day0.day))]
                except ValueError:  # 31 d'un mois plus court
                    days = []
        else:
            yield start
            return
        for day in sorted(days):
            if day < day0:
                continue
            value = at(day)
            if past_until(value) or day > until_day or (count is not None and produced >= count):
                return
            produced += 1
            yield value


def to_local(value, tz: ZoneInfo) -> str:
    if isinstance(value, datetime):
        return value.astimezone(tz).strftime("%Y-%m-%dT%H:%M")
    return value.isoformat()


def expand(raw: list[dict], start: date, end: date, tz: ZoneInfo) -> list[dict]:
    """Évènements entre deux dates (incluses), avec les répétitions et leurs exceptions."""
    overrides = {(e.get("uid"), e["recurrence"]) for e in raw if e.get("recurrence") is not None}
    out = []
    for e in raw:
        if e.get("status") == "CANCELLED":
            continue
        s0 = e["start"]
        if "end" in e:
            length = e["end"] - s0
        elif e.get("duration"):
            length = parse_duration(e["duration"])
        else:
            length = timedelta(days=1) if e.get("allDay") else timedelta(0)
        starts = [s0] if not e.get("rrule") or e.get("recurrence") is not None else occurrences(s0, e["rrule"], end)
        for s in starts:
            if e.get("rrule") and e.get("recurrence") is None and (s in e["exdates"] or (e.get("uid"), s) in overrides):
                continue
            finish = s + length
            first = s.astimezone(tz).date() if isinstance(s, datetime) else s
            last = finish.astimezone(tz).date() if isinstance(finish, datetime) else finish - timedelta(days=1)
            if last < start or first > end:
                continue
            ident = hashlib.sha1(f"{e.get('uid', '')}|{to_local(s, tz)}".encode("utf-8")).hexdigest()[:24]
            title = e.get("title") or "(Sans titre)"
            out.append({"id": "ics-" + ident, "source": "ics", "title": title,
                        "start": to_local(s, tz), "end": to_local(finish, tz), "allDay": bool(e.get("allDay")),
                        "location": e.get("location", ""), "online": "", "description": e.get("description", "")[:4000],
                        "webLink": "https://outlook.live.com/calendar/",
                        "kind": "entretien" if re.search(r"entretien|interview|recrut", title, re.I) else "outlook",
                        "link": None, "reminder": None, "cancelled": False})
    return out


def compose_link(event: dict, tz: ZoneInfo) -> str:
    """Lien qui ouvre Outlook.com avec l'évènement pré-rempli : il reste à cliquer sur Enregistrer."""
    def stamp(value: str) -> str:
        if event["allDay"]:
            return value[:10]
        local = datetime.fromisoformat(value).replace(tzinfo=tz)
        return local.isoformat(timespec="seconds")
    params = {"path": "/calendar/action/compose", "rru": "addevent", "subject": event["title"],
              "startdt": stamp(event["start"]), "enddt": stamp(event["end"]),
              "allday": "true" if event["allDay"] else "false",
              "location": event.get("location") or "", "body": event.get("description") or ""}
    return "https://outlook.live.com/calendar/0/deeplink/compose?" + urlencode(params, quote_via=quote)
