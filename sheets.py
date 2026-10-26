"""Accès au Google Sheet de recherche d'emploi via l'API Google Sheets.

Règles appliquées partout :
  - les colonnes sont repérées par leur NOM (ligne 1), jamais par leur position ;
  - aucune ligne n'est supprimée (archivage = changement de statut) ;
  - une cellule contenant une formule n'est jamais écrite ;
  - les textes sont écrits comme du texte (jamais interprétés comme formule) ;
  - les dates sont écrites comme de vraies dates au format JJ/MM/AAAA ;
  - toutes les écritures sont sérialisées par un verrou et relisent la ligne à jour ;
  - aucun envoi d'email ni de message.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from datetime import date, timedelta
from urllib.parse import quote

API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets/"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
STATUS_TAB = "Statuts"
DASHBOARD_TAB = "Tableau de bord"
EPOCH = date(1899, 12, 30)  # jour 0 des dates Google Sheets
META_PREFIX = "suivi_candidatures_max_id_"

# Formules qui « débordent » sur d'autres cellules : toute la colonne est protégée.
SPILL_RE = re.compile(r"ARRAYFORMULA\s*\(|^\s*=\s*(MAP|BYROW|QUERY|FILTER|SORT|UNIQUE|IMPORTRANGE)\s*\(", re.I)
HYPERLINK_RE = re.compile(r'HYPERLINK\s*\(\s*"([^"]+)"', re.I)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FR_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
NUMBER_RE = re.compile(r"^-?\d+([.,]\d+)?$")

CELL_FIELDS = (
    "userEnteredValue,effectiveValue,formattedValue,hyperlink,dataValidation,"
    "textFormatRuns(format(link(uri))),effectiveFormat(numberFormat(type)),userEnteredFormat(numberFormat)"
)
GRID_FIELDS = (
    "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),"
    f"data(rowData(values({CELL_FIELDS}))))"
)
META_FIELDS = (
    "properties(title,timeZone),sheets(properties(sheetId,title)),"
    "developerMetadata(metadataId,metadataKey,metadataValue)"
)

TABS = {
    "offres": {
        "name": "Offres",
        "columns": ["Statut", "Lien pour postuler", "ID offre", "Entreprise", "Poste", "Correspondance CV",
                    "Localisation", "Contrat", "Date publication", "Salaire / indemnité", "Résumé du poste",
                    "Préparation entretien", "Processus / contact", "Date candidature", "Date relance",
                    "Prochaine action", "Lettre de motivation", "Lien lettre DOCX", "CV .tex (Drive)", "CV PDF (Drive)"],
        "dateCols": ["Date publication", "Date candidature", "Date relance"],
        "idCol": "ID offre",
        "idPrefix": "JOB-",
        "keyCols": ["ID offre", "Entreprise", "Poste"],
        "dupRules": [["Lien pour postuler"], ["Entreprise", "Poste", "Lien pour postuler"]],
        "required": ["Entreprise", "Poste"],
        "archiveStatus": "Retirée",
        "statuses": ["À postuler", "Dossier en préparation", "Candidature envoyée", "Contact recruteur",
                     "Attente de retour", "Relance", "Entretien", "Entretien RH", "Test technique", "Entretien technique",
                     "Entretien final", "Offre", "Négociation", "Contrat signé", "Refus", "Sans réponse", "Retirée",
                     "Offre clôturée"],
    },
    "spont": {
        "name": "Spontanées",
        "columns": ["Statut", "ID", "Priorité", "Entreprise", "Pays", "Poste / rôle proposé", "Contact",
                    "Canal (email ou formulaire)", "Langue", "Préparation", "Date d'envoi", "Relance prévue",
                    "Prochaine action", "Angle & points à vérifier", "Mail à envoyer / relance", "Mail envoyé",
                    "Dossier Drive", "CV PDF", "CV TEX", "Lettre DOCX"],
        "dateCols": ["Date d'envoi", "Relance prévue"],
        "idCol": "ID",
        "idPrefix": "SP-",
        "keyCols": ["ID", "Entreprise"],
        "dupRules": [["Entreprise", "Poste / rôle proposé"]],
        "required": ["Entreprise"],
        "archiveStatus": "Abandonnée",
        "statuses": ["À qualifier", "À préparer", "Dossier prêt", "Candidature envoyée", "Relance",
                     "Réponse positive", "Entretien", "Refus", "Sans réponse", "Abandonnée"],
    },
    "contacts": {
        "name": "Contacts",
        "columns": ["Statut", "Projet", "Priorité", "Entreprise", "Nom", "Poste / rôle", "Type de contact",
                    "Niveau", "Langue", "Date 1er contact", "Relance prévue", "Canal conseillé", "Email",
                    "Fiabilité email", "Note / message LinkedIn", "Email de prospection", "Notes / réponse",
                    "Prochain message à envoyer"],
        "dateCols": ["Date 1er contact", "Relance prévue"],
        "keyCols": ["Entreprise", "Nom"],
        "dupRules": [["Nom", "Entreprise"]],
        "required": ["Nom", "Entreprise"],
        "archiveStatus": "Refus / pas de VIE",
        "notesCol": "Notes / réponse",
        "statuses": ["À contacter", "Invitation envoyée", "Connecté", "Message envoyé", "Email envoyé",
                     "Relancé", "Réponse positive", "Entretien", "Pas de réponse", "Refus / pas de VIE"],
    },
    "vie": {
        "name": "Entreprises VIE NY",
        "columns": ["Statut", "Priorité", "Entreprise", "Offre VIE ouverte", "Deadline", "Nb contacts",
                    "Contactés", "Prochaine action", "Présence NYC", "Preuve VIE US/NYC", "Angle à mettre en avant",
                    "Qui viser", "Source"],
        "dateCols": ["Deadline"],
        "keyCols": ["Entreprise"],
        "dupRules": [["Entreprise"]],
        "required": ["Entreprise"],
        "archiveStatus": "Abandonné",
        "statuses": ["À postuler", "À évaluer", "À contacter", "En cours", "Candidature envoyée", "Entretien",
                     "Offre", "Abandonné"],
    },
}


class UserError(Exception):
    """Erreur métier : le message est affiché tel quel dans l'interface."""


