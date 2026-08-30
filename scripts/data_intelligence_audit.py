"""
scripts/data_intelligence_audit.py — Phase 8A : XFOOT DATA INTELLIGENCE
FOUNDATION V1 + FEATURE REGISTRY V1, rapport.
=============================================================================

LECTURE SEULE — aucune écriture DB, aucune migration, aucun réentraînement.
Lit `match`/`match_stats`/`model_predictions`/`model_versions`/`team_ratings`
en lecture seule pour produire des statistiques de couverture réelles
(jamais supposées), dump le Feature Registry (api/app/ai/features/registry.py,
code, aucune table), et écrit UNIQUEMENT des fichiers sous reports/data/.

NE modifie AUCUN modèle, endpoint, scheduler, dashboard, ni prédiction
historique. NE réentraîne rien (§44 du prompt : l'audit doit rester léger).

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/data_intelligence_audit.py \
        [--outdir reports/data]
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_API_DIR = _SCRIPTS_DIR.parent / "api"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_API_DIR))

from sqlmodel import Session, func, select  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.match import Match, MatchStats  # noqa: E402
from app.models.model_prediction import ModelPrediction  # noqa: E402
from app.models.team_rating import ModelVersion, TeamRating  # noqa: E402
from app.ai.features.registry import (  # noqa: E402
    FEATURE_REGISTRY, LEAKAGE_RISKS, STATUSES, list_by_leakage_risk, list_by_status,
    traffic_light, validate_registry,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("data_intelligence_audit")


# ---------------------------------------------------------------------------
# 1. Inventaire des sources — lecture seule, chiffres réels (§2/§3 du prompt).
# ---------------------------------------------------------------------------

def _match_coverage(session: Session) -> dict:
    total = session.exec(select(func.count()).select_from(Match)).one()
    leagues = sorted(session.exec(select(Match.league).distinct()).all())
    date_min = session.exec(select(func.min(Match.date))).one()
    date_max = session.exec(select(func.max(Match.date))).one()

    stats_total = session.exec(select(func.count()).select_from(MatchStats)).one()
    stats_complete = session.exec(
        select(func.count()).select_from(MatchStats).where(
            MatchStats.home_shots.is_not(None), MatchStats.away_shots.is_not(None),
            MatchStats.home_shots_target.is_not(None), MatchStats.away_shots_target.is_not(None),
            MatchStats.home_corners.is_not(None), MatchStats.away_corners.is_not(None),
        )
    ).one()

    per_league = {}
    for lg in leagues:
        n = session.exec(select(func.count()).select_from(Match).where(Match.league == lg)).one()
        per_league[lg] = n

    return {
        "total_matches": total, "leagues": leagues, "n_leagues": len(leagues),
        "date_min": str(date_min), "date_max": str(date_max),
        "match_stats_rows": stats_total, "match_stats_complete": stats_complete,
        "match_stats_coverage_ratio": round(stats_complete / total, 4) if total else None,
        "per_league_match_count": per_league,
    }


def _model_prediction_coverage(session: Session) -> dict:
    by_model = {}
    model_types = session.exec(select(ModelPrediction.model_type).distinct()).all()
    for mt in sorted(model_types):
        n_total = session.exec(select(func.count()).select_from(ModelPrediction).where(ModelPrediction.model_type == mt)).one()
        n_resolved = session.exec(
            select(func.count()).select_from(ModelPrediction).where(ModelPrediction.model_type == mt, ModelPrediction.status == "resolved")
        ).one()
        by_model[mt] = {"total": n_total, "resolved": n_resolved}
    n_versions = session.exec(select(func.count()).select_from(ModelVersion)).one()
    n_ratings = session.exec(select(func.count()).select_from(TeamRating)).one()
    return {"by_model_type": by_model, "model_versions": n_versions, "team_ratings": n_ratings}


DATA_SOURCES_INVENTORY = [
    # Source | Existe | Historique | Timestamp | Qualité | Utilisable
    {"source": "match (DB, ex-CSV all_leagues_raw_with_stats.csv)", "exists": True, "historical": True,
     "timestamped": "Date seule, sans heure (naïve) — aucune heure de coup d'envoi persistée", "quality": "Bonne (NOT NULL sur les champs clés)", "usable": True},
    {"source": "match_stats (tirs/corners)", "exists": True, "historical": True,
     "timestamped": "Hérité de match.date", "quality": "Partielle — voir couverture réelle calculée ci-dessous", "usable": True},
    {"source": "team_ratings (Elo, Dixon-Coles)", "exists": True, "historical": "Snapshot le plus récent uniquement, pas de série temporelle", "timestamped": "computed_at",
     "quality": "Bonne pour l'usage servi, pas pour une reconstruction historique arbitraire", "usable": True},
    {"source": "api/model_artifacts/*.json (Dixon-Coles production)", "exists": True, "historical": "Snapshot le plus récent uniquement", "timestamped": "data_up_to",
     "quality": "Bonne", "usable": True},
    {"source": "model_predictions / prediction_log", "exists": True, "historical": True, "timestamped": "predicted_at / created_at",
     "quality": "Bonne", "usable": True},
    {"source": "API-Football (fixtures, scores, logos)", "exists": True, "historical": False,
     "timestamped": "fixture.date (UTC, avec heure)", "quality": "Externe, dépend de la disponibilité réseau/quota", "usable": "Live uniquement, jamais pour l'historique persisté"},
    {"source": "Bookmaker odds", "exists": False, "historical": "N/A", "timestamped": "N/A", "quality": "N/A", "usable": False},
    {"source": "Injuries / suspensions", "exists": False, "historical": "N/A", "timestamped": "N/A", "quality": "N/A", "usable": False},
    {"source": "Lineups", "exists": False, "historical": "N/A", "timestamped": "N/A", "quality": "N/A", "usable": False},
    {"source": "Weather", "exists": False, "historical": "N/A", "timestamped": "N/A", "quality": "N/A", "usable": False},
    {"source": "Standings / league table", "exists": False, "historical": "N/A (calculable par agrégation, non implémenté)", "timestamped": "N/A", "quality": "N/A", "usable": False},
    {"source": "season / season_id", "exists": False, "historical": "N/A (dérivable, jamais persisté)", "timestamped": "N/A", "quality": "N/A", "usable": False},
]

TEAM_LEAGUE_NORMALIZATION_FINDINGS = """
Aucun identifiant canonique d'équipe (`team_id`) n'existe dans le dépôt — chaque référence à une équipe est une
chaîne de caractères exacte. DEUX mécanismes de normalisation fuzzy INDÉPENDANTS coexistent :

