"""
Contrat commun de prédiction LIVE — Phase 7 (§3-§6 du ticket).

Un seul contrat (`PredictionModel.predict()` -> `PredictionOutcome`) pour
Dixon-Coles, Elo, XGBoost, LightGBM et tout futur modèle — AUCUNE
réimplémentation des modèles eux-mêmes ici : chaque classe ci-dessous est un
adaptateur fin autour de ce qui existe déjà (LeagueModel dans main.py pour
Dixon-Coles, TeamRating/ModelVersion pour Elo), jamais un second moteur.

=== Audit préalable (Phase 7 §4) — ce que chaque modèle peut/ne peut pas
    servir EN DIRECT aujourd'hui, vérifié dans le code, pas supposé ===

  - Dixon-Coles : déjà servi en direct par main.py::_resolve_and_predict via
    LEAGUE_MODELS (artefact JSON par ligue) — 1X2, BTTS et OVER_UNDER_2_5.
  - Elo : app/ai/engine/elo.py::EloEngine calcule bien une calibration
    (c, scale) par ligue et des ratings par équipe, mais scripts/backtest_elo.py
    ne persistait AVANT ce ticket que les ratings (table team_ratings) — la
    calibration, elle, restait en mémoire du process de backtest puis était
    perdue. Sans elle, impossible de convertir un écart de rating en
    probabilités 1X2 pour un match qui n'a jamais fait partie d'un backtest.
    Ce ticket ajoute donc UNIQUEMENT la persistance de cette calibration
    (ModelVersion.config, voir app/models/team_rating.py) — jamais un
    recalcul en ligne, jamais une calibration inventée. Un ModelVersion actif
    créé AVANT ce ticket n'a pas de config -> NOT_AVAILABLE, honnêtement (§10).
  - XGBoost / LightGBM : scripts/train_ml_stacking_from_db.py le dit
    explicitement dans son propre docstring : « Le modèle entraîné lui-même
    (arbres/poids) n'est PAS sérialisé/stocké : aucun mécanisme d'artefact
    générique n'existe aujourd'hui pour ce type de modèle ». Aucun artefact
    -> aucune prédiction LIVE possible, sans fabriquer un nouveau mécanisme
    de sérialisation ni un pipeline de features pour un match non encore
    joué (features.py calcule ses features à partir de matchs déjà en base
    — un match futur n'en a pas). Restent donc NOT_AVAILABLE en LIVE dans
    cette phase (voir RAPPORT_PHASE7, section modèles backtest-only) tout en
    respectant intégralement le contrat commun ci-dessous — c'est un statut
    honnête, pas une implémentation manquante du contrat.
"""

from __future__ import annotations

import json
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from app.models.team_rating import ModelVersion, TeamRating

from .prediction_logging import PredictionRecord, get_or_create_active_model_version

logger = logging.getLogger("xfoot.arena.models")

# Tolérance de normalisation (§6 du ticket) : un écart de somme <= 2% est
# normalisé silencieusement (bruit d'arrondi habituel) ; un écart <= 10%
# est normalisé mais JOURNALISÉ (probablement un bug à surveiller) ; au-delà
# de 10%, la prédiction est rejetée (jamais fabriquée/forcée à sommer à 1).
_PROB_SUM_SOFT_TOLERANCE = 0.02
_PROB_SUM_HARD_REJECT = 0.10

ELO_INITIAL_RATING = 1500.0  # même convention que EloEngine (démarrage à froid)


@dataclass
class MatchContext:
    """Match déjà résolu (noms d'équipes canoniques) sur lequel produire une
    prédiction — la résolution de noms ambigus (alias/fuzzy) reste
    entièrement à la charge de l'appelant (main.py::resolve_team_name),
    jamais dupliquée ici."""
    league: str
    home_team: str
    away_team: str
    match_date: date


@dataclass
class AvailabilityCheck:
    """
    Résultat de PredictionModel.check_availability() (Phase 8, Partie B —
    §12). INDÉPENDANT d'un match précis (contrairement à predict(), qui a
    besoin de deux équipes) : répond uniquement « ce modèle a-t-il une
    version + les artefacts nécessaires pour produire une prédiction LIVE
    en ce moment ? ». Réutilisé tel quel par app/ai/arena/availability.py —
    jamais une seconde implémentation de cette vérification.
    """
    live_available: bool
    model_version_id: Optional[int] = None
    version_name: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class PredictionOutcome:
    """
    Résultat de PredictionModel.predict() pour UN modèle sur UN match.
    Jamais de succès partiel silencieux : soit `record` est entièrement
    rempli (status="ok"), soit il vaut None avec `reason` explicite
    (status="unavailable" : modèle/version/donnée manquante, prévisible ;
    "error" : la prédiction a été calculée mais rejetée, ex. probabilités
    invalides) — jamais un mélange des deux.
    """
    model_type: str
    status: str  # "ok" | "unavailable" | "error"
    model_version_id: Optional[int] = None
    record: Optional[PredictionRecord] = None
    reason: Optional[str] = None


