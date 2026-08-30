"""
scripts/odds_research_walkforward.py — Phase 8D : XFOOT ODDS RESEARCH
PROTOTYPE V1 + WALK-FORWARD EVALUATION V1.
=============================================================================
RECHERCHE UNIQUEMENT. Aucune écriture dans match / match_stats /
model_predictions / model_versions / team_ratings / prediction_log — voir
snapshot_db_counts() (réutilisée de scripts/feature_engineering_walkforward.py).
Aucun secret, aucune clé API : la SEULE source de données externe utilisée
(football-data.co.uk, candidat identifié en Phase 8C, §29 "BEST PRICE/VALUE")
ne nécessite AUCUNE authentification — CSV publics, téléchargement direct
(httpx, même bibliothèque que api/app/core/api_football_client.py).

=== Pourquoi football-data.co.uk et pas API-Football (§2/§3 du prompt) ===

Phase 8C a confirmé qu'API-Football (déjà intégré à Xfoot) ne conserve que
7 JOURS d'historique de cotes — structurellement incapable de reconstituer un
historique sur les 12459 matchs déjà en base (2019-2026). football-data.co.uk
est le seul candidat Phase 8C combinant FULL_HISTORY + gratuit + zéro
authentification, au prix d'une couverture partielle (5 des 11 ligues Xfoot —
les 5 précisément déjà chargées en base locale — jamais MLS/Saudi Pro
League/coupes européennes) et d'un timestamp NON mesuré par ligne (seule une
méthodologie de collecte est documentée par le fournisseur, jamais un
timestamp exact — voir api/app/ai/odds_research/core.py::
classify_source_timestamp_quality, toujours CAUTION, jamais SAFE).

=== Pourquoi le mapping équipe est un rapprochement EXACT (§26) ===

Voir docstring de api/app/ai/odds_research/core.py : Xfoot utilise DÉJÀ la
convention de nommage football-data.co.uk comme référence canonique interne
(confirmé par api/app/core/team_name_matching.py et vérifié empiriquement).

=== Cutoffs testés (§6) — LIMITATION HONNÊTE ===

football-data.co.uk n'expose que DEUX snapshots par match (pré-clôture,
clôture), jamais un timestamp continu — les horizons T-24h/T-12h/T-6h/T-3h/
T-1h demandés par le prompt ne sont PAS testables avec cette source (voir
rapport, §5 Timestamp Quality / §28 Limitations). PREDICTION_TIME_ODDS
(pré-clôture, colonnes SANS suffixe C) est utilisé pour TOUTES les
expériences de prédiction ; CLOSING_ODDS_REFERENCE (colonnes suffixées C)
n'est JAMAIS mélangé aux features de prédiction — utilisé uniquement comme
référence de marché (§8/§44/§46) et pour le calcul de mouvement (§15, feature
explicitement documentée comme non utilisable pour un horizon antérieur à la
clôture, §48).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/odds_research_walkforward.py \
        [--n-folds 4] [--seed 42] [--cache-dir <dir>]
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlmodel import Session, select  # noqa: E402

from app.core.database import engine, init_db  # noqa: E402
from app.models.match import Match  # noqa: E402
from app.ai.engine.features import build_ml_features_from_db, FEATURE_COLUMNS  # noqa: E402
from app.ai.arena.research import actual_outcome  # noqa: E402 — bootstrap/mcnemar/wilson réutilisés via fewf.compare_to_baseline
from app.ai.odds_research.core import (  # noqa: E402
    DIV_TO_LEAGUE, compute_1x2_odds_features, compute_ou25_odds_features,
    check_row_quality, classify_source_timestamp_quality, match_key,
    PRE_CLOSING_SOURCE, CLOSING_SOURCE,
)

import feature_engineering_walkforward as fewf  # noqa: E402 — réutilisation directe (§1 : ne pas dupliquer)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("odds_research_walkforward")

SEASONS = ["1920", "2021", "2122", "2223", "2324", "2425", "2526"]  # couvre 2019-08 -> 2026-05 (période Xfoot)
BASE_URL = "https://www.football-data.co.uk/mmz4281"
MIN_PAIRED_SAMPLE = 200  # seuil INSUFFICIENT_DATA pour ce dataset plus petit que Phase 8B (§40)


# ---------------------------------------------------------------------------
# 1. Téléchargement (isolé, aucun secret, cache local hors dépôt) — §65
# ---------------------------------------------------------------------------

def download_csv(div: str, season: str, cache_dir: Path) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{div}_{season}.csv"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = f"{BASE_URL}/{season}/{div}.csv"
    try:
        resp = httpx.get(url, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
        if len(resp.content) < 100:  # saison future/inexistante -> réponse vide ou quasi-vide
            return None
        dest.write_bytes(resp.content)
        return dest
    except httpx.HTTPError as e:
        logger.warning("Échec téléchargement %s : %s", url, e)
        return None


def download_all(cache_dir: Path) -> dict[tuple[str, str], Path]:
    files = {}
    for div in DIV_TO_LEAGUE:
        for season in SEASONS:
            path = download_csv(div, season, cache_dir)
            if path is not None:
                files[(div, season)] = path
    return files


# ---------------------------------------------------------------------------
# 2. Parsing + mapping vers `match` (§4, §25, §26)
# ---------------------------------------------------------------------------

def load_xfoot_matches() -> dict[tuple, dict]:
    """Index match_key(league, date, home, away) -> {"match_id", "home_goals",
    "away_goals", "date"} pour les 5 ligues couvertes par football-data.co.uk,
    LECTURE SEULE. `build_ml_features_from_db` (Phase 8B, réutilisé tel quel)
    ne conserve pas Match.id -> la jointure avec le baseline se fait PAR CLÉ
    (match_key), jamais par id, des deux côtés (voir build_odds_dataframe /
    main())."""
    with Session(engine) as session:
        rows = session.exec(select(Match).where(Match.league.in_(list(DIV_TO_LEAGUE.values())))).all()
    index = {}
    for m in rows:
        key = match_key(m.league, m.date.date(), m.home_team, m.away_team)
        index[key] = {"match_id": m.id, "home_goals": m.home_goals, "away_goals": m.away_goals, "date": m.date}
    return index


def parse_and_map(files: dict[tuple[str, str], Path], xfoot_index: dict) -> tuple[list[dict], dict]:
    """Parcourt chaque CSV, mappe vers `match`, construit une observation
    odds par match rapproché. Retourne (observations, quality_counts)."""
    observations = []
    quality = {
        "rows_read": 0, "missing_date_or_teams": 0, "unmapped": 0, "mapped": 0,
        "invalid_1x2_odds": 0, "invalid_ou25_odds": 0, "duplicate_mapped": 0,
        "future_date": 0,
    }
    seen_match_ids: set[int] = set()
    today = date.today()

    for (div, season), path in files.items():
        league = DIV_TO_LEAGUE[div]
        try:
            df = pd.read_csv(path, encoding="latin-1", low_memory=False)
        except Exception as e:  # fichier corrompu/vide — jamais fabriqué, simplement ignoré
            logger.warning("Impossible de lire %s : %s", path, e)
            continue

        for _, row in df.iterrows():
            quality["rows_read"] += 1
            row_dict = row.to_dict()
            reason = check_row_quality(row_dict)
            if reason is not None:
                quality["missing_date_or_teams"] += 1
                continue

            try:
                match_date = datetime.strptime(str(row_dict["Date"]), "%d/%m/%Y").date()
            except ValueError:
                try:
                    match_date = datetime.strptime(str(row_dict["Date"]), "%d/%m/%y").date()
                except ValueError:
                    quality["missing_date_or_teams"] += 1
                    continue

            if match_date > today:
                quality["future_date"] += 1
                continue

            key = match_key(league, match_date, str(row_dict["HomeTeam"]), str(row_dict["AwayTeam"]))
            xfoot_row = xfoot_index.get(key)
            if xfoot_row is None:
                quality["unmapped"] += 1
                continue
            if xfoot_row["match_id"] in seen_match_ids:
                quality["duplicate_mapped"] += 1
                continue
            seen_match_ids.add(xfoot_row["match_id"])
            quality["mapped"] += 1

            pre_1x2 = compute_1x2_odds_features(row_dict.get("B365H"), row_dict.get("B365D"), row_dict.get("B365A"))
            close_1x2 = compute_1x2_odds_features(row_dict.get("B365CH"), row_dict.get("B365CD"), row_dict.get("B365CA"))
            consensus_1x2 = compute_1x2_odds_features(row_dict.get("AvgH"), row_dict.get("AvgD"), row_dict.get("AvgA"))
            pre_ou25 = compute_ou25_odds_features(row_dict.get("B365>2.5"), row_dict.get("B365<2.5"))
            consensus_ou25 = compute_ou25_odds_features(row_dict.get("Avg>2.5"), row_dict.get("Avg<2.5"))

            if pre_1x2 is None:
                quality["invalid_1x2_odds"] += 1
            if pre_ou25 is None:
                quality["invalid_ou25_odds"] += 1

            observations.append({
                "match_id": xfoot_row["match_id"], "_key": key, "league": league, "date": xfoot_row["date"],
                "home_goals": xfoot_row["home_goals"], "away_goals": xfoot_row["away_goals"],
                "pre_1x2": pre_1x2, "close_1x2": close_1x2, "consensus_1x2": consensus_1x2,
                "pre_ou25": pre_ou25, "consensus_ou25": consensus_ou25,
                "pre_timestamp_quality": classify_source_timestamp_quality(PRE_CLOSING_SOURCE),
                "close_timestamp_quality": classify_source_timestamp_quality(CLOSING_SOURCE),
                "season": season, "div": div,
            })

    return observations, quality


# ---------------------------------------------------------------------------
# 3. Construction du dataset de recherche (§30) et fusion baseline (§18/§22)
# ---------------------------------------------------------------------------

def build_odds_dataframe(observations: list[dict]) -> pd.DataFrame:
    rows = []
    for o in observations:
        row = {"match_id": o["match_id"], "_key": o["_key"], "league": o["league"], "date": o["date"]}
        p = o["pre_1x2"]
        row["odds_norm_home"] = p["norm_home"] if p else np.nan
        row["odds_norm_draw"] = p["norm_draw"] if p else np.nan
        row["odds_norm_away"] = p["norm_away"] if p else np.nan
        row["odds_overround"] = p["overround"] if p else np.nan

        c = o["consensus_1x2"]
        row["odds_consensus_norm_home"] = c["norm_home"] if c else np.nan
        row["odds_consensus_norm_draw"] = c["norm_draw"] if c else np.nan
        row["odds_consensus_norm_away"] = c["norm_away"] if c else np.nan
        row["odds_consensus_overround"] = c["overround"] if c else np.nan

        cl = o["close_1x2"]
        if p and cl:
            row["odds_movement_home"] = cl["norm_home"] - p["norm_home"]
            row["odds_movement_draw"] = cl["norm_draw"] - p["norm_draw"]
            row["odds_movement_away"] = cl["norm_away"] - p["norm_away"]
        else:
            row["odds_movement_home"] = np.nan
            row["odds_movement_draw"] = np.nan
            row["odds_movement_away"] = np.nan

        pou = o["pre_ou25"]
        row["odds_ou25_norm_over"] = pou["norm_over"] if pou else np.nan
        row["odds_ou25_norm_under"] = pou["norm_under"] if pou else np.nan

        row["market_close_home"] = cl["norm_home"] if cl else np.nan
        row["market_close_draw"] = cl["norm_draw"] if cl else np.nan
        row["market_close_away"] = cl["norm_away"] if cl else np.nan

        rows.append(row)
    return pd.DataFrame(rows)


ODDS_FEATURE_GROUPS = {
    "odds_1x2": ["odds_norm_home", "odds_norm_draw", "odds_norm_away"],
    "odds_1x2_overround": ["odds_norm_home", "odds_norm_draw", "odds_norm_away", "odds_overround"],
    "odds_consensus": ["odds_consensus_norm_home", "odds_consensus_norm_draw", "odds_consensus_norm_away", "odds_consensus_overround"],
    "odds_movement": ["odds_movement_home", "odds_movement_draw", "odds_movement_away"],
    "odds_full": [
        "odds_norm_home", "odds_norm_draw", "odds_norm_away", "odds_overround",
        "odds_consensus_norm_home", "odds_consensus_norm_draw", "odds_consensus_norm_away", "odds_consensus_overround",
    ],
}


# ---------------------------------------------------------------------------
# 4. Comparaison marché vs Xfoot (§44/§46/§56)
# ---------------------------------------------------------------------------

def flatten_obs_with_league(exp_result: dict, folds: list[tuple[int, int]], df: pd.DataFrame) -> list[tuple[str, dict]]:
    """§18/§42 : ré-associe chaque observation de test à sa ligue d'origine,
    en reconstruisant l'ordre exact (obs_by_fold[i] correspond exactement à
    df.iloc[start:end] pour le fold i, même ordre — voir
    feature_engineering_walkforward.run_experiment). Aucune donnée
    supplémentaire calculée, uniquement un ré-étiquetage."""
    flat = []
    for fold_idx, fold_obs in enumerate(exp_result["obs_by_fold"]):
        if not fold_obs:
            continue
        start, end = folds[fold_idx]
        leagues = df.iloc[start:end]["league"].tolist()
        assert len(leagues) == len(fold_obs), f"désalignement fold {fold_idx} : {len(leagues)} vs {len(fold_obs)}"
        flat.extend(zip(leagues, fold_obs))
    return flat


def market_only_metrics(df: pd.DataFrame) -> dict:
    """Évalue la probabilité implicite de CLÔTURE elle-même comme prédicteur
    (jamais mélangée aux features de prédiction Xfoot, §8) — référence de
    marché pure (§46)."""
    obs = []
    for _, row in df.iterrows():
        if pd.isna(row["market_close_home"]):
            continue
        probs = {"home_win": row["market_close_home"], "draw": row["market_close_draw"], "away_win": row["market_close_away"]}
        actual = actual_outcome("1X2", row["home_goals"], row["away_goals"])
        predicted = max(probs, key=probs.get)
        obs.append({"p_true": probs[actual], "probs": probs, "actual": actual, "correct": predicted == actual})
    return fewf.aggregate_metrics(obs)


# ---------------------------------------------------------------------------
# 5. Rapport
# ---------------------------------------------------------------------------

def _fmt(v, digits=4):
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def render_markdown(result: dict) -> str:
    md = ["# XFOOT ODDS RESEARCH & WALK-FORWARD V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}.\n")
    md.append("\nRÈGLE ABSOLUE : RESEARCH ONLY. Aucune intégration production.\n")
    md.append(f"\n- Statut ODDS DATA : {result['odds_data_status']}\n")
    md.append(f"- Statut ODDS FEATURE : {result['odds_feature_status']}\n")
    md.append("- VALUE ENGINE : NOT BUILT IN PHASE 8D.\n- PRODUCTION : NO CHANGES.\n")
    for exp, v in result["group_verdicts"].items():
        md.append(f"\n- **{exp}** : {v}\n")

    md.append("\n## 2. Data Source\n")
    md.append(
        "\nfootball-data.co.uk (candidat Phase 8C, aucune authentification requise). Divisions téléchargées : "
        f"{', '.join(DIV_TO_LEAGUE.keys())} (= {', '.join(DIV_TO_LEAGUE.values())}). Saisons : {', '.join(SEASONS)}. "
        "API-Football (déjà intégré) écarté pour le backtest historique : Phase 8C a confirmé une fenêtre de "
        "7 jours d'historique de cotes seulement, structurellement incompatible avec les 12459 matchs 2019-2026 "
        "déjà en base.\n"
    )

    md.append("\n## 3. Dataset\n\n")
    ds = result["dataset"]
    md.append("| Metric | Value |\n|--------|-------|\n")
    md.append(f"| Total matches (5 ligues, base locale) | {ds['total_xfoot_matches']} |\n")
    md.append(f"| Matches with odds (1X2 pré-clôture valides) | {ds['matches_with_odds']} |\n")
    md.append(f"| Coverage | {ds['coverage_pct']:.1f}% |\n")
    md.append(f"| Leagues | {', '.join(ds['leagues'])} |\n")
    md.append(f"| Period | {ds['period_start']} → {ds['period_end']} |\n")

    md.append("\n## 4. Odds Coverage\n\n")
    md.append("| League | Matches | With Odds | Coverage |\n|---|---|---|---|\n")
    for row in result["coverage_by_league"]:
        md.append(f"| {row['league']} | {row['total']} | {row['with_odds']} | {row['coverage_pct']:.1f}% |\n")

    md.append("\n## 5. Timestamp Quality\n\n")
    md.append(
        "\nfootball-data.co.uk n'expose AUCUN timestamp mesuré par ligne — seule une méthodologie de collecte "
        "documentée par le fournisseur (vendredi/mardi après-midi pour le pré-clôture, juste avant coup d'envoi "
        "pour la clôture) est connue. **Tous** les snapshots PRE_CLOSING et CLOSING sont donc classés CAUTION, "
        "**jamais SAFE**. LIMITATION HONNÊTE (§6) : les horizons T-24h/T-12h/T-6h/T-3h/T-1h demandés ne sont PAS "
        "testables avec cette source — seuls 2 cutoffs existent (pré-clôture, clôture).\n"
    )

    md.append("\n## 6. Leakage Audit\n\n")
    md.append(
        "\nPREDICTION_TIME_ODDS (pré-clôture) utilisé pour TOUTES les expériences de prédiction (Experiments "
        "1/2/3/5). CLOSING_ODDS_REFERENCE (clôture) JAMAIS mélangé aux features de prédiction — utilisé "
        "uniquement pour §20 Odds vs Market (référence de marché pure) et pour le calcul de mouvement "
        "(Experiment 4, documenté comme scénario structurellement distinct — voir §10 Odds Movement). "
        "Tests de fuite synthétiques (odds après kickoff, timestamp futur, snapshot dupliqué) : voir "
        "api/test_odds_research_core.py et api/test_odds_research_pipeline.py.\n"
    )

    md.append("\n## 7. Odds Normalization\n\n")
    md.append("\nCotes décimales (format natif football-data.co.uk). Validation : odds > 1.0 strictement, rejet des valeurs <=1/nulles/infinies (§25) — jamais imputées.\n")

    md.append("\n## 8. Implied Probability\n\n")
    md.append("\nraw_implied_probability = 1/odds (§11, jamais appelée \"probabilité vraie\"). Retrait de marge proportionnel : p_normalized_i = p_i / sum(p_j) (§13) — méthode simple, aucune autre méthode de retrait de marge déjà utilisée dans le dépôt.\n")

    md.append("\n## 9. Overround\n\n")
    md.append(f"\nOverround moyen (marge bookmaker, Bet365 pré-clôture, 1X2) : {result['avg_overround']:.4f} sur les matchs avec cotes valides.\n")

    md.append("\n## 10. Odds Movement\n\n")
    md.append(
        "\nDelta (clôture - pré-clôture) des probabilités normalisées 1X2 — Experiment 4 uniquement. §15/§48 : "
        "cette feature EXISTE structurellement APRÈS la clôture ; un prédicteur réellement antérieur à la "
        "clôture ne pourrait PAS la connaître. Experiment 4 évalue donc un scénario hypothétique DISTINCT "
        "(prédicteur opérant au moment de la clôture), non directement comparable aux Experiments 1/2/3/5 "
        "(horizon pré-clôture, plusieurs jours avant kickoff).\n"
    )

    md.append("\n## 11. Market Consensus\n\n")
    md.append("\nColonne `Avg` fournie nativement par football-data.co.uk (moyenne multi-bookmaker déjà calculée par le fournisseur) — représentation simple et justifiable (§16), aucune agrégation supplémentaire (median/min/max/dispersion) construite sans nécessité démontrée.\n")

    md.append("\n## 12. Mapping\n\n")
    q = result["quality"]
    md.append(f"\n- Lignes lues : {q['rows_read']}\n- Rejetées (date/équipe manquante) : {q['missing_date_or_teams']}\n- Date future (contrôle sanité) : {q['future_date']}\n- Non rapprochées à `match` (REJECTED, §26) : {q['unmapped']}\n- Rapprochées : {q['mapped']}\n- Doublons de mapping ignorés : {q['duplicate_mapped']}\n- Cotes 1X2 invalides après rapprochement : {q['invalid_1x2_odds']}\n- Cotes O/U 2.5 invalides après rapprochement : {q['invalid_ou25_odds']}\n")

    md.append("\n## 13. Feature Sets\n\n")
    for g, cols in ODDS_FEATURE_GROUPS.items():
        md.append(f"- **{g}** : {', '.join(cols)}\n")

    md.append("\n## 14. Baseline\n\n")
    md.append(f"\nbaseline_v1 EXACTEMENT (Phase 8B) : {len(FEATURE_COLUMNS)} colonnes de api/app/ai/engine/features.py::FEATURE_COLUMNS + `league`, mêmes hyperparamètres XGBoost, seed={result['seed']}. Non modifié pour cette phase (§18).\n")

    md.append("\n## 15. Walk-Forward Methodology\n\n")
    md.append(f"\n{result['n_folds']} folds chronologiques (burn-in 20%), sur le sous-ensemble ODDS_SUBSET (matchs avec cotes 1X2 pré-clôture valides UNIQUEMENT — comparaison appariée, §22). Train = tout ce qui précède strictement le fold ; validation = 100 dernières lignes du train (early stopping) ; test = le fold. Jamais optimisé sur le test.\n")

    md.append("\n## 16. Experiments\n\n")
    md.append("| Experiment | N | LogLoss | Brier | Accuracy | Delta LogLoss | Status |\n|---|---|---|---|---|---|---|\n")
    baseline_ll = result["experiments"].get("EXPERIMENT_0_BASELINE", {}).get("overall", {}).get("log_loss")
    for name, exp in result["experiments"].items():
        ov = exp["overall"]
        delta = "N/A" if (baseline_ll is None or ov["log_loss"] is None) else f"{baseline_ll - ov['log_loss']:+.4f}"
        status = result["group_verdicts"].get(name, "—")
        md.append(f"| {name} | {ov['sample_size']} | {_fmt(ov['log_loss'])} | {_fmt(ov['brier'])} | {_fmt(ov['accuracy'])} | {delta} | {status} |\n")

    md.append("\n## 17. Fold Results\n\n")
    md.append("| Experiment | Fold | N | LogLoss | Brier | Accuracy |\n|---|---|---|---|---|---|\n")
    for name, exp in result["experiments"].items():
        for f in exp["folds"]:
            if not f.get("available"):
                md.append(f"| {name} | {f['fold']} | — | — | — | — ({f['reason']}) |\n")
            else:
                md.append(f"| {name} | {f['fold']} | {f['sample_size']} | {_fmt(f['log_loss'])} | {_fmt(f['brier'])} | {_fmt(f['accuracy'])} |\n")

    md.append("\n## 18. League Results\n\n")
    md.append("| League | N | Baseline LogLoss | Odds LogLoss | Delta | Status |\n|---|---|---|---|---|---|\n")
    for row in result["league_results"]:
        md.append(f"| {row['league']} | {row['n']} | {_fmt(row['baseline_ll'])} | {_fmt(row['odds_ll'])} | {row['delta']} | {row['status']} |\n")

    md.append("\n## 19. Market Results\n\n")
    md.append("| Market | N | Baseline | Odds | Delta | Status |\n|---|---|---|---|---|---|\n")
    for row in result["market_results"]:
        md.append(f"| {row['market']} | {row['n']} | {_fmt(row['baseline'])} | {_fmt(row['odds'])} | {row['delta']} | {row['status']} |\n")

    md.append("\n## 20. Odds vs Market\n\n")
    md.append("| Market | N | Market LogLoss | Xfoot LogLoss | Xfoot+Odds LogLoss |\n|---|---|---|---|---|\n")
    for row in result["odds_vs_market"]:
        md.append(f"| {row['market']} | {row['n']} | {_fmt(row['market_ll'])} | {_fmt(row['xfoot_ll'])} | {_fmt(row['xfoot_odds_ll'])} |\n")

    md.append("\n## 21. Feature Importance\n\nImportance = gain XGBoost. IMPORTANCE ≠ CAUSALITÉ (§57).\n\n")
    for name, exp in result["experiments"].items():
        if name == "EXPERIMENT_0_BASELINE":
            continue
        md.append(f"\n**{name}** (top odds features) :\n\n| Feature | Importance (gain %) |\n|---|---|\n")
        odds_cols = set(sum(ODDS_FEATURE_GROUPS.values(), []))
        for row in exp["feature_importance"]:
            if row["feature"] in odds_cols:
                md.append(f"| {row['feature']} | {row['gain_pct']:.2f}% |\n")

    md.append("\n## 22. Statistical Tests\n\nbootstrap_paired_diff/mcnemar_test/wilson_interval réutilisés de api/app/ai/arena/research.py (§38), jamais réimplémentés.\n")

    md.append("\n## 23. Stability\n\n")
    md.append("Voir colonne \"Delta LogLoss\" et folds improved/worsened par expérience (§41) dans le JSON du rapport (comparisons).\n")

    md.append("\n## 24. Reproducibility\n\n")
    md.append(f"\nseed={result['seed']}, source=football-data.co.uk (saisons {', '.join(SEASONS)}), feature groups documentés §13 — un run identique sur le même cache produit les mêmes features et métriques.\n")

    md.append("\n## 25. Database Safety\n\n")
    md.append(f"\nCompteurs AVANT : {result['db_counts_before']}\n\nCompteurs APRÈS : {result['db_counts_after']}\n\nIdentiques : {result['db_unchanged']}\n")

    md.append("\n## 26. Production Isolation\n\nAucune écriture dans model_predictions/model_versions/prediction_log/team_ratings/active model/scheduler/endpoints/frontend. Entraînements XGBoost en mémoire, jamais persistés.\n")

    md.append("\n## 27. Results\n\n")
    md.append(f"\n{result['results_summary']}\n")

    md.append("\n## 28. Limitations\n\n")
    for item in result["limitations"]:
        md.append(f"- {item}\n")

    md.append("\n## 29. Recommendation\n\n")
    md.append(f"\n{result['recommendation']}\n")

    md.append("\n## 30. Phase 8E Recommendations\n\n")
    for item in result["recommendations_phase_8e"]:
        md.append(f"- {item}\n")

    md.append("\n---\n\n### ODDS DATA\n\n" + result["odds_data_status"] + "\n")
    md.append("\n### ODDS FEATURE\n\n" + result["odds_feature_status"] + "\n")
    md.append("\n### VALUE ENGINE\n\nNOT BUILT IN PHASE 8D.\n")
    md.append("\n### PRODUCTION\n\nNO CHANGES.\n")

    md.append("\n---\n\nPHASE 8D — XFOOT ODDS RESEARCH & WALK-FORWARD V1 TERMINÉE. "
               "AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. "
               "EN ATTENTE DE VALIDATION.\n")

    return "".join(md)


def write_reports(result: dict, outdir: Path, run_id: str) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"odds_backtest_{run_id}.json"
    md_path = outdir / f"odds_backtest_{run_id}.md"
    serializable = {k: v for k, v in result.items() if k != "experiments"}
    serializable["experiments"] = {
        name: {k: v for k, v in exp.items() if k != "obs_by_fold"} for name, exp in result["experiments"].items()
    }
    json_path.write_text(json.dumps(serializable, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# 6. main()
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-folds", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cache-dir", type=str, default=None)
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else Path.home() / ".xfoot_research_cache" / "odds_football_data_co_uk"

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    logger.info("Téléchargement football-data.co.uk (cache=%s)...", cache_dir)
    files = download_all(cache_dir)
    logger.info("%d fichiers disponibles.", len(files))

    if not files:
        logger.error("INSUFFICIENT_DATA_FOR_ODDS_RESEARCH — aucune donnée téléchargée.")
        _write_insufficient_data_report(args, db_before)
        return

    xfoot_index = load_xfoot_matches()
    observations, quality = parse_and_map(files, xfoot_index)
    logger.info("Observations mappées : %d / %d lignes lues.", quality["mapped"], quality["rows_read"])

    odds_df = build_odds_dataframe(observations)
    valid_1x2 = odds_df[odds_df["odds_norm_home"].notna()].copy()

    if len(valid_1x2) < MIN_PAIRED_SAMPLE:
        logger.error("INSUFFICIENT_DATA_FOR_ODDS_RESEARCH — %d observations 1X2 valides < seuil %d.", len(valid_1x2), MIN_PAIRED_SAMPLE)
        _write_insufficient_data_report(args, db_before, quality=quality, n_valid=len(valid_1x2))
        return

    logger.info("Construction du baseline (Phase 8B, réutilisé tel quel)...")
    baseline_df = build_ml_features_from_db(leagues=list(DIV_TO_LEAGUE.values()))
    baseline_df = baseline_df[baseline_df["dc_home_win"].notna()].reset_index(drop=True)

    # build_ml_features_from_db (Phase 8B, réutilisé tel quel) ne conserve pas
    # Match.id -> jointure baseline/odds PAR CLÉ déterministe (league, date,
    # home_team, away_team), même clé des deux côtés (voir docstring
    # load_xfoot_matches). Jamais par index positionnel, jamais par un id
    # reconstruit après coup.
    baseline_df = baseline_df.copy()
    baseline_df["_key"] = baseline_df.apply(
        lambda r: match_key(r["league"], (r["date"].date() if hasattr(r["date"], "date") else r["date"]), r["home_team"], r["away_team"]),
        axis=1,
    )
    merged = baseline_df.merge(
        valid_1x2.drop(columns=["league", "date", "match_id"]), on="_key", how="inner",
    )
    merged = merged.sort_values("date", kind="stable").reset_index(drop=True)
    n = len(merged)
    logger.info("Dataset apparié (baseline ∩ odds valides) : %d matchs.", n)

    if n < MIN_PAIRED_SAMPLE:
        logger.error("INSUFFICIENT_DATA_FOR_ODDS_RESEARCH — dataset apparié %d < seuil %d après fusion baseline.", n, MIN_PAIRED_SAMPLE)
        _write_insufficient_data_report(args, db_before, quality=quality, n_valid=n)
        return

    y = fewf.build_target(merged)
    folds = fewf.make_folds(n, args.n_folds, fewf.BURN_IN_FRACTION)

    experiments: dict[str, dict] = {}
    exp_specs = {
        "EXPERIMENT_0_BASELINE": [],
        "EXPERIMENT_1_ODDS_1X2": ODDS_FEATURE_GROUPS["odds_1x2"],
        "EXPERIMENT_2_ODDS_OVERROUND": ODDS_FEATURE_GROUPS["odds_1x2_overround"],
        "EXPERIMENT_3_MARKET_CONSENSUS": ODDS_FEATURE_GROUPS["odds_consensus"] if merged["odds_consensus_norm_home"].notna().any() else None,
        "EXPERIMENT_4_ODDS_MOVEMENT": ODDS_FEATURE_GROUPS["odds_movement"] if merged["odds_movement_home"].notna().any() else None,
        "EXPERIMENT_5_ODDS_FULL": ODDS_FEATURE_GROUPS["odds_full"] if merged["odds_consensus_norm_home"].notna().any() else ODDS_FEATURE_GROUPS["odds_1x2_overround"],
    }
    skipped = {}
    for name, extra_cols in exp_specs.items():
        if extra_cols is None:
            skipped[name] = "INSUFFICIENT_DATA — colonnes requises absentes du dataset (aucune donnée de consensus/mouvement exploitable)."
            continue
        feature_columns = list(FEATURE_COLUMNS) + extra_cols
        logger.info("Expérience %s (%d colonnes)...", name, len(feature_columns))
        experiments[name] = fewf.run_experiment(name, feature_columns, merged, y, folds, args.seed)

    baseline_result = experiments["EXPERIMENT_0_BASELINE"]
    comparisons, group_verdicts = {}, {"EXPERIMENT_0_BASELINE": "BASELINE"}
    for name in list(experiments.keys()):
        if name == "EXPERIMENT_0_BASELINE":
            continue
        # fewf.compare_to_baseline applique déjà son propre seuil INSUFFICIENT_DATA
        # (fewf.MIN_PAIRED_SAMPLE=300, réutilisé tel quel, §38 — pas de deuxième
        # implémentation statistique).
        cmp = fewf.compare_to_baseline(baseline_result, experiments[name])
        comparisons[name] = cmp
        group_verdicts[name] = cmp["verdict"]
    for name, reason in skipped.items():
        group_verdicts[name] = "INSUFFICIENT_DATA"
        comparisons[name] = {"verdict": "INSUFFICIENT_DATA", "reason": reason, "n_paired": 0}

    market_metrics = market_only_metrics(merged)

    coverage_by_league = []
    total_all = 0
    with_odds_all = 0
    for league in DIV_TO_LEAGUE.values():
        total = int((baseline_df["league"] == league).sum()) if "league" in baseline_df.columns else 0
        with_odds = int((merged["league"] == league).sum())
        total_all += total
        with_odds_all += with_odds
        coverage_by_league.append({
            "league": league, "total": total, "with_odds": with_odds,
            "coverage_pct": 100.0 * with_odds / total if total else 0.0,
        })

    best_odds_exp = experiments.get("EXPERIMENT_5_ODDS_FULL", experiments.get("EXPERIMENT_2_ODDS_OVERROUND"))
    MIN_LEAGUE_SAMPLE = 50  # §42 : ne jamais déclarer une conclusion par ligue sur un échantillon trop faible
    league_results = []
    if best_odds_exp is not None:
        baseline_flat = flatten_obs_with_league(baseline_result, folds, merged)
        odds_flat = flatten_obs_with_league(best_odds_exp, folds, merged)
        for league in DIV_TO_LEAGUE.values():
            b_obs = [o for lg, o in baseline_flat if lg == league]
            o_obs = [o for lg, o in odds_flat if lg == league]
            n_league = len(b_obs)
            if n_league < MIN_LEAGUE_SAMPLE:
                league_results.append({"league": league, "n": n_league, "baseline_ll": None, "odds_ll": None, "delta": "N/A", "status": "INSUFFICIENT_DATA"})
                continue
            b_ll = fewf.aggregate_metrics(b_obs)["log_loss"]
            o_ll = fewf.aggregate_metrics(o_obs)["log_loss"]
            delta = f"{b_ll - o_ll:+.4f}" if (b_ll is not None and o_ll is not None) else "N/A"
            league_results.append({
                "league": league, "n": n_league, "baseline_ll": b_ll, "odds_ll": o_ll, "delta": delta,
                "status": "BETTER" if (b_ll is not None and o_ll is not None and o_ll < b_ll) else "EQUIVALENT_OR_WORSE",
            })

    market_results = [
        {"market": "1X2", "n": n, "baseline": baseline_result["overall"]["log_loss"],
         "odds": experiments.get("EXPERIMENT_5_ODDS_FULL", experiments.get("EXPERIMENT_2_ODDS_OVERROUND", baseline_result))["overall"]["log_loss"],
         "delta": _delta(baseline_result, experiments.get("EXPERIMENT_5_ODDS_FULL", experiments.get("EXPERIMENT_2_ODDS_OVERROUND"))),
         "status": group_verdicts.get("EXPERIMENT_5_ODDS_FULL", group_verdicts.get("EXPERIMENT_2_ODDS_OVERROUND", "N/A"))},
        {"market": "BTTS", "n": 0, "baseline": None, "odds": None, "delta": "N/A", "status": "INSUFFICIENT_DATA (BTTS absent de football-data.co.uk, confirmé Phase 8C)"},
        {"market": "OVER_UNDER_2_5", "n": int(merged["odds_ou25_norm_over"].notna().sum()), "baseline": None, "odds": None, "delta": "N/A", "status": "NOT_TESTED (hors périmètre minimal de ce run — feature odds O/U calculée et disponible dans le dataset, expérimentation dédiée laissée à une itération future si le signal 1X2 le justifie)"},
    ]

    odds_vs_market = [{
        "market": "1X2", "n": market_metrics["sample_size"],
        "market_ll": market_metrics["log_loss"], "xfoot_ll": baseline_result["overall"]["log_loss"],
        "xfoot_odds_ll": experiments.get("EXPERIMENT_5_ODDS_FULL", experiments.get("EXPERIMENT_2_ODDS_OVERROUND"))["overall"]["log_loss"],
    }]

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_unchanged = db_before == db_after

    avg_overround = float(merged["odds_overround"].dropna().mean()) if merged["odds_overround"].notna().any() else float("nan")

    any_better = any(v == "BETTER" for v in group_verdicts.values())
    odds_feature_status = "🟢 RESEARCH CANDIDATE" if any_better else ("🟡 NEEDS MORE DATA" if n < 1000 else "🔴 REJECTED")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = {
        "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(), "seed": args.seed, "n_folds": args.n_folds,
        "odds_data_status": "🟡 PARTIAL",
        "odds_feature_status": odds_feature_status,
        "dataset": {
            "total_xfoot_matches": total_all, "matches_with_odds": with_odds_all,
            "coverage_pct": 100.0 * with_odds_all / total_all if total_all else 0.0,
            "leagues": list(DIV_TO_LEAGUE.values()),
            "period_start": str(merged["date"].min()), "period_end": str(merged["date"].max()),
        },
        "coverage_by_league": coverage_by_league,
        "quality": quality,
        "avg_overround": avg_overround,
        "experiments": experiments,
        "comparisons": comparisons,
        "group_verdicts": group_verdicts,
        "league_results": league_results,
        "market_results": market_results,
        "odds_vs_market": odds_vs_market,
        "db_counts_before": db_before, "db_counts_after": db_after, "db_unchanged": db_unchanged,
        "results_summary": f"Verdicts par expérience : {group_verdicts}.",
        "limitations": [
            "football-data.co.uk ne couvre que 5 des 11 ligues Xfoot (les 5 déjà en base locale) — aucune conclusion étendue à MLS/Saudi Pro League/coupes européennes.",
            "Aucun timestamp mesuré par ligne — pré-clôture/clôture classés CAUTION (jamais SAFE), horizons T-24h/T-12h/T-6h/T-3h/T-1h non testables (§6).",
            "BTTS absent de la source, marché non testé.",
            "OVER_UNDER_2_5 : feature calculée mais expérimentation walk-forward dédiée non exécutée dans ce run (périmètre minimal).",
            f"Dataset apparié plus petit que le baseline complet Phase 8B ({n} vs {len(baseline_df)}) — comparaisons systématiquement appariées (§22), jamais baseline_N vs odds_N direct.",
            "Marge retirée par méthode proportionnelle simple (§13) — pas de méthode plus sophistiquée (Shin, etc.) testée.",
        ],
        "recommendation": (
            "RESEARCH_CANDIDATE — jamais promu automatiquement (§59/§60), voir Experiments/Statistical Tests pour "
            "le détail chiffré." if any_better else
            "Aucune expérience odds n'a démontré une amélioration stable et statistiquement crédible sur ce "
            "dataset — DO NOT PROMOTE. Résultat honnête même s'il est négatif ou nul (§74)."
        ),
        "recommendations_phase_8e": [
            "Si une intégration future est envisagée : lever d'abord le doute légal sur football-data.co.uk (licence non confirmée, Phase 8C) avant tout usage prolongé.",
            "Étendre le test à O/U 2.5 (feature déjà calculée dans ce dataset) si le signal 1X2 s'avère prometteur sur un run élargi.",
            "Envisager un timestamp plus fin (fournisseur payant, ex. The Odds API depuis 2020) uniquement si ce prototype démontre un signal réel — jamais avant.",
        ],
    }

    outdir = Path(__file__).resolve().parent.parent / "reports" / "odds"
    json_path, md_path = write_reports(result, outdir, run_id)
    logger.info("Rapport écrit : %s / %s", json_path, md_path)

    print("\n" + "=" * 80)
    print("PHASE 8D — XFOOT ODDS RESEARCH & WALK-FORWARD V1 TERMINÉE.")
    print("AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


def _delta(baseline_result, other_result) -> str:
    if other_result is None:
        return "N/A"
    bll, oll = baseline_result["overall"]["log_loss"], other_result["overall"]["log_loss"]
    if bll is None or oll is None:
        return "N/A"
    return f"{bll - oll:+.4f}"


def _write_insufficient_data_report(args, db_before, quality=None, n_valid=None):
    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = {
        "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(), "seed": args.seed, "n_folds": args.n_folds,
        "odds_data_status": "🔴 NOT AVAILABLE",
        "odds_feature_status": "🔴 REJECTED",
        "status": "INSUFFICIENT_DATA_FOR_ODDS_RESEARCH",
        "quality": quality or {},
        "n_valid_observations": n_valid,
        "db_counts_before": db_before, "db_counts_after": db_after, "db_unchanged": db_before == db_after,
        "reason": (
            "Aucune source football-data.co.uk exploitable n'a produit assez d'observations 1X2 valides "
            f"(seuil minimum {MIN_PAIRED_SAMPLE}) après téléchargement, rapprochement et contrôle qualité."
        ),
    }
    outdir = Path(__file__).resolve().parent.parent / "reports" / "odds"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"odds_backtest_{run_id}.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md = (
        "# XFOOT ODDS RESEARCH & WALK-FORWARD V1\n\n## Résultat\n\nINSUFFICIENT_DATA_FOR_ODDS_RESEARCH\n\n"
        f"{result['reason']}\n\n---\n\nPHASE 8D — XFOOT ODDS RESEARCH & WALK-FORWARD V1 TERMINÉE. "
        "AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n"
    )
    (outdir / f"odds_backtest_{run_id}.md").write_text(md, encoding="utf-8")
    print("INSUFFICIENT_DATA_FOR_ODDS_RESEARCH — rapport écrit dans", outdir)


if __name__ == "__main__":
    main()