1. `api/app/core/team_name_matching.py` — 18 alias codés en dur, normalisation NFKD/minuscule, repli
   `difflib.SequenceMatcher.ratio() >= 0.6`. Résout SILENCIEUSEMENT sur un match fuzzy (booléen simple,
   `names_match()`), utilisé pour rapprocher les noms d'équipes API-Football lors de la résolution de résultats.
2. `api/main.py` (~300 lignes, `TEAM_ALIASES` + `resolve_team_name()`) — table d'alias totalement séparée, même
   seuil fuzzy 0.6, mais NE résout JAMAIS silencieusement en fuzzy : retourne toujours des suggestions à l'appelant.

Ces deux tables ne sont pas synchronisées entre elles — une correction apportée à l'une ne se propage pas à
l'autre. Risque de faux positif documenté ici (seuil 0.6 sur des noms de clubs courts et proches) mais NON corrigé
dans cette phase (audit uniquement, aucune modification du code de normalisation).

La normalisation de LIGUE, à l'inverse, est un dictionnaire EXACT {nom: id API-Football} (11 ligues) — aucun
risque de faux positif, mais toute compétition hors de cette liste est silencieusement ignorée partout où les
fixtures sont ingérées.
"""

FEATURE_CORRELATION_NOTES = """
Revue manuelle (§36 du prompt — pas de calcul de corrélation automatisé, qui nécessiterait de rejouer tout le
pipeline de features sur l'historique complet, hors périmètre "audit léger" de cette phase) :

- `dc_home_win`/`dc_draw`/`dc_away_win` (Dixon-Coles) et `home_form_points_avg`/`away_form_points_avg` sont deux
  proxies DIFFÉRENTS de la force relative des équipes (l'un un modèle statistique appris sur tout l'historique de
  la ligue, l'autre une moyenne glissante sur 5 matchs) — corrélation attendue, mais pas une duplication
  d'information au sens strict (fenêtres temporelles et méthodes différentes).
- `home_current_streak` et `home_form_points_avg` capturent tous deux une notion de "forme récente" mais sur des
  définitions différentes (série signée vs moyenne de points) — candidats à une revue de redondance si un futur
  travail de sélection de features est entrepris, jamais tranché ici.
- Aucune paire de colonnes strictement identique (même définition, deux noms) n'a été identifiée dans les 25
  features de production.
"""


def _availability_matrix() -> list[dict]:
    families = {
        "Team Strength": ["dixon_coles_attack", "dixon_coles_defense", "dixon_coles_home_advantage", "dixon_coles_rho", "elo_rating"],
        "Form": ["home_form_points_avg", "away_form_points_avg", "home_current_streak", "away_current_streak"],
        "Goals": ["home_form_goals_scored_avg", "home_form_goals_conceded_avg"],
        "Head-to-Head": ["h2h_matches_found", "h2h_home_win_rate"],
        "Ranking": ["league_standing"],
        "Schedule/Rest": ["home_days_since_last_match", "matches_last_7_days", "matches_last_14_days"],
        "Injuries": ["injuries"], "Suspensions": ["suspensions"], "Lineups": ["lineups"],
        "Odds": ["odds_opening"], "Odds Movement": ["odds_movement"], "Market Probability": ["implied_probability"],
        "Weather": ["weather"],
    }
    rows = []
    for family, examples in families.items():
        sample = FEATURE_REGISTRY.get(examples[0])
        if sample is None:
            continue
        rows.append({
            "family": family, "source": sample.source.split(" ; ")[0].split(" (")[0][:60],
            "historical": sample.status in ("PRODUCTION", "AVAILABLE"),
            "timestamped": sample.timestamp_field is not None,
            "coverage": sample.availability, "leakage_risk": sample.leakage_risk,
        })
    return rows


def _priority_matrix() -> list[dict]:
    rows = []
    for fd in FEATURE_REGISTRY.values():
        if fd.status in ("MISSING", "EXPERIMENTAL") and fd.priority:
            rows.append({
                "feature": fd.feature_name, "quality": fd.availability[:50],
                "coverage": "N/A" if fd.status == "MISSING" else "dérivable",
                "leakage_risk": fd.leakage_risk, "priority": fd.priority,
            })
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "REJECTED": 4}
    rows.sort(key=lambda r: order.get(r["priority"], 9))
    return rows


def main(outdir: str = "reports/data"):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    registry_problems = validate_registry()

    init_db()
    with Session(engine) as session:
        match_cov = _match_coverage(session)
        model_cov = _model_prediction_coverage(session)

    status_counts = {s: len(list_by_status(s)) for s in STATUSES}
    leakage_counts = {r: len(list_by_leakage_risk(r)) for r in LEAKAGE_RISKS}
    traffic_counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    for fd in FEATURE_REGISTRY.values():
        traffic_counts[traffic_light(fd)] += 1

    feature_rows = [
        {"feature": fd.feature_name, "source": fd.source.split(" ; ")[0].split(" (")[0][:50], "type": fd.data_type,
         "cutoff": fd.cutoff_rule[:60], "leakage": fd.leakage_risk, "status": fd.status, "traffic_light": traffic_light(fd)}
        for fd in sorted(FEATURE_REGISTRY.values(), key=lambda f: (f.category, f.feature_name))
    ]

    data_foundation_verdict = "READY" if match_cov["total_matches"] > 1000 and not registry_problems else "NEEDS_IMPROVEMENT"
    feature_registry_verdict = "READY" if not registry_problems else "NEEDS_IMPROVEMENT"

    CSV_SOURCE_LEAGUES = {
        "Bundesliga", "ChampionsLeague", "ConferenceLeague", "EuropaLeague", "LaLiga", "Ligue1",
        "MLS", "PremierLeague", "PrimeiraLiga", "SaudiProLeague", "SerieA",
    }
    leagues_not_loaded = sorted(CSV_SOURCE_LEAGUES - set(match_cov["leagues"]))
    mc_leagues_str = f"{match_cov['n_leagues']} ligue(s) ({', '.join(match_cov['leagues'])})"

    limitations = [
        "match.date n'a jamais d'heure de coup d'envoi (100% des lignes historiques à minuit) — toute règle de "
        "cutoff au jour près, jamais à l'heure près, pour l'historique persisté.",
        "Aucun identifiant canonique d'équipe — deux tables d'alias fuzzy indépendantes et non synchronisées "
        "(voir section Team Normalization).",
        "Les ratings Dixon-Coles/Elo en base (team_ratings, api/model_artifacts) ne sont que des snapshots les "
        "plus récents — aucune série temporelle n'est persistée ; seule research.py::build_dixon_coles_walk_forward "
        "reconstruit un état historique fidèle à une date arbitraire (recherche/backtest uniquement).",
        (
            f"La base `match` de CET environnement ne couvre que {mc_leagues_str} — {len(leagues_not_loaded)} ligue(s) "
            f"présente(s) dans data/all_leagues_raw_with_stats.csv ({', '.join(leagues_not_loaded)}) n'y sont PAS "
            f"chargées. Couverture match_stats calculée ci-dessus ({_fmt(match_cov['match_stats_coverage_ratio'])}) "
            f"porte donc UNIQUEMENT sur ce sous-ensemble déjà chargé, pas sur le CSV source complet (dont la "
            f"couverture réelle est nettement plus faible, ~57%, sur les ligues ajoutées plus récemment)."
        ) if leagues_not_loaded else "",
        "Aucune donnée de marché (cotes), contextuelle (blessures/compositions/météo) n'existe dans le dépôt — "
        "documenté comme SOURCE CANDIDATE (§45), aucune intégration décidée dans cette phase.",
    ]
    limitations = [l for l in limitations if l]

    result = {
        "status": "ok", "run_id": run_id, "generated_at": generated_at,
        "data_sources_inventory": DATA_SOURCES_INVENTORY,
        "match_coverage": match_cov, "model_prediction_coverage": model_cov,
        "availability_matrix": _availability_matrix(),
        "feature_registry": feature_rows, "status_counts": status_counts, "leakage_counts": leakage_counts,
        "traffic_light_counts": traffic_counts, "registry_validation_problems": registry_problems,
        "priority_matrix": _priority_matrix(),
        "team_league_normalization": TEAM_LEAGUE_NORMALIZATION_FINDINGS,
        "feature_correlation_notes": FEATURE_CORRELATION_NOTES,
        "limitations": limitations,
        "verdicts": {
            "data_foundation": data_foundation_verdict, "feature_registry": feature_registry_verdict,
            "external_data_candidates": ["odds (P2, nécessite un fournisseur externe)", "injuries (P2, endpoint API-Football /injuries jamais appelé)",
                                          "league_standing (P1, calculable par agrégation, aucune source externe requise)",
                                          "season (P1, dérivable par règle documentée, aucune source externe requise)"],
            "production": "NO PRODUCTION CHANGES",
        },
        "conclusion": (
            f"{len(FEATURE_REGISTRY)} features documentées ({status_counts.get('PRODUCTION', 0)} en production, "
            f"{status_counts.get('MISSING', 0)} absentes documentées). Fondation de données : {data_foundation_verdict}. "
            f"Registre de features : {feature_registry_verdict}. Aucune modification de production."
        ),
    }

    outdir_path = Path(outdir)
    outdir_path.mkdir(parents=True, exist_ok=True)
    json_path = outdir_path / f"data_intelligence_{run_id}.json"
    md_path = outdir_path / f"data_intelligence_{run_id}.md"

    import json
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_render_markdown(result), encoding="utf-8")

    logger.info(f"Rapports écrits : {json_path} / {md_path}")
    logger.info(f"DATA FOUNDATION: {data_foundation_verdict} | FEATURE REGISTRY: {feature_registry_verdict}")
    print("\nPHASE 8A — XFOOT DATA INTELLIGENCE & FEATURE REGISTRY V1 TERMINÉE. "
          "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    return result


def _fmt(v, digits=4):
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _render_markdown(result: dict) -> str:
    md = ["# XFOOT DATA INTELLIGENCE & FEATURE REGISTRY V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}.\n")
    md.append(f"\n{result['conclusion']}\n")

    md.append("\n## 2. Current Data Architecture\n")
    mc = result["match_coverage"]
    md.append(f"\n- `match` : {mc['total_matches']} lignes, {mc['n_leagues']} ligues, {mc['date_min']} → {mc['date_max']}\n")
    md.append(f"- `match_stats` : {mc['match_stats_complete']}/{mc['total_matches']} matchs avec statistiques complètes "
               f"({_fmt(mc['match_stats_coverage_ratio'])})\n")
    mp = result["model_prediction_coverage"]
    md.append(f"- `model_predictions` par modèle : {mp['by_model_type']}\n")
    md.append(f"- `model_versions` : {mp['model_versions']} ; `team_ratings` : {mp['team_ratings']}\n")

    md.append("\n## 3. Data Sources\n\n")
    md.append("| Source | Exists | Historical | Timestamped | Coverage | Status |\n|---|---|---|---|---|---|\n")
    for s in result["data_sources_inventory"]:
        md.append(f"| {s['source']} | {s['exists']} | {s['historical']} | {s['timestamped']} | {s['quality']} | {s['usable']} |\n")

    md.append("\n## 4. Availability Matrix\n\n")
    md.append("| Feature Family | Source | Historical | Timestamped | Coverage | Leakage Risk |\n|---|---|---|---|---|---|\n")
    for r in result["availability_matrix"]:
        md.append(f"| {r['family']} | {r['source']} | {r['historical']} | {r['timestamped']} | {r['coverage'][:60]} | {r['leakage_risk']} |\n")

    md.append("\n## 5. Feature Registry\n\n")
    md.append(f"\nTotal : {len(result['feature_registry'])} features. Par statut : {result['status_counts']}. "
               f"Par risque de fuite : {result['leakage_counts']}. Par feu tricolore (§28) : {result['traffic_light_counts']}.\n\n")
    md.append("| Feature | Source | Type | Cutoff | Leakage | Status |\n|---|---|---|---|---|---|\n")
    for r in result["feature_registry"]:
        md.append(f"| {r['feature']} | {r['source']} | {r['type']} | {r['cutoff']} | {r['leakage']} | {r['status']} |\n")

    md.append("\n## 6. Feature Definitions\n\n")
    md.append("\nVoir §5 ci-dessus (tableau complet) et `api/app/ai/features/registry.py` pour la définition intégrale "
               "de chaque feature (description, unité, missing_value_strategy, current_model_usage).\n")

    md.append("\n## 7. Timestamp Analysis\n\n")
    md.append("\n`match.date` ne porte JAMAIS d'heure de coup d'envoi dans l'historique persisté (100% à minuit) — "
               "seules les fixtures LIVE (API-Football) portent une heure réelle (UTC), perdue dès l'écriture en base "
               "(`.date()` uniquement). Toute règle de cutoff sur l'historique est donc au JOUR près, jamais à l'heure près.\n")

    md.append("\n## 8. Temporal Cutoff\n\n")
    md.append("\nRègle uniforme pour les 25 features ML de production : `Match.date < as_of` (strict), appliquée "
               "directement dans chaque requête de `live_features.py` — jamais un filtre après coup. Voir "
               "`api/app/ai/features/snapshot.py::validate_cutoff` pour la primitive de vérification générique.\n")

    md.append("\n## 9. Leakage Audit\n\n")
    md.append(f"\nRépartition : {result['leakage_counts']}. Toute feature `PRODUCTION` a `leakage_risk` SAFE ou CAUTION "
               f"documenté (jamais LEAKAGE_RISK/REJECTED) — vérifié par `validate_registry()` : "
               f"{'aucun problème' if not result['registry_validation_problems'] else result['registry_validation_problems']}.\n")

    md.append("\n## 10. Team Normalization\n")
    md.append(f"\n{result['team_league_normalization']}\n")

    md.append("\n## 11. League Normalization\n")
    md.append("\nDictionnaire exact {nom: id API-Football}, 11 ligues — voir §10 ci-dessus pour le détail complet "
               "(section combinée équipe+ligue).\n")

    md.append("\n## 12. Season Availability\n")
    season = FEATURE_REGISTRY.get("season")
    md.append(f"\nStatut : **{season.status}**. {season.availability}\n")

    md.append("\n## 13. Form Features\n")
    md.append("\nVoir catégorie 'form' dans le registre (§5) — home/away_form_points_avg, goals_scored/conceded_avg, "
               "current_streak — toutes PRODUCTION, SAFE, fenêtre de 5 derniers matchs strictement antérieurs.\n")

    md.append("\n## 14. Team Strength\n")
    md.append("\nVoir catégorie 'team_strength' dans le registre (§5) — dixon_coles_attack/defense/home_advantage/rho, "
               "elo_rating — tous PRODUCTION, leakage_risk=CAUTION (snapshot le plus récent, pas une série temporelle "
               "persistée ; seul le walk-forward Phase 5.7 reconstruit un état historique fidèle).\n")

    md.append("\n## 15. Schedule / Rest\n")
    md.append("\ndays_since_last_match/returning_from_break : PRODUCTION, SAFE. matches_last_7/14_days : MISSING, "
               "dérivable, non implémenté (P2).\n")

    md.append("\n## 16. Injuries\n")
    inj = FEATURE_REGISTRY.get("injuries")
    md.append(f"\nStatut : **{inj.status}**. {inj.source}\n")

    md.append("\n## 17. Lineups\n")
    lu = FEATURE_REGISTRY.get("lineups")
    md.append(f"\nStatut : **{lu.status}**. {lu.source}\n")

    md.append("\n## 18. Odds\n")
    odds = FEATURE_REGISTRY.get("odds_opening")
    md.append(f"\nStatut : **{odds.status}**. {odds.source}\n")

    md.append("\n## 19. Odds Movement\n")
    md.append("\nMême statut MISSING que les cotes elles-mêmes (odds_movement dépend d'au moins deux snapshots de "
               "cotes, tous deux absents).\n")

    md.append("\n## 20. Weather\n")
    w = FEATURE_REGISTRY.get("weather")
    md.append(f"\nStatut : **{w.status}**. {w.source}\n")

    md.append("\n## 21. Data Quality\n")
    md.append(f"\nCouverture match_stats : {_fmt(result['match_coverage']['match_stats_coverage_ratio'])}. "
               "Aucune valeur manquante n'est imputée à 0 par défaut dans les features de production (voir "
               "missing_value_strategy de chaque feature, §5).\n")

    md.append("\n## 22. Missing Data\n")
    md.append(f"\n{result['status_counts'].get('MISSING', 0)} features MISSING documentées explicitement (jamais "
               "simulées) — voir §5 et la matrice de priorité (§24).\n")

    md.append("\n## 23. Feature Lineage\n")
    md.append("\nExemple (§8 du prompt) : `match` (résultats bruts) → fenêtre 5 derniers matchs → "
               "`home_form_points_avg`/`home_current_streak` → XGBoost/LightGBM. "
               "`match` → entraînement Dixon-Coles (export_model_artifacts.py) → `dc_home_win`/... → XGBoost/LightGBM "
               "(en tant que feature) ET Dixon-Coles lui-même (en tant que prédiction directe).\n")

    md.append("\n## 24. Feature Priority\n\n")
    md.append("| Feature | Quality | Coverage | Leakage Risk | Priority |\n|---|---|---|---|---|\n")
    for r in result["priority_matrix"]:
        md.append(f"| {r['feature']} | {r['quality']} | {r['coverage']} | {r['leakage_risk']} | {r['priority']} |\n")

    md.append("\n## 25. Historical Reconstruction\n")
    md.append("\n\"Que savait Xfoot avant une date T ?\" est reconstructible pour les 25 features ML "
               "(`build_feature_snapshot`, wrapper de `live_features.build_live_features`, testé colonne par colonne) "
               "et pour Dixon-Coles via `research.py::build_dixon_coles_walk_forward` (retrain tronqué). Pas "
               "reconstructible pour Elo à une date arbitraire (seul l'état final est persisté).\n")

    md.append("\n## 26. Tests\n")
    md.append("\nVoir `api/test_feature_registry.py` — complétude/cohérence du registre, cutoff, rejet d'information "
               "future, normalisation équipe/ligue, valeurs manquantes, reproductibilité, snapshot, sécurité DB.\n")

    md.append("\n## 27. Database Safety\n")
    md.append("\nLecture seule sur `match`/`match_stats`/`model_predictions`/`model_versions`/`team_ratings` — "
               "aucune écriture, aucune migration. Vérifié par test dédié (comptes de lignes identiques avant/après).\n")

    md.append("\n## 28. Production Isolation\n")
    md.append("\nAucun modèle, endpoint, scheduler, dashboard, ni prédiction historique modifiés par cette phase.\n")

    md.append("\n## 29. Limitations\n\n")
    for limit in result["limitations"]:
        md.append(f"\n- {limit}\n")

    md.append("\n## 30. Recommendations Phase 8B\n")
    md.append("\n1. `league_standing` et `season` (P1) : dérivables sans nouvelle source externe, candidates directes.\n")
    md.append("2. `matches_last_7/14_days` (P2) : même mécanisme que days_since_last_match, faible effort.\n")
    md.append("3. Odds/injuries (P2) : nécessitent un fournisseur externe — décision séparée, hors périmètre ici.\n")
    md.append("4. Unifier les deux tables d'alias équipe (team_name_matching.py / main.py) — dette technique "
               "identifiée, pas un risque de fuite mais un risque de divergence silencieuse.\n")

    md.append("\n---\n\n### DATA FOUNDATION\n\n")
    md.append(f"{'🟢' if result['verdicts']['data_foundation'] == 'READY' else '🟡'} {result['verdicts']['data_foundation']}\n")
    md.append("\n### FEATURE REGISTRY\n\n")
    md.append(f"{'🟢' if result['verdicts']['feature_registry'] == 'READY' else '🟡'} {result['verdicts']['feature_registry']}\n")
    md.append("\n### EXTERNAL DATA\n\n")
    for c in result["verdicts"]["external_data_candidates"]:
        md.append(f"- {c}\n")
    md.append("\n### PRODUCTION\n\n")
    md.append(f"**{result['verdicts']['production']}**\n")

    md.append("\n---\n\nPHASE 8A — XFOOT DATA INTELLIGENCE & FEATURE REGISTRY V1 TERMINÉE. "
               "AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")

    return "".join(md)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outdir", type=str, default=str(_SCRIPTS_DIR.parent / "reports" / "data"))
    args = parser.parse_args()
    main(outdir=args.outdir)
    sys.exit(0)
