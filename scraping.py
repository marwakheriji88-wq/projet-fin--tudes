# -*- coding: utf-8 -*-
"""
============================================================================
 SCRIPT UNIQUE DE WEB SCRAPING — Plateforme de durabilité des universités
============================================================================

Démarche structurée EN COUCHES (web scraping « par layers ») :

  Layer 1 — Sources & pages          : URLs des plateformes (constantes SRC_*)
  Layer 2 — Universités & profils    : extraction des établissements (nom, pays, rang)
  Layer 3 — Indicateurs d'évaluation : scores par source
        • THE (World/Impact/Subject) et QS : via les API JSON officielles (données ouvertes)
        • ARWU / ShanghaiRanking : via PLAYWRIGHT (site dynamique, rendu JS + pagination)
        • Rapports annuels : téléchargement PDF/HTML + extraction NLP (spaCy)
  Layer 4 — Harmonisation            : consolidation université × année, noms/pays/années
  Layer 5 — Contrôle qualité         : doublons, valeurs manquantes, cohérence
  Layer 6 — OCR complémentaire       : sur les plateformes complexes (ex. STARS), lorsque des
        scores/sous-scores ne sont pas extractibles du HTML (tableaux non sélectionnables,
        images), on les récupère par OCR sur une capture de la zone. NE REMPLACE PAS le
        scraping HTML : couche AJOUTÉE en complément (voir module `stars_ocr.py`).

Le script construit la base SQLite et produit les fichiers de résultats. CHAQUE
FICHIER DE RÉSULTAT CONTIENT UNE COLONNE « lien » indiquant l'URL de la source.

Utilisation :
    python scraping.py                # collecte complète en ligne + base
    python scraping.py --offline      # reconstruit depuis les données déjà téléchargées
    python scraping.py --no-reports   # sans les rapports annuels (plus rapide)
    python scraping.py --stars        # Layer 6 : démonstration OCR complémentaire (STARS)

Résultats (data/processed/) :
    the_world_rankings.csv, the_impact_rankings.csv, the_subject_rankings.csv,
    qs_world_rankings.csv, annual_reports.csv  -> avec colonne « lien »
    universities.csv, dataset_universities_x_year.csv, dataset_durabilite.xlsx
    annual_reports.json, collection_meta.json
Base : database/sustainability.db
"""

import os
import re
import io
import sys
import json
import sqlite3
import pathlib
import unicodedata
import datetime as dt
from difflib import SequenceMatcher

import requests
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent
# Dossier « data » : fichiers résultats (noms clairs, avec colonne « lien »).
# Sous-dossier « sources_brutes » : réponses brutes des API (traçabilité + mode --offline).
DATA = ROOT / "data"
RAW = DATA / "sources_brutes"
PROC = DATA
DB_PATH = ROOT / "database" / "sustainability.db"
RAW.mkdir(parents=True, exist_ok=True)
(ROOT / "database").mkdir(exist_ok=True)

# Noms de fichiers de résultats (clairs et lisibles)
F_THE_WORLD = "classement_the_monde.csv"
F_THE_IMPACT = "classement_the_impact_odd.csv"
F_THE_SUBJECT = "classement_the_disciplines.csv"
F_QS = "classement_qs_monde.csv"
F_ARWU = "classement_arwu_shanghai.csv"
F_REPORTS_CSV = "rapports_annuels.csv"
F_REPORTS_JSON = "rapports_annuels_details.json"
F_UNIVERSITIES = "universites.csv"
F_DATASET = "donnees_consolidees.csv"
F_SUBJECTS = "disciplines_par_universite.csv"
F_META = "metadonnees_collecte.json"
F_EXCEL = "donnees_durabilite.xlsx"

YEARS = [2021, 2022, 2023, 2024, 2025]
QS_NID, QS_YEAR = 3990755, 2025          # édition QS courante rattachée à 2025
SUBJECT_YEAR = 2025
ARWU_YEAR, ARWU_MAX_PAGES = 2024, 7      # Shanghai Ranking (Playwright) : top ~210

