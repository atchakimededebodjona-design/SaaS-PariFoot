"""
test_live_shadow_comparison.py — Phase 11 : app/ai/arena/shadow_comparison.py
::compute_matched_comparison — comparaison UNIQUEMENT sur l'intersection des
matchs réellement prédits ET résolus par ACTIVE et SHADOW, jamais deux
échantillons indépendants présentés comme comparables.

Inclut aussi le test end-to-end LOCAL demandé au §17 du ticket Phase 11
(1 match synthétique, ACTIVE + SHADOW, stockage → résolution → comparaison
matched) — sur la même base SQLite ISOLÉE que tout le reste de cette suite
(jamais api/app.db, voir _test_support.py) : les données sont donc déjà,
par construction, clairement séparées du LIVE de production, sans mécanisme
supplémentaire nécessaire.

Usage : python api/test_live_shadow_comparison.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_live_shadow_comparison.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.arena.prediction_logging import PredictionRecord, log_prediction, resolve_prediction
from app.ai.arena import shadow_comparison, promotion

init_db()

BASE_DATE = date(2026, 1, 1)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


def _make_version(session, model_type: str, *, is_active=False, status="active") -> ModelVersion:
    v = ModelVersion(name=f"test-{model_type}-{datetime.now(timezone.utc).timestamp()}",
                      model_type=model_type, trained_at=datetime.now(timezone.utc),
                      is_active=is_active, status=status)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _log(session, model_type, version_id, match_date, home_team, away_team, p_true, *, role, resolve=True):
    other = (1.0 - p_true) / 2
    record = PredictionRecord(
        league="Ligue1", match_date=match_date, home_team=home_team, away_team=away_team,
        model_type=model_type, prob_home=p_true, prob_draw=other, prob_away=other, source="live", role=role,
    )
    row = log_prediction(session, record, version_id)
    if resolve:
        resolve_prediction(row, 2, 0)  # home win
        session.add(row)
    session.commit()
    return row


def test_no_active_returns_no_active():
    _clean_all()
    with Session(engine) as session:
        d = shadow_comparison.compute_matched_comparison(session, "xgboost", "1X2")
        assert d.status == "no_active"


def test_no_shadow_returns_no_shadow():
    _clean_all()
    with Session(engine) as session:
        _make_version(session, "xgboost", is_active=True, status="active")
        d = shadow_comparison.compute_matched_comparison(session, "xgboost", "1X2")
        assert d.status == "no_shadow"


def test_matched_excludes_predictions_made_by_only_one_side():
    """§9 du ticket : seule l'INTERSECTION compte — un match prédit
    uniquement par l'un des deux ne doit jamais gonfler l'échantillon matched."""
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")

        for i in range(120):
            d = BASE_DATE + timedelta(days=i)
            _log(session, "xgboost", active.id, d, f"H{i}", f"A{i}", 0.6, role="active")
            _log(session, "xgboost", shadow.id, d, f"H{i}", f"A{i}", 0.7, role="shadow")
        # 30 matchs prédits UNIQUEMENT par active (shadow indisponible ce jour-là) — ne
        # doivent jamais compter dans matched_sample_size.
        for i in range(30):
            d = BASE_DATE + timedelta(days=500 + i)
            _log(session, "xgboost", active.id, d, f"OnlyActive{i}", f"X{i}", 0.6, role="active")

        result = shadow_comparison.compute_matched_comparison(session, "xgboost", "1X2")
        assert result.status == "ok"
        assert result.matched_sample_size == 120, f"attendu 120, obtenu {result.matched_sample_size}"


def test_insufficient_matched_sample():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        for i in range(10):  # < LIVE_MIN_SAMPLE_SIZE (100)
            d = BASE_DATE + timedelta(days=i)
            _log(session, "xgboost", active.id, d, f"H{i}", f"A{i}", 0.6, role="active")
            _log(session, "xgboost", shadow.id, d, f"H{i}", f"A{i}", 0.7, role="shadow")

        result = shadow_comparison.compute_matched_comparison(session, "xgboost", "1X2")
        assert result.status == "insufficient_matched_sample"
        assert result.matched_sample_size == 10


def test_matched_comparison_metrics_and_deltas_correct():
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        shadow = _make_version(session, "xgboost", is_active=False, status="shadow")
        for i in range(120):
            d = BASE_DATE + timedelta(days=i)
            _log(session, "xgboost", active.id, d, f"H{i}", f"A{i}", 0.55, role="active")
            _log(session, "xgboost", shadow.id, d, f"H{i}", f"A{i}", 0.90, role="shadow")

        result = shadow_comparison.compute_matched_comparison(session, "xgboost", "1X2")
        assert result.status == "ok"
        assert result.active_version_id == active.id
        assert result.shadow_version_id == shadow.id
        assert abs(result.active_accuracy - 1.0) < 1e-9  # p_true=0.55 sur home win = pick correct
        assert abs(result.shadow_accuracy - 1.0) < 1e-9
        assert result.shadow_log_loss < result.active_log_loss, "shadow (p=0.9) doit avoir un log loss plus bas qu'active (p=0.55)"
        assert result.delta_log_loss < 0, "delta = shadow - active ; négatif attendu ici (shadow meilleur)"


def test_two_shadow_versions_never_mixed_in_matched_comparison():
    """Item §8/§16 : version A ≠ version B — si deux versions shadow
    successives existent, compute_matched_comparison (par défaut : la plus
    récente) ne doit jamais mélanger leurs échantillons."""
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "xgboost", is_active=True, status="active")
        v1 = _make_version(session, "xgboost", is_active=False, status="shadow")
        for i in range(120):
            d = BASE_DATE + timedelta(days=i)
            _log(session, "xgboost", active.id, d, f"H{i}", f"A{i}", 0.6, role="active")
            _log(session, "xgboost", v1.id, d, f"H{i}", f"A{i}", 0.2, role="shadow")

        result_v1 = shadow_comparison.compute_matched_comparison(session, "xgboost", "1X2", shadow_version_id=v1.id)
        assert result_v1.status == "ok"
        assert result_v1.shadow_version_id == v1.id

        # v2 remplace v1 comme shadow "la plus récente" — v1 reste en base
        # (status changé manuellement ici pour simuler une promotion/retrait),
        # v2 a un échantillon totalement disjoint.
        v1.status = "retired"
        session.add(v1)
        v2 = _make_version(session, "xgboost", is_active=False, status="shadow")
        for i in range(120):
            d = BASE_DATE + timedelta(days=1000 + i)
            _log(session, "xgboost", active.id, d, f"H2-{i}", f"A2-{i}", 0.6, role="active")
            _log(session, "xgboost", v2.id, d, f"H2-{i}", f"A2-{i}", 0.95, role="shadow")
        session.commit()

        result_v2 = shadow_comparison.compute_matched_comparison(session, "xgboost", "1X2")
        assert result_v2.status == "ok"
        assert result_v2.shadow_version_id == v2.id
        assert result_v2.shadow_log_loss != result_v1.shadow_log_loss


def test_end_to_end_local_synthetic_match():
    """§17 du ticket Phase 11 : test réel local — 1 match synthétique,
    ACTIVE + SHADOW, stockage DB, résolution, métriques, comparaison matched.
    Équipes/ligue préfixées "E2E-TEST" pour rester identifiables comme des
    données de test si jamais inspectées manuellement — jamais mélangées au
    LIVE de production (base isolée dédiée, voir DB_PATH ci-dessus)."""
    _clean_all()
    with Session(engine) as session:
        active = _make_version(session, "lightgbm", is_active=True, status="active")
        shadow = _make_version(session, "lightgbm", is_active=False, status="shadow")
        promotion.set_shadow(session, shadow.id)
        session.commit()

        match_date = BASE_DATE
        league, home, away = "E2E-TEST-Ligue1", "E2E-TEST-Home", "E2E-TEST-Away"

        # 1. Prédiction — même match, même date de référence, deux modèles.
        active_record = PredictionRecord(league=league, match_date=match_date, home_team=home, away_team=away,
                                          model_type="lightgbm", prob_home=0.5, prob_draw=0.3, prob_away=0.2,
                                          source="live", role="active")
        shadow_record = PredictionRecord(league=league, match_date=match_date, home_team=home, away_team=away,
                                          model_type="lightgbm", prob_home=0.8, prob_draw=0.15, prob_away=0.05,
                                          source="live", role="shadow")
        active_row = log_prediction(session, active_record, active.id)
        shadow_row = log_prediction(session, shadow_record, shadow.id)
        session.commit()

        # 2. Stockage DB — deux lignes indépendantes, statut pending.
        assert active_row.id != shadow_row.id
        assert active_row.status == "pending" and shadow_row.status == "pending"

        # 3. Résolution — résultat réel connu APRÈS coup, jamais avant.
        resolve_prediction(active_row, 2, 0)
        resolve_prediction(shadow_row, 2, 0)
        session.add(active_row)
        session.add(shadow_row)
        session.commit()
        assert active_row.status == "resolved" and shadow_row.status == "resolved"
        assert active_row.correct_1x2 is True and shadow_row.correct_1x2 is True
        active_id, shadow_id = active.id, shadow.id

    # 4. Métriques + comparaison matched — un seul match ne suffit jamais
    # (< LIVE_MIN_SAMPLE_SIZE) : le pipeline doit rester honnête, pas
    # fabriquer une comparaison statistiquement significative à partir d'1
    # seul point.
    with Session(engine) as session:
        result = shadow_comparison.compute_matched_comparison(session, "lightgbm", "1X2")
        assert result.status == "insufficient_matched_sample", (
            f"1 seul match matched ne doit jamais être présenté comme 'ok', obtenu {result.status}"
        )
        assert result.matched_sample_size == 1
        assert result.active_version_id == active_id
        assert result.shadow_version_id == shadow_id


UNIT_TESTS = [
    test_no_active_returns_no_active,
    test_no_shadow_returns_no_shadow,
    test_matched_excludes_predictions_made_by_only_one_side,
    test_insufficient_matched_sample,
    test_matched_comparison_metrics_and_deltas_correct,
    test_two_shadow_versions_never_mixed_in_matched_comparison,
    test_end_to_end_local_synthetic_match,
]


if __name__ == "__main__":
    failures = 0
    for t in UNIT_TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
            print("  [OK]")
        except AssertionError as e:
            failures += 1
            print(f"  [ECHEC] {e}")

    total = len(UNIT_TESTS)
    cleanup_db(DB_PATH)
    print(f"\n{'='*60}\n{total-failures}/{total} tests reussis\n{'='*60}")
    sys.exit(1 if failures else 0)
