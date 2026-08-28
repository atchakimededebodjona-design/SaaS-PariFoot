"""
test_prediction_registry_v1.py — Phase 5.5 : ce que ce ticket a réellement
ajouté à app/ai/arena/prediction_logging.py (Phase 6, déjà en place).

NE duplique PAS test_multi_model_prediction_logging.py (Phase 6, déjà
exhaustif sur logging/résolution/idempotence multi-modèles/multi-versions —
voir rapport Phase 5.5, §"architecture retenue") : ce fichier couvre
UNIQUEMENT les 3 ajouts de ce ticket — protection anti-leakage par
kickoff_at, fonctions de recherche (get_prediction_by_id/
get_predictions_by_match/get_predictions_by_model), et un test
d'immutabilité explicite (ré-appeler log_prediction avec des probabilités
DIFFÉRENTES ne doit jamais altérer la ligne existante).

Base isolée dédiée (jamais api/app.db) — même précaution que les autres
suites api/test_*.py.

Usage : python api/test_prediction_registry_v1.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_prediction_registry_v1.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import (
    PredictionRecord,
    log_prediction,
    resolve_prediction,
    get_prediction_by_id,
    get_predictions_by_match,
    get_predictions_by_model,
)

init_db()

MATCH_DATE = date(2026, 9, 12)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str) -> int:
    v = ModelVersion(
        name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
        model_type=model_type, trained_at=datetime.now(timezone.utc), is_active=False,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v.id


# ---------------------------------------------------------------------------
# 1. Anti data-leakage — kickoff_at (§10, §22, §26 du ticket)
# ---------------------------------------------------------------------------

def test_prediction_before_kickoff_is_accepted():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "xgboost")
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="Lens", away_team="Lille",
            model_type="xgboost", prob_home=0.4, prob_draw=0.3, prob_away=0.3, source="live",
        )
        kickoff_in_future = datetime.now(timezone.utc) + timedelta(hours=2)
        row = log_prediction(session, record, version_id, kickoff_at=kickoff_in_future)
        session.commit()
        assert row.id is not None
        assert row.status == "pending"
    print("  [OK] prédiction enregistrée avant le coup d'envoi (kickoff_at futur) -> acceptée")


def test_prediction_after_kickoff_is_rejected():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "xgboost")
        record = PredictionRecord(
            league="Ligue1", match_date=MATCH_DATE, home_team="Lens", away_team="Lille",
            model_type="xgboost", prob_home=0.4, prob_draw=0.3, prob_away=0.3, source="live",
        )
        kickoff_in_past = datetime.now(timezone.utc) - timedelta(minutes=5)
        try:
            log_prediction(session, record, version_id, kickoff_at=kickoff_in_past)
            assert False, "log_prediction() aurait dû lever ValueError (coup d'envoi déjà passé)"
        except ValueError as e:
            assert "coup d'envoi" in str(e) or "kickoff" in str(e).lower()

        remaining = session.exec(
            select(ModelPrediction).where(
                ModelPrediction.league == "Ligue1", ModelPrediction.home_team == "Lens",
            )
        ).all()
        assert len(remaining) == 0, "aucune ligne ne doit être écrite quand la prédiction est refusée"
    print("  [OK] prédiction tentée après le coup d'envoi (kickoff_at passé) -> ValueError, rien écrit")


def test_prediction_at_exact_kickoff_is_rejected():
    """Cas limite : now == kickoff_at doit être refusé (pas seulement now > kickoff_at) —
    §22 du ticket : 'prediction_created_at >= kickoff_at'."""
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "elo")
        record = PredictionRecord(
            league="SerieA", match_date=MATCH_DATE, home_team="Roma", away_team="Napoli",
            model_type="elo", prob_home=0.35, prob_draw=0.3, prob_away=0.35, source="live",
        )
        try:
            log_prediction(session, record, version_id, kickoff_at=datetime.now(timezone.utc))
            assert False, "now == kickoff_at doit être refusé (>=), pas seulement now > kickoff_at"
        except ValueError:
            pass
    print("  [OK] now == kickoff_at (limite exacte) -> refusé, comme now > kickoff_at")


def test_kickoff_at_absent_preserves_existing_callers():
    """Rétrocompatibilité : les appelants existants (scheduler.py, qui filtre
    déjà en amont — voir docstring log_prediction) n'ont pas à fournir
    kickoff_at ; son absence ne doit rien changer au comportement Phase 6."""
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "dixon_coles")
        record = PredictionRecord(
            league="LaLiga", match_date=MATCH_DATE, home_team="Sevilla", away_team="Betis",
            model_type="dixon_coles", prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="live",
        )
        row = log_prediction(session, record, version_id)  # pas de kickoff_at, comme avant ce ticket
        session.commit()
        assert row.id is not None
    print("  [OK] kickoff_at omis (comportement Phase 6 inchangé) -> toujours accepté")


# ---------------------------------------------------------------------------
# 2. Immutabilité explicite (§13, §26) — ré-appeler avec des probabilités
#    DIFFÉRENTES ne doit jamais écraser la ligne existante.
# ---------------------------------------------------------------------------

def test_relog_with_different_probabilities_never_overwrites():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "lightgbm")
        original = PredictionRecord(
            league="Bundesliga", match_date=MATCH_DATE, home_team="RB Leipzig", away_team="Union Berlin",
            model_type="lightgbm", prob_home=0.7, prob_draw=0.2, prob_away=0.1, source="live",
        )
        row1 = log_prediction(session, original, version_id)
        session.commit()
        original_probs = (row1.prob_home, row1.prob_draw, row1.prob_away)

        # Même clé naturelle (match + modèle + version), probabilités TOTALEMENT différentes —
        # simule un bug appelant ou une tentative de ré-écriture.
        tampered = PredictionRecord(
            league="Bundesliga", match_date=MATCH_DATE, home_team="RB Leipzig", away_team="Union Berlin",
            model_type="lightgbm", prob_home=0.1, prob_draw=0.1, prob_away=0.8, source="live",
        )
        row2 = log_prediction(session, tampered, version_id)
        session.commit()

        assert row1.id == row2.id, "même clé naturelle -> même ligne, jamais une seconde"
        assert (row2.prob_home, row2.prob_draw, row2.prob_away) == original_probs, \
            "les probabilités ORIGINALES doivent survivre intactes, jamais remplacées par le second appel"
    print("  [OK] ré-appeler log_prediction avec des probabilités différentes -> ligne originale intacte")


# ---------------------------------------------------------------------------
# 3. Recherche (§20 du ticket)
# ---------------------------------------------------------------------------

def test_get_prediction_by_id():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "xgboost")
        record = PredictionRecord(
            league="PremierLeague", match_date=MATCH_DATE, home_team="Arsenal", away_team="Chelsea",
            model_type="xgboost", prob_home=0.5, prob_draw=0.25, prob_away=0.25, source="live",
        )
        row = log_prediction(session, record, version_id)
        session.commit()
        pred_id = row.id

    with Session(engine) as session:
        found = get_prediction_by_id(session, pred_id)
        assert found is not None and found.id == pred_id
        assert get_prediction_by_id(session, pred_id + 9999) is None
    print("  [OK] get_prediction_by_id -> trouve la prédiction existante, None si absente")


def test_get_predictions_by_match_returns_all_models_for_that_match():
    _clean_all()
    with Session(engine) as session:
        v_dc = _make_version(session, "dixon_coles")
        v_elo = _make_version(session, "elo")
        v_xgb = _make_version(session, "xgboost")

        common = dict(league="LaLiga", match_date=MATCH_DATE, home_team="Real Madrid", away_team="Barcelona")
        log_prediction(session, PredictionRecord(**common, model_type="dixon_coles", prob_home=0.5, prob_draw=0.25, prob_away=0.25, source="live"), v_dc)
        log_prediction(session, PredictionRecord(**common, model_type="elo", prob_home=0.45, prob_draw=0.3, prob_away=0.25, source="live"), v_elo)
        log_prediction(session, PredictionRecord(**common, model_type="xgboost", prob_home=0.4, prob_draw=0.35, prob_away=0.25, source="live"), v_xgb)
        # Un autre match ne doit jamais apparaître dans le résultat.
        log_prediction(session, PredictionRecord(league="LaLiga", match_date=MATCH_DATE, home_team="Sevilla", away_team="Betis", model_type="elo", prob_home=0.4, prob_draw=0.3, prob_away=0.3, source="live"), v_elo)
        session.commit()

        results = get_predictions_by_match(session, "LaLiga", MATCH_DATE, "Real Madrid", "Barcelona")
        assert len(results) == 3
        assert {r.model_type for r in results} == {"dixon_coles", "elo", "xgboost"}
    print("  [OK] get_predictions_by_match -> les 3 modèles pour CE match, jamais un autre match")


def test_get_predictions_by_model_filters_correctly():
    _clean_all()
    with Session(engine) as session:
        v1 = _make_version(session, "xgboost")
        v2 = _make_version(session, "xgboost")  # 2e version du même modèle (§26 : "plusieurs versions")

        log_prediction(session, PredictionRecord(league="Ligue1", match_date=MATCH_DATE, home_team="PSG", away_team="Lyon", model_type="xgboost", prob_home=0.6, prob_draw=0.25, prob_away=0.15, source="backtest"), v1)
        log_prediction(session, PredictionRecord(league="Ligue1", match_date=MATCH_DATE, home_team="Monaco", away_team="Nice", model_type="xgboost", prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="backtest"), v2)
        log_prediction(session, PredictionRecord(league="Ligue1", match_date=MATCH_DATE, home_team="Lens", away_team="Lille", model_type="elo", prob_home=0.4, prob_draw=0.3, prob_away=0.3, source="backtest"), v1)
        session.commit()

        all_xgb = get_predictions_by_model(session, "xgboost")
        assert len(all_xgb) == 2, "les 2 versions xgboost, jamais elo"

        only_v1 = get_predictions_by_model(session, "xgboost", model_version_id=v1)
        assert len(only_v1) == 1 and only_v1[0].home_team == "PSG"

        only_v2 = get_predictions_by_model(session, "xgboost", model_version_id=v2)
        assert len(only_v2) == 1 and only_v2[0].home_team == "Monaco"
    print("  [OK] get_predictions_by_model -> filtre correctement par model_type et par version")


def test_get_predictions_by_model_status_and_date_filters():
    _clean_all()
    with Session(engine) as session:
        version_id = _make_version(session, "elo")
        row_pending = log_prediction(session, PredictionRecord(league="SerieA", match_date=MATCH_DATE, home_team="Inter", away_team="Milan", model_type="elo", prob_home=0.5, prob_draw=0.25, prob_away=0.25, source="live"), version_id)
        row_resolved = log_prediction(session, PredictionRecord(league="SerieA", match_date=MATCH_DATE - timedelta(days=5), home_team="Juventus", away_team="Roma", model_type="elo", prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="live"), version_id)
        resolve_prediction(row_resolved, 2, 0)
        session.add(row_resolved)
        session.commit()

        pending_only = get_predictions_by_model(session, "elo", status="pending")
        assert len(pending_only) == 1 and pending_only[0].id == row_pending.id

        resolved_only = get_predictions_by_model(session, "elo", status="resolved")
        assert len(resolved_only) == 1 and resolved_only[0].id == row_resolved.id

        recent_only = get_predictions_by_model(session, "elo", since=MATCH_DATE)
        assert len(recent_only) == 1 and recent_only[0].id == row_pending.id
    print("  [OK] get_predictions_by_model -> filtres status/since fonctionnent indépendamment")


UNIT_TESTS = [
    test_prediction_before_kickoff_is_accepted,
    test_prediction_after_kickoff_is_rejected,
    test_prediction_at_exact_kickoff_is_rejected,
    test_kickoff_at_absent_preserves_existing_callers,
    test_relog_with_different_probabilities_never_overwrites,
    test_get_prediction_by_id,
    test_get_predictions_by_match_returns_all_models_for_that_match,
    test_get_predictions_by_model_filters_correctly,
    test_get_predictions_by_model_status_and_date_filters,
]

if __name__ == "__main__":
    failures = 0
    for t in UNIT_TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    cleanup_db(DB_PATH)
    total = len(UNIT_TESTS)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