# ── Liens des sources (colonne « lien ») ────────────────────────────────────
SRC_THE_WORLD = "https://www.timeshighereducation.com/world-university-rankings"
SRC_THE_IMPACT = "https://www.timeshighereducation.com/impactrankings"
SRC_THE_SUBJECT = "https://www.timeshighereducation.com/world-university-rankings/by-subject"
SRC_QS = "https://www.topuniversities.com/world-university-rankings"
SRC_ARWU = "https://www.shanghairanking.com/rankings/arwu/{year}"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# Disciplines THE (slug -> libellé)
SUBJECTS = {
    "computer_science": "Informatique", "engineering_technology": "Ingénierie & technologie",
    "business_economics": "Commerce & économie", "physical_sciences": "Sciences physiques",
    "life_sciences": "Sciences du vivant", "arts_humanities": "Arts & humanités",
    "social_sciences": "Sciences sociales", "education": "Éducation",
    "law": "Droit", "psychology": "Psychologie",
}

# Rapports annuels / de durabilité d'autres universités (sources fixes ; lien conservé)
REPORTS = [
    {"university": "University of Oxford", "year": 2024,
     "url": "https://sustainability.admin.ox.ac.uk/sitefiles/annual-report-2024-2.pdf"},
    {"university": "Queen's University Belfast", "year": 2024,
     "url": "https://www.qub.ac.uk/about/sustainability/files/Annual%20Sustainability%20Report%202023-24.pdf"},
    {"university": "University of St Andrews", "year": 2024,
     "url": "https://www.st-andrews.ac.uk/assets/university/sustainability/documents/sustainability-report-2023-2024.pdf"},
    {"university": "Korea University", "year": 2023,
     "url": "https://www.timeshighereducation.com/cms-academic/sites/default/files/institution_downloads/2024-11/2023%20Korea%20University%20Sustainability%20Report-compressed_%EC%95%95%EC%B6%95.pdf"},
    {"university": "University of Worcester", "year": 2024,
     "url": "https://www.worcester.ac.uk/documents/Annual-Sustainability-Report-2023-24.pdf"},
]
# Site HBS : les PDF annuels (multi-années) sont découverts dynamiquement (Playwright)
HBS_REPORTS_URL = "https://www.hbs.edu/about/annualreport"

# ── Cartographie pays -> région ─────────────────────────────────────────────
_REGION_LISTS = {
    "Afrique": ["Algeria", "Angola", "Benin", "Botswana", "Burkina Faso", "Comoros",
                "Côte d'Ivoire", "Democratic Republic of the Congo", "Egypt", "Ethiopia",
                "Ghana", "Kenya", "Libya", "Malawi", "Mauritius", "Morocco", "Mozambique",
                "Namibia", "Nigeria", "Rwanda", "Senegal", "Sierra Leone", "Somalia",
                "South Africa", "Sudan", "Tanzania", "Tunisia", "Uganda", "Zambia", "Zimbabwe"],
    "Asie": ["Afghanistan", "Armenia", "Azerbaijan", "Bahrain", "Bangladesh", "Brunei Darussalam",
             "Cambodia", "China", "Georgia", "Hong Kong", "India", "Indonesia", "Iran", "Iraq",
             "Israel", "Japan", "Jordan", "Kazakhstan", "Kuwait", "Kyrgyzstan", "Lebanon", "Macao",
             "Malaysia", "Maldives", "Mongolia", "Nepal", "Oman", "Pakistan", "Palestine",
             "Philippines", "Qatar", "Saudi Arabia", "Singapore", "South Korea", "Sri Lanka",
             "Syria", "Taiwan", "Thailand", "Turkey", "Turkmenistan", "United Arab Emirates",
             "Uzbekistan", "Vietnam", "Yemen"],
    "Europe": ["Albania", "Austria", "Belarus", "Belgium", "Bosnia and Herzegovina", "Bulgaria",
               "Croatia", "Cyprus", "Czechia", "Denmark", "Estonia", "Finland", "France", "Germany",
               "Greece", "Hungary", "Iceland", "Ireland", "Italy", "Kosovo", "Latvia", "Lithuania",
               "Luxembourg", "Malta", "Moldova", "Montenegro", "Netherlands", "North Macedonia",
               "Northern Cyprus", "Norway", "Poland", "Portugal", "Romania", "Russian Federation",
               "Serbia", "Slovakia", "Slovenia", "Spain", "Sweden", "Switzerland", "Ukraine",
               "United Kingdom"],
    "Amérique du Nord": ["Canada", "United States"],
    "Amérique latine": ["Anguilla", "Argentina", "Bolivia", "Brazil", "Chile", "Colombia",
                        "Costa Rica", "Cuba", "Dominican Republic", "Ecuador", "El Salvador",
                        "Haiti", "Honduras", "Jamaica", "Mexico", "Panama", "Paraguay", "Peru",
                        "Puerto Rico", "Turks and Caicos Islands", "Uruguay", "Venezuela"],
    "Océanie": ["Australia", "Fiji", "New Zealand", "Papua New Guinea"],
}
REGION_BY_COUNTRY = {c: r for r, cs in _REGION_LISTS.items() for c in cs}
def region_of(country): return REGION_BY_COUNTRY.get(country, "Autre") if country else "Autre"

