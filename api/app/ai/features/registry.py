"""
registry.py — Phase 8A : XFOOT FEATURE REGISTRY V1.

Catalogue CENTRALISÉ, en code (pas une nouvelle table DB — voir §40 du
prompt Phase 8A : "aucune migration DB obligatoire, n'en créer une que si
absolument nécessaire" ; ce registre n'a besoin d'aucune persistance, il
documente des faits déjà vrais dans le code existant). Aucune fonction de ce
module n'écrit en base, n'entraîne un modèle, ni ne modifie une prédiction.

=== Provenance : audit à froid du dépôt entier, pas une supposition ===

Chaque entrée ci-dessous vient d'une lecture RÉELLE du code source
correspondant (jamais inventée) :
- Les 25 features ML de production : api/app/ai/engine/features.py::FEATURE_COLUMNS,
  servies en LIVE par api/app/ai/engine/live_features.py::build_live_features
  (chaque requête interne filtre déjà Match.date < as_of — anti-fuite par
  construction, vérifié colonne par colonne par api/test_live_features.py).
- Les paramètres Dixon-Coles/Elo : export_model_artifacts.py (production),
  api/app/ai/engine/elo.py, api/app/models/team_rating.py.
- season/season_id : CONFIRMÉ ABSENT (aucune colonne nulle part) — la seule
  règle de dérivation (mois >= 7) existe dans build_features.py/
  update_raw_data.py mais n'est JAMAIS persistée ni exposée comme feature —
  voir api/app/ai/arena/schemas.py:65, qui le documente déjà explicitement.
- odds/bookmaker, injuries, suspensions, lineups, weather : CONFIRMÉ ABSENTS
  (grep exhaustif du dépôt entier, aucun faux positif écarté sans
  vérification) — jamais fabriqués ici, jamais une intégration esquissée.
- standings/classement : CONFIRMÉ ABSENT (aucune table, aucun endpoint) —
  calculable par agrégation de `match` mais non implémenté.

Statuts (`FeatureDefinition.status`, §4 du prompt) :
  AVAILABLE | PARTIAL | MISSING | EXPERIMENTAL | REJECTED | PRODUCTION

Risque de fuite (`leakage_risk`, §7 du prompt) :
  SAFE | CAUTION | LEAKAGE_RISK | REJECTED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FeatureDefinition:
    feature_name: str
    category: str
    description: str
    source: str
    data_type: str
    unit: Optional[str]
    availability: str                      # description humaine de la couverture réelle (jamais un simple booléen)
    timestamp_field: Optional[str]           # champ dont dépend le cutoff temporel (ex. "Match.date"), None si N/A
    cutoff_rule: str                          # règle exacte, en texte, jamais implicite
    leakage_risk: str                          # SAFE | CAUTION | LEAKAGE_RISK | REJECTED
    missing_value_strategy: str                 # comment une valeur manquante est traitée (jamais "0" sans justification)
    current_model_usage: list[str] = field(default_factory=list)  # ex. ["xgboost", "lightgbm"], [] si non consommée
    status: str = "MISSING"                       # AVAILABLE | PARTIAL | MISSING | EXPERIMENTAL | REJECTED | PRODUCTION
    priority: Optional[str] = None                 # P0-P3 | REJECTED — seulement pour les features MISSING/EXPERIMENTAL (§47)
    notes: Optional[str] = None


_ML_FEATURE_MODELS = ["xgboost", "lightgbm"]  # confirmé : les 25 colonnes de FEATURE_COLUMNS ne servent QUE XGBoost/LightGBM (dixon_coles/elo n'en consomment aucune)

_ML_CUTOFF_RULE = "Match.date < as_of (borne stricte) — appliqué dans chaque requête de live_features.py, jamais un filtre après coup."
_ML_SOURCE = "api/app/ai/engine/live_features.py::build_live_features (live), api/app/ai/engine/features.py::build_ml_features_from_db (entraînement) — même définition, testée colonne par colonne (api/test_live_features.py)."


def _form_features() -> dict[str, FeatureDefinition]:
    out = {}
    for side in ("home", "away"):
        for stat, desc in (
            ("form_points_avg", "Points moyens (3/1/0) sur les 5 derniers matchs de l'équipe dans cette ligue, domicile/extérieur confondus."),
            ("form_goals_scored_avg", "Buts marqués moyens sur les 5 derniers matchs."),
            ("form_goals_conceded_avg", "Buts encaissés moyens sur les 5 derniers matchs."),
        ):
            name = f"{side}_{stat}"
            out[name] = FeatureDefinition(
                feature_name=name, category="form", description=desc, source=_ML_SOURCE,
                data_type="float", unit=None,
                availability="match (2019-08→2026-05, 11 ligues) — disponible dès qu'un match antérieur existe pour l'équipe.",
                timestamp_field="Match.date", cutoff_rule=_ML_CUTOFF_RULE, leakage_risk="SAFE",
                missing_value_strategy="NaN si aucun match antérieur (période de rodage) — jamais imputé à 0.",
                current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
            )
    return out


def _rest_features() -> dict[str, FeatureDefinition]:
    out = {}
    for side in ("home", "away"):
        out[f"{side}_days_since_last_match"] = FeatureDefinition(
            feature_name=f"{side}_days_since_last_match", category="rest",
            description="Jours écoulés depuis le match précédent de l'équipe dans cette ligue.",
            source=_ML_SOURCE, data_type="float", unit="days",
            availability="Disponible dès qu'un match antérieur existe.",
            timestamp_field="Match.date", cutoff_rule=_ML_CUTOFF_RULE, leakage_risk="SAFE",
            missing_value_strategy="NaN si aucun match antérieur — jamais imputé à 0.",
            current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
            notes="Écart documenté et assumé : JAMAIS plafonnée (pas de .clip(upper=90)) dans le pipeline DB réellement "
                  "utilisé à l'entraînement — voir docstring de live_features.py ; le pipeline CSV historique "
                  "(build_features.py) plafonne, mais n'est plus celui qui alimente XGBoost/LightGBM aujourd'hui.",
        )
        out[f"{side}_returning_from_break"] = FeatureDefinition(
            feature_name=f"{side}_returning_from_break", category="rest",
            description="Indicateur binaire : plus de 90 jours depuis le match précédent.",
            source=_ML_SOURCE, data_type="int (0/1)", unit=None,
            availability="Disponible dès qu'un match antérieur existe.",
            timestamp_field="Match.date", cutoff_rule=_ML_CUTOFF_RULE, leakage_risk="SAFE",
            missing_value_strategy="0 si aucun match antérieur (convention documentée, pas un NaN) — voir live_features.py::_rest_features.",
            current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
        )
    return out


def _h2h_features() -> dict[str, FeatureDefinition]:
    return {
        "h2h_matches_found": FeatureDefinition(
            feature_name="h2h_matches_found", category="head_to_head",
            description="Nombre de confrontations directes trouvées parmi les 5 dernières (mêmes deux équipes, même ligue).",
            source=_ML_SOURCE, data_type="int", unit=None, availability="0 à 5 selon l'historique réel des confrontations.",
            timestamp_field="Match.date", cutoff_rule=_ML_CUTOFF_RULE, leakage_risk="SAFE",
            missing_value_strategy="0 si aucune confrontation antérieure — jamais fabriqué.",
            current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
        ),
        "h2h_home_win_rate": FeatureDefinition(
            feature_name="h2h_home_win_rate", category="head_to_head",
            description="Taux de victoire de l'équipe recevante sur les confrontations directes trouvées (nul = 0.5).",
            source=_ML_SOURCE, data_type="float", unit=None, availability="Disponible seulement si h2h_matches_found > 0.",
            timestamp_field="Match.date", cutoff_rule=_ML_CUTOFF_RULE, leakage_risk="SAFE",
            missing_value_strategy="NaN si h2h_matches_found == 0 — jamais imputé.",
            current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
        ),
    }


def _dixon_coles_market_features() -> dict[str, FeatureDefinition]:
    out = {}
    for key, desc in (
        ("dc_home_win", "Probabilité victoire domicile selon l'artefact Dixon-Coles de PRODUCTION (pas un fit walk-forward par requête)."),
        ("dc_draw", "Probabilité nul selon Dixon-Coles production."),
        ("dc_away_win", "Probabilité victoire extérieur selon Dixon-Coles production."),
        ("dc_over_2_5", "Probabilité plus de 2.5 buts selon Dixon-Coles production."),
        ("dc_under_2_5", "Probabilité moins de 2.5 buts selon Dixon-Coles production."),
    ):
        out[key] = FeatureDefinition(
            feature_name=key, category="team_strength", description=desc, source=_ML_SOURCE,
            data_type="float", unit="probability [0,1]",
            availability="Disponible pour toute équipe déjà vue par l'artefact api/model_artifacts/*.json actuellement chargé.",
            timestamp_field=None, cutoff_rule=(
                "Approximation DOCUMENTÉE (pas un fit tronqué à as_of) : réutilise l'artefact Dixon-Coles de production "
                "déjà entraîné exclusivement sur l'historique antérieur à SA propre date d'entraînement — anti-fuite "
                "garanti relativement au match prédit, à la fraîcheur du dernier ré-entraînement près "
                "(refresh_and_retrain.py). Un fit véritablement tronqué à as_of n'existe que via "
                "research.py::build_dixon_coles_walk_forward (recherche/backtest, jamais servi en direct)."
            ),
            leakage_risk="SAFE",
            missing_value_strategy="NaN si équipe inconnue de l'artefact (promotion récente) — jamais fabriqué (KeyError capturé).",
            current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
        )
    return out


def _shot_stats_features() -> dict[str, FeatureDefinition]:
    out = {}
    for side in ("home", "away"):
        for stat, label in (("shots_diff_avg", "tirs"), ("shots_target_diff_avg", "tirs cadrés"), ("corners_diff_avg", "corners")):
            name = f"{side}_{stat}"
            out[name] = FeatureDefinition(
                feature_name=name, category="match_stats",
                description=f"Différentiel moyen ({label} pour - {label} contre) sur les 10 derniers matchs AVEC statistiques disponibles.",
                source=_ML_SOURCE, data_type="float", unit=None,
                availability=(
                    "Variable selon la source : ~57% des lignes de data/all_leagues_raw_with_stats.csv (11 ligues, "
                    "25 727 lignes) ont match_stats complète ; la base `match` LOCALE actuellement chargée (5 "
                    "ligues historiques d'origine, 12 459 lignes) est bien mieux couverte (>99%, voir le calcul "
                    "dynamique du rapport data_intelligence_audit.py) — la base locale n'est PAS un miroir complet "
                    "du CSV source (6 ligues du CSV, ex. ChampionsLeague/MLS, ne sont pas chargées en base "
                    "localement). Un match sans stats est de toute façon EXCLU de la fenêtre, jamais compté comme 0."
                ),
                timestamp_field="Match.date", cutoff_rule=_ML_CUTOFF_RULE, leakage_risk="SAFE",
                missing_value_strategy="NaN si aucun des 10 derniers matchs n'a de match_stats renseignée.",
                current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
            )
    return out


def _streak_features() -> dict[str, FeatureDefinition]:
    out = {}
    for side in ("home", "away"):
        name = f"{side}_current_streak"
        out[name] = FeatureDefinition(
            feature_name=name, category="form",
            description="Série en cours signée (+N victoires consécutives / -N défaites consécutives / 0 après un nul).",
            source=_ML_SOURCE, data_type="float", unit=None, availability="Disponible dès qu'un match antérieur existe.",
            timestamp_field="Match.date", cutoff_rule=_ML_CUTOFF_RULE, leakage_risk="SAFE",
            missing_value_strategy="NaN si aucun match antérieur.",
            current_model_usage=list(_ML_FEATURE_MODELS), status="PRODUCTION",
        )
    return out


def _match_identity_features() -> dict[str, FeatureDefinition]:
    return {
        "match_date": FeatureDefinition(
            feature_name="match_date", category="match", description="Date du match (SANS heure de coup d'envoi).",
            source="api/app/models/match.py::Match.date (historique) ; api_football_client.py (fixtures à venir, avec heure UTC).",
            data_type="date", unit=None,
            availability="Toujours renseignée (NOT NULL) pour l'historique ; asymétrie documentée : l'historique persisté "
                         "n'a jamais d'heure (100% des lignes CSV à minuit), seules les fixtures LIVE en ont une, "
                         "perdue dès l'écriture en base (.date() uniquement, voir scheduler.py:112).",
            timestamp_field="Match.date", cutoff_rule="N/A — c'est la référence temporelle elle-même.", leakage_risk="SAFE",
            missing_value_strategy="N/A (jamais nulle).", current_model_usage=["dixon_coles", "elo", "xgboost", "lightgbm"],
            status="PRODUCTION",
        ),
        "league": FeatureDefinition(
            feature_name="league", category="match", description="Identifiant de ligue/compétition (chaîne exacte, 11 valeurs connues).",
            source="api/app/models/match.py::Match.league ; api/app/core/api_football_config.py::API_FOOTBALL_LEAGUE_IDS.",
            data_type="categorical (str)", unit=None,
            availability="Toujours renseignée. 11 valeurs connues côté SOURCE (data/all_leagues_raw_with_stats.csv) : "
                         "Bundesliga, ChampionsLeague, ConferenceLeague, EuropaLeague, LaLiga, Ligue1, MLS, "
                         "PremierLeague, PrimeiraLiga, SaudiProLeague, SerieA — mais la table `match` LOCALE "
                         "actuellement chargée n'en contient que 5 (les 5 ligues historiques d'origine ; 6 ligues du "
                         "CSV source, dont ChampionsLeague/MLS, ne sont pas chargées en base localement — voir le "
                         "décompte par ligue calculé dynamiquement dans le rapport data_intelligence_audit.py).",
            timestamp_field=None, cutoff_rule="N/A", leakage_risk="SAFE", missing_value_strategy="N/A (jamais nulle).",
            current_model_usage=["dixon_coles", "elo", "xgboost", "lightgbm"], status="PRODUCTION",
            notes="3 des 11 valeurs sont des coupes européennes (Champions/Europa/Conference League) — un club y est "
                  "traité comme une entité COMPLÈTEMENT SÉPARÉE de son historique en championnat national (aucune "
                  "colonne de compétition transverse, voir train_ml_stacking_from_db.py:123-126, confirmé dans le code).",
        ),
        "home_team": FeatureDefinition(
            feature_name="home_team", category="match", description="Nom de l'équipe recevante (chaîne, pas d'ID canonique).",
            source="api/app/models/match.py::Match.home_team.", data_type="categorical (str)", unit=None,
            availability="Toujours renseignée.", timestamp_field=None, cutoff_rule="N/A", leakage_risk="SAFE",
            missing_value_strategy="N/A (jamais nulle).", current_model_usage=["dixon_coles", "elo", "xgboost", "lightgbm"],
            status="PRODUCTION",
            notes="AUCUN identifiant canonique d'équipe dans tout le dépôt (confirmé) — deux mécanismes de "
                  "normalisation fuzzy INDÉPENDANTS coexistent (team_name_matching.py, 18 alias, résout "
                  "silencieusement en fuzzy à partir de 0.6 ; main.py, ~300 alias, ne résout JAMAIS silencieusement "
                  "en fuzzy) — non synchronisés entre eux, limitation réelle documentée ici, non corrigée dans cette phase.",
        ),
        "away_team": FeatureDefinition(
            feature_name="away_team", category="match", description="Nom de l'équipe visiteuse (chaîne, pas d'ID canonique).",
            source="api/app/models/match.py::Match.away_team.", data_type="categorical (str)", unit=None,
            availability="Toujours renseignée.", timestamp_field=None, cutoff_rule="N/A", leakage_risk="SAFE",
            missing_value_strategy="N/A (jamais nulle).", current_model_usage=["dixon_coles", "elo", "xgboost", "lightgbm"],
            status="PRODUCTION", notes="Même limitation de normalisation que home_team.",
        ),
        "season": FeatureDefinition(
            feature_name="season", category="match",
            description="Identifiant de saison (ex. 2025-2026) — NE PEUT PAS être lu, doit être dérivé.",
            source="AUCUNE colonne season/season_id nulle part dans le schéma (confirmé) — voir "
                  "api/app/ai/arena/schemas.py:65, qui documente déjà cette absence.",
            data_type="derived (int, année de début)", unit=None,
            availability="NOT AVAILABLE en tant que champ stocké. Une règle de dérivation FIABLE existe et est déjà "
                         "utilisée ailleurs dans le code (jamais pour cette feature) : mois >= 7 -> saison = année en "
                         "cours, sinon année précédente — voir build_features.py:113 et update_raw_data.py:132-147.",
            timestamp_field="Match.date", cutoff_rule="Dérivable de Match.date, jamais du futur — SAFE si calculée à la volée.",
            leakage_risk="CAUTION", missing_value_strategy="Ne jamais fabriquer une saison — absente tant que non dérivée explicitement.",
            current_model_usage=[], status="EXPERIMENTAL", priority="P1",
            notes="Méthode de dérivation documentée et réutilisable (voir source) mais jamais persistée ni exposée "
                  "comme feature de modèle à ce jour — candidate directe pour la Phase 8B si un besoin réel apparaît "
                  "(ex. split train/test par saison plutôt que par nombre de matchs).",
        ),
    }


def _team_strength_ratings() -> dict[str, FeatureDefinition]:
    dc_source = "export_model_artifacts.py (_FastDixonColesL2, moteur de production) ; api/model_artifacts/*.json ; dual-write dans team_ratings (model_type='dixon_coles')."
    out = {
        "dixon_coles_attack": FeatureDefinition(
            feature_name="dixon_coles_attack", category="team_strength", description="Force offensive apprise par équipe (paramètre Dixon-Coles).",
            source=dc_source, data_type="float", unit=None,
            availability="Une valeur par équipe/ligue, snapshot LE PLUS RÉCENT uniquement (pas de série temporelle persistée).",
            timestamp_field="LeagueModel.data_up_to (artefact) / TeamRating.computed_at (dual-write DB)",
            cutoff_rule="SAFE pour servir une prédiction (l'artefact n'a appris que du passé au moment de son entraînement). "
                        "CAUTION pour une reconstruction historique arbitraire : seul research.py::build_dixon_coles_walk_forward "
                        "recrée une valeur réellement tronquée à une date passée précise.",
            leakage_risk="CAUTION",
            missing_value_strategy="KeyError capturé -> indisponible pour une équipe jamais vue par l'artefact, jamais fabriqué.",
            current_model_usage=["dixon_coles"], status="PRODUCTION",
        ),
        "dixon_coles_defense": FeatureDefinition(
            feature_name="dixon_coles_defense", category="team_strength", description="Force défensive apprise par équipe (paramètre Dixon-Coles).",
            source=dc_source, data_type="float", unit=None, availability="Comme dixon_coles_attack.",
            timestamp_field="LeagueModel.data_up_to / TeamRating.computed_at",
            cutoff_rule="Identique à dixon_coles_attack.", leakage_risk="CAUTION",
            missing_value_strategy="Identique à dixon_coles_attack.", current_model_usage=["dixon_coles"], status="PRODUCTION",
        ),
        "dixon_coles_home_advantage": FeatureDefinition(
            feature_name="dixon_coles_home_advantage", category="team_strength",
            description="Avantage du terrain (gamma), UNIQUE par ligue (pas par équipe).",
            source=dc_source, data_type="float", unit=None, availability="Une valeur par ligue.",
            timestamp_field="LeagueModel.data_up_to", cutoff_rule="Identique à dixon_coles_attack.", leakage_risk="CAUTION",
            missing_value_strategy="N/A (toujours présent si l'artefact de la ligue existe).",
            current_model_usage=["dixon_coles"], status="PRODUCTION",
        ),
        "dixon_coles_rho": FeatureDefinition(
            feature_name="dixon_coles_rho", category="team_strength",
            description="Correction de corrélation basse-marque (rho), unique par ligue.",
            source=dc_source, data_type="float", unit=None, availability="Une valeur par ligue.",
            timestamp_field="LeagueModel.data_up_to", cutoff_rule="Identique à dixon_coles_attack.", leakage_risk="CAUTION",
            missing_value_strategy="N/A.", current_model_usage=["dixon_coles"], status="PRODUCTION",
        ),
        "elo_rating": FeatureDefinition(
            feature_name="elo_rating", category="team_strength", description="Rating Elo courant par équipe (échelle base-10/400).",
            source="api/app/ai/engine/elo.py::EloEngine ; team_ratings (model_type='elo', colonne attack, defense=0.0 par convention).",
            data_type="float", unit=None,
            availability="Une valeur par équipe/ligue, snapshot LE PLUS RÉCENT uniquement — aucune série temporelle "
                         "persistée (walk_forward() calcule un diff pré-match par ligne mais ne l'écrit jamais en base).",
            timestamp_field="TeamRating.computed_at", cutoff_rule="SAFE pour servir (rating pré-match par construction dans walk_forward). "
                        "Pas de reconstruction historique arbitraire possible sans rejouer walk_forward() tronqué.",
            leakage_risk="CAUTION", missing_value_strategy="Cold-start à 1500.0 pour une équipe inconnue (convention EloEngine, documentée).",
            current_model_usage=["elo"], status="PRODUCTION",
            notes="NON injectée dans les 25 features XGBoost/LightGBM (confirmé : absente de FEATURE_COLUMNS) — sert "
                  "uniquement à produire les prédictions Elo elles-mêmes.",
        ),
    }
    return out


def _not_available_features() -> dict[str, FeatureDefinition]:
    """§45 du prompt : SOURCE CANDIDATE — documentées, jamais intégrées ici."""
    out = {
        "league_standing": FeatureDefinition(
            feature_name="league_standing", category="context", description="Position au classement au moment du match.",
            source="NOT AVAILABLE — aucune table de classement/standings dans le dépôt (confirmé, zéro résultat).",
            data_type="derived (int)", unit=None,
            availability="Calculable par agrégation de `match` (points cumulés par équipe avant chaque date) — jamais implémenté.",
            timestamp_field="Match.date", cutoff_rule="Serait SAFE si calculé strictement sur les matchs antérieurs (même règle que les features de forme).",
            leakage_risk="CAUTION", missing_value_strategy="MISSING — aucune valeur produite tant que non implémentée.",
            current_model_usage=[], status="MISSING", priority="P1",
            notes="Candidate directe (mêmes garanties de cutoff que le reste des features de forme) — nécessite un "
                  "calcul d'agrégation cumulative, pas une nouvelle source de données externe.",
        ),
        "matches_last_7_days": FeatureDefinition(
            feature_name="matches_last_7_days", category="rest", description="Nombre de matchs joués par l'équipe dans les 7 derniers jours.",
            source="NOT AVAILABLE en tant que feature — dérivable de Match.date comme days_since_last_match, jamais implémenté.",
            data_type="derived (int)", unit=None, availability="Calculable, non implémenté.",
            timestamp_field="Match.date", cutoff_rule="Serait SAFE (même requête que _team_matches_before, bornée à Match.date < as_of).",
            leakage_risk="CAUTION", missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P2",
            notes="Limité à UNE seule compétition (`league`) comme le reste — pas de congestion inter-compétitions calculable (voir league ci-dessus).",
        ),
        "matches_last_14_days": FeatureDefinition(
            feature_name="matches_last_14_days", category="rest", description="Nombre de matchs joués par l'équipe dans les 14 derniers jours.",
            source="NOT AVAILABLE en tant que feature — même remarque que matches_last_7_days.",
            data_type="derived (int)", unit=None, availability="Calculable, non implémenté.",
            timestamp_field="Match.date", cutoff_rule="Serait SAFE (même principe).", leakage_risk="CAUTION",
            missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P2",
        ),
        "odds_opening": FeatureDefinition(
            feature_name="odds_opening", category="market", description="Cote d'ouverture du marché 1X2 (ou autre).",
            source="NOT AVAILABLE — aucune donnée de cote nulle part dans le dépôt (grep exhaustif odds|bookmaker|"
                  "implied_prob|bet365|market_prob|betting sur tout le dépôt : zéro résultat réel, tous les hits sont "
                  "le vocabulaire interne Xfoot pour 1X2/BTTS/O-U, jamais des prix de marché réels).",
            data_type="float", unit="decimal odds", availability="NOT AVAILABLE.", timestamp_field="odds_timestamp (hypothétique, à définir si intégré)",
            cutoff_rule="Règle FUTURE si intégré un jour : odds_timestamp doit être < match_kickoff, jamais après (§22 du prompt).",
            leakage_risk="REJECTED", missing_value_strategy="MISSING — aucune source, jamais simulée.",
            current_model_usage=[], status="MISSING", priority="P2",
            notes="SOURCE CANDIDATE (§45) : nécessiterait un fournisseur de cotes externe (ex. The Odds API, "
                  "Betfair Exchange) — coût/contrainte inconnus, aucune intégration décidée ici. Distinction "
                  "MODEL SIGNAL vs MARKET SIGNAL à préserver strictement si jamais intégré (§25), pour un futur Value Engine.",
        ),
        "odds_closing": FeatureDefinition(
            feature_name="odds_closing", category="market", description="Cote de clôture du marché 1X2 (ou autre).",
            source="NOT AVAILABLE — voir odds_opening.", data_type="float", unit="decimal odds", availability="NOT AVAILABLE.",
            timestamp_field="odds_timestamp (hypothétique)",
            cutoff_rule="REJECTED par construction pour toute prédiction faite avant clôture (§24 du prompt) — "
                        "une cote de clôture est POSTÉRIEURE au moment de la prédiction par définition.",
            leakage_risk="REJECTED", missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P3",
            notes="Même famille que odds_opening — jamais utilisable pour une prédiction pré-match, quelle que soit "
                  "la source future (règle structurelle, pas un problème de disponibilité).",
        ),
        "implied_probability": FeatureDefinition(
            feature_name="implied_probability", category="market", description="Probabilité implicite dérivée d'une cote (1/cote, normalisée de la marge bookmaker).",
            source="NOT AVAILABLE — dépend d'odds_opening/closing, elles-mêmes absentes.", data_type="float", unit="probability [0,1]",
            availability="NOT AVAILABLE.", timestamp_field="odds_timestamp (hypothétique)",
            cutoff_rule="Hériterait de la règle de la cote source (odds_opening SAFE si antérieure au kickoff, closing REJECTED).",
            leakage_risk="REJECTED", missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P3",
            notes="§23 du prompt : NE PAS utiliser en production même si un jour disponible — signal de MARCHÉ, "
                  "jamais mélangé au signal du modèle (§25).",
        ),
        "odds_movement": FeatureDefinition(
            feature_name="odds_movement", category="market", description="Évolution de la cote entre ouverture et un instant donné.",
            source="NOT AVAILABLE — nécessite plusieurs snapshots de cotes dans le temps, absents.", data_type="float", unit="delta odds",
            availability="NOT AVAILABLE.", timestamp_field="odds_timestamp (hypothétique, multiple snapshots)",
            cutoff_rule="Chaque snapshot utilisé devrait individuellement être antérieur au moment de la prédiction.",
            leakage_risk="REJECTED", missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P3",
        ),
        "injuries": FeatureDefinition(
            feature_name="injuries", category="context", description="Blessures de joueurs affectant une équipe avant le match.",
            source="NOT AVAILABLE — confirmé absent (grep exhaustif injur|suspension|lineup|weather|fatigue sur tout "
                  "le dépôt : 2 résultats, tous deux sans rapport, une note sur l'interruption COVID-19 de la saison "
                  "2020). api_football_client.py n'appelle JAMAIS d'endpoint blessures.",
            data_type="N/A", unit=None, availability="NOT AVAILABLE.", timestamp_field="injury_report_timestamp (hypothétique)",
            cutoff_rule="Règle FUTURE si intégré : le rapport de blessure doit être publié avant le kickoff.",
            leakage_risk="REJECTED", missing_value_strategy="MISSING — jamais simulée.", current_model_usage=[], status="MISSING", priority="P2",
            notes="SOURCE CANDIDATE (§45) : API-Football propose un endpoint /injuries — jamais appelé aujourd'hui, "
                  "coût/quota API à évaluer séparément si une intégration future est décidée.",
        ),
        "suspensions": FeatureDefinition(
            feature_name="suspensions", category="context", description="Suspensions de joueurs (cartons cumulés, etc.).",
            source="NOT AVAILABLE — confirmé absent (même grep que injuries).", data_type="N/A", unit=None,
            availability="NOT AVAILABLE.", timestamp_field="N/A", cutoff_rule="N/A", leakage_risk="REJECTED",
            missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P3",
        ),
        "lineups": FeatureDefinition(
            feature_name="lineups", category="context", description="Composition d'équipe annoncée avant le match.",
            source="NOT AVAILABLE — confirmé absent, aucun endpoint lineups jamais appelé.", data_type="N/A", unit=None,
            availability="NOT AVAILABLE.", timestamp_field="lineup_published_at (hypothétique)",
            cutoff_rule="§20 du prompt : une composition publiée ~60 min avant le match serait utilisable ; publiée "
                        "après le coup d'envoi = REJECTED par construction.",
            leakage_risk="REJECTED", missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P2",
        ),
        "weather": FeatureDefinition(
            feature_name="weather", category="context", description="Conditions météo au moment du match.",
            source="NOT AVAILABLE — confirmé absent, aucune source météo dans le dépôt.", data_type="N/A", unit=None,
            availability="NOT AVAILABLE.", timestamp_field="weather_timestamp (hypothétique)",
            cutoff_rule="§26 du prompt : une PRÉVISION disponible avant kickoff serait potentiellement utilisable ; "
                        "une observation post-match = REJECTED par construction.",
            leakage_risk="REJECTED", missing_value_strategy="MISSING.", current_model_usage=[], status="MISSING", priority="P3",
        ),
    }
    return out


def _build_registry() -> dict[str, FeatureDefinition]:
    registry: dict[str, FeatureDefinition] = {}
    for builder in (
        _match_identity_features, _form_features, _rest_features, _h2h_features,
        _dixon_coles_market_features, _shot_stats_features, _streak_features,
        _team_strength_ratings, _not_available_features,
    ):
        for name, fd in builder().items():
            if name in registry:
                raise ValueError(f"Feature dupliquée dans le registre : {name}")
            registry[name] = fd
    return registry


FEATURE_REGISTRY: dict[str, FeatureDefinition] = _build_registry()

STATUSES = ("AVAILABLE", "PARTIAL", "MISSING", "EXPERIMENTAL", "REJECTED", "PRODUCTION")
LEAKAGE_RISKS = ("SAFE", "CAUTION", "LEAKAGE_RISK", "REJECTED")


def get_feature(name: str) -> Optional[FeatureDefinition]:
    return FEATURE_REGISTRY.get(name)


def list_by_status(status: str) -> list[FeatureDefinition]:
    return [fd for fd in FEATURE_REGISTRY.values() if fd.status == status]


def list_by_category(category: str) -> list[FeatureDefinition]:
    return [fd for fd in FEATURE_REGISTRY.values() if fd.category == category]


def list_by_leakage_risk(risk: str) -> list[FeatureDefinition]:
    return [fd for fd in FEATURE_REGISTRY.values() if fd.leakage_risk == risk]


def traffic_light(fd: FeatureDefinition) -> str:
    """GREEN/YELLOW/RED (§28 du prompt) — DÉRIVÉ à la volée depuis
    status/leakage_risk/availability, jamais stocké séparément (une seule
    source de vérité par feature, jamais deux champs pouvant se contredire).

    GREEN : status=PRODUCTION et leakage_risk=SAFE (historique + timestamp +
            couverture suffisante, utilisée en production sans réserve).
    YELLOW : status in (AVAILABLE, PARTIAL, EXPERIMENTAL) ou leakage_risk=CAUTION
             (partielle ou incertaine, jamais utilisée sans vérification).
    RED : status in (MISSING, REJECTED) ou leakage_risk in (LEAKAGE_RISK, REJECTED)
          (absente ou risque de fuite avéré).
    """
    if fd.status in ("MISSING", "REJECTED") or fd.leakage_risk in ("LEAKAGE_RISK", "REJECTED"):
        return "RED"
    if fd.status == "PRODUCTION" and fd.leakage_risk == "SAFE":
        return "GREEN"
    return "YELLOW"


def validate_registry() -> list[str]:
    """Vérifie la cohérence interne du registre — retourne la liste des
    incohérences trouvées (vide si tout est cohérent). Jamais une feature
    PRODUCTION sans cutoff_rule documentée, jamais un leakage_risk=SAFE sans
    règle de cutoff explicite."""
    problems = []
    for name, fd in FEATURE_REGISTRY.items():
        if fd.status not in STATUSES:
            problems.append(f"{name}: status invalide '{fd.status}'")
        if fd.leakage_risk not in LEAKAGE_RISKS:
            problems.append(f"{name}: leakage_risk invalide '{fd.leakage_risk}'")
        if fd.status == "PRODUCTION" and fd.leakage_risk != "SAFE" and fd.leakage_risk != "CAUTION":
            problems.append(f"{name}: status=PRODUCTION mais leakage_risk='{fd.leakage_risk}' (attendu SAFE ou CAUTION documenté)")
        if fd.leakage_risk == "SAFE" and not fd.cutoff_rule:
            problems.append(f"{name}: leakage_risk=SAFE sans cutoff_rule documentée")
        if fd.status == "PRODUCTION" and not fd.current_model_usage:
            problems.append(f"{name}: status=PRODUCTION mais current_model_usage vide")
        if not fd.missing_value_strategy:
            problems.append(f"{name}: missing_value_strategy manquante")
    return problems
