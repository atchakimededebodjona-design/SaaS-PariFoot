"""
scripts/train_ml_stacking_from_db.py — Phase 4 : XGBoost/LightGBM avec
features réellement nouvelles, sourcées en base (match/match_stats).
=============================================================================

Le stacking XGBoost par-dessus Dixon-Coles a déjà été testé deux fois dans ce
projet, même méthodologie (split temporel, bootstrap apparié 2000 tirages) :

  - 712 lignes exploitables (Ligue1 seule, train_xgboost_stacking.py) :
    log-loss XGBoost=1.0713 vs Dixon-Coles=1.0219, DC meilleur dans 97.4%
    des tirages bootstrap.
  - ~10165 lignes (5 ligues, train_xgboost_stacking_multileague.py) : même
    verdict, DC meilleur dans ~98.1% des tirages, écart significatif dans
    les 5 ligues sans exception.

Verdict les deux fois : PAS DE GAIN, Dixon-Coles reste seul en production.
Mêmes 17 features (forme 5 matchs, repos, head-to-head, probabilités
Dixon-Coles en stacking) — CE SCRIPT NE REJOUE PAS CE TEST À L'IDENTIQUE.

Ce qui change ici :
  1. Source : match/match_stats (Phase 2) via api.app.ai.engine.features,
     jamais le CSV.
  2. Features : les 17 existantes + 8 réellement nouvelles (jamais testées
     avant) — voir api/app/ai/engine/features.py pour le détail et la
     justification (moyennes glissantes de tirs/tirs cadrés/corners sur une
     fenêtre plus longue que la forme, séries de résultats en cours). La
     piste « repos multi-compétitions » de l'audit n'est PAS réalisable
     avec les données actuelles (pas de colonne de compétition en base,
     uniquement `league`) — non testée, rapportée comme telle plutôt que
     forcée.
  3. Modèle : XGBoost (hyperparamètres IDENTIQUES aux tentatives passées,
     pour isoler l'effet des nouvelles features) ET LightGBM (config
     équivalente), tous deux comparés à Dixon-Coles sur le même test set.
  4. Persistance (Phase 4, RÉVISÉE Phase 8 — §5/§10/§13 du ticket) : ModelVersion
     créée pour chaque modèle entraîné (model_type="xgboost"/"lightgbm"), avec
     désormais un artefact RÉELLEMENT sérialisé (`booster.save_raw("json")`
     pour XGBoost, `booster.model_to_string()` pour LightGBM — mécanismes
     natifs de chaque librairie, jamais un format inventé) et une `config`
     JSON reproduisant tout le contexte nécessaire pour SERVIR ce modèle en
     direct (feature_columns, league_categories, class_order — voir
     app/ai/arena/models_common.py::_MLPredictionModel, qui consomme ce
     contrat exact).
     Politique d'activation CHANGÉE (Phase 8, §13 : « ne pas désactiver un
     modèle à cause d'un petit échantillon ») : `is_active=True`
     INCONDITIONNELLEMENT à chaque entraînement réussi, comme Dixon-Coles/Elo
     — `is_active` ne représente plus qu'un état OPÉRATIONNEL (« ceci est la
     version actuellement servie en direct »), jamais un jugement de
     performance face à Dixon-Coles. La comparaison au log-loss/bootstrap
     Dixon-Coles ci-dessous reste calculée et journalisée dans `notes` À TITRE
     INFORMATIF UNIQUEMENT — c'est désormais EnsembleEngine (via
     `ensemble_eligible`, seuil MIN_BENCHMARK_SAMPLE_SIZE sur un historique
     résolu réel, voir app/ai/arena/availability.py) qui décide si ce modèle
     doit peser dans l'Ensemble, jamais ce script.
     Aucune TeamRating créée — n'a pas de sens pour un modèle à features (pas
     de rating par équipe), contrairement à Dixon-Coles/Elo.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/train_ml_stacking_from_db.py
"""