# ── Rapprochement des noms (matching) ───────────────────────────────────────
_STOP = {"the", "of", "de", "del", "la", "le", "les", "and", "et", "for", "in", "at", "a",
         "university", "universite", "universidad", "universita", "universiteit", "universitat",
         "universidade", "college", "institute", "institut"}

def _clean(name):
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\([^)]*\)", " ", s).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()

def _tokens(name):
    return frozenset(t for t in _clean(name).split() if t not in _STOP)

def _to_float(value):
    if value is None:
        return None
    s = str(value).strip().replace("%", "").replace(",", "")
    if not s or s.lower() in {"n/a", "na", "-", "reporter"}:
        return None
    for sep in ("–", "-", "—"):
        if sep in s:
            s = s.split(sep)[0].strip(); break
    try:
        return float(s)
    except ValueError:
        return None


def build_matcher(universe_names):
    """Prépare les index de rapprochement (nom exact + ensemble de tokens)."""
    by_norm, by_tokens = {}, {}
    for nm, uid in universe_names.items():
        by_norm[nm] = uid
        ts = _tokens(nm)
        if ts:
            by_tokens.setdefault(ts, []).append(uid)
    return by_norm, by_tokens

def match_to_universe(name, by_norm, by_tokens):
    """Retourne l'university_id correspondant (exact, puis tokenset unique)."""
    n = _clean(name)
    if n in by_norm:
        return by_norm[n]
    cand = by_tokens.get(_tokens(name))
    return cand[0] if cand and len(cand) == 1 else None

# ── HTTP ────────────────────────────────────────────────────────────────────
def _session():
    s = requests.Session(); s.headers.update(HEADERS); return s

def _get_json(session, url, retries=3, timeout=45):
    last = None
    for i in range(retries):
        try:
            r = session.get(url, timeout=timeout); r.raise_for_status(); return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise last

# ── SCRAPING : Times Higher Education ───────────────────────────────────────
THE_API = "https://www.timeshighereducation.com/json/ranking_tables/{table}/{year}"

def scrape_the(session, offline):
    """Collecte THE World + Impact (toutes années). Retourne (world_records, impact_records)."""
    world, impact = [], []
    for year in YEARS:
        for table, store, parse in [("world_university_rankings", "world", _parse_world),
                                    ("world_impact_rankings", "impact", _parse_impact)]:
            raw_file = RAW / f"the_{store}_{year}.json"
            data = None
            if offline and raw_file.exists():
                data = json.loads(raw_file.read_text(encoding="utf-8"))
            elif not offline:
                try:
                    data = _get_json(session, THE_API.format(table=table, year=year))
                    raw_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    print(f"  [!] THE {table} {year} : {e}")
            if not data:
                continue
            recs = [parse(r, year) for r in data.get("data", [])]
            (world if store == "world" else impact).extend(recs)
            print(f"  THE {store} {year} : {len(recs)}")
    return world, impact

def _parse_world(rec, year):
    return {"name": rec.get("name"), "country": rec.get("location"), "year": year,
            "the_rank": rec.get("rank"), "the_overall": _to_float(rec.get("scores_overall")),
            "the_teaching": _to_float(rec.get("scores_teaching")),
            "the_research": _to_float(rec.get("scores_research")),
            "the_citations": _to_float(rec.get("scores_citations")),
            "the_industry_income": _to_float(rec.get("scores_industry_income")),
            "the_international_outlook": _to_float(rec.get("scores_international_outlook")),
            "n_students": _to_float(rec.get("stats_number_students")),
            "student_staff_ratio": _to_float(rec.get("stats_student_staff_ratio")),
            "pc_intl_students": _to_float(rec.get("stats_pc_intl_students"))}

def _parse_impact(rec, year):
    return {"name": rec.get("name"), "country": rec.get("location"), "year": year,
            "impact_rank": rec.get("rank"), "impact_overall": _to_float(rec.get("scores_overall")),
            "sdg17_score": _to_float(rec.get("sdg17_rankings_score_display"))}