def normalize_market(probs: dict[str, float], market: str, context: str) -> Optional[dict[str, float]]:
    """
    Vérifie qu'un ensemble de probabilités d'UN marché somme à 1 (§6 du
    ticket) et le normalise si l'écart reste dans une tolérance raisonnable
    — ne masque JAMAIS une erreur grave : une valeur hors [0,1] ou un écart
    de somme > _PROB_SUM_HARD_REJECT renvoie None (probabilité rejetée,
    jamais forcée) et journalise toujours ce qui a été observé.
    """
    values = list(probs.values())
    if any(v < -1e-9 or v > 1 + 1e-9 for v in values):
        logger.warning(f"model_prediction_invalid market={market} context={context} probs={probs} reason=out_of_range")
        return None

    total = sum(values)
    if total <= 0 or abs(total - 1.0) > _PROB_SUM_HARD_REJECT:
        logger.warning(f"model_prediction_invalid market={market} context={context} probs={probs} sum={total:.4f} reason=sum_out_of_tolerance")
        return None

    if abs(total - 1.0) <= 1e-9:
        return probs

    if abs(total - 1.0) > _PROB_SUM_SOFT_TOLERANCE:
        logger.warning(f"model_prediction_normalized market={market} context={context} probs={probs} sum={total:.4f}")

    return {k: v / total for k, v in probs.items()}


class PredictionModel(ABC):
    """Contrat commun (§3) : `model_type` identifie le modèle dans
    model_predictions/model_versions (mêmes valeurs qu'aujourd'hui —
    "dixon_coles"/"elo"/"xgboost"/"lightgbm", jamais un nouveau vocabulaire)."""

    model_type: str

    @abstractmethod
    def predict(self, session: Session, ctx: MatchContext) -> PredictionOutcome:
        ...

    @abstractmethod
    def check_availability(self, session: Session) -> AvailabilityCheck:
        ...


class DixonColesPredictionModel(PredictionModel):
    """Adaptateur autour de main.py::LEAGUE_MODELS (LeagueModel) — le
    modèle de production. Duck-typé sur `predict_1x2`/`predict_over_under`/
    `predict_btts`/`attack` (mêmes méthodes que LeagueModel), jamais une
    dépendance d'import vers main.py (qui importe déjà ce module — import
    circulaire) : le dict `league_models` est injecté par l'appelant."""

    model_type = "dixon_coles"

    def __init__(self, league_models: dict):
        self._league_models = league_models

    def predict(self, session: Session, ctx: MatchContext) -> PredictionOutcome:
        model = self._league_models.get(ctx.league)
        if model is None:
            return PredictionOutcome(
                self.model_type, "unavailable",
                reason=f"Aucun artefact Dixon-Coles chargé pour la ligue '{ctx.league}'.",
            )

        try:
            probs_1x2 = model.predict_1x2(ctx.home_team, ctx.away_team)
            probs_ou = model.predict_over_under(ctx.home_team, ctx.away_team, line=2.5)
            probs_btts = model.predict_btts(ctx.home_team, ctx.away_team)
        except KeyError as e:
            return PredictionOutcome(
                self.model_type, "unavailable",
                reason=f"Équipe inconnue du modèle Dixon-Coles pour '{ctx.league}' : {e}",
            )

        ctx_label = f"dixon_coles/{ctx.league}"
        n_1x2 = normalize_market(
            {"home_win": probs_1x2["home_win"], "draw": probs_1x2["draw"], "away_win": probs_1x2["away_win"]},
            "1X2", ctx_label,
        )
        if n_1x2 is None:
            return PredictionOutcome(self.model_type, "error", reason="Probabilités 1X2 Dixon-Coles invalides.")
        n_ou = normalize_market({"over": probs_ou["over"], "under": probs_ou["under"]}, "OVER_UNDER_2_5", ctx_label)
        n_btts = normalize_market({"yes": probs_btts["yes"], "no": probs_btts["no"]}, "BTTS", ctx_label)

        version = get_or_create_active_model_version(
            session, "dixon_coles", "xfoot-dixon-coles",
            notes="Version de production — auto-créée au premier appel loggué via le logger commun (Phase 6).",
        )
        record = PredictionRecord(
            league=ctx.league, match_date=ctx.match_date, home_team=ctx.home_team, away_team=ctx.away_team,
            model_type=self.model_type,
            prob_home=n_1x2["home_win"], prob_draw=n_1x2["draw"], prob_away=n_1x2["away_win"],
            prob_btts_yes=n_btts["yes"] if n_btts else None,
            prob_btts_no=n_btts["no"] if n_btts else None,
            prob_over_2_5=n_ou["over"] if n_ou else None,
            prob_under_2_5=n_ou["under"] if n_ou else None,
            source="live",
        )
        return PredictionOutcome(self.model_type, "ok", model_version_id=version.id, record=record)

    def check_availability(self, session: Session) -> AvailabilityCheck:
        """Ne CRÉE jamais de ModelVersion ici (contrairement à predict()) —
        une vérification de disponibilité n'a pas d'effet de bord ; la
        version dixon_coles existante (si déjà auto-créée par un appel
        réel) est purement informative."""
        if not self._league_models:
            return AvailabilityCheck(live_available=False, reason="Aucun artefact Dixon-Coles chargé (LEAGUE_MODELS vide).")
        version = session.exec(
            select(ModelVersion).where(ModelVersion.model_type == self.model_type, ModelVersion.is_active == True)  # noqa: E712
        ).first()
        return AvailabilityCheck(
            live_available=True,
            model_version_id=version.id if version else None,
            version_name=version.name if version else None,
        )


