"""
test_ml_live_serving.py — Phase 8, Partie A (§11 §27 du ticket) : XGBoost/
LightGBM sérialisés, rechargés, servis EN DIRECT via
models_common.py::_MLPredictionModel.

Entraîne des modèles PETITS et SYNTHÉTIQUES (mêmes 25 colonnes de
FEATURE_COLUMNS + "league" catégorielle, cible 0/1/2 — jamais la vraie
pipeline build_ml_features_from_db, ~35 min, voir test_ml_stacking.py) pour
prouver que train -> save -> load -> predict est numériquement identique
(§11), sans imposer ce coût à la suite de tests.

Usage : python api/test_ml_live_serving.py
"""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_ml_live_serving.db")

import xgboost as xgb
import lightgbm as lgb
from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.match import Match
from app.models.team_rating import ModelVersion
from app.ai.engine.features import FEATURE_COLUMNS
from app.ai.arena.models_common import MatchContext, XGBoostPredictionModel, LightGBMPredictionModel

init_db()

LEAGUES = ["Bundesliga", "LaLiga", "Ligue1", "PremierLeague", "SerieA"]
MATCH_DATE = date(2026, 6, 1)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(Match)).all():
            session.delete(row)
        for row in session.exec(select(ModelVersion)).all():
            session.delete(row)
        session.commit()


def _synthetic_training_frame(n: int = 300, seed: int = 0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({c: rng.normal(size=n) for c in FEATURE_COLUMNS})
    X["league"] = pd.Categorical(rng.choice(LEAGUES, size=n))
    y = pd.Series(rng.choice([0, 1, 2], size=n), name="target")  # 0=nul,1=domicile,2=extérieur
    return X, y


def _train_xgboost(X, y):
    model = xgb.XGBClassifier(
        max_depth=3, n_estimators=25, objective="multi:softprob", num_class=3,
        enable_categorical=True, tree_method="hist", random_state=0,
    )
    model.fit(X, y)
    return model


def _train_lightgbm(X, y):
    X_lgb = X.copy()
    X_lgb["league"] = X_lgb["league"].cat.codes.astype("category")
    model = lgb.LGBMClassifier(max_depth=3, n_estimators=25, objective="multiclass", num_class=3, verbosity=-1, random_state=0)
    model.fit(X_lgb, y, categorical_feature=["league"])
    return model


def _make_version(model_type: str, artifact: str, config: dict, is_active: bool = True) -> int:
    with Session(engine) as session:
        v = ModelVersion(
            name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
            model_type=model_type, trained_at=datetime.now(timezone.utc),
            is_active=is_active, artifact=artifact, config=json.dumps(config),
        )
        session.add(v)
        session.commit()
        session.refresh(v)
        return v.id


# ---------------------------------------------------------------------------
# 1. Roundtrip numérique — train -> save -> load -> predict (§11)
# ---------------------------------------------------------------------------

def test_xgboost_roundtrip_identical_probabilities():
    X, y = _synthetic_training_frame()
    model = _train_xgboost(X, y)
    league_categories = X["league"].cat.categories.tolist()
    class_order = [int(c) for c in model.classes_]

    raw = model.get_booster().save_raw(raw_format="json").decode("utf-8")

    version_id = _make_version("xgboost", raw, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": league_categories, "class_order": class_order,
    })

    row = {c: float(X[c].iloc[0]) for c in FEATURE_COLUMNS}
    league = str(X["league"].iloc[0])

    with Session(engine) as session:
        pm = XGBoostPredictionModel({})
        version = session.get(ModelVersion, version_id)
        booster = pm._get_booster(version)
        proba = pm._predict_proba(booster, row, league, league_categories)

    expected = model.predict_proba(X.iloc[[0]])[0]
    assert np.allclose(proba, expected, atol=1e-9), f"divergence roundtrip XGBoost : {proba} vs {expected}"
    print(f"  [OK] XGBoost train->save->load->predict identique (max diff={np.max(np.abs(np.array(proba)-expected)):.2e})")


def test_lightgbm_roundtrip_identical_probabilities():
    X, y = _synthetic_training_frame(seed=1)
    model = _train_lightgbm(X, y)
    league_categories = X["league"].cat.categories.tolist()
    class_order = [int(c) for c in model.classes_]

    s = model.booster_.model_to_string()
    version_id = _make_version("lightgbm", s, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": league_categories, "class_order": class_order,
    })

    row = {c: float(X[c].iloc[2]) for c in FEATURE_COLUMNS}
    league = str(X["league"].iloc[2])

    with Session(engine) as session:
        pm = LightGBMPredictionModel({})
        version = session.get(ModelVersion, version_id)
        booster = pm._get_booster(version)
        proba = pm._predict_proba(booster, row, league, league_categories)

    X_lgb = X.copy()
    X_lgb["league"] = X_lgb["league"].cat.codes.astype("category")
    expected = model.predict_proba(X_lgb.iloc[[2]])[0]
    assert np.allclose(proba, expected, atol=1e-9), f"divergence roundtrip LightGBM : {proba} vs {expected}"
    print(f"  [OK] LightGBM train->save->load->predict identique (max diff={np.max(np.abs(np.array(proba)-expected)):.2e})")