def scrape_subjects(session, offline):
    """Collecte les classements par discipline (THE Subject)."""
    out = []
    for slug, label in SUBJECTS.items():
        raw_file = RAW / f"the_subject_{slug}_{SUBJECT_YEAR}.json"
        data = None
        if offline and raw_file.exists():
            data = json.loads(raw_file.read_text(encoding="utf-8"))
        elif not offline:
            try:
                data = _get_json(session, THE_API.format(table=f"{slug}_rankings", year=SUBJECT_YEAR))
                raw_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                print(f"  [!] subject {slug} : {e}")
        if not data:
            continue
        for r in data.get("data", []):
            out.append({"name": r.get("name"), "subject_slug": slug, "subject": label,
                        "year": SUBJECT_YEAR, "rank": r.get("rank"),
                        "score": _to_float(r.get("scores_overall"))})
        print(f"  THE subject {slug} : {len(data.get('data', []))}")
    return out

# ── SCRAPING : QS ───────────────────────────────────────────────────────────
def scrape_qs(session, offline):
    """Collecte QS World (paginé) avec employabilité."""
    raw_file = RAW / "qs_qs_world.json"
    records = None
    if offline and raw_file.exists():
        records = json.loads(raw_file.read_text(encoding="utf-8"))
    elif not offline:
        records, page = [], 0
        while True:
            url = (f"https://www.topuniversities.com/rankings/endpoint?nid={QS_NID}&page={page}"
                   f"&items_per_page=100&tab=indicators&sort_by=rank&order_by=asc")
            data = _get_json(session, url)
            nodes = data.get("score_nodes", [])
            if not nodes:
                break
            records.extend(nodes)
            if len(records) >= int(data.get("total_record", 0)):
                break
            page += 1
        raw_file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    records = records or []
    print(f"  QS world : {len(records)}")
    out = []
    for r in records:
        emp = {i.get("indicator_name"): _to_float(i.get("score"))
               for i in (r.get("scores", {}) or {}).get("Employability", []) or []}
        out.append({"name": r.get("title"), "country": r.get("country"), "year": QS_YEAR,
                    "qs_rank": r.get("rank_display") or r.get("rank"),
                    "qs_overall": _to_float(r.get("overall_score")),
                    "qs_employer_reputation": emp.get("Employer Reputation"),
                    "qs_employment_outcomes": emp.get("Employment Outcomes")})
    return out

# ── SCRAPING : Shanghai Ranking / ARWU (Playwright — site dynamique) ────────
def scrape_arwu(offline):
    """
    Collecte le classement ARWU (ShanghaiRanking) via PLAYWRIGHT.

    Le site charge ses données dynamiquement (JavaScript) et pagine les résultats :
    Playwright pilote un navigateur, attend le rendu, lit le tableau et clique sur
    « page suivante ». Démontre l'approche recommandée pour les sites dynamiques.
    """
    raw_file = RAW / f"arwu_{ARWU_YEAR}.json"
    if offline and raw_file.exists():
        records = json.loads(raw_file.read_text(encoding="utf-8"))
        print(f"  ARWU {ARWU_YEAR} (cache) : {len(records)}")
        return records

    records = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(SRC_ARWU.format(year=ARWU_YEAR), wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)
            for _ in range(ARWU_MAX_PAGES):
                for tr in page.query_selector_all("table tbody tr"):
                    td = tr.query_selector_all("td")
                    if len(td) < 4:
                        continue
                    inst = td[1].inner_text().strip().split("\n")
                    name = inst[0].strip()
                    country = inst[1].strip() if len(inst) > 1 else None
                    records.append({"name": name, "country": country, "year": ARWU_YEAR,
                                    "arwu_rank": td[0].inner_text().strip(),
                                    "arwu_score": _to_float(td[3].inner_text())})
                nxt = page.query_selector(".ant-pagination-next")
                if nxt and "disabled" not in (nxt.get_attribute("class") or ""):
                    nxt.click(); page.wait_for_timeout(1500)
                else:
                    break
            browser.close()
        raw_file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  [!] ARWU (Playwright) indisponible : {e}")
    print(f"  ARWU {ARWU_YEAR} : {len(records)}")
    return records