class ApiError(Exception):
    """Erreur renvoyée par l'API Google Sheets."""


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def norm(s) -> str:
    """Minuscules, sans accents ni ponctuation."""
    s = unicodedata.normalize("NFD", "" if s is None else str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def strip_desc(s) -> str:
    """« Relance prévue (formule) » → « Relance prévue »."""
    return re.sub(r"\s*\(.*$", "", "" if s is None else str(s))


def find_col(headers, name) -> int:
    n = norm(name)
    if not n:
        return -1
    for i, h in enumerate(headers):
        if norm(h) == n:
            return i
    ns = norm(strip_desc(name))
    if not ns:
        return -1
    for i, h in enumerate(headers):
        if h and norm(strip_desc(h)) == ns:
            return i
    return -1


def a1(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def serial_to_iso(n) -> str:
    return (EPOCH + timedelta(days=int(n // 1))).isoformat()


def to_serial(d: date) -> int:
    return (d - EPOCH).days


def cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def is_empty(v) -> bool:
    return v is None or v == ""


class Cell:
    __slots__ = ("value", "formula", "link", "dv", "is_date", "num_format")

    def __init__(self, value="", formula="", link="", dv=None, is_date=False, num_format=None):
        self.value = value
        self.formula = formula
        self.link = link
        self.dv = dv
        self.is_date = is_date
        self.num_format = num_format


EMPTY = Cell()


def parse_cell(c: dict | None) -> Cell:
    c = c or {}
    uev = c.get("userEnteredValue") or {}
    ev = c.get("effectiveValue") or {}
    formula = uev.get("formulaValue", "")
    nf_type = ((c.get("effectiveFormat") or {}).get("numberFormat") or {}).get("type", "")
    is_date = False
    if "numberValue" in ev:
        n = ev["numberValue"]
        if nf_type in ("DATE", "DATE_TIME"):
            value, is_date = serial_to_iso(n), True
        else:
            value = int(n) if float(n).is_integer() else n
    elif "boolValue" in ev:
        value = ev["boolValue"]
    elif "stringValue" in ev:
        value = ev["stringValue"]
    elif "errorValue" in ev:
        value = c.get("formattedValue", "#ERREUR")
    else:
        value = ""
    link = c.get("hyperlink") or ""
    if not link:
        for run in c.get("textFormatRuns") or []:
            uri = ((run.get("format") or {}).get("link") or {}).get("uri")
            if uri:
                link = uri
                break
    if not link and formula:
        m = HYPERLINK_RE.search(formula)
        if m:
            link = m.group(1)
    num_format = (c.get("userEnteredFormat") or {}).get("numberFormat")
    return Cell(value, formula, link, c.get("dataValidation"), is_date, num_format)


def cell_request(sheet_id: int, r0: int, c: int, value, num_format=None) -> dict:
    """Requête updateCells pour la ligne de données r0 (0 = 2e ligne du Sheet)."""
    cell: dict = {}
    fields = ["userEnteredValue"]
    if isinstance(value, bool):
        cell["userEnteredValue"] = {"boolValue": value}
    elif isinstance(value, date):
        cell["userEnteredValue"] = {"numberValue": to_serial(value)}
        num_format = {"type": "DATE", "pattern": "dd/mm/yyyy"}
    elif isinstance(value, (int, float)):
        cell["userEnteredValue"] = {"numberValue": value}
    elif value != "":
        cell["userEnteredValue"] = {"stringValue": str(value)}  # jamais interprété comme formule
    if num_format:
        cell["userEnteredFormat"] = {"numberFormat": num_format}
        fields.append("userEnteredFormat.numberFormat")
    return {"updateCells": {
        "rows": [{"values": [cell]}],
        "fields": ",".join(fields),
        "start": {"sheetId": sheet_id, "rowIndex": r0 + 1, "columnIndex": c},
    }}


def grid_range(sheet_id, row, col, rows=1, cols=1) -> dict:
    return {"sheetId": sheet_id, "startRowIndex": row, "endRowIndex": row + rows,
            "startColumnIndex": col, "endColumnIndex": col + cols}


# ---------------------------------------------------------------------------
# Onglet chargé en mémoire
# ---------------------------------------------------------------------------

class Tab:
    def __init__(self, key: str, sheet: dict):
        self.key = key
        self.cfg = TABS[key]
        props = sheet["properties"]
        self.sheet_id = props["sheetId"]
        self.title = props["title"]
        self.row_count = (props.get("gridProperties") or {}).get("rowCount", 0)
        data = (sheet.get("data") or [{}])[0]
        grid = [[parse_cell(c) for c in (r.get("values") or [])] for r in (data.get("rowData") or [])]
        head = grid[0] if grid else []
        headers = [cell_str(c.value).strip() for c in head]
        while headers and not headers[-1]:
            headers.pop()
        self.headers = headers
        self.ncol = len(headers)
        self.rows = grid[1:]
        self.spill = {c for c in range(min(self.ncol, len(head))) if head[c].formula and SPILL_RE.search(head[c].formula)}
        self.date_idx = set()
        for row in self.rows:
            for c, cell in enumerate(row[:self.ncol]):
                if cell.formula and SPILL_RE.search(cell.formula):
                    self.spill.add(c)
                if cell.is_date:
                    self.date_idx.add(c)
        for name in self.cfg["dateCols"]:
            i = self.col(name)
            if i >= 0:
                self.date_idx.add(i)

    def col(self, name) -> int:
        return find_col(self.headers, name)

    def cell(self, r0: int, c: int) -> Cell:
        if 0 <= r0 < len(self.rows) and c < len(self.rows[r0]):
            return self.rows[r0][c]
        return EMPTY

    def is_formula(self, r0: int, c: int) -> bool:
        return c in self.spill or bool(self.cell(r0, c).formula)

    def has_content(self, r0: int) -> bool:
        for c in range(self.ncol):
            if self.is_formula(r0, c):
                continue
            v = self.cell(r0, c).value
            if v not in ("", None, False):
                return True
        return False

    def get(self, r0: int, name):
        c = self.col(name)
        return "" if c < 0 else self.cell(r0, c).value

    def build_row(self, r0: int) -> dict | None:
        if not self.has_content(r0):
            return None
        row = {"_row": r0 + 2, "_f": [], "_links": {}}
        for c, h in enumerate(self.headers):
            if not h:
                continue
            cell = self.cell(r0, c)
            if self.is_formula(r0, c):
                row["_f"].append(h)
            row[h] = "" if cell.value is None else cell.value
            if cell.link:
                row["_links"][h] = cell.link
        return row


def list_from_dv(dv, range_reader):
    """Renvoie (liste ou None, est_case_à_cocher, stricte)."""
    if not dv:
        return None, False, False
    cond = dv.get("condition") or {}
    kind = cond.get("type")
    strict = bool(dv.get("strict"))
    values = [v.get("userEnteredValue", "") for v in cond.get("values") or []]
    if kind == "BOOLEAN":
        return None, True, strict
    if kind == "ONE_OF_LIST":
        return [v.strip() for v in values if v.strip()], False, strict
    if kind == "ONE_OF_RANGE" and values:
        return range_reader(values[0].lstrip("=")), False, strict
    return None, False, strict


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class Backend:
    def __init__(self, session, sheet_id: str):
        """session : objet de type requests.Session authentifié (AuthorizedSession)."""
        self.session = session
        self.sheet_id = sheet_id
        self.lock = threading.RLock()
        self._titles: dict | None = None
        self._statuts: dict | None = None
        self._range_cache: dict = {}
        self._dev_meta: dict = {}
        self._status_help: dict = {}

    # ---------- HTTP ----------

    def _call(self, method: str, suffix: str = "", **kwargs) -> dict:
        try:
            r = self.session.request(method, API_ROOT + self.sheet_id + suffix, timeout=60, **kwargs)
        except Exception as exc:
            # Réseau, DNS, proxy ou jeton refusé : message clair. Toute autre erreur remonte telle quelle.
            if type(exc).__module__.split(".")[0] in ("requests", "urllib3", "google", "socket", "ssl"):
                raise ApiError(f"Connexion à Google impossible : {exc}") from exc
            raise
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:300]
            if r.status_code == 403:
                msg += " — vérifiez que ce compte a accès au Sheet en modification."
            if r.status_code == 404:
                msg += " — vérifiez sheet_id dans config.json."
            raise ApiError(f"API Google Sheets ({r.status_code}) : {msg}")
        return r.json() if r.content else {}

    def _meta(self) -> dict:
        meta = self._call("GET", params={"fields": META_FIELDS})
        self._dev_meta = {m["metadataKey"]: m for m in meta.get("developerMetadata") or []}
        titles = [s["properties"]["title"] for s in meta.get("sheets") or []]
        self._titles = {k: self._match_title(titles, cfg["name"]) for k, cfg in TABS.items()}
        self._titles[STATUS_TAB] = self._match_title(titles, STATUS_TAB)
        self._titles[DASHBOARD_TAB] = self._match_title(titles, DASHBOARD_TAB)
        return meta

    @staticmethod
    def _match_title(titles, name):
        for t in titles:
            if t == name:
                return t
        for t in titles:
            if norm(t) == norm(name):
                return t
        return None

    def _grids(self, ranges) -> dict:
        if not ranges:
            return {}
        data = self._call("GET", params={"includeGridData": "true", "ranges": ranges, "fields": GRID_FIELDS})
        return {s["properties"]["title"]: s for s in data.get("sheets") or []}

    def _batch(self, requests) -> None:
        if requests:
            self._call("POST", ":batchUpdate", json={"requests": requests})

    def _read_range(self, ref: str):
        if ref not in self._range_cache:
            data = self._call("GET", "/values/" + quote(ref, safe=""), params={"majorDimension": "ROWS"})
            out = []
            for row in data.get("values") or []:
                for v in row:
                    v = str(v).strip()
                    if v and v not in out:
                        out.append(v)
            self._range_cache[ref] = out
        return self._range_cache[ref]

    def _title(self, key: str) -> str:
        if self._titles is None:
            self._meta()
        title = self._titles.get(key)
        if not title:
            raise UserError(f"Onglet « {TABS[key]['name'] if key in TABS else key} » introuvable dans le Sheet.")
        return title

    def _fresh(self, key: str) -> Tab:
        if key not in TABS:
            raise UserError(f"Onglet inconnu : {key}")
        title = self._title(key)
        sheet = self._grids([a1(title)]).get(title)
        if not sheet:
            raise UserError(f"Onglet « {title} » introuvable dans le Sheet.")
        tab = Tab(key, sheet)
        if not tab.headers:
            raise UserError(f"Onglet « {title} » vide (aucun en-tête en ligne 1).")
        return tab

    def spreadsheet_title(self) -> str:
        with self.lock:
            return self._meta().get("properties", {}).get("title", "")

    # ---------- Statuts ----------

    def _load_statuts(self, sheet: dict | None) -> None:
        """Lit l'onglet « Statuts ». Deux formats acceptés :
        - en lignes : Onglet | Statut | … | Quand l'utiliser (format actuel du Sheet) ;
        - en colonnes : une colonne par onglet, les statuts en dessous."""
        self._statuts, self._status_help = {}, {}
        if not sheet:
            return
        data = (sheet.get("data") or [{}])[0]
        grid = [[cell_str(parse_cell(c).value).strip() for c in (r.get("values") or [])] for r in (data.get("rowData") or [])]
        if not grid:
            return
        tab_c, status_c, help_c = find_col(grid[0], "Onglet"), find_col(grid[0], "Statut"), find_col(grid[0], "Quand l'utiliser")
        if tab_c >= 0 and status_c >= 0:
            for row in grid[1:]:
                cell = lambda c: row[c].strip() if 0 <= c < len(row) else ""  # noqa: E731
                tab, status = norm(cell(tab_c)), cell(status_c)
                if not tab or not status:
                    continue
                self._statuts.setdefault(tab, [])
                if status not in self._statuts[tab]:
                    self._statuts[tab].append(status)
                if cell(help_c):
                    self._status_help.setdefault(tab, {})[status] = cell(help_c)
            return
        for c, h in enumerate(grid[0]):
            if not h:
                continue
            vals = []
            for row in grid[1:]:
                v = row[c] if c < len(row) else ""
                if v and v not in vals:
                    vals.append(v)
            self._statuts[norm(h)] = vals

    def status_list(self, key: str, from_validation=None):
        """Validation du Sheet > onglet « Statuts » (colonne nommée comme l'onglet) > liste par défaut."""
        if from_validation:
            return list(from_validation)
        if self._statuts is None:
            if self._titles is None:
                self._meta()
            title = self._titles.get(STATUS_TAB)
            self._load_statuts(self._grids([a1(title)]).get(title) if title else None)
        vals = self._statuts.get(norm(TABS[key]["name"]))
        return list(vals) if vals else list(TABS[key]["statuses"])

    def _statuses_for(self, tab: Tab):
        c = tab.col("Statut")
        lst = None
        if c >= 0:
            lst, _, _ = list_from_dv(tab.cell(0, c).dv, self._read_range)
        return self.status_list(tab.key, lst)

    # ---------- Lecture ----------

    def get_all_data(self) -> dict:
        with self.lock:
            self._range_cache = {}
            meta = self._meta()
            ranges = [a1(self._titles[k]) for k in TABS if self._titles.get(k)]
            if self._titles.get(STATUS_TAB):
                ranges.append(a1(self._titles[STATUS_TAB]))
            if self._titles.get(DASHBOARD_TAB):
                ranges.append(a1(self._titles[DASHBOARD_TAB]) + "!A1:Z100")
            grids = self._grids(ranges)
            self._load_statuts(grids.get(self._titles.get(STATUS_TAB)))
            res = {
                "today": date.today().isoformat(),
                "timeZone": meta.get("properties", {}).get("timeZone", ""),
                "title": meta.get("properties", {}).get("title", ""),
                "tabs": {},
                "dashboard": None,
            }
            sync = []
            for key in TABS:
                sheet = grids.get(self._titles.get(key)) if self._titles.get(key) else None
                tab = Tab(key, sheet) if sheet else None
                res["tabs"][key] = self._read_tab(key, tab)
                if tab and tab.headers and TABS[key].get("idCol"):
                    sync += self._high_water_requests(tab, self._max_id(tab), only_if_higher=True)
            if sync:
                self._batch(sync)
                self._meta()  # récupère l'identifiant des métadonnées tout juste créées
            dash = grids.get(self._titles.get(DASHBOARD_TAB)) if self._titles.get(DASHBOARD_TAB) else None
            if dash:
                res["dashboard"] = {"rows": self._dashboard_rows(dash)}
            return res

    def _read_tab(self, key: str, tab: Tab | None) -> dict:
        cfg = TABS[key]
        out = {
            "key": key, "name": cfg["name"], "exists": False, "headers": [], "cols": {}, "missing": [], "rows": [],
            "statuses": [], "options": {}, "checkboxCols": [], "dateCols": [], "formulaCols": [],
            "keyCols": cfg["keyCols"], "idCol": cfg.get("idCol"), "required": cfg.get("required", []),
            "dupRules": cfg.get("dupRules", []), "statusHelp": {},
        }
        if not tab or not tab.headers:
            out["missing"] = list(cfg["columns"])
            out["statuses"] = self.status_list(key)
            return out
        out["exists"] = True
        out["name"] = tab.title
        out["statusHelp"] = (self._status_help or {}).get(norm(cfg["name"]), {})
        out["headers"] = tab.headers
        for name in cfg["columns"]:
            c = tab.col(name)
            out["cols"][name] = tab.headers[c] if c >= 0 else None
            if c < 0:
                out["missing"].append(name)
        for r0 in range(len(tab.rows)):
            row = tab.build_row(r0)
            if row:
                out["rows"].append(row)
        for c, h in enumerate(tab.headers):
            if not h:
                continue
            if c in tab.date_idx:
                out["dateCols"].append(h)
            if c in tab.spill or any(tab.cell(r0, c).formula for r0 in range(len(tab.rows))):
                out["formulaCols"].append(h)
        # Listes déroulantes : validations de la 2e ligne, à défaut de la dernière ligne.
        for r0 in ([0, len(tab.rows) - 1] if len(tab.rows) > 1 else [0]):
            for c, h in enumerate(tab.headers):
                if not h or h in out["options"] or h in out["checkboxCols"]:
                    continue
                lst, checkbox, _ = list_from_dv(tab.cell(r0, c).dv, self._read_range)
                if checkbox:
                    out["checkboxCols"].append(h)
                elif lst:
                    out["options"][h] = lst
        status_head = out["cols"].get("Statut")
        out["statuses"] = self.status_list(key, out["options"].get(status_head) if status_head else None)
        if status_head and status_head not in out["options"] and out["statuses"]:
            out["options"][status_head] = out["statuses"]
        return out

    @staticmethod
    def _dashboard_rows(sheet: dict):
        data = (sheet.get("data") or [{}])[0]
        rows = [[(c or {}).get("formattedValue", "") for c in (r.get("values") or [])] for r in (data.get("rowData") or [])]
        rows = [r for r in rows if any(str(x).strip() for x in r)]
        width = max((max((i + 1 for i, x in enumerate(r) if str(x).strip()), default=0) for r in rows), default=0)
        start = min((min((i for i, x in enumerate(r) if str(x).strip()), default=width) for r in rows), default=0)
        return [(r + [""] * width)[start:width] for r in rows]

    # ---------- Écritures publiques ----------

    def update_row(self, key, row_num, ident, changes):
        with self.lock:
            tab = self._fresh(key)
            r0 = self._locate(tab, row_num, ident)
            reqs, res = self._plan(tab, r0, changes or {})
            self._batch(reqs)
            return self._result(key, r0, res)

    def set_status(self, key, row_num, ident, status):
        with self.lock:
            tab = self._fresh(key)
            r0 = self._locate(tab, row_num, ident)
            reqs, res = self._plan(tab, r0, self._status_changes(tab, r0, status))
            self._batch(reqs)
            return self._result(key, r0, res)

    def mark_spont_sent(self, row_num, ident):
        """Spontanée : statut « Candidature envoyée » + Date d'envoi = aujourd'hui."""
        with self.lock:
            tab = self._fresh("spont")
            r0 = self._locate(tab, row_num, ident)
            changes = self._status_changes(tab, r0, "Candidature envoyée")
            if tab.col("Date d'envoi") >= 0:
                changes["Date d'envoi"] = date.today()
            if isinstance(tab.get(r0, "Mail envoyé"), bool):
                changes["Mail envoyé"] = True
            prep = tab.col("Préparation")
            if prep >= 0 and not tab.is_formula(r0, prep):
                choices, _, _ = list_from_dv(tab.cell(r0, prep).dv or tab.cell(0, prep).dv, self._read_range)
                if not choices or "Envoyée" in choices:
                    changes["Préparation"] = "Envoyée"
            reqs, res = self._plan(tab, r0, changes)
            self._batch(reqs)
            return self._result("spont", r0, res)

    def archive_row(self, key, row_num, ident):
        with self.lock:
            tab = self._fresh(key)
            r0 = self._locate(tab, row_num, ident)
            reqs, res = self._plan(tab, r0, {"Statut": self._archive_status(tab)})
            self._batch(reqs)
            return self._result(key, r0, res)

    def add_reply(self, key, row_num, ident, text, status=""):
        text = str(text or "").strip()
        if not text:
            raise UserError("La réponse est vide.")
        with self.lock:
            tab = self._fresh(key)
            notes_col = tab.cfg.get("notesCol")
            if not notes_col:
                raise UserError(f"Pas de colonne de notes pour « {tab.title} ».")
            c = tab.col(notes_col)
            if c < 0:
                raise UserError(f"Colonne « {notes_col} » introuvable.")
            r0 = self._locate(tab, row_num, ident)
            prev = cell_str(tab.cell(r0, c).value).strip()
            stamp = date.today().strftime("[%d/%m/%Y] ")
            changes = self._status_changes(tab, r0, status) if status else {}
            changes[notes_col] = (prev + "\n\n" if prev else "") + stamp + text
            reqs, res = self._plan(tab, r0, changes)
            self._batch(reqs)
            return self._result(key, r0, res)

    def add_row(self, key, data):
        """Crée une ligne après la dernière ligne remplie. Doublons refusés ; ID généré ici."""
        with self.lock:
            tab = self._fresh(key)
            cfg = tab.cfg
            data = dict(data or {})
            for name in cfg.get("required", []):
                if not cell_str(data.get(name)).strip():
                    raise UserError(f"Champ obligatoire : {name}")
            self._check_duplicates(tab, data)

            if tab.col("Statut") >= 0 and not cell_str(data.get("Statut")).strip():
                statuses = self._statuses_for(tab)
                if statuses:
                    data["Statut"] = statuses[0]

            requests = []
            new_id = None
            if cfg.get("idCol"):
                data.pop(cfg["idCol"], None)
                if tab.col(cfg["idCol"]) >= 0:
                    n = self._max_id(tab, include_memory=True) + 1
                    new_id = cfg["idPrefix"] + str(n).zfill(3)
                    data[cfg["idCol"]] = new_id
                    requests += self._high_water_requests(tab, n)

            last = max((r for r in range(len(tab.rows)) if tab.has_content(r)), default=-1)
            r0 = last + 1
            grid_row = r0 + 1
            if grid_row >= tab.row_count:
                requests.append({"appendDimension": {"sheetId": tab.sheet_id, "dimension": "ROWS",
                                                     "length": grid_row - tab.row_count + 1}})

            dv_override, num_formats = {}, {}
            if last >= 0:
                for c in range(tab.ncol):
                    tpl = tab.cell(last, c)
                    if tpl.dv and not tab.cell(r0, c).dv:
                        dv_override[c] = tpl.dv
                        requests.append({"copyPaste": {"source": grid_range(tab.sheet_id, last + 1, c),
                                                       "destination": grid_range(tab.sheet_id, grid_row, c),
                                                       "pasteType": "PASTE_DATA_VALIDATION"}})
                    if tpl.num_format:
                        num_formats[c] = tpl.num_format

            # Recopie les formules (ex. « Relance prévue » = Date 1er contact + 7) depuis la dernière ligne qui en contient.
            formula_cols = set()
            for c in range(tab.ncol):
                if c in tab.spill or tab.cell(r0, c).formula:
                    continue
                for r in range(last, -1, -1):
                    if tab.cell(r, c).formula:
                        requests.append({"copyPaste": {"source": grid_range(tab.sheet_id, r + 1, c),
                                                       "destination": grid_range(tab.sheet_id, grid_row, c),
                                                       "pasteType": "PASTE_FORMULA"}})
                        formula_cols.add(c)
                        break

            changes = {k: v for k, v in data.items() if v not in (None, "", False)}
            reqs, res = self._plan(tab, r0, changes, dv_override, num_formats, formula_cols)
            self._batch(requests + reqs)
            res["id"] = new_id
            return self._result(key, r0, res)

    # ---------- Logique interne ----------

    def _locate(self, tab: Tab, row_num, ident) -> int:
        """Retrouve la ligne : position attendue, sinon recherche par identité (le Sheet a pu être trié)."""
        keys = []
        for name, v in (ident or {}).items():
            c = tab.col(name)
            if c >= 0 and norm(v):
                keys.append((c, norm(v)))
        if not keys:
            raise UserError("Impossible d'identifier la ligne (identifiants vides).")

        def matches(r0):
            return 0 <= r0 < len(tab.rows) and all(norm(cell_str(tab.cell(r0, c).value)) == v for c, v in keys)

        try:
            r0 = int(row_num) - 2
        except (TypeError, ValueError):
            r0 = -1
        if matches(r0):
            return r0
        found = [r for r in range(len(tab.rows)) if matches(r)]
        if len(found) == 1:
            return found[0]
        raise UserError("Cette ligne a été modifiée ou déplacée dans le Sheet. Actualisez les données puis réessayez.")

    def _plan(self, tab: Tab, r0, changes, dv_override=None, num_formats=None, formula_cols=()):
        """Valide tous les changements puis prépare les requêtes. Colonnes absentes ou calculées : ignorées et signalées."""
        skipped, planned = [], []
        for name, raw in changes.items():
            c = tab.col(name)
            if c < 0:
                skipped.append(f"{name} (colonne absente)")
                continue
            header = tab.headers[c]
            if tab.is_formula(r0, c) or c in formula_cols:
                skipped.append(f"{header} (formule protégée)")
                continue
            cur = tab.cell(r0, c)
            value = self._coerce(tab, c, raw, cur.value)
            self._check_allowed(tab, c, value, cur.dv or (dv_override or {}).get(c))
            planned.append((c, value))
        reqs = [cell_request(tab.sheet_id, r0, c, v, (num_formats or {}).get(c)) for c, v in planned]
        return reqs, {"skipped": skipped, "written": [tab.headers[c] for c, _ in planned]}

    @staticmethod
    def _coerce(tab: Tab, c, raw, cur):
        if raw is None:
            return ""
        if isinstance(raw, bool):
            return raw
        if c in tab.date_idx:
            return Backend._to_date(raw, tab.headers[c])
        if isinstance(raw, (int, float)):
            return raw
        s = str(raw)
        t = s.strip()
        if isinstance(cur, (int, float)) and not isinstance(cur, bool) and NUMBER_RE.match(t):
            n = float(t.replace(",", "."))
            return int(n) if n.is_integer() else n
        return s

    @staticmethod
    def _to_date(raw, header):
        if isinstance(raw, date):
            return raw
        s = str(raw).strip()
        if not s:
            return ""
        try:
            if ISO_RE.match(s):
                return date.fromisoformat(s)
            m = FR_DATE_RE.match(s)
            if m:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
        raise UserError(f"Date invalide pour « {header} » : {s} (format attendu JJ/MM/AAAA).")

    def _check_allowed(self, tab: Tab, c, value, dv):
        """Respecte les listes déroulantes du Sheet (mode « refuser la saisie ») et la liste des statuts."""
        if value == "" or isinstance(value, (bool, date)):
            return
        lst, _, strict = list_from_dv(dv, self._read_range)
        if not lst and c == tab.col("Statut"):
            lst, strict = self._statuses_for(tab), True
        if lst and strict and cell_str(value) not in lst:
            raise UserError(f"Valeur « {value} » non autorisée pour « {tab.headers[c]} ». Valeurs possibles : {', '.join(lst)}")

    def _status_changes(self, tab: Tab, r0, status):
        """Changements induits par un nouveau statut (les dates déjà remplies ne sont jamais écrasées)."""
        changes = {"Statut": status}
        today = date.today()
        has = lambda name: tab.col(name) >= 0  # noqa: E731
        if tab.key == "offres" and status == "Candidature envoyée":
            sent = tab.get(r0, "Date candidature")
            base = date.fromisoformat(sent) if isinstance(sent, str) and ISO_RE.match(sent) else today
            if has("Date candidature") and is_empty(sent):
                changes["Date candidature"] = today
            if has("Date relance") and is_empty(tab.get(r0, "Date relance")):
                changes["Date relance"] = base + timedelta(days=7)
        if tab.key == "spont" and status == "Candidature envoyée":
            if has("Date d'envoi") and is_empty(tab.get(r0, "Date d'envoi")):
                changes["Date d'envoi"] = today
        if tab.key == "contacts" and status in ("Message envoyé", "Email envoyé"):
            if has("Date 1er contact") and is_empty(tab.get(r0, "Date 1er contact")):
                changes["Date 1er contact"] = today
        return changes

    def _archive_status(self, tab: Tab):
        lst = self._statuses_for(tab)
        pref = tab.cfg.get("archiveStatus")
        if pref and (not lst or pref in lst):
            return pref
        for s in lst:
            if re.search(r"abandon|archiv|retir|clotur|refus", norm(s)):
                return s
        raise UserError(f"Aucun statut d'archivage disponible pour « {tab.title} ».")

    def _check_duplicates(self, tab: Tab, data):
        for rule in tab.cfg.get("dupRules", []):
            if not norm(data.get(rule[0])):
                continue
            cols = [(tab.col(n), norm(data.get(n))) for n in rule]
            cols = [(c, v) for c, v in cols if c >= 0]
            if not cols:
                continue
            for r0 in range(len(tab.rows)):
                if tab.has_content(r0) and all(norm(cell_str(tab.cell(r0, c).value)) == v for c, v in cols):
                    raise UserError(f"Doublon : une ligne avec le même {' + '.join(rule)} existe déjà (ligne {r0 + 2}).")

    # ---------- Identifiants (jamais réutilisés) ----------

    def _max_id(self, tab: Tab, include_memory=False) -> int:
        c = tab.col(tab.cfg["idCol"])
        prefix = norm(tab.cfg["idPrefix"])
        best = 0
        if c >= 0:
            for r0 in range(len(tab.rows)):
                v = cell_str(tab.cell(r0, c).value)
                if not norm(v).startswith(prefix):
                    continue
                m = re.search(r"(\d+)\s*$", v)
                if m:
                    best = max(best, int(m.group(1)))
        if include_memory:
            if not self._dev_meta:
                self._meta()
            meta = self._dev_meta.get(META_PREFIX + tab.key)
            if meta:
                try:
                    best = max(best, int(meta.get("metadataValue") or 0))
                except ValueError:
                    pass
        return best

    def _high_water_requests(self, tab: Tab, n: int, only_if_higher=False):
        """Mémorise le plus grand ID dans les métadonnées invisibles du Sheet : un ID supprimé n'est jamais réattribué."""
        key = META_PREFIX + tab.key
        meta = self._dev_meta.get(key)
        if meta and meta.get("metadataId") is None:
            self._meta()
            meta = self._dev_meta.get(key)
        if meta:
            try:
                stored = int(meta.get("metadataValue") or 0)
            except ValueError:
                stored = 0
            if only_if_higher and n <= stored:
                return []
            meta["metadataValue"] = str(n)
            return [{"updateDeveloperMetadata": {
                "dataFilters": [{"developerMetadataLookup": {"metadataId": meta["metadataId"]}}],
                "developerMetadata": {"metadataValue": str(n)},
                "fields": "metadataValue",
            }}]
        if only_if_higher and n <= 0:
            return []
        self._dev_meta[key] = {"metadataKey": key, "metadataValue": str(n), "metadataId": None}
        return [{"createDeveloperMetadata": {"developerMetadata": {
            "metadataKey": key, "metadataValue": str(n), "location": {"spreadsheet": True}, "visibility": "DOCUMENT",
        }}}]

    def _result(self, key, r0, res):
        """Relit l'onglet après écriture (formules recalculées) et renvoie la ligne à jour."""
        tab = self._fresh(key)
        if any(m.get("metadataId") is None for m in self._dev_meta.values()):
            self._meta()  # récupère l'identifiant d'une métadonnée tout juste créée
        row = tab.build_row(r0) or {"_row": r0 + 2, "_f": [], "_links": {}}
        return {"row": row, "skipped": res.get("skipped", []), "written": res.get("written", []), "id": res.get("id")}