def test_lightgbm_column_order_safety():
    """§32/§27 : Booster.predict() de LightGBM ne validant pas l'ordre des
    colonnes (vérifié empiriquement), _predict_proba DOIT reconstruire cet
    ordre depuis booster.feature_name() — ce test le prouve en passant un
    `row` dict dans un ordre d'insertion VOLONTAIREMENT différent de
    FEATURE_COLUMNS et en vérifiant que le résultat reste correct."""
    X, y = _synthetic_training_frame(seed=2)
    model = _train_lightgbm(X, y)
    league_categories = X["league"].cat.categories.tolist()
    s = model.booster_.model_to_string()
    version_id = _make_version("lightgbm", s, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": league_categories,
        "class_order": [int(c) for c in model.classes_],
    })

    shuffled_columns = list(reversed(FEATURE_COLUMNS))
    row = {c: float(X[c].iloc[5]) for c in shuffled_columns}  # dict inséré à l'envers
    league = str(X["league"].iloc[5])

    with Session(engine) as session:
        pm = LightGBMPredictionModel({})
        version = session.get(ModelVersion, version_id)
        booster = pm._get_booster(version)
        proba = pm._predict_proba(booster, row, league, league_categories)

    X_lgb = X.copy()
    X_lgb["league"] = X_lgb["league"].cat.codes.astype("category")
    expected = model.predict_proba(X_lgb.iloc[[5]])[0]
    assert np.allclose(proba, expected, atol=1e-9), (
        f"un dict `row` dans le désordre a produit un résultat différent : {proba} vs {expected} "
        "-- l'ordre des colonnes n'a pas été correctement reconstruit depuis le booster."
    )
    print("  [OK] ordre d'insertion du dict `row` sans effet -- l'ordre réel vient de booster.feature_name()")


# ---------------------------------------------------------------------------
# 2. Service LIVE bout-en-bout (models_common.py::_MLPredictionModel.predict)
# ---------------------------------------------------------------------------

def test_live_predict_end_to_end_with_no_history_uses_nan_natively():
    """Aucun historique en base pour ces équipes -> build_live_features
    renvoie des NaN pour la quasi-totalité des features -- XGBoost/LightGBM
    doivent les gérer NATIVEMENT (comme à l'entraînement, voir
    scripts/train_ml_stacking_from_db.py::select_features), jamais un 500."""
    _clean_all()
    X, y = _synthetic_training_frame(seed=3)
    model = _train_xgboost(X, y)
    raw = model.get_booster().save_raw(raw_format="json").decode("utf-8")
    _make_version("xgboost", raw, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": X["league"].cat.categories.tolist(),
        "class_order": [int(c) for c in model.classes_],
    })

    with Session(engine) as session:
        pm = XGBoostPredictionModel({})  # league_models vide -> dc_* = NaN aussi
        ctx = MatchContext("Ligue1", "Equipe Jamais Vue A", "Equipe Jamais Vue B", MATCH_DATE)
        outcome = pm.predict(session, ctx)

    assert outcome.status == "ok", outcome.reason
    total = outcome.record.prob_home + outcome.record.prob_draw + outcome.record.prob_away
    assert abs(total - 1.0) < 1e-6
    print(f"  [OK] aucun historique -> features NaN gérées nativement, prédiction valide "
          f"(home={outcome.record.prob_home:.3f}, draw={outcome.record.prob_draw:.3f}, away={outcome.record.prob_away:.3f})")


def test_live_predict_unknown_league_is_unavailable():
    _clean_all()
    X, y = _synthetic_training_frame(seed=4)
    model = _train_lightgbm(X, y)
    s = model.booster_.model_to_string()
    _make_version("lightgbm", s, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": X["league"].cat.categories.tolist(),
        "class_order": [int(c) for c in model.classes_],
    })

    with Session(engine) as session:
        pm = LightGBMPredictionModel({})
        outcome = pm.predict(session, MatchContext("Ligue Jamais Entraînée", "A", "B", MATCH_DATE))

    assert outcome.status == "unavailable"
    assert "catégories" in outcome.reason or "categories" in outcome.reason.lower()
    print(f"  [OK] ligue absente des catégories apprises -> unavailable : {outcome.reason}")


def test_check_availability_no_active_version():
    _clean_all()
    with Session(engine) as session:
        result = XGBoostPredictionModel({}).check_availability(session)
    assert result.live_available is False
    print("  [OK] aucune version active -> live_available=False")


def test_check_availability_active_without_artifact():
    _clean_all()
    with Session(engine) as session:
        v = ModelVersion(name="test-xgb-no-artifact", model_type="xgboost",
                          trained_at=datetime.now(timezone.utc), is_active=True, artifact=None, config=None)
        session.add(v)
        session.commit()
        result = XGBoostPredictionModel({}).check_availability(session)
    assert result.live_available is False
    assert result.model_version_id is not None
    print("  [OK] version active sans artefact (créée avant Phase 8) -> live_available=False, jamais un crash")