import json
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
import lightgbm as lgb
from sklearn.metrics import log_loss, accuracy_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.team_rating import ModelVersion, next_version_name, deactivate_other_versions  # noqa: E402
# Import EN TÊTE (pas paresseux) : ModelPrediction doit être enregistrée dans
# SQLModel.metadata AVANT tout appel à init_db() plus bas (même raison que
# scripts/backtest_elo.py), sinon create_all() ne crée jamais la table.
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction  # noqa: E402
from app.ai.engine.features import (  # noqa: E402
    build_ml_features_from_db, FEATURE_COLUMNS, CATEGORICAL_COLUMNS,
    EXISTING_FEATURE_COLUMNS, NEW_FEATURE_COLUMNS,
    PREVIOUSLY_UNUSED_MATCH_STATS_COLUMNS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_ml_stacking_from_db")

N_TEST = 300
N_VAL = 100  # pris dans le train, pour l'early stopping — jamais dans le test

# Ordre de classe fixé : 0=nul, 1=domicile, 2=extérieur — mêmes conventions
# que train_xgboost_stacking_multileague.py. Une erreur d'alignement ici
# fausserait silencieusement toute comparaison de log-loss.
CLASS_LABELS = [0, 1, 2]
DC_PROBA_COLUMNS_IN_LABEL_ORDER = ["dc_draw", "dc_home_win", "dc_away_win"]

# Seuil de verdict "gain crédible" — identique aux deux tentatives passées,
# pour ne pas assouplir la barre au moment où on ajoute des features.
VERDICT_FRAC_THRESHOLD = 0.90


def report_unused_columns():
    print("=" * 80)
    print("0. RECENSEMENT — colonnes match_stats jamais exploitées avant ce ticket")
    print("=" * 80)
    print("  Colonnes présentes dans match_stats depuis la Phase 2, toujours explicitement")
    print("  exclues de FEATURE_COLUMNS dans les deux tentatives XGBoost passées (valeur")
    print("  BRUTE du match courant = fuite) :")
    for c in PREVIOUSLY_UNUSED_MATCH_STATS_COLUMNS:
        print(f"    - {c}")
    print("\n  Exploitées ici pour la première fois, mais UNIQUEMENT via des moyennes")
    print("  glissantes sur les matchs PASSÉS de chaque équipe (jamais la valeur du match")
    print("  courant) — voir api/app/ai/engine/features.py::build_shot_stats_features :")
    for c in NEW_FEATURE_COLUMNS[:6]:
        print(f"    -> {c}")
    print("\n  Features de séquence (nouvelles, dérivées de home_goals/away_goals — déjà")
    print("  utilisées pour la moyenne de forme, mais jamais pour l'ORDRE des résultats) :")
    for c in NEW_FEATURE_COLUMNS[6:]:
        print(f"    -> {c}")
    print("\n  Piste NON RETENUE (rapportée, pas forcée) : repos réel multi-compétitions —")
    print("  `match` n'a pas de colonne de compétition/coupe, uniquement `league`")
    print("  (championnat). Seul le repos intra-championnat est calculable, déjà capturé")
    print("  par home/away_days_since_last_match (feature existante, pas nouvelle ici).")
    print()


def load_and_filter():
    df = build_ml_features_from_db()
    n_before = len(df)
    df = df[df["dc_home_win"].notna()].reset_index(drop=True)
    n_after = len(df)

    print("=" * 80)
    print("1. FILTRAGE — dc_home_win non-NaN (5 ligues confondues)")
    print("=" * 80)
    print(f"  Lignes avant filtrage : {n_before}")
    print(f"  Lignes après filtrage : {n_after}  ({n_before - n_after} écartées, "
          f"période de rodage Dixon-Coles / équipes jamais vues, PAR LIGUE)")
    print("  Répartition par ligue après filtrage :")
    print(df["league"].value_counts().to_string())
    print()
    return df


def build_target(df: pd.DataFrame) -> pd.Series:
    conditions = [df["home_goals"] > df["away_goals"], df["home_goals"] == df["away_goals"]]
    choices = [1, 0]
    y = np.select(conditions, choices, default=2)
    print("=" * 80)
    print("2. CIBLE — distribution 1X2 (0=nul, 1=domicile, 2=extérieur)")
    print("=" * 80)
    print(pd.Series(y).value_counts().sort_index().to_string())
    print()
    return pd.Series(y, index=df.index, name="target")


def select_features(df: pd.DataFrame) -> pd.DataFrame:
    print("=" * 80)
    print(f"3. FEATURES D'ENTRÉE — {len(EXISTING_FEATURE_COLUMNS)} existantes + "
          f"{len(NEW_FEATURE_COLUMNS)} nouvelles + league (catégorielle native)")
    print("=" * 80)
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    assert not missing, f"Colonnes de features manquantes : {missing}"

    all_columns = FEATURE_COLUMNS + CATEGORICAL_COLUMNS
    X = df[all_columns].copy()
    X["league"] = X["league"].astype("category")

    print(f"  Existantes ({len(EXISTING_FEATURE_COLUMNS)}) : {EXISTING_FEATURE_COLUMNS}")
    print(f"  Nouvelles ({len(NEW_FEATURE_COLUMNS)})   : {NEW_FEATURE_COLUMNS}")

    nan_counts = X.isna().sum()
    nan_counts = nan_counts[nan_counts > 0]
    if len(nan_counts):
        print("\n  NaN résiduels (laissés tels quels — XGBoost/LightGBM gèrent nativement) :")
        print(nan_counts.to_string())
    print()
    return X


def temporal_split(df, X, y):
    print("=" * 80)
    print("4. SPLIT TEMPOREL — 300 derniers matchs, TOUTES LIGUES CONFONDUES")
    print("=" * 80)
    n = len(df)
    test_start = n - N_TEST
    val_start = test_start - N_VAL

    X_test, y_test = X.iloc[test_start:], y.iloc[test_start:]
    X_val, y_val = X.iloc[val_start:test_start], y.iloc[val_start:test_start]
    X_fit, y_fit = X.iloc[:val_start], y.iloc[:val_start]

    print(f"  Train (fit)       : {len(X_fit)} matchs   [{df['date'].iloc[0]} -> {df['date'].iloc[val_start - 1]}]")
    print(f"  Validation (ES)   : {len(X_val)} matchs   [{df['date'].iloc[val_start]} -> {df['date'].iloc[test_start - 1]}]")
    print(f"  Test              : {len(X_test)} matchs   [{df['date'].iloc[test_start]} -> {df['date'].iloc[-1]}]")

    df_test = df.iloc[test_start:]
    print(f"\n  Composition en ligues du test set ({N_TEST} matchs) :")
    print(df_test["league"].value_counts().to_string())
    print()

    return (X_fit, y_fit), (X_val, y_val), (X_test, y_test), df_test


def train_xgboost(X_fit, y_fit, X_val, y_val):
    print("=" * 80)
    print("5a. ENTRAÎNEMENT XGBOOST (hyperparamètres identiques aux tentatives passées)")
    print("=" * 80)
    model = xgb.XGBClassifier(
        max_depth=4, learning_rate=0.05, n_estimators=300,
        objective="multi:softprob", num_class=3, eval_metric="mlogloss",
        early_stopping_rounds=20, random_state=42,
        enable_categorical=True, tree_method="hist",
    )
    model.fit(X_fit, y_fit, eval_set=[(X_val, y_val)], verbose=False)
    best_iter = getattr(model, "best_iteration", None)
    print(f"  best_iteration (early stopping) : {best_iter}\n")
    return model


def train_lightgbm(X_fit, y_fit, X_val, y_val):
    print("=" * 80)
    print("5b. ENTRAÎNEMENT LIGHTGBM (configuration équivalente à XGBoost ci-dessus)")
    print("=" * 80)
    X_fit_lgb, X_val_lgb = X_fit.copy(), X_val.copy()
    X_fit_lgb["league"] = X_fit_lgb["league"].cat.codes.astype("category")
    X_val_lgb["league"] = pd.Categorical(X_val_lgb["league"], categories=X_fit["league"].cat.categories).codes
    X_val_lgb["league"] = X_val_lgb["league"].astype("category")

    model = lgb.LGBMClassifier(
        max_depth=4, num_leaves=15, learning_rate=0.05, n_estimators=300,
        objective="multiclass", num_class=3, random_state=42, verbosity=-1,
    )
    model.fit(
        X_fit_lgb, y_fit, eval_set=[(X_val_lgb, y_val)],
        categorical_feature=["league"],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
    )
    print(f"  best_iteration (early stopping) : {model.best_iteration_}\n")
    return model, X_fit["league"].cat.categories


def _reorder_proba(proba_raw, class_order):
    reorder = [class_order.index(c) for c in CLASS_LABELS]
    return proba_raw[:, reorder]


def evaluate_model(name, proba_model, df_test, y_test, dc_logloss, dc_acc, proba_dc):
    print("=" * 80)
    print(f"6. ÉVALUATION — {name} vs Dixon-Coles seul")
    print("=" * 80)

    pred = np.array(CLASS_LABELS)[np.argmax(proba_model, axis=1)]
    model_logloss = log_loss(y_test, proba_model, labels=CLASS_LABELS)
    model_acc = accuracy_score(y_test, pred)

    print(f"  {'Modèle':<20} {'log-loss':>10} {'accuracy':>10}")
    print(f"  {'Dixon-Coles seul':<20} {dc_logloss:>10.4f} {dc_acc:>10.4f}")
    print(f"  {name:<20} {model_logloss:>10.4f} {model_acc:>10.4f}")

    delta = dc_logloss - model_logloss
    print(f"\n  Delta log-loss (DC - {name}) : {delta:+.4f}  "
          f"({name + ' meilleur' if delta > 0 else 'Dixon-Coles meilleur ou égal'})")

    rng = np.random.default_rng(42)
    n = len(y_test)
    y_arr = y_test.to_numpy()
    deltas = []
    for _ in range(2000):
        idx = rng.integers(0, n, size=n)
        dc_ll = log_loss(y_arr[idx], proba_dc[idx], labels=CLASS_LABELS)
        model_ll = log_loss(y_arr[idx], proba_model[idx], labels=CLASS_LABELS)
        deltas.append(dc_ll - model_ll)
    deltas = np.array(deltas)
    frac_better = (deltas > 0).mean()
    print(f"  Bootstrap apparié (2000 tirages, n={n}) : {name} meilleur dans {frac_better:.1%} des tirages, "
          f"IC95% du delta = [{np.quantile(deltas, 0.025):+.4f}, {np.quantile(deltas, 0.975):+.4f}]")

    verdict_gain = bool(frac_better > VERDICT_FRAC_THRESHOLD and delta > 0)
    honest_verdict = (
        f"VERDICT : {name} améliore la log-loss de façon crédible (gain positif et robuste au bootstrap)."
        if verdict_gain else
        f"VERDICT : PAS DE GAIN CLAIR pour {name} — l'écart de log-loss est faible et/ou instable au bootstrap. "
        f"Ne pas présenter comme une amélioration confirmée, même avec les nouvelles features."
    )
    print(f">>> {honest_verdict}\n")

    return {
        "model_logloss": model_logloss, "model_acc": model_acc,
        "dc_logloss": dc_logloss, "dc_acc": dc_acc,
        "frac_better": frac_better, "delta": delta, "gain": verdict_gain,
        "delta_ci95": (float(np.quantile(deltas, 0.025)), float(np.quantile(deltas, 0.975))),
    }


def feature_importance(name, gain_dict, feature_names):
    print("=" * 80)
    print(f"7. IMPORTANCE DES FEATURES ({name}, gain)")
    print("=" * 80)
    for c in feature_names:
        gain_dict.setdefault(c, 0.0)
    total = sum(gain_dict.values()) or 1.0
    table = pd.DataFrame(
        [{"feature": k, "gain": v, "gain_pct": 100 * v / total} for k, v in gain_dict.items()]
    ).sort_values("gain", ascending=False).reset_index(drop=True)
    print(table.to_string(index=False))

    new_pct = table[table["feature"].isin(NEW_FEATURE_COLUMNS)]["gain_pct"].sum()
    print(f"\n  Part de gain totale des 8 NOUVELLES features : {new_pct:.1f}%")
    if new_pct < 2:
        print("  -> Quasi nulle : les nouvelles features n'apportent pas d'information exploitée par ce modèle.")
    print()
    return table, new_pct


def persist_model_version(
    model_type: str, metrics: dict, top_features: list[str],
    artifact: str | None = None, config: dict | None = None,
) -> int:
    """
    Crée une NOUVELLE ModelVersion à chaque run (voir next_version_name).

    Phase 8 (§13) : is_active=True INCONDITIONNELLEMENT sur un entraînement
    réussi (déactive les autres versions du même model_type au préalable,
    même politique que Dixon-Coles/Elo) — la comparaison à Dixon-Coles
    ci-dessous reste calculée et journalisée dans `notes`, mais NE GATE PLUS
    l'activation (voir docstring module). `artifact`/`config` : None pour un
    appel de test sans entraînement réel (voir test_ml_stacking.py) — une
    ModelVersion sans artefact reste alors honnêtement NOT live_available
    (voir app/ai/arena/models_common.py::_MLPredictionModel), jamais un demi-
    état fabriqué. Aucune TeamRating : n'a pas de sens pour un modèle à
    features (pas de rating par équipe à stocker).
    """
    init_db()
    notes = (
        f"log-loss={metrics['model_logloss']:.4f} vs Dixon-Coles={metrics['dc_logloss']:.4f} "
        f"(delta={metrics['delta']:+.4f}) ; accuracy={metrics['model_acc']:.4f} vs DC={metrics['dc_acc']:.4f} ; "
        f"bootstrap 2000 tirages : meilleur dans {metrics['frac_better']:.1%} des cas, "
        f"IC95%=[{metrics['delta_ci95'][0]:+.4f}, {metrics['delta_ci95'][1]:+.4f}] ; "
        f"{len(EXISTING_FEATURE_COLUMNS)} features existantes + {len(NEW_FEATURE_COLUMNS)} nouvelles "
        f"(voir api/app/ai/engine/features.py) ; top features : {', '.join(top_features[:5])} ; "
        f"verdict vs Dixon-Coles : {'GAIN CRÉDIBLE' if metrics['gain'] else 'PAS DE GAIN CLAIR'} "
        "(informatif — n'affecte plus is_active depuis la Phase 8 ; voir ensemble_eligible pour "
        "le critère réel d'inclusion dans l'Ensemble)."
    )
    with Session(engine) as session:
        deactivate_other_versions(session, model_type)
        version = ModelVersion(
            name=next_version_name(session, f"xfoot-{model_type}"),
            model_type=model_type,
            trained_at=datetime.now(timezone.utc),
            is_active=True,
            notes=notes,
            artifact=artifact,
            config=json.dumps(config) if config is not None else None,
        )
        session.add(version)
        session.commit()
        session.refresh(version)
        return version.id


def _log_ml_test_predictions(model_type: str, df_test: pd.DataFrame, proba: np.ndarray, model_version_id: int) -> int:
    """
    Journalise (Phase 6) chaque prédiction INDIVIDUELLE du test-set final
    (déjà un split temporel, jamais entraîné dessus — sans fuite) via le
    logger commun, puis la résout IMMÉDIATEMENT : le match est déjà dans le
    passé et son résultat déjà connu de ce script (même discipline que
    scripts/backtest_elo.py::_log_elo_test_predictions).

    `proba` est ordonnée selon CLASS_LABELS=[0,1,2]=[draw,home,away] (voir
    _reorder_proba) — XGBoost/LightGBM ne modélisent QUE le 1X2 (vérifié :
    aucune cible BTTS/over-under n'est jamais construite dans ce script),
    donc prob_btts_*/prob_over_2_5/prob_under_2_5 restent None, jamais
    fabriquées.
    """
    n = 0
    with Session(engine) as session:
        for row, p in zip(df_test.itertuples(), proba):
            record = PredictionRecord(
                league=row.league,
                match_date=row.date.date(),
                home_team=row.home_team,
                away_team=row.away_team,
                model_type=model_type,
                prob_draw=float(p[0]), prob_home=float(p[1]), prob_away=float(p[2]),
                source="backtest",
            )
            pred_row = log_prediction(session, record, model_version_id)
            if pred_row.status != "resolved":
                resolve_prediction(pred_row, int(row.home_goals), int(row.away_goals))
                session.add(pred_row)
            n += 1
        session.commit()
    return n


def main():
    report_unused_columns()
    df = load_and_filter()
    y = build_target(df)
    X = select_features(df)

    (X_fit, y_fit), (X_val, y_val), (X_test, y_test), df_test = temporal_split(df, X, y)

    proba_dc = df_test[DC_PROBA_COLUMNS_IN_LABEL_ORDER].to_numpy()
    row_sums = proba_dc.sum(axis=1)
    assert np.allclose(row_sums, 1.0, atol=1e-6), "Probabilités Dixon-Coles ne somment pas à 1 sur le test"
    dc_pred = np.array(CLASS_LABELS)[np.argmax(proba_dc, axis=1)]
    dc_logloss = log_loss(y_test, proba_dc, labels=CLASS_LABELS)
    dc_acc = accuracy_score(y_test, dc_pred)

    results = {}

    # Config partagée par les deux modèles (Phase 8, §10) — league_categories
    # est LE MÊME ordre pour XGBoost (catégorie string native) et LightGBM
    # (converti en codes entiers dans train_lightgbm, sur ce même ordre) :
    # une seule source de vérité, jamais deux listes qui pourraient diverger.
    league_categories = X_fit["league"].cat.categories.tolist()
    base_config = {
        "feature_columns": FEATURE_COLUMNS,
        "league_categories": league_categories,
        "feature_version": "phase8-v1",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_window": {"n_fit": len(X_fit), "n_val": len(X_val), "n_test": len(X_test)},
        "markets": ["1X2"],
    }

    xgb_model = train_xgboost(X_fit, y_fit, X_val, y_val)
    proba_xgb = _reorder_proba(xgb_model.predict_proba(X_test), list(xgb_model.classes_))
    xgb_metrics = evaluate_model("XGBoost", proba_xgb, df_test, y_test, dc_logloss, dc_acc, proba_dc)
    xgb_gain = xgb_model.get_booster().get_score(importance_type="gain")
    xgb_table, xgb_new_pct = feature_importance("XGBoost", xgb_gain, list(X_fit.columns))
    # Sérialisation native (§5) : booster.save_raw("json"), jamais un format inventé —
    # roundtrip identique vérifié en test (api/test_ml_live_serving.py).
    xgb_artifact = xgb_model.get_booster().save_raw(raw_format="json").decode("utf-8")
    xgb_config = {**base_config, "class_order": [int(c) for c in xgb_model.classes_]}
    xgb_version_id = persist_model_version("xgboost", xgb_metrics, xgb_table["feature"].tolist(),
                                            artifact=xgb_artifact, config=xgb_config)
    n_xgb_logged = _log_ml_test_predictions("xgboost", df_test, proba_xgb, xgb_version_id)
    print(f"  {n_xgb_logged} prédiction(s) individuelle(s) journalisées dans model_predictions (ModelVersion #{xgb_version_id}).\n")
    results["xgboost"] = {**xgb_metrics, "new_features_gain_pct": xgb_new_pct, "model_version_id": xgb_version_id}

    lgb_model, lgb_league_categories = train_lightgbm(X_fit, y_fit, X_val, y_val)
    X_test_lgb = X_test.copy()
    X_test_lgb["league"] = pd.Categorical(X_test_lgb["league"], categories=lgb_league_categories).codes
    X_test_lgb["league"] = X_test_lgb["league"].astype("category")
    proba_lgb = _reorder_proba(lgb_model.predict_proba(X_test_lgb), list(lgb_model.classes_))
    lgb_metrics = evaluate_model("LightGBM", proba_lgb, df_test, y_test, dc_logloss, dc_acc, proba_dc)
    lgb_gain = dict(zip(lgb_model.feature_name_, lgb_model.booster_.feature_importance(importance_type="gain")))
    lgb_table, lgb_new_pct = feature_importance("LightGBM", lgb_gain, list(X_fit.columns))
    # model_to_string() : sérialisation texte native LightGBM (§5) — roundtrip
    # identique vérifié en test. class_order ici correspond aux CODES 0/1/2
    # (déjà la convention CLASS_LABELS, `league` mise à part) — inchangé par
    # la conversion cat.codes appliquée UNIQUEMENT à la colonne "league".
    lgb_artifact = lgb_model.booster_.model_to_string()
    lgb_config = {**base_config, "class_order": [int(c) for c in lgb_model.classes_]}
    lgb_version_id = persist_model_version("lightgbm", lgb_metrics, lgb_table["feature"].tolist(),
                                            artifact=lgb_artifact, config=lgb_config)
    n_lgb_logged = _log_ml_test_predictions("lightgbm", df_test, proba_lgb, lgb_version_id)
    print(f"  {n_lgb_logged} prédiction(s) individuelle(s) journalisées dans model_predictions (ModelVersion #{lgb_version_id}).\n")
    results["lightgbm"] = {**lgb_metrics, "new_features_gain_pct": lgb_new_pct, "model_version_id": lgb_version_id}

    print("=" * 80)
    print("RÉSUMÉ FINAL")
    print("=" * 80)
    for name, r in results.items():
        print(f"  {name:<10} log-loss={r['model_logloss']:.4f} (DC={r['dc_logloss']:.4f})  "
              f"gain crédible vs DC={r['gain']}  ModelVersion #{r['model_version_id']} (is_active=True, live-servable)")
    any_gain = any(r["gain"] for r in results.values())
    print(f"\n>>> VERDICT GLOBAL (log-loss vs Dixon-Coles, informatif — Phase 8) : "
          f"{'au moins un modèle montre un gain crédible vs Dixon-Coles.' if any_gain else 'AUCUN GAIN CLAIR vs Dixon-Coles sur ce test set.'} "
          "Les deux modèles sont désormais LIVE-SERVABLES et évalués en continu par l'Ensemble "
          "(ensemble_eligible, voir app/ai/arena/availability.py) — ce verdict ponctuel ne détermine plus leur activation.")

    return results


if __name__ == "__main__":
    main()
    sys.exit(0)  # un "pas de gain" documenté n'est pas un échec de ce script