# ── SCRAPING : Rapports annuels HBS multi-années (Playwright — Layer 1) ─────
def discover_hbs_reports():
    """
    LAYER 1 — Découvre dynamiquement les PDF de rapports annuels HBS (toutes années).

    Le site HBS héberge ses rapports sur un CDN avec des liens signés (tokens
    expirables) chargés en JavaScript. Playwright ouvre la page, attend le rendu
    et récupère les liens PDF + l'année associée. Démontre la collecte « par layers »
    sur un site dynamique riche en PDF.
    """
    out = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(HBS_REPORTS_URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)
            links = page.eval_on_selector_all(
                "a", "els => els.map(e => ({t: e.innerText.trim(), h: e.href}))")
            browser.close()
        seen = set()
        for l in links:
            href = l.get("h") or ""
            if ".pdf" not in href.lower():
                continue
            # L'année est dans le nom de fichier (HBS-Annual-2020.pdf), pas dans le token
            m = re.search(r"-(20\d{2})\.pdf", href) or re.search(r"(20\d{2})", l.get("t", ""))
            if not m:
                continue
            year = int(m.group(1))
            if not (2010 <= year <= 2026) or year in seen:
                continue
            seen.add(year)
            out.append({"university": "Harvard Business School", "year": year, "url": href})
        print(f"  HBS (Layer 1) : {len(out)} rapports PDF découverts ({min(seen) if seen else '-'}–{max(seen) if seen else '-'})")
    except Exception as e:  # noqa: BLE001
        print(f"  [!] Découverte HBS indisponible : {e}")
    return out


# ── SCRAPING : Rapports annuels (NLP) ───────────────────────────────────────
THEMES = {"Durabilité": r"sustainab", "Climat": r"\bclimate\b",
          "Carbone / émissions": r"carbon|emission|greenhouse|co2",
          "Neutralité (net zero)": r"net[\s-]?zero|carbon neutral",
          "Énergie renouvelable": r"renewable|solar|wind power",
          "ODD (SDG)": r"\bsdg\b|sustainable development goal",
          "Diversité & inclusion": r"diversity|inclusion|equity",
          "Déchets / recyclage": r"\bwaste\b|recycl", "Biodiversité": r"biodiversity|ecosystem"}
FIGS = {"Étudiants": r"([\d][\d,\.]{2,})\s+(?:students|undergraduates|enrolled|enrolment|enrollment)",
        "Enseignants / staff": r"([\d][\d,\.]{1,})\s+(?:faculty|academic staff|professors|staff)",
        "Émissions CO2": r"([\d][\d,\.]+)\s*(?:metric tons|tonnes|tons|mtco2e?)[^.]{0,40}(?:co2|carbon|emission)?"}
_nlp = None

def _spacy():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm", disable=["lemmatizer", "tagger", "parser"])
    return _nlp

def _report_text(content, kind):
    if kind == "pdf":
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:40])
    html = content.decode("utf-8", errors="ignore")
    html = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))

def process_report(rep):
    out = {"university": rep["university"], "year": rep["year"], "source_url": rep["url"], "status": "ok"}
    try:
        r = requests.get(rep["url"], headers=HEADERS, timeout=60); r.raise_for_status()
        kind = "pdf" if ("pdf" in r.headers.get("Content-Type", "").lower()
                         or rep["url"].lower().endswith(".pdf") or r.content[:4] == b"%PDF") else "html"
        text = _report_text(r.content, kind)
        if len(text) < 200:
            out["status"] = "texte insuffisant"; return out
        low = text.lower()
        out["figures"] = {k: re.search(p, low).group(1) for k, p in FIGS.items() if re.search(p, low)}
        out["themes"] = {k: len(re.findall(p, low)) for k, p in THEMES.items() if re.findall(p, low)}
        from collections import Counter
        doc = _spacy()(text[:200000])
        out["entities"] = {lab: [w for w, _ in Counter(
            e.text.strip() for e in doc.ents if e.label_ == lab).most_common(5)]
            for lab in ("MONEY", "PERCENT", "ORG")}
    except Exception as e:  # noqa: BLE001
        out["status"] = f"échec : {e}"
    return out