class EloPredictionModel(PredictionModel):
    """
    Adaptateur autour de la version Elo ACTIVE (table model_versions) et de
    ses team_ratings — jamais un nouvel entraînement, jamais EloEngine
    réinstancié (pas besoin de numpy/scipy/l'historique complet pour SERVIR
    une prédiction déjà calibrée, seulement le calcul terminal de
    EloEngine.predict_1x2, reproduit ici à l'identique). Sans version active
    (verdict backtest négatif — voir scripts/backtest_elo.py) ou sans
    `config` exploitable (calibration jamais persistée avant ce ticket, ou
    ligue absente de la calibration), la réponse est honnêtement
    NOT_AVAILABLE (§10 : "jamais utilisé dans l'Ensemble" dans ce cas).
    """

    model_type = "elo"

    def predict(self, session: Session, ctx: MatchContext) -> PredictionOutcome:
        version = session.exec(
            select(ModelVersion).where(ModelVersion.model_type == self.model_type, ModelVersion.is_active == True)  # noqa: E712
        ).first()
        if version is None:
            return PredictionOutcome(
                self.model_type, "unavailable",
                reason="Aucune version Elo active — le dernier backtest n'a pas démontré de gain "
                       "(voir scripts/backtest_elo.py et ModelVersion.notes).",
            )
        if not version.config:
            return PredictionOutcome(
                self.model_type, "unavailable", model_version_id=version.id,
                reason=f"Version Elo active '{version.name}' sans configuration de calibration exploitable "
                       "(créée avant Phase 7, ou backtest à relancer).",
            )

        try:
            config = json.loads(version.config)
            league_cfg = config["leagues"][ctx.league]
            c, scale = float(league_cfg["c"]), float(league_cfg["scale"])
            home_advantage = float(config["home_advantage"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return PredictionOutcome(
                self.model_type, "unavailable", model_version_id=version.id,
                reason=f"Configuration Elo invalide/incomplète pour la ligue '{ctx.league}' : {e}",
            )

        home_rating = self._rating(session, ctx.home_team, ctx.league, version.id)
        away_rating = self._rating(session, ctx.away_team, ctx.league, version.id)
        diff = home_rating + home_advantage - away_rating

        # Même formule que EloEngine.predict_1x2 (elo.py) — reproduite ici
        # pour ne dépendre que de la config déjà persistée, jamais réimportée
        # comme un second moteur concurrent.
        z = diff / scale
        p_home = 1.0 / (1.0 + math.exp(-(z - c)))
        p_away = 1.0 - 1.0 / (1.0 + math.exp(-(z + c)))
        p_draw = 1.0 - p_home - p_away

        n_1x2 = normalize_market(
            {"home_win": max(p_home, 0.0), "draw": max(p_draw, 0.0), "away_win": max(p_away, 0.0)},
            "1X2", f"elo/{ctx.league}",
        )
        if n_1x2 is None:
            return PredictionOutcome(self.model_type, "error", model_version_id=version.id,
                                      reason="Probabilités 1X2 Elo invalides.")

        record = PredictionRecord(
            league=ctx.league, match_date=ctx.match_date, home_team=ctx.home_team, away_team=ctx.away_team,
            model_type=self.model_type,
            prob_home=n_1x2["home_win"], prob_draw=n_1x2["draw"], prob_away=n_1x2["away_win"],
            source="live",
        )
        return PredictionOutcome(self.model_type, "ok", model_version_id=version.id, record=record)

    @staticmethod
    def _rating(session: Session, team: str, league: str, model_version_id: int) -> float:
        row = session.exec(
            select(TeamRating).where(
                TeamRating.team == team, TeamRating.league == league,
                TeamRating.model_version_id == model_version_id,
            )
        ).first()
        # Équipe jamais vue par CETTE version -> rating initial, même
        # convention de démarrage à froid que EloEngine._get (jamais fabriqué
        # au-delà de cette convention déjà établie et documentée).
        return row.attack if row is not None else ELO_INITIAL_RATING

    def check_availability(self, session: Session) -> AvailabilityCheck:
        version = session.exec(
            select(ModelVersion).where(ModelVersion.model_type == self.model_type, ModelVersion.is_active == True)  # noqa: E712
        ).first()
        if version is None:
            return AvailabilityCheck(live_available=False, reason="Aucune version Elo active.")
        if not version.config:
            return AvailabilityCheck(
                live_available=False, model_version_id=version.id, version_name=version.name,
                reason="Version active sans configuration de calibration exploitable.",
            )
        try:
            json.loads(version.config)
        except json.JSONDecodeError as e:
            return AvailabilityCheck(
                live_available=False, model_version_id=version.id, version_name=version.name,
                reason=f"config JSON invalide : {e}",
            )
        return AvailabilityCheck(live_available=True, model_version_id=version.id, version_name=version.name)


class _MLPredictionModel(PredictionModel):
    """
    Base commune XGBoost/LightGBM (Phase 8, Partie A — §5-§8 du ticket).
    MÊME pipeline de features (live_features.build_live_features — un seul
    endroit, réutilisé aussi par le backtest via ce même contrat de config,
    voir scripts/train_ml_stacking_from_db.py), seule la (dé)sérialisation
    native du booster diffère entre les deux librairies (_load_booster/
    _predict_proba, implémentées par XGBoostPredictionModel/
    LightGBMPredictionModel ci-dessous).

    Contrat de `ModelVersion.config` attendu (voir §10 du ticket) :
        {
          "feature_columns": [...],       # ordre EXACT utilisé à l'entraînement
          "league_categories": [...],     # catégories "league" dans l'ordre appris (cat.categories)
          "class_order": [0, 1, 2],       # ordre RÉEL de model.classes_ à l'entraînement
          "feature_version": "...",
          "trained_at": "...",
          "training_window": {...},
          "markets": ["1X2"],
        }
    `ModelVersion.artifact` : le modèle entraîné sérialisé nativement
    (booster.save_raw("json").decode() pour XGBoost, booster.model_to_string()
    pour LightGBM — jamais un format inventé, §5).

    0=nul, 1=domicile, 2=extérieur — convention FIXE de
    scripts/train_ml_stacking_from_db.py::build_target(), jamais redéfinie
    ici (source unique de vérité pour cette convention).
    """

    _CLASS_TO_OUTCOME = {0: "draw", 1: "home_win", 2: "away_win"}

    def __init__(self, league_models: dict):
        self._league_models = league_models
        # Cache (§31) : au plus UN booster chargé à la fois par instance —
        # invalidé dès qu'une version différente de celle en cache est
        # demandée (typiquement après activation d'une nouvelle version),
        # jamais rechargé à chaque requête pour LA MÊME version active.
        self._cached_version_id: Optional[int] = None
        self._cached_booster = None

    def _load_booster(self, artifact_text: str):
        raise NotImplementedError

    def _predict_proba(self, booster, row: dict, league: str, league_categories: list[str]) -> list[float]:
        """`row` porte toutes les colonnes de FEATURE_COLUMNS SAUF `league`
        (gérée séparément ici, l'encodage — string catégorielle pour
        XGBoost, code entier catégoriel pour LightGBM, voir
        scripts/train_ml_stacking_from_db.py::train_lightgbm — diffère par
        librairie). Retourne les probabilités dans l'ordre RÉEL des classes
        du booster déjà chargé — jamais un ordre supposé sans vérification :
        LightGBM en particulier ne valide JAMAIS l'ordre/les noms de colonnes
        en interne (contrairement à XGBoost, qui lève une erreur explicite en
        cas de désaccord — vérifié empiriquement, voir RAPPORT_PHASE8), donc
        l'ordre des colonnes RÉELLEMENT utilisé ici doit toujours venir du
        booster lui-même (booster.feature_name()), jamais de `config` seul."""
        raise NotImplementedError

    def _get_booster(self, version: ModelVersion):
        if self._cached_version_id == version.id:
            return self._cached_booster
        booster = self._load_booster(version.artifact)
        self._cached_version_id = version.id
        self._cached_booster = booster
        return booster

    def _active_usable_version(self, session: Session) -> tuple[Optional[ModelVersion], Optional[str]]:
        version = session.exec(
            select(ModelVersion).where(ModelVersion.model_type == self.model_type, ModelVersion.is_active == True)  # noqa: E712
        ).first()
        if version is None:
            return None, f"Aucune version {self.model_type} active."
        if not version.artifact or not version.config:
            return version, (
                f"Version {self.model_type} active '{version.name}' sans artefact/configuration exploitable "
                "(créée avant Phase 8, ou entraînement à relancer)."
            )
        return version, None

    def predict(self, session: Session, ctx: MatchContext) -> PredictionOutcome:
        version, reason = self._active_usable_version(session)
        if version is None:
            return PredictionOutcome(self.model_type, "unavailable", reason=reason)
        if reason is not None:
            return PredictionOutcome(self.model_type, "unavailable", model_version_id=version.id, reason=reason)
        return self._predict_with_version(session, ctx, version)

    def predict_for_shadow(self, session: Session, ctx: MatchContext, version: ModelVersion) -> PredictionOutcome:
        """
        Variante Phase 9 (§24-25) : produit une inférence pour une
        ModelVersion SPÉCIFIQUE (typiquement `status="shadow"`), jamais
        forcément celle activement servie — réutilise EXACTEMENT la même
        logique d'inférence que predict() (_predict_with_version ci-dessous),
        jamais une seconde implémentation qui pourrait diverger.

        Le PredictionRecord produit a `role="active"` par défaut (valeur par
        défaut de PredictionRecord, inchangée) — c'est la responsabilité de
        l'APPELANT (app/ai/arena/scheduler.py) de reconstruire un record
        avec `role="shadow"` avant de le loguer ; cette méthode se contente
        de l'inférence pour la version demandée, jamais de la décision
        d'étiquetage.
        """
        if not version.artifact or not version.config:
            return PredictionOutcome(
                self.model_type, "unavailable", model_version_id=version.id,
                reason=f"Version {self.model_type} '{version.name}' sans artefact/configuration exploitable.",
            )
        return self._predict_with_version(session, ctx, version)

    def _predict_with_version(self, session: Session, ctx: MatchContext, version: ModelVersion) -> PredictionOutcome:
        try:
            config = json.loads(version.config)
            feature_columns = config["feature_columns"]
            league_categories = config["league_categories"]
            class_order = config["class_order"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return PredictionOutcome(
                self.model_type, "unavailable", model_version_id=version.id,
                reason=f"Configuration {self.model_type} invalide/incomplète : {e}",
            )

        try:
            booster = self._get_booster(version)
        except Exception as e:
            return PredictionOutcome(
                self.model_type, "unavailable", model_version_id=version.id,
                reason=f"Artefact {self.model_type} corrompu ou illisible : {e}",
            )

        from ..engine.live_features import build_live_features

        if ctx.league not in league_categories:
            return PredictionOutcome(
                self.model_type, "unavailable", model_version_id=version.id,
                reason=f"Ligue '{ctx.league}' absente des catégories apprises à l'entraînement ({league_categories}).",
            )

        league_model = self._league_models.get(ctx.league)
        raw_features = build_live_features(session, league_model, ctx.league, ctx.home_team, ctx.away_team, ctx.match_date)
        row = {c: raw_features.get(c, float("nan")) for c in feature_columns}

        try:
            proba = self._predict_proba(booster, row, ctx.league, league_categories)
        except Exception as e:
            return PredictionOutcome(
                self.model_type, "error", model_version_id=version.id,
                reason=f"Échec de l'inférence {self.model_type} : {e}",
            )

        proba_by_label = dict(zip(class_order, proba))
        probs = {
            self._CLASS_TO_OUTCOME[label]: float(p)
            for label, p in proba_by_label.items() if label in self._CLASS_TO_OUTCOME
        }
        n_1x2 = normalize_market(probs, "1X2", f"{self.model_type}/{ctx.league}")
        if n_1x2 is None:
            return PredictionOutcome(self.model_type, "error", model_version_id=version.id,
                                      reason=f"Probabilités 1X2 {self.model_type} invalides.")

        record = PredictionRecord(
            league=ctx.league, match_date=ctx.match_date, home_team=ctx.home_team, away_team=ctx.away_team,
            model_type=self.model_type,
            prob_home=n_1x2["home_win"], prob_draw=n_1x2["draw"], prob_away=n_1x2["away_win"],
            features_version=config.get("feature_version"),
            source="live",
        )
        return PredictionOutcome(self.model_type, "ok", model_version_id=version.id, record=record)

    def check_availability(self, session: Session) -> AvailabilityCheck:
        version, reason = self._active_usable_version(session)
        if version is None:
            return AvailabilityCheck(live_available=False, reason=reason)
        if reason is not None:
            return AvailabilityCheck(live_available=False, model_version_id=version.id, version_name=version.name, reason=reason)
        try:
            self._get_booster(version)
        except Exception as e:
            return AvailabilityCheck(
                live_available=False, model_version_id=version.id, version_name=version.name,
                reason=f"Artefact corrompu ou illisible : {e}",
            )
        return AvailabilityCheck(live_available=True, model_version_id=version.id, version_name=version.name)


class XGBoostPredictionModel(_MLPredictionModel):
    model_type = "xgboost"

    def _load_booster(self, artifact_text: str):
        import xgboost as xgb
        booster = xgb.Booster()
        booster.load_model(bytearray(artifact_text.encode("utf-8")))
        return booster

    def _predict_proba(self, booster, row: dict, league: str, league_categories: list[str]) -> list[float]:
        import pandas as pd
        import xgboost as xgb

        # L'ordre RÉEL utilisé est celui du booster lui-même (booster.feature_names) —
        # XGBoost valide ET lève une erreur explicite en cas de désaccord de noms/ordre
        # (vérifié empiriquement, voir RAPPORT_PHASE8) : jamais une divergence silencieuse.
        full_row = {**row, "league": league}
        columns = booster.feature_names or list(full_row.keys())
        data = {c: [full_row.get(c)] for c in columns}
        df = pd.DataFrame(data)
        # Catégorie STRING (pas de conversion en code) — même convention que
        # scripts/train_ml_stacking_from_db.py::select_features pour XGBoost.
        df["league"] = pd.Categorical([league], categories=league_categories)
        df = df[columns]  # ordre final garanti identique à booster.feature_names
        dm = xgb.DMatrix(df, enable_categorical=True)
        proba = booster.predict(dm)
        return proba[0].tolist()


class LightGBMPredictionModel(_MLPredictionModel):
    model_type = "lightgbm"

    def _load_booster(self, artifact_text: str):
        import lightgbm as lgb
        return lgb.Booster(model_str=artifact_text)

    def _predict_proba(self, booster, row: dict, league: str, league_categories: list[str]) -> list[float]:
        import pandas as pd

        # CRITIQUE (vérifié empiriquement, voir RAPPORT_PHASE8) : contrairement à
        # XGBoost, Booster.predict() de LightGBM NE VALIDE PAS les noms/l'ordre des
        # colonnes — un désaccord produit un résultat FAUX en silence, jamais une
        # erreur. On construit donc TOUJOURS le DataFrame dans l'ordre exact retourné
        # par booster.feature_name() (la seule source de vérité fiable), jamais un
        # ordre supposé à partir de `config` seul.
        league_code = league_categories.index(league)
        full_row = {**row, "league": league_code}
        columns = booster.feature_name()
        data = {c: [full_row.get(c)] for c in columns}
        df = pd.DataFrame(data)
        if "league" in df.columns:
            # Catégorie de CODES entiers — même convention que
            # scripts/train_ml_stacking_from_db.py::train_lightgbm (cat.codes,
            # jamais la chaîne d'origine, contrairement à XGBoost).
            df["league"] = pd.Categorical([league_code], categories=list(range(len(league_categories))))
        df = df[columns]  # ordre final garanti identique à booster.feature_name()
        proba = booster.predict(df)
        return proba[0].tolist()
