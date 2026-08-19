"""
api/app/ai/arena/retraining.py — Phase 9, Partie F : infrastructure de
réentraînement continu pour XGBoost/LightGBM UNIQUEMENT (Dixon-Coles/Elo
gardent leurs propres scripts de backtest, inchangés — voir
scripts/train_dixon_coles_from_db.py / scripts/backtest_elo.py).

Trois responsabilités, séparées volontairement (§20-21-22 du ticket) :

  1. check_training_data()   : la donnée est-elle prête (§28) ? Requêtes
     RAPIDES sur la table `match` (comptage/dates/doublons), jamais un appel
     à build_ml_features_from_db() (le pipeline complet prend ~35 min sur la
     base de dev, voir api/test_ml_stacking.py) — inadapté à une simple
     vérification de disponibilité.
  2. compute_temporal_split() : découpage TRAIN/VALIDATION/TEST (§21),
     jamais aléatoire — mêmes deux constantes N_TEST/N_VAL et EXACTEMENT la
     même formule de tranchage que scripts/train_ml_stacking_from_db.py
     (dupliquées ICI intentionnellement plutôt que ré-importées depuis un
     script du dépôt racine : ce module vit dans le package api/, un import
     inverse api -> scripts/ créerait un couplage fragile dans l'autre sens
     que toutes les dépendances existantes de ce dépôt. Voir le rapport
     final Phase 9, section Limitations, pour cette décision explicite).
  3. fit_ml_model()/create_candidate_version() : entraîne RÉELLEMENT un
     modèle sur des données déjà chargées (jamais chargées ici — le pipeline
     de features reste sous la responsabilité de l'appelant, précisément
     pour que ce module reste TESTABLE avec un petit DataFrame synthétique
     sans jamais payer le coût du pipeline complet).

VALIDATION vs TEST (§22-23) : create_candidate_version() stocke les DEUX
dans ModelVersion.metrics (JSON {"validation": {...}, "test": {...}}), mais
seule la partie "validation" est jamais lue par
app/ai/arena/promotion.py::evaluate_promotion — "test" n'existe que pour
audit/rapport, jamais pour décider (test_anti_leakage_phase9.py le vérifie
explicitement).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, log_loss

from sqlmodel import Session, select

from app.models.match import Match
from app.models.team_rating import ModelVersion, next_version_name

from .ensemble import _active_version
from .promotion import (
    PROMOTION_LOG_LOSS_TOLERANCE,
    PROMOTION_MIN_VALIDATION_SAMPLE,
    PromotionDecision,
    apply_promotion,
    evaluate_promotion,
)

SUPPORTED_MODEL_TYPES = ("xgboost", "lightgbm")

# Mêmes valeurs, même rôle que scripts/train_ml_stacking_from_db.py (voir
# docstring module ci-dessus pour pourquoi elles sont dupliquées plutôt
# qu'importées).
N_TEST = 300
N_VAL = 100  # pris dans le train, pour l'early stopping — jamais dans le test

CLASS_LABELS = [0, 1, 2]  # 0=nul, 1=domicile, 2=extérieur — même convention que le script Phase 4/8
FEATURE_VERSION = "phase9-v1"

# Seuils de disponibilité des données (§28 du ticket) — PARAMÈTRES
# opérationnels, pas des vérités scientifiques (même esprit que
# monitoring.py::MIN_MONITORING_SAMPLE) : RETRAIN_MIN_MATCHES=500 laisse
# une marge confortable au-dessus de N_TEST+N_VAL=400 (le split lui-même a
# besoin d'au moins ça), RETRAIN_MIN_PERIOD_DAYS=90 évite un réentraînement
# sur une fenêtre trop courte pour capturer une saison représentative.
RETRAIN_MIN_MATCHES = int(os.environ.get("RETRAIN_MIN_MATCHES", "500"))
RETRAIN_MIN_LEAGUES = int(os.environ.get("RETRAIN_MIN_LEAGUES", "1"))
RETRAIN_MIN_PERIOD_DAYS = int(os.environ.get("RETRAIN_MIN_PERIOD_DAYS", "90"))


# ---------------------------------------------------------------------------
# 1. Data readiness (§28)
# ---------------------------------------------------------------------------

@dataclass
class DataReadiness:
    ready: bool
    reason: Optional[str]
    match_count: int
    league_count: int
    duplicate_count: int
    missing_value_columns: list[str] = field(default_factory=list)
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    leakage_detected: bool = False


def check_training_data(
    session: Session,
    *,
    min_matches: int = RETRAIN_MIN_MATCHES,
    min_leagues: int = RETRAIN_MIN_LEAGUES,
    min_period_days: int = RETRAIN_MIN_PERIOD_DAYS,
) -> DataReadiness:
    """
    Vérifie que `match` (Phase 2, données historiques déjà résolues)
    contient assez de données saines pour justifier un réentraînement — sans
    jamais construire les features complètes (voir docstring module).

    Chaque condition est indépendante et rapportée : `ready=False` dès la
    première qui échoue, avec une `reason` explicite (§28 : "ABORT avec une
    raison explicite"), jamais un abandon silencieux.
    """
    rows = session.exec(select(Match)).all()
    match_count = len(rows)
    leagues = {r.league for r in rows}
    league_count = len(leagues)

    seen: dict[tuple, int] = {}
    for r in rows:
        key = (r.league, r.date, r.home_team, r.away_team)
        seen[key] = seen.get(key, 0) + 1
    duplicate_count = sum(1 for c in seen.values() if c > 1)

    # Match.date est un datetime (voir app/models/match.py) — normalisé en
    # `date` pure ici, seule granularité qui a un sens pour une période
    # d'entraînement.
    dates = [r.date.date() if hasattr(r.date, "date") else r.date for r in rows]
    period_start = min(dates) if dates else None
    period_end = max(dates) if dates else None

    # Fuite structurelle (§43 : test_no_future_matches_in_training_features) —
    # `match` ne doit contenir QUE des rencontres déjà jouées (résultat
    # connu) ; une date dans le futur signalerait une donnée mal ingérée,
    # jamais une prédiction en direct (qui n'a pas sa place dans `match`,
    # voir docstring de app/models/match.py).
    today = date.today()
    leakage_detected = any(d > today for d in dates)

    missing_value_columns: list[str] = []
    # Les colonnes shot/corner de match_stats sont NULLABLES par nature
    # (Phase 2) — signalées ici si ENTIÈREMENT absentes (jamais partiellement,
    # ce que XGBoost/LightGBM gèrent nativement, voir train_ml_stacking_from_db.py
    # §3), un vrai symptôme d'ingestion incomplète plutôt qu'un bruit normal.
    if match_count > 0:
        from app.models.match import MatchStats
        stats_rows = session.exec(select(MatchStats)).all()
        if not stats_rows:
            missing_value_columns = [
                "home_shots", "away_shots", "home_shots_target", "away_shots_target",
                "home_corners", "away_corners",
            ]

    reason = None
    if match_count < min_matches:
        reason = f"match_count={match_count} < min_matches={min_matches}."
    elif league_count < min_leagues:
        reason = f"league_count={league_count} < min_leagues={min_leagues}."
    elif duplicate_count > 0:
        reason = f"{duplicate_count} clé(s) naturelle(s) dupliquée(s) détectée(s) dans `match`."
    elif leakage_detected:
        reason = "Au moins un match a une date future — fuite/donnée mal ingérée détectée."
    elif period_start is not None and period_end is not None and (period_end - period_start).days < min_period_days:
        reason = f"Période couverte ({(period_end - period_start).days}j) < min_period_days={min_period_days}."

    return DataReadiness(
        ready=reason is None,
        reason=reason,
        match_count=match_count,
        league_count=league_count,
        duplicate_count=duplicate_count,
        missing_value_columns=missing_value_columns,
        period_start=period_start,
        period_end=period_end,
        leakage_detected=leakage_detected,
    )


# ---------------------------------------------------------------------------
# 2. Temporal split (§21)
# ---------------------------------------------------------------------------

@dataclass
class TemporalSplit:
    n_train: int
    n_validation: int
    n_test: int
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    val_start_idx: int
    test_start_idx: int


def compute_temporal_split(dates_sorted: list[date], n_test: int = N_TEST, n_val: int = N_VAL) -> TemporalSplit:
    """
    `dates_sorted` : les dates de match, DÉJÀ triées chronologiquement
    (croissant), une par ligne de features — mêmes indices que
    scripts/train_ml_stacking_from_db.py::temporal_split (300 derniers matchs
    = test, 100 précédents = validation/early-stopping, le reste = train).
    Jamais un split aléatoire (§21 : "INTERDICTION du random split").
    """
    n = len(dates_sorted)
    if n < n_test + n_val + 1:
        raise ValueError(
            f"Pas assez de lignes ({n}) pour un split test={n_test}+validation={n_val}+au moins 1 ligne de train."
        )

    test_start_idx = n - n_test
    val_start_idx = test_start_idx - n_val

    return TemporalSplit(
        n_train=val_start_idx, n_validation=n_val, n_test=n_test,
        train_start=dates_sorted[0], train_end=dates_sorted[val_start_idx - 1],
        validation_start=dates_sorted[val_start_idx], validation_end=dates_sorted[test_start_idx - 1],
        test_start=dates_sorted[test_start_idx], test_end=dates_sorted[-1],
        val_start_idx=val_start_idx, test_start_idx=test_start_idx,
    )


# ---------------------------------------------------------------------------
# 3. Fit + candidate ModelVersion (§20, §22)
# ---------------------------------------------------------------------------

@dataclass
class FitResult:
    artifact: str
    config: dict
    validation_metrics: dict
    test_metrics: dict


def _build_target(df: pd.DataFrame) -> pd.Series:
    conditions = [df["home_goals"] > df["away_goals"], df["home_goals"] == df["away_goals"]]
    choices = [1, 0]
    y = np.select(conditions, choices, default=2)
    return pd.Series(y, index=df.index, name="target")


def _multiclass_metrics(y_true, proba: np.ndarray) -> dict:
    """Mêmes métriques (log_loss/accuracy/brier_score, sklearn pour les deux
    premières) que ce que evaluate_model() calcule dans
    train_ml_stacking_from_db.py — brier_score multiclasse = moyenne de la
    somme des carrés (proba - indicatrice), même formule que
    service.py::_compute_market_metrics, adaptée ici à un tableau numpy
    (CLASS_LABELS) plutôt qu'un dict de marché nommé."""
    y_arr = np.asarray(y_true)
    pred = np.array(CLASS_LABELS)[np.argmax(proba, axis=1)]
    n = len(y_arr)
    onehot = np.zeros_like(proba)
    for i, c in enumerate(CLASS_LABELS):
        onehot[:, i] = (y_arr == c).astype(float)
    brier = float(np.mean(np.sum((proba - onehot) ** 2, axis=1)))
    return {
        "log_loss": float(log_loss(y_arr, proba, labels=CLASS_LABELS)),
        "accuracy": float(accuracy_score(y_arr, pred)),
        "brier_score": brier,
        "sample_size": int(n),
    }


def fit_ml_model(
    model_type: str,
    X_fit: pd.DataFrame, y_fit: pd.Series,
    X_val: pd.DataFrame, y_val: pd.Series,
    X_test: pd.DataFrame, y_test: pd.Series,
    feature_columns: list[str],
) -> FitResult:
    """
    Entraîne UN modèle (XGBoost ou LightGBM) — mêmes hyperparamètres que
    scripts/train_ml_stacking_from_db.py (documentés ici pour rester
    identiques ; voir le rapport final Phase 9 pour la décision explicite de
    ne pas extraire cette logique du script existant plutôt que de la
    dupliquer avec les mêmes valeurs, pour ne jamais risquer de régression
    sur un pipeline déjà validé numériquement).

    `X_fit["league"]` doit déjà être catégorielle (comme dans le script). Le
    retour contient TOUJOURS `validation_metrics` ET `test_metrics` — ce
    dernier n'est ici QUE pour audit (voir app/ai/arena/promotion.py, qui ne
    lit jamais "test").
    """
    if model_type not in SUPPORTED_MODEL_TYPES:
        raise ValueError(f"model_type non supporté pour le réentraînement continu : {model_type}")

    if model_type == "xgboost":
        import xgboost as xgb
        model = xgb.XGBClassifier(
            max_depth=4, learning_rate=0.05, n_estimators=300,
            objective="multi:softprob", num_class=3, eval_metric="mlogloss",
            early_stopping_rounds=20, random_state=42,
            enable_categorical=True, tree_method="hist",
        )
        model.fit(X_fit, y_fit, eval_set=[(X_val, y_val)], verbose=False)
        class_order = [int(c) for c in model.classes_]

        def _proba(X):
            raw = model.predict_proba(X)
            reorder = [class_order.index(c) for c in CLASS_LABELS]
            return raw[:, reorder]

        artifact = model.get_booster().save_raw(raw_format="json").decode("utf-8")
        league_categories = X_fit["league"].cat.categories.tolist()

    else:  # lightgbm
        import lightgbm as lgb
        X_fit_lgb, X_val_lgb, X_test_lgb = X_fit.copy(), X_val.copy(), X_test.copy()
        categories = X_fit_lgb["league"].cat.categories
        X_fit_lgb["league"] = X_fit_lgb["league"].cat.codes.astype("category")
        X_val_lgb["league"] = pd.Categorical(X_val_lgb["league"], categories=categories).codes
        X_val_lgb["league"] = X_val_lgb["league"].astype("category")
        X_test_lgb["league"] = pd.Categorical(X_test_lgb["league"], categories=categories).codes
        X_test_lgb["league"] = X_test_lgb["league"].astype("category")

        model = lgb.LGBMClassifier(
            max_depth=4, num_leaves=15, learning_rate=0.05, n_estimators=300,
            objective="multiclass", num_class=3, random_state=42, verbosity=-1,
        )
        model.fit(
            X_fit_lgb, y_fit, eval_set=[(X_val_lgb, y_val)],
            categorical_feature=["league"],
            callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
        )
        class_order = [int(c) for c in model.classes_]

        def _proba(X):
            X_lgb = X.copy()
            X_lgb["league"] = pd.Categorical(X_lgb["league"], categories=categories).codes
            X_lgb["league"] = X_lgb["league"].astype("category")
            raw = model.predict_proba(X_lgb)
            reorder = [class_order.index(c) for c in CLASS_LABELS]
            return raw[:, reorder]

        artifact = model.booster_.model_to_string()
        league_categories = categories.tolist()

    validation_metrics = _multiclass_metrics(y_val, _proba(X_val))
    test_metrics = _multiclass_metrics(y_test, _proba(X_test))

    config = {
        "feature_columns": feature_columns,
        "league_categories": league_categories,
        "class_order": class_order,
        "feature_version": FEATURE_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_window": {"n_fit": len(X_fit), "n_val": len(X_val), "n_test": len(X_test)},
        "markets": ["1X2"],
    }

    return FitResult(artifact=artifact, config=config, validation_metrics=validation_metrics, test_metrics=test_metrics)


def create_candidate_version(
    session: Session,
    model_type: str,
    df: pd.DataFrame,
    feature_columns: list[str],
    categorical_columns: list[str],
    *,
    n_test: int = N_TEST,
    n_val: int = N_VAL,
) -> ModelVersion:
    """
    `df` : déjà construit par l'appelant (typiquement
    app.ai.engine.features.build_ml_features_from_db(), filtré sur
    dc_home_win non-NaN — voir scripts/train_ml_stacking_from_db.py) et
    TRIÉ CHRONOLOGIQUEMENT par date de match — jamais construit ICI (voir
    docstring module : garde ce module testable sans le coût du pipeline
    complet).

    Crée une ModelVersion `status="candidate"`, `is_active=False` — NE
    L'ACTIVE JAMAIS (§22 : "une nouvelle version ne devient pas
    automatiquement active parce que l'entraînement réussit"). Flush
    seulement, laisse l'appelant committer.
    """
    dates_sorted = df["date"].tolist()
    if hasattr(dates_sorted[0], "date"):
        dates_sorted = [d.date() if hasattr(d, "date") else d for d in dates_sorted]
    split = compute_temporal_split(dates_sorted, n_test=n_test, n_val=n_val)

    y = _build_target(df)
    all_columns = feature_columns + categorical_columns
    X = df[all_columns].copy()
    X["league"] = X["league"].astype("category")

    X_fit, y_fit = X.iloc[:split.val_start_idx], y.iloc[:split.val_start_idx]
    X_val, y_val = X.iloc[split.val_start_idx:split.test_start_idx], y.iloc[split.val_start_idx:split.test_start_idx]
    X_test, y_test = X.iloc[split.test_start_idx:], y.iloc[split.test_start_idx:]

    fit_result = fit_ml_model(model_type, X_fit, y_fit, X_val, y_val, X_test, y_test, feature_columns)

    baseline = _active_version(session, model_type)

    version = ModelVersion(
        name=next_version_name(session, f"xfoot-{model_type}"),
        model_type=model_type,
        trained_at=datetime.now(timezone.utc),
        is_active=False,
        status="candidate",
        artifact=fit_result.artifact,
        config=json.dumps(fit_result.config),
        feature_version=FEATURE_VERSION,
        training_period_start=split.train_start, training_period_end=split.train_end,
        validation_period_start=split.validation_start, validation_period_end=split.validation_end,
        test_period_start=split.test_start, test_period_end=split.test_end,
        sample_size=fit_result.validation_metrics["sample_size"],
        metrics=json.dumps({"validation": fit_result.validation_metrics, "test": fit_result.test_metrics}),
        baseline_version_id=baseline.id if baseline is not None else None,
        notes=(
            f"Candidat Phase 9 — validation log_loss={fit_result.validation_metrics['log_loss']:.4f}, "
            f"test log_loss={fit_result.test_metrics['log_loss']:.4f} (audit uniquement, jamais utilisé "
            "pour la décision de promotion, voir app/ai/arena/promotion.py)."
        ),
    )
    session.add(version)
    session.flush()
    return version


# ---------------------------------------------------------------------------
# 4. Orchestration (§26-27) — cœur partagé par scripts/retrain_ml_models.py
# ---------------------------------------------------------------------------

@dataclass
class RetrainResult:
    model_type: str
    status: str  # "dry_run" | "data_not_ready" | "candidate_created" | "promoted" | "rejected" | "error"
    readiness: Optional[DataReadiness] = None
    split_preview: Optional[TemporalSplit] = None
    candidate_version_id: Optional[int] = None
    decision: Optional[PromotionDecision] = None
    message: str = ""


def run_retrain(
    session: Session,
    model_type: str,
    *,
    dry_run: bool = True,
    force: bool = False,
    df_builder=None,
) -> RetrainResult:
    """
    Cœur unique de la commande de réentraînement (§26 : partagé par
    scripts/retrain_ml_models.py — et par un futur endpoint HTTP le cas
    échéant, jamais une deuxième implémentation).

    `df_builder` : callable sans argument retournant le DataFrame de
    features déjà filtré/trié chronologiquement (défaut : None -> importe et
    appelle app.ai.engine.features.build_ml_features_from_db(), PUIS filtre
    dc_home_win non-NaN, EXACTEMENT comme scripts/train_ml_stacking_from_db.py
    ::load_and_filter — jamais rejoué automatiquement par les tests de ce
    dépôt, ~35 min sur la base de dev réelle). Paramétrable pour les tests
    (DataFrame synthétique) et pour ne JAMAIS payer ce coût en mode
    `dry_run` (qui n'en a pas besoin, voir ci-dessous).

    `dry_run=True` (défaut, §26) : calcule la disponibilité des données et
    un APERÇU du split temporel à partir de `match` uniquement (rapide,
    jamais le pipeline complet) — n'écrit RIEN en base, ne construit ni
    n'entraîne aucun modèle.
    `dry_run=False` : construit réellement les features (df_builder),
    entraîne, crée une ModelVersion CANDIDATE. Si `force=True`, évalue et
    applique la promotion dans la foulée si la règle passe — reste TOUJOURS
    gated par evaluate_promotion, jamais un raccourci qui la court-circuite.
    """
    if model_type not in SUPPORTED_MODEL_TYPES:
        return RetrainResult(model_type=model_type, status="error",
                              message=f"model_type non supporté : {model_type}")

    readiness = check_training_data(session)
    if not readiness.ready:
        return RetrainResult(model_type=model_type, status="data_not_ready", readiness=readiness,
                              message=readiness.reason or "Données insuffisantes.")

    if dry_run:
        rows = session.exec(select(Match)).all()
        dates_sorted = sorted(r.date.date() if hasattr(r.date, "date") else r.date for r in rows)
        try:
            split_preview = compute_temporal_split(dates_sorted)
        except ValueError as e:
            return RetrainResult(model_type=model_type, status="data_not_ready", readiness=readiness,
                                  message=str(e))

        baseline = _active_version(session, model_type)
        baseline_metrics = json.loads(baseline.metrics) if (baseline is not None and baseline.metrics) else None
        message = (
            f"[dry-run] {readiness.match_count} matchs disponibles ({readiness.league_count} ligue(s), "
            f"{readiness.period_start} -> {readiness.period_end}). Aperçu du split (basé sur `match`, "
            f"le nombre réel de lignes de features peut différer légèrement à cause de la période de "
            f"rodage Dixon-Coles) : train jusqu'à {split_preview.train_end}, validation "
            f"{split_preview.validation_start} -> {split_preview.validation_end}, test "
            f"{split_preview.test_start} -> {split_preview.test_end}. "
            f"Baseline actuelle : {baseline.name if baseline is not None else 'aucune (bootstrap)'}"
            + (f", metrics stockées={baseline_metrics}" if baseline_metrics else "")
            + f". Règle de promotion qui s'appliquerait : log_loss_validation <= baseline + "
              f"{PROMOTION_LOG_LOSS_TOLERANCE} ET sample_size_validation >= {PROMOTION_MIN_VALIDATION_SAMPLE}."
        )
        return RetrainResult(model_type=model_type, status="dry_run", readiness=readiness,
                              split_preview=split_preview, message=message)

    from app.ai.engine.features import FEATURE_COLUMNS, CATEGORICAL_COLUMNS

    try:
        if df_builder is None:
            from app.ai.engine.features import build_ml_features_from_db
            df = build_ml_features_from_db()
            df = df[df["dc_home_win"].notna()].reset_index(drop=True)
        else:
            df = df_builder()
        candidate = create_candidate_version(session, model_type, df, FEATURE_COLUMNS, CATEGORICAL_COLUMNS)
    except Exception as e:  # chargement des features/entraînement — jamais silencieux (§26, code de sortie 2)
        return RetrainResult(model_type=model_type, status="error", readiness=readiness,
                              message=f"Échec de l'entraînement : {e}")

    if not force:
        session.commit()
        return RetrainResult(model_type=model_type, status="candidate_created", readiness=readiness,
                              candidate_version_id=candidate.id,
                              message=f"Candidat #{candidate.id} créé (status=candidate, is_active=False). "
                                      "Aucune promotion appliquée (relancer avec --force pour évaluer la promotion).")

    baseline = session.get(ModelVersion, candidate.baseline_version_id) if candidate.baseline_version_id else None
    decision = evaluate_promotion(candidate, baseline)
    if decision.promote:
        apply_promotion(session, candidate)
        session.commit()
        return RetrainResult(model_type=model_type, status="promoted", readiness=readiness,
                              candidate_version_id=candidate.id, decision=decision, message=decision.reason)

    session.commit()  # le candidat reste en base (status="candidate") — jamais supprimé (§22)
    return RetrainResult(model_type=model_type, status="rejected", readiness=readiness,
                          candidate_version_id=candidate.id, decision=decision, message=decision.reason)