# ── CONSOLIDATION + BASE + EXPORTS ──────────────────────────────────────────
SCHEMA = """
CREATE TABLE universities (university_id INTEGER PRIMARY KEY, name TEXT, country TEXT, region TEXT);
CREATE TABLE academic_rankings (id INTEGER PRIMARY KEY AUTOINCREMENT, university_id INTEGER, year INTEGER,
  the_rank TEXT, the_overall REAL, the_teaching REAL, the_research REAL, the_citations REAL,
  the_industry_income REAL, the_international_outlook REAL, qs_rank TEXT, qs_overall REAL,
  qs_employer_reputation REAL, qs_employment_outcomes REAL, arwu_rank TEXT, arwu_score REAL,
  n_students REAL, student_staff_ratio REAL, pc_intl_students REAL, UNIQUE(university_id, year));
CREATE TABLE sustainability_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, university_id INTEGER, year INTEGER,
  impact_rank TEXT, impact_overall REAL, sdg4_score REAL, sdg7_score REAL, sdg13_score REAL,
  sdg17_score REAL, UNIQUE(university_id, year));
CREATE TABLE greenmetric_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, university_id INTEGER, year INTEGER,
  gm_rank TEXT, gm_total REAL, gm_infrastructure REAL, gm_energy_climate REAL, gm_waste REAL,
  gm_water REAL, gm_transport REAL, gm_education REAL, UNIQUE(university_id, year));
CREATE TABLE subject_rankings (id INTEGER PRIMARY KEY AUTOINCREMENT, university_id INTEGER, year INTEGER,
  subject_slug TEXT, subject TEXT, rank TEXT, score REAL, UNIQUE(university_id, year, subject_slug));
CREATE VIEW v_university_year AS
  SELECT u.university_id, u.name, u.country, u.region, COALESCE(a.year, s.year) AS year,
         a.the_rank, a.the_overall, a.qs_rank, a.qs_overall, s.impact_rank, s.impact_overall, s.sdg17_score
  FROM universities u
  LEFT JOIN academic_rankings a ON a.university_id = u.university_id
  LEFT JOIN sustainability_scores s ON s.university_id = u.university_id AND s.year = a.year;
"""