def test_check_availability_corrupted_artifact():
    _clean_all()
    version_id = _make_version("lightgbm", "CECI N'EST PAS UN MODELE LIGHTGBM VALIDE", {
        "feature_columns": FEATURE_COLUMNS, "league_categories": LEAGUES, "class_order": [0, 1, 2],
    })
    with Session(engine) as session:
        result = LightGBMPredictionModel({}).check_availability(session)
    assert result.live_available is False
    assert result.model_version_id == version_id
    print(f"  [OK] artefact corrompu -> live_available=False (jamais une exception non gérée) : {result.reason}")


def test_check_availability_healthy_version():
    _clean_all()
    X, y = _synthetic_training_frame(seed=5)
    model = _train_xgboost(X, y)
    raw = model.get_booster().save_raw(raw_format="json").decode("utf-8")
    version_id = _make_version("xgboost", raw, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": X["league"].cat.categories.tolist(),
        "class_order": [int(c) for c in model.classes_],
    })
    with Session(engine) as session:
        result = XGBoostPredictionModel({}).check_availability(session)
    assert result.live_available is True
    assert result.model_version_id == version_id
    print("  [OK] version active + artefact/config valides -> live_available=True")


# ---------------------------------------------------------------------------
# 3. Cache (§31) — pas de rechargement à chaque prédiction pour LA MÊME version
# ---------------------------------------------------------------------------

def test_booster_cached_across_predictions_same_version():
    _clean_all()
    X, y = _synthetic_training_frame(seed=6)
    model = _train_xgboost(X, y)
    raw = model.get_booster().save_raw(raw_format="json").decode("utf-8")
    _make_version("xgboost", raw, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": X["league"].cat.categories.tolist(),
        "class_order": [int(c) for c in model.classes_],
    })

    load_count = {"n": 0}
    pm = XGBoostPredictionModel({})
    original_load = pm._load_booster

    def _counting_load(artifact_text):
        load_count["n"] += 1
        return original_load(artifact_text)

    pm._load_booster = _counting_load

    with Session(engine) as session:
        version = session.exec(select(ModelVersion).where(ModelVersion.model_type == "xgboost")).first()
        pm._get_booster(version)
        pm._get_booster(version)
        pm._get_booster(version)

    assert load_count["n"] == 1, f"le booster a été rechargé {load_count['n']} fois pour la même version (attendu 1)"
    print("  [OK] booster chargé UNE seule fois pour 3 appels sur la même ModelVersion (cache actif)")


def test_booster_cache_invalidated_on_new_version():
    _clean_all()
    X, y = _synthetic_training_frame(seed=7)
    model_a = _train_xgboost(X, y)
    raw_a = model_a.get_booster().save_raw(raw_format="json").decode("utf-8")
    _make_version("xgboost", raw_a, {
        "feature_columns": FEATURE_COLUMNS, "league_categories": X["league"].cat.categories.tolist(),
        "class_order": [int(c) for c in model_a.classes_],
    }, is_active=True)

    pm = XGBoostPredictionModel({})
    with Session(engine) as session:
        v1 = session.exec(select(ModelVersion).where(ModelVersion.model_type == "xgboost")).first()
        b1 = pm._get_booster(v1)

    # Nouvelle version activée (ex. ré-entraînement) -> ancienne désactivée.
    with Session(engine) as session:
        old = session.exec(select(ModelVersion).where(ModelVersion.model_type == "xgboost")).first()
        old.is_active = False
        session.add(old)
        X2, y2 = _synthetic_training_frame(seed=8)
        model_b = _train_xgboost(X2, y2)
        raw_b = model_b.get_booster().save_raw(raw_format="json").decode("utf-8")
        v2 = ModelVersion(
            name="test-xgb-v2", model_type="xgboost", trained_at=datetime.now(timezone.utc), is_active=True,
            artifact=raw_b, config=json.dumps({
                "feature_columns": FEATURE_COLUMNS, "league_categories": X2["league"].cat.categories.tolist(),
                "class_order": [int(c) for c in model_b.classes_],
            }),
        )
        session.add(v2)
        session.commit()
        session.refresh(v2)

    with Session(engine) as session:
        v2_reloaded = session.get(ModelVersion, v2.id)
        b2 = pm._get_booster(v2_reloaded)

    assert b1 is not b2, "le cache aurait dû être invalidé pour une NOUVELLE ModelVersion.id"
    print("  [OK] activer une nouvelle version invalide bien le cache (booster différent rechargé)")


TESTS = [
    test_xgboost_roundtrip_identical_probabilities,
    test_lightgbm_roundtrip_identical_probabilities,
    test_lightgbm_column_order_safety,
    test_live_predict_end_to_end_with_no_history_uses_nan_natively,
    test_live_predict_unknown_league_is_unavailable,
    test_check_availability_no_active_version,
    test_check_availability_active_without_artifact,
    test_check_availability_corrupted_artifact,
    test_check_availability_healthy_version,
    test_booster_cached_across_predictions_same_version,
    test_booster_cache_invalidated_on_new_version,
]


if __name__ == "__main__":
    failures = 0
    for t in TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    total = len(TESTS)
    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
