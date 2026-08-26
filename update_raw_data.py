"""
update_raw_data.py — Actualise data/all_leagues_raw_with_stats.csv depuis le
miroir GitHub datasets/football-datasets (5 grands championnats) et,
pour les ligues absentes de ce miroir, directement depuis football-data.co.uk
(voir LEAGUE_DIRECT_CODES).
=============================================================================

Source vérifiée (voir README section "Source de données") :
  https://github.com/datasets/football-datasets
  datasets/{ligue-1,premier-league,la-liga,bundesliga,serie-a}/season-XXYY.csv

Confirmé par inspection directe des fichiers : mêmes noms d'équipes
("Paris SG", "Ath Madrid", "Bayern Munich", ...) et mêmes colonnes
(Date, HomeTeam, AwayTeam, FTHG, FTAG, HS, AS, HST, AST, HC, AC, ...) que
data/all_leagues_raw_with_stats.csv — c'est très probablement la source
d'origine (ou une source identique en amont, football-data.co.uk) de ce
fichier. Aucune fabrication : la structure a été vérifiée en direct sur le
dépôt avant d'écrire ce module (cf. season-2526.csv, 306 lignes, colonnes
identiques).

Ligues ajoutées après coup et absentes de ce miroir GitHub (ex. Primeira
Liga portugaise) : téléchargées directement depuis football-data.co.uk
(https://www.football-data.co.uk/mmz4281/XXYY/<code>.csv, mêmes colonnes
mais dates en DD/MM/YYYY — voir download_direct_season). Vérifié en direct :
football-data.co.uk couvre bien plus de championnats que le miroir GitHub
(Eredivisie, Belgique, Écosse, Turquie, Grèce, Championship anglais, etc.)
— ce module peut donc suivre d'autres ligues sans changer de source, en
ajoutant simplement une entrée à LEAGUE_DIRECT_CODES.

Stratégie de récupération : pas de suivi fin "quels matchs sont vraiment
nouveaux" — on retélécharge simplement les fichiers de la saison EN COURS
et de la saison PRÉCÉDENTE à chaque exécution (les deux, pour ne rien
perdre pendant une transition de saison : le fichier de la nouvelle saison
peut ne pas encore exister sur le dépôt en tout début d'année, cf. 404
observé pour season-2627.csv début août 2026), puis on fusionne avec le
CSV existant en dédoublonnant sur (date, home_team, away_team). C'est plus
simple et largement suffisant pour un rafraîchissement hebdomadaire — le
dédoublonnage rend l'opération idempotente (ré-exécuter le job sans
nouvelles données ne change rien).

Si une vraie source temps réel (Sportmonks, API-Football) est branchée
plus tard, le point d'ancrage est `fetch_latest_matches()` : elle doit
continuer à retourner un DataFrame avec les colonnes
[date, home_team, away_team, home_goals, away_goals, home_shots,
away_shots, home_shots_target, away_shots_target, home_corners,
away_corners, league] — le reste du pipeline (merge, dédoublonnage,
écriture) n'a pas besoin de changer.

Usage autonome (test manuel, sans ré-entraînement) :
    python update_raw_data.py
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

RAW_FILE = "data/all_leagues_raw_with_stats.csv"

BASE_URL = "https://raw.githubusercontent.com/datasets/football-datasets/main/datasets"

# Nom de ligue interne (utilisé partout dans le projet, cf. colonne `league`
# de data/all_leagues_raw_with_stats.csv) -> nom du dossier dans le dépôt.
LEAGUE_REPO_DIRS = {
    "Ligue1": "ligue-1",
    "PremierLeague": "premier-league",
    "LaLiga": "la-liga",
    "Bundesliga": "bundesliga",
    "SerieA": "serie-a",
}

# Ligues absentes du miroir GitHub datasets/football-datasets (celui-ci ne
# couvre que les "5 grands championnats" ci-dessus) — téléchargées
# DIRECTEMENT depuis football-data.co.uk (source d'origine du miroir,
# format identique : mêmes colonnes, mais dates en DD/MM/YYYY au lieu de
# YYYY-MM-DD, voir download_direct_season). Nom de ligue interne -> code
# division football-data.co.uk (ex. "P1" = Primeira Liga, voir
# https://www.football-data.co.uk/portugalm.php).
LEAGUE_DIRECT_CODES = {
    "PrimeiraLiga": "P1",
}

DIRECT_BASE_URL = "https://www.football-data.co.uk/mmz4281"

# Compétitions absentes à la fois du miroir GitHub et de football-data.co.uk
# (championnats hors Europe/Amérique du Sud couverts par ce dernier, et
# coupes continentales qu'il ne couvre jamais) — récupérées directement via
# API-Football (déjà utilisée pour /live-scores et les fixtures à venir,
# voir api/app/core/api_football_config.py). Contrairement aux deux sources
# ci-dessus, ce n'est QUE le score final qui est disponible ici (pas de
# tirs/corners) : home_shots/away_shots/home_shots_target/away_shots_target/
# home_corners/away_corners restent vides pour ces lignes — sans impact sur
# l'entraînement Dixon-Coles (export_model_artifacts.py n'utilise que
# date/home_team/away_team/home_goals/away_goals), potentiellement limitant
# pour un futur modèle ML qui en dépendrait (voir app/ai/engine/features.py).
# Nom de ligue interne -> id de ligue API-Football (mêmes ids que
# API_FOOTBALL_LEAGUE_IDS dans api/app/core/api_football_config.py — pas
# importé directement pour ne pas faire dépendre ce script racine du
# sous-package api/, voir docstring module).
LEAGUE_API_FOOTBALL_IDS = {
    "MLS": 253,
    "SaudiProLeague": 307,
    "ChampionsLeague": 2,
    "EuropaLeague": 3,
    "ConferenceLeague": 848,
}

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"

# Colonnes du dépôt source (format football-data.co.uk) -> nos colonnes.
COLUMN_MAP = {
    "Date": "date",
    "HomeTeam": "home_team",
    "AwayTeam": "away_team",
    "FTHG": "home_goals",
    "FTAG": "away_goals",
    "HS": "home_shots",
    "AS": "away_shots",
    "HST": "home_shots_target",
    "AST": "away_shots_target",
    "HC": "home_corners",
    "AC": "away_corners",
}
OUTPUT_COLUMNS = list(COLUMN_MAP.values()) + ["league"]


def _season_code(season_start_year: int) -> str:
    """2019 -> '1920' (saison 2019-2020), au format utilisé par le dépôt."""
    return f"{season_start_year % 100:02d}{(season_start_year + 1) % 100:02d}"


def current_and_previous_season_codes(reference_date: datetime | None = None) -> list[str]:
    """
    Retourne [saison_precedente, saison_courante]. Une saison européenne
    démarre en juillet/août ; on la fait donc démarrer conventionnellement
    en juillet (mois >= 7 -> saison de cette année-là, sinon année précédente).
    Les deux codes (pas un seul) pour couvrir la transition de saison sans
    rater de matchs (cf. docstring du module).
    """
    reference_date = reference_date or datetime.now(timezone.utc)
    current_start_year = reference_date.year if reference_date.month >= 7 else reference_date.year - 1
    return [_season_code(current_start_year - 1), _season_code(current_start_year)]


def download_league_season(league_repo_dir: str, season_code: str, timeout: int = 20) -> pd.DataFrame | None:
    """
    Télécharge un fichier season-XXYY.csv pour une ligue. Retourne None si
    le fichier n'existe pas encore sur le dépôt (404 — cas normal en tout
    début de saison, PAS une erreur). Toute autre erreur réseau/HTTP est
    laissée remonter (l'appelant décide comment réagir).
    """
    url = f"{BASE_URL}/{league_repo_dir}/season-{season_code}.csv"
    try:
        df = pd.read_csv(url, storage_options={"timeout": timeout})
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg or "HTTP Error 404" in msg:
            logger.info(f"  {url} -> pas encore disponible (404), ignoré")
            return None
        raise RuntimeError(f"Échec de téléchargement de {url} : {e}") from e

    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:
        raise ValueError(f"{url} : colonnes attendues manquantes {missing} — schéma source a peut-être changé")

    return df


def download_direct_season(division_code: str, season_code: str, timeout: int = 20) -> pd.DataFrame | None:
    """
    Équivalent de download_league_season() mais pour une ligue absente du
    miroir GitHub (voir LEAGUE_DIRECT_CODES) — télécharge directement
    depuis football-data.co.uk. Dates en DD/MM/YYYY (dayfirst=True,
    contrairement au miroir GitHub qui est déjà en YYYY-MM-DD) : converties
    en datetime ICI, avant tout concat avec les autres frames, pour ne
    jamais dépendre de la détection automatique de format de pandas sur un
    mélange de styles de dates.
    """
    url = f"{DIRECT_BASE_URL}/{season_code}/{division_code}.csv"
    try:
        df = pd.read_csv(url, storage_options={"timeout": timeout})
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg or "HTTP Error 404" in msg:
            logger.info(f"  {url} -> pas encore disponible (404), ignoré")
            return None
        raise RuntimeError(f"Échec de téléchargement de {url} : {e}") from e

    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:
        raise ValueError(f"{url} : colonnes attendues manquantes {missing} — schéma source a peut-être changé")

    df = df.rename(columns=COLUMN_MAP)[list(COLUMN_MAP.values())].copy()
    df["date"] = pd.to_datetime(df["date"], dayfirst=True)
    return df


def download_api_football_season(league_id: int, season_year: int, api_key: str, timeout: int = 30) -> pd.DataFrame:
    """
    Équivalent de download_league_season()/download_direct_season() pour une
    compétition suivie uniquement via API-Football (voir LEAGUE_API_FOOTBALL_IDS).
    `season_year` est l'année de DÉBUT de saison au sens API-Football (ex.
    2025 pour "2025-2026" en Europe, ou pour la saison MLS qui, elle, se
    déroule dans cette seule année civile — API-Football encode déjà cette
    différence par compétition, ce script n'a pas à la connaître).

    Ne filtre que sur status="FT" (matchs terminés) — jamais de match en
    cours/à venir dans l'historique d'entraînement. Contrairement aux deux
    autres sources, une saison sans aucun match terminé (pas encore commencée)
    n'est PAS une erreur : retourne un DataFrame vide plutôt que None (pas de
    notion de 404 ici, juste une réponse "response": [] normale).

    Noms d'équipe nettoyés des espaces parasites en tête/fin (cf. bug constaté
    à l'ajout des coupes UEFA : "Olympiakos Piraeus" vs "Olympiakos Piraeus ",
    deux lignes du même club scindées en deux équipes dans le modèle). Pas de
    canonisation plus large des variantes de noms (ex. "Bayern Munich" vs
    "Bayern München", constaté et corrigé une fois manuellement) — à surveiller
    au cas par cas si ça réapparaît, jamais résolu automatiquement ici (risque
    de fusionner par erreur deux clubs réellement différents au nom proche,
    ex. "Hibernian" (Écosse) vs "Hibernians" (Malte), tous deux réels).
    """
    resp = httpx.get(
        f"{API_FOOTBALL_BASE_URL}/fixtures",
        params={"league": league_id, "season": season_year, "status": "FT"},
        headers={"x-apisports-key": api_key},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"réponse API-Football en erreur (league={league_id}, season={season_year}) : {errors}")

    rows = []
    for f in data.get("response", []):
        rows.append({
            "date": f["fixture"]["date"][:10],
            "home_team": f["teams"]["home"]["name"].strip(),
            "away_team": f["teams"]["away"]["name"].strip(),
            "home_goals": f["goals"]["home"],
            "away_goals": f["goals"]["away"],
        })
    return pd.DataFrame(rows, columns=["date", "home_team", "away_team", "home_goals", "away_goals"])


def current_and_previous_seasons_int(reference_date: datetime | None = None) -> list[int]:
    """
    Équivalent de current_and_previous_season_codes() mais pour le paramètre
    `season` d'API-Football, un simple entier (année de début de saison,
    voir download_api_football_season) — même règle de bascule début juillet
    que la version "XXYY" (fonctionne aussi pour la MLS, dont la saison
    coïncide avec l'année civile : la bascule début juillet tombe alors en
    plein milieu de saison, mais [année précédente, année courante] couvre
    quand même sans trou la fenêtre de rafraîchissement hebdomadaire visée).
    """
    reference_date = reference_date or datetime.now(timezone.utc)
    current_start_year = reference_date.year if reference_date.month >= 7 else reference_date.year - 1
    return [current_start_year - 1, current_start_year]


def fetch_latest_matches(reference_date: datetime | None = None) -> pd.DataFrame:
    """
    Télécharge la saison courante + précédente pour toutes les ligues
    suivies (miroir GitHub pour les 5 grands championnats, football-data.co.uk
    en direct pour certaines autres, API-Football pour le reste — voir
    LEAGUE_DIRECT_CODES et LEAGUE_API_FOOTBALL_IDS) et retourne un DataFrame
    unique au format attendu par le reste du pipeline (mêmes colonnes que
    data/all_leagues_raw_with_stats.csv — home_shots/home_corners/etc.
    resteront vides pour les lignes issues d'API-Football, voir
    download_api_football_season).
    """
    season_codes = current_and_previous_season_codes(reference_date)
    logger.info(f"Codes de saison recherchés : {season_codes}")

    frames = []
    for league, repo_dir in LEAGUE_REPO_DIRS.items():
        for season_code in season_codes:
            logger.info(f"Téléchargement {league} — season-{season_code}.csv ...")
            df = download_league_season(repo_dir, season_code)
            if df is None:
                continue
            df = df.rename(columns=COLUMN_MAP)[list(COLUMN_MAP.values())].copy()
            df["league"] = league
            frames.append(df)
            logger.info(f"  -> {len(df)} matchs")

    for league, division_code in LEAGUE_DIRECT_CODES.items():
        for season_code in season_codes:
            logger.info(f"Téléchargement {league} — {division_code}.csv (saison {season_code}, football-data.co.uk) ...")
            df = download_direct_season(division_code, season_code)
            if df is None:
                continue
            df["league"] = league
            frames.append(df)
            logger.info(f"  -> {len(df)} matchs")

    if LEAGUE_API_FOOTBALL_IDS:
        api_football_key = os.environ.get("API_FOOTBALL_KEY", "")
        if not api_football_key:
            logger.warning(
                "API_FOOTBALL_KEY absente — compétitions suivies via API-Football "
                f"({sorted(LEAGUE_API_FOOTBALL_IDS)}) sautées cette exécution, pas d'échec du job pour autant."
            )
        else:
            season_years = current_and_previous_seasons_int(reference_date)
            for league, league_id in LEAGUE_API_FOOTBALL_IDS.items():
                for season_year in season_years:
                    logger.info(f"Téléchargement {league} — saison {season_year} (API-Football, id={league_id}) ...")
                    df = download_api_football_season(league_id, season_year, api_football_key)
                    if df.empty:
                        continue
                    df["league"] = league
                    frames.append(df)
                    logger.info(f"  -> {len(df)} matchs")

    if not frames:
        raise RuntimeError("Aucune donnée récupérée sur aucune ligue — vérifier la connectivité réseau ou le dépôt source")

    result = pd.concat(frames, ignore_index=True)
    result["date"] = pd.to_datetime(result["date"])
    return result[OUTPUT_COLUMNS]


def merge_and_dedup(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """
    Fusionne l'historique existant avec les données fraîchement
    téléchargées, en dédoublonnant sur (date, home_team, away_team). En cas
    de doublon, la version FRAÎCHEMENT téléchargée est conservée (keep=
    "last") — au cas où une correction aurait été apportée en amont
    (score corrigé après contestation, etc).
    """
    existing = existing.copy()
    existing["date"] = pd.to_datetime(existing["date"])
    combined = pd.concat([existing, fresh], ignore_index=True)
    combined = combined.sort_values("date", kind="stable")
    before = len(combined)
    combined = combined.drop_duplicates(subset=["date", "home_team", "away_team"], keep="last")
    logger.info(f"Dédoublonnage : {before} -> {len(combined)} lignes ({before - len(combined)} doublons retirés)")
    return combined.sort_values(["league", "date"], kind="stable").reset_index(drop=True)


def update_raw_data_file(raw_file: str = RAW_FILE, reference_date: datetime | None = None) -> dict:
    """
    Orchestration complète : lit le CSV existant, télécharge les données
    fraîches, fusionne, écrit le résultat, et retourne un résumé par ligue
    (utilisé par refresh_and_retrain.py pour le log récapitulatif).

    Lève une exception si une étape échoue — l'appelant (refresh_and_retrain.py)
    est responsable d'arrêter proprement le job sans toucher aux artefacts
    existants dans ce cas.
    """
    raw_path = Path(raw_file)
    existing = pd.read_csv(raw_path, parse_dates=["date"])
    n_before_total = len(existing)
    counts_before = existing["league"].value_counts().to_dict()

    fresh = fetch_latest_matches(reference_date)
    merged = merge_and_dedup(existing, fresh)

    merged.to_csv(raw_path, index=False)

    counts_after = merged["league"].value_counts().to_dict()
    summary = {
        "n_before_total": n_before_total,
        "n_after_total": len(merged),
        "n_added_total": len(merged) - n_before_total,
        "per_league": {
            league: {
                "before": counts_before.get(league, 0),
                "after": counts_after.get(league, 0),
                "added": counts_after.get(league, 0) - counts_before.get(league, 0),
            }
            for league in {**LEAGUE_REPO_DIRS, **LEAGUE_DIRECT_CODES, **LEAGUE_API_FOOTBALL_IDS}
        },
    }
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    summary = update_raw_data_file()
    print("\n=== Résumé mise à jour ===")
    print(f"Total : {summary['n_before_total']} -> {summary['n_after_total']} "
          f"({summary['n_added_total']:+d} lignes)")
    for league, s in summary["per_league"].items():
        print(f"  {league:15s} {s['before']:5d} -> {s['after']:5d}  ({s['added']:+d})")