def build(offline=False, do_reports=True):
    session = _session()
    print("=== 1. SCRAPING ===")
    world, impact = scrape_the(session, offline)
    subjects = scrape_subjects(session, offline)
    qs = scrape_qs(session, offline)
    arwu = scrape_arwu(offline)

    # Univers de référence (ancré sur THE World + Impact)
    universe = {}
    for rec in world + impact:
        nm = _clean(rec["name"])
        if nm:
            universe.setdefault(nm, {"name": rec["name"], "country": rec["country"]})
    norm_sorted = sorted(universe)
    uid_of = {nm: i + 1 for i, nm in enumerate(norm_sorted)}
    by_norm, by_tokens = build_matcher({nm: uid_of[nm] for nm in norm_sorted})

    def uid(name):
        return uid_of.get(_clean(name))

    # Index THE par (uid, année)
    world_map = {(uid(r["name"]), r["year"]): r for r in world if uid(r["name"])}
    impact_map = {(uid(r["name"]), r["year"]): r for r in impact if uid(r["name"])}
    qs_map = {}
    for r in qs:
        u = match_to_universe(r["name"], by_norm, by_tokens)
        if u:
            qs_map[u] = r
    arwu_map = {}
    for r in arwu:
        u = match_to_universe(r["name"], by_norm, by_tokens)
        if u:
            arwu_map[u] = r

    print("\n=== 2. CONSOLIDATION ===")
    WCOLS = ["the_rank", "the_overall", "the_teaching", "the_research", "the_citations",
             "the_industry_income", "the_international_outlook", "n_students",
             "student_staff_ratio", "pc_intl_students"]
    ICOLS = ["impact_rank", "impact_overall", "sdg17_score"]
    QCOLS = ["qs_rank", "qs_overall", "qs_employer_reputation", "qs_employment_outcomes"]
    ACOLS = ["arwu_rank", "arwu_score"]

    rows = []
    for nm in norm_sorted:
        u, info = uid_of[nm], universe[nm]
        for year in YEARS:
            w, im = world_map.get((u, year)), impact_map.get((u, year))
            if not w and not im:
                continue
            row = {"university_id": u, "university_name": info["name"],
                   "country": info["country"], "region": region_of(info["country"]), "year": year}
            for c in WCOLS:
                row[c] = (w or {}).get(c)
            for c in ICOLS:
                row[c] = (im or {}).get(c)
            qsr = qs_map.get(u) if year == QS_YEAR else None
            for c in QCOLS:
                row[c] = (qsr or {}).get(c)
            arw = arwu_map.get(u) if year == ARWU_YEAR else None
            for c in ACOLS:
                row[c] = (arw or {}).get(c)
            rows.append(row)
    df = pd.DataFrame(rows).sort_values(["university_id", "year"]).reset_index(drop=True)

    uni_df = pd.DataFrame([{"university_id": uid_of[nm], "name": universe[nm]["name"],
                            "country": universe[nm]["country"],
                            "region": region_of(universe[nm]["country"]),
                            "lien": SRC_THE_WORLD} for nm in norm_sorted])

    # Disciplines -> table longue
    subj_rows = []
    for s in subjects:
        u = uid(s["name"])
        if u:
            subj_rows.append({"university_id": u, "year": s["year"], "subject_slug": s["subject_slug"],
                              "subject": s["subject"], "rank": s["rank"], "score": s["score"]})
    subj_df = pd.DataFrame(subj_rows)

    # Rapports annuels (NLP)
    print("\n=== 3. RAPPORTS ANNUELS (NLP) ===")
    reports = []
    if do_reports:
        all_reports = REPORTS + discover_hbs_reports()   # Layer 1 : découverte HBS dynamique
        reports = [process_report(r) for r in all_reports]
    elif (PROC / F_REPORTS_JSON).exists():
        reports = json.loads((PROC / F_REPORTS_JSON).read_text(encoding="utf-8"))
    for res in reports:
        if res.get("status") == "ok":
            res["university_id"] = match_to_universe(res["university"], by_norm, by_tokens)
    print(f"  Rapports : {sum(1 for r in reports if r.get('status') == 'ok')}/{len(reports)}")

    print("\n=== 4. HARMONISATION & EXPORTS (avec colonne « lien ») ===")
    _export_source_csvs(world, impact, subjects, qs, arwu, reports)
    uni_df.to_csv(PROC / F_UNIVERSITIES, index=False, encoding="utf-8-sig")
    df.to_csv(PROC / F_DATASET, index=False, encoding="utf-8-sig")
    if not subj_df.empty:
        subj_df.assign(lien=SRC_THE_SUBJECT).to_csv(PROC / F_SUBJECTS, index=False, encoding="utf-8-sig")
    (PROC / F_REPORTS_JSON).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")

    _build_db(df, uni_df, subj_df)
    _export_excel(df, subj_df)

    print("\n=== 5. CONTRÔLE QUALITÉ ===")
    quality = _quality_control(df)

    meta = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "n_universities": int(uni_df.shape[0]), "n_rows": int(df.shape[0]), "years": YEARS,
            "n_the_world": int(df["the_overall"].notna().sum()),
            "n_the_impact": int(df["impact_overall"].notna().sum()),
            "n_qs": int(df["qs_rank"].notna().sum()),
            "n_arwu": int(df["arwu_score"].notna().sum()),
            "n_subjects": int(subj_df.shape[0]) if not subj_df.empty else 0,
            "n_reports": sum(1 for r in reports if r.get("status") == "ok"),
            "qualite": quality}
    (PROC / F_META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nUniversités : {meta['n_universities']} | Lignes : {meta['n_rows']} | "
          f"ARWU : {meta['n_arwu']} | Disciplines : {meta['n_subjects']} | Rapports : {meta['n_reports']}")
    print(f"Base : {DB_PATH}")
    return df


def _quality_control(df):
    """LAYER 5 — Contrôle qualité : doublons, valeurs manquantes, cohérence."""
    dup = int(df.duplicated(subset=["university_id", "year"]).sum())
    miss = {c: round(100 * df[c].isna().mean(), 1)
            for c in ["the_overall", "impact_overall", "qs_rank", "arwu_score"]}
    qc = pd.DataFrame([{"controle": "Doublons (université × année)", "valeur": dup},
                       {"controle": "Universités distinctes", "valeur": int(df["university_id"].nunique())},
                       {"controle": "Pays distincts", "valeur": int(df["country"].nunique())},
                       *[{"controle": f"Valeurs manquantes — {c}", "valeur": f"{v} %"} for c, v in miss.items()]])
    qc.to_csv(PROC / "controle_qualite.csv", index=False, encoding="utf-8-sig")
    print(f"  Doublons : {dup} | Manquants THE : {miss['the_overall']}% | "
          f"Impact : {miss['impact_overall']}% | ARWU : {miss['arwu_score']}%")
    return {"doublons": dup, "manquants": miss}


def _export_source_csvs(world, impact, subjects, qs, arwu, reports):
    """Écrit un CSV par source, chacun AVEC une colonne « lien »."""
    pd.DataFrame(world).assign(lien=SRC_THE_WORLD).to_csv(
        PROC / F_THE_WORLD, index=False, encoding="utf-8-sig")
    pd.DataFrame(impact).assign(lien=SRC_THE_IMPACT).to_csv(
        PROC / F_THE_IMPACT, index=False, encoding="utf-8-sig")
    pd.DataFrame(subjects).assign(lien=SRC_THE_SUBJECT).to_csv(
        PROC / F_THE_SUBJECT, index=False, encoding="utf-8-sig")
    pd.DataFrame(qs).assign(lien=SRC_QS).to_csv(
        PROC / F_QS, index=False, encoding="utf-8-sig")
    pd.DataFrame(arwu).assign(lien=SRC_ARWU.format(year=ARWU_YEAR)).to_csv(
        PROC / F_ARWU, index=False, encoding="utf-8-sig")
    rep_rows = [{"university": r["university"], "year": r["year"], "statut": r["status"],
                 "nb_indicateurs": len(r.get("figures", {})), "nb_themes": len(r.get("themes", {})),
                 "lien": r["source_url"]} for r in reports]
    pd.DataFrame(rep_rows).to_csv(PROC / F_REPORTS_CSV, index=False, encoding="utf-8-sig")


def _build_db(df, uni_df, subj_df):
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.executescript(SCHEMA)
    cur.executemany("INSERT INTO universities VALUES (?,?,?,?)",
                    [(int(r.university_id), r.name, r.country, r.region) for r in uni_df.itertuples()])
    for _, r in df.iterrows():
        u, y = int(r["university_id"]), int(r["year"])
        g = lambda c: (str(r[c]) if c in ("the_rank", "qs_rank", "impact_rank", "arwu_rank") and pd.notna(r.get(c))
                       else (r[c] if pd.notna(r.get(c)) else None))
        cur.execute("""INSERT OR REPLACE INTO academic_rankings (university_id,year,the_rank,the_overall,
            the_teaching,the_research,the_citations,the_industry_income,the_international_outlook,qs_rank,
            qs_overall,qs_employer_reputation,qs_employment_outcomes,arwu_rank,arwu_score,n_students,
            student_staff_ratio,pc_intl_students) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (u, y, g("the_rank"), g("the_overall"), g("the_teaching"), g("the_research"), g("the_citations"),
             g("the_industry_income"), g("the_international_outlook"), g("qs_rank"), g("qs_overall"),
             g("qs_employer_reputation"), g("qs_employment_outcomes"), g("arwu_rank"), g("arwu_score"),
             g("n_students"), g("student_staff_ratio"), g("pc_intl_students")))
        cur.execute("""INSERT OR REPLACE INTO sustainability_scores (university_id,year,impact_rank,
            impact_overall,sdg17_score) VALUES (?,?,?,?,?)""",
            (u, y, g("impact_rank"), g("impact_overall"), g("sdg17_score")))
    if not subj_df.empty:
        for _, r in subj_df.iterrows():
            cur.execute("""INSERT OR REPLACE INTO subject_rankings (university_id,year,subject_slug,subject,
                rank,score) VALUES (?,?,?,?,?,?)""",
                (int(r["university_id"]), int(r["year"]), r["subject_slug"], r["subject"],
                 str(r["rank"]) if pd.notna(r["rank"]) else None,
                 r["score"] if pd.notna(r["score"]) else None))
    con.commit(); con.close()


def _export_excel(df, subj_df):
    sources = pd.DataFrame([
        {"Source": "THE World University Rankings", "Type": "Classement académique", "Lien": SRC_THE_WORLD},
        {"Source": "THE Impact Rankings", "Type": "Durabilité (ODD)", "Lien": SRC_THE_IMPACT},
        {"Source": "THE Subject Rankings", "Type": "Par discipline", "Lien": SRC_THE_SUBJECT},
        {"Source": "QS World University Rankings", "Type": "Classement académique", "Lien": SRC_QS},
    ])
    with pd.ExcelWriter(PROC / F_EXCEL, engine="openpyxl") as xl:
        sources.to_excel(xl, sheet_name="Sources", index=False)
        df.to_excel(xl, sheet_name="Donnees", index=False)
        if not subj_df.empty:
            subj_df.assign(lien=SRC_THE_SUBJECT).to_excel(xl, sheet_name="Disciplines", index=False)


if __name__ == "__main__":
    if "--stars" in sys.argv:
        # Layer 6 — OCR complémentaire (démonstration sur STARS / Agnes Scott College)
        import stars_ocr
        stars_ocr.run()
    else:
        build(offline="--offline" in sys.argv, do_reports="--no-reports" not in sys.argv)
