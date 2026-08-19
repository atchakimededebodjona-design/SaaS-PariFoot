"""
test_scheduler.py — Phase 9, Partie A/F : scheduler de prédictions LIVE
(app/ai/arena/scheduler.py) — génération, idempotence (run 1 -> N
prédictions, run 2 -> 0 doublon), fenêtre temporelle (match déjà passé
ignoré), et passe SHADOW (role="shadow" séparé, jamais mélangé aux actives).

Base isolée dédiée (jamais api/app.db) — même précaution que les suites de
tests précédentes (voir _test_support.py).

Usage : python api/test_scheduler.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_scheduler.db")

import xgboost as xgb
from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.model_prediction import ModelPrediction
from app.models.team_rating import ModelVersion
from app.ai.engine.features import FEATURE_COLUMNS
from app.ai.arena.prediction_logging import PredictionRecord
from app.ai.arena.models_common import MatchContext, PredictionModel, PredictionOutcome, XGBoostPredictionModel
from app.ai.arena.orchestrator import ModelOrchestrator
from app.ai.arena.scheduler import UpcomingFixture, generate_live_predictions
from app.ai.arena import promotion
from app.core.api_football_client import fetch_upcoming_fixtures
import app.core.api_football_config as api_football_config

init_db()

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _clean_all():
    with Session(engine) as session:
        for row in session.exec(select(ModelPrediction)).all():
            session.delete(row)
        for v in session.exec(select(ModelVersion)).all():
            session.delete(v)
        session.commit()


class _RecordingFakeOrchestrator:
    """Ne délègue jamais à ModelOrchestrator réel — sert uniquement à vérifier
    QUELLES fixtures generate_live_predictions() lui a effectivement soumises
    (§8 : jamais un match déjà commencé/terminé), sans dépendre d'un vrai
    modèle entraîné."""
    def __init__(self):
        self.calls: list[MatchContext] = []
        self._models = []

    def predict_all(self, session, ctx, persist=True):
        self.calls.append(ctx)
        return {}


def test_future_fixtures_are_submitted_to_orchestrator():
    orchestrator = _RecordingFakeOrchestrator()
    fixtures = [
        UpcomingFixture(league="Ligue1", home_team="A", away_team="B", match_date=NOW + timedelta(hours=5)),
        UpcomingFixture(league="Ligue1", home_team="C", away_team="D", match_date=NOW + timedelta(hours=30)),
    ]
    with Session(engine) as session:
        result = generate_live_predictions(session, orchestrator, fixtures, include_shadow=False, now=NOW)

    assert result.fixtures_considered == 2
    assert result.fixtures_skipped_past == 0
    assert len(orchestrator.calls) == 2
    assert {c.home_team for c in orchestrator.calls} == {"A", "C"}


def test_past_and_in_progress_fixtures_are_skipped_never_submitted():
    orchestrator = _RecordingFakeOrchestrator()
    fixtures = [
        UpcomingFixture(league="Ligue1", home_team="Started", away_team="X", match_date=NOW - timedelta(minutes=1)),
        UpcomingFixture(league="Ligue1", home_team="Finished", away_team="Y", match_date=NOW - timedelta(days=2)),
        UpcomingFixture(league="Ligue1", home_team="Future", away_team="Z", match_date=NOW + timedelta(hours=1)),
    ]
    with Session(engine) as session:
        result = generate_live_predictions(session, orchestrator, fixtures, include_shadow=False, now=NOW)

    assert result.fixtures_considered == 3
    assert result.fixtures_skipped_past == 2
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0].home_team == "Future"


# ---------------------------------------------------------------------------
# Idempotence bout-en-bout (contrainte UNIQUE existante, aucune logique de
# déduplication propre à ce module)
# ---------------------------------------------------------------------------

class _DynamicFakeModel(PredictionModel):
    """Modèle factice dont predict() construit un PredictionRecord à partir
    du VRAI ctx reçu (contrairement à un outcome figé) — nécessaire pour
    qu'un run réel sur plusieurs fixtures distinctes produise des clés
    naturelles distinctes, et que relancer generate_live_predictions() sur
    les MÊMES fixtures retombe sur les MÊMES clés (test d'idempotence réel)."""
    model_type = "dixon_coles"

    def __init__(self, version_id: int):
        self._version_id = version_id

    def predict(self, session, ctx):
        record = PredictionRecord(
            league=ctx.league, match_date=ctx.match_date, home_team=ctx.home_team, away_team=ctx.away_team,
            model_type=self.model_type, prob_home=0.5, prob_draw=0.3, prob_away=0.2, source="live",
        )
        return PredictionOutcome(self.model_type, "ok", model_version_id=self._version_id, record=record)

    def check_availability(self, session):
        from app.ai.arena.models_common import AvailabilityCheck
        return AvailabilityCheck(live_available=True)


def test_idempotent_across_two_runs_no_duplicate():
    _clean_all()
    with Session(engine) as session:
        version = ModelVersion(name="test-dc-shadow-idem", model_type="dixon_coles",
                                trained_at=datetime.now(timezone.utc), is_active=True)
        session.add(version)
        session.commit()
        session.refresh(version)

        orchestrator = ModelOrchestrator([_DynamicFakeModel(version.id)])
        fixtures = [
            UpcomingFixture(league="Ligue1", home_team="A", away_team="B", match_date=NOW + timedelta(hours=5)),
            UpcomingFixture(league="Ligue1", home_team="C", away_team="D", match_date=NOW + timedelta(hours=10)),
        ]

        result1 = generate_live_predictions(session, orchestrator, fixtures, include_shadow=False, now=NOW)
        rows_after_run1 = session.exec(select(ModelPrediction)).all()
        assert result1.fixtures_considered == 2
        assert len(rows_after_run1) == 2, "run 1 aurait dû produire N prédictions"

        result2 = generate_live_predictions(session, orchestrator, fixtures, include_shadow=False, now=NOW)
        rows_after_run2 = session.exec(select(ModelPrediction)).all()
        assert len(rows_after_run2) == 2, "run 2 a créé des doublons (0 attendu)"
        assert {r.id for r in rows_after_run1} == {r.id for r in rows_after_run2}


# ---------------------------------------------------------------------------
# Passe SHADOW
# ---------------------------------------------------------------------------

def _tiny_xgb_version(session, *, status: str, is_active: bool, leagues=("Ligue1",)) -> ModelVersion:
    rng = np.random.default_rng(0)
    n = 200
    X = pd.DataFrame({c: rng.normal(size=n) for c in FEATURE_COLUMNS})
    X["league"] = pd.Categorical(rng.choice(leagues, size=n), categories=list(leagues))
    y = pd.Series(rng.choice([0, 1, 2], size=n))
    model = xgb.XGBClassifier(max_depth=3, n_estimators=15, objective="multi:softprob", num_class=3,
                               enable_categorical=True, tree_method="hist", random_state=0)
    model.fit(X, y)
    artifact = model.get_booster().save_raw(raw_format="json").decode("utf-8")
    config = {
        "feature_columns": FEATURE_COLUMNS, "league_categories": list(leagues),
        "class_order": [int(c) for c in model.classes_], "feature_version": "test-v1",
    }
    v = ModelVersion(
        name=f"test-xgb-{status}-{datetime.now(timezone.utc).timestamp()}",
        model_type="xgboost", trained_at=datetime.now(timezone.utc),
        is_active=is_active, status=status, artifact=artifact, config=json.dumps(config),
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def test_shadow_version_produces_role_shadow_prediction_alongside_active():
    _clean_all()
    with Session(engine) as session:
        active_version = _tiny_xgb_version(session, status="active", is_active=True)
        shadow_candidate = _tiny_xgb_version(session, status="candidate", is_active=False)
        promotion.set_shadow(session, shadow_candidate.id)
        session.commit()

        orchestrator = ModelOrchestrator([XGBoostPredictionModel({})])
        fixtures = [UpcomingFixture(league="Ligue1", home_team="A", away_team="B", match_date=NOW + timedelta(hours=5))]

        result = generate_live_predictions(session, orchestrator, fixtures, include_shadow=True, now=NOW)

        active_rows = session.exec(
            select(ModelPrediction).where(ModelPrediction.role == "active", ModelPrediction.model_type == "xgboost")
        ).all()
        shadow_rows = session.exec(
            select(ModelPrediction).where(ModelPrediction.role == "shadow", ModelPrediction.model_type == "xgboost")
        ).all()

        assert len(active_rows) == 1, "la prédiction active (version servie) est absente"
        assert active_rows[0].model_version_id == active_version.id
        assert len(shadow_rows) == 1, "la prédiction shadow n'a pas été générée"
        assert shadow_rows[0].model_version_id == shadow_candidate.id
        assert result.shadow_predictions_logged == 1
        assert not result.shadow_errors


def test_no_shadow_versions_means_zero_shadow_predictions():
    _clean_all()
    with Session(engine) as session:
        _tiny_xgb_version(session, status="active", is_active=True)

        orchestrator = ModelOrchestrator([XGBoostPredictionModel({})])
        fixtures = [UpcomingFixture(league="Ligue1", home_team="A", away_team="B", match_date=NOW + timedelta(hours=5))]

        result = generate_live_predictions(session, orchestrator, fixtures, include_shadow=True, now=NOW)
        assert result.shadow_predictions_logged == 0
        shadow_rows = session.exec(select(ModelPrediction).where(ModelPrediction.role == "shadow")).all()
        assert shadow_rows == []


def test_include_shadow_false_never_queries_or_logs_shadow():
    _clean_all()
    with Session(engine) as session:
        active_version = _tiny_xgb_version(session, status="active", is_active=True)
        shadow_candidate = _tiny_xgb_version(session, status="candidate", is_active=False)
        promotion.set_shadow(session, shadow_candidate.id)
        session.commit()

        orchestrator = ModelOrchestrator([XGBoostPredictionModel({})])
        fixtures = [UpcomingFixture(league="Ligue1", home_team="A", away_team="B", match_date=NOW + timedelta(hours=5))]

        result = generate_live_predictions(session, orchestrator, fixtures, include_shadow=False, now=NOW)
        assert result.shadow_predictions_logged == 0
        shadow_rows = session.exec(select(ModelPrediction).where(ModelPrediction.role == "shadow")).all()
        assert shadow_rows == []
        active_rows = session.exec(select(ModelPrediction).where(ModelPrediction.role == "active")).all()
        assert len(active_rows) == 1 and active_rows[0].model_version_id == active_version.id


# ---------------------------------------------------------------------------
# fetch_upcoming_fixtures — API-Football mockée (jamais le vrai réseau)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fixture(home, away, league_id, kickoff_iso):
    return {
        "fixture": {"date": kickoff_iso, "status": {"short": "NS"}},
        "league": {"id": league_id},
        "teams": {"home": {"name": home}, "away": {"name": away}},
    }


def _date_filtered_fake_get(fixtures_payload: list[dict]):
    """Reproduit le comportement RÉEL d'API-Football (une réponse filtrée par
    le paramètre `date` de la requête) — indispensable dès qu'une fenêtre
    couvre plusieurs dates calendaires (plusieurs appels httpx.get), sinon le
    mock renverrait les mêmes fixtures pour chaque date interrogée."""
    def fake_get(url, params=None, headers=None, timeout=None):
        requested_date = params["date"]
        matching = [f for f in fixtures_payload if f["fixture"]["date"].startswith(requested_date)]
        return _FakeResponse({"response": matching, "paging": {"current": 1, "total": 1}})
    return fake_get


def test_fetch_upcoming_fixtures_filters_by_window_and_known_league():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    ligue1_id = api_football_config.API_FOOTBALL_LEAGUE_IDS["Ligue1"]
    fixtures_payload = [
        _fixture("A", "B", ligue1_id, "2026-06-01T12:00:00+00:00"),   # dans la fenêtre (24h)
        _fixture("C", "D", ligue1_id, "2026-06-03T12:00:00+00:00"),   # hors fenêtre (trop tard)
        _fixture("E", "F", 999999, "2026-06-01T14:00:00+00:00"),      # ligue non suivie
    ]

    with patch("httpx.get", side_effect=_date_filtered_fake_get(fixtures_payload)):
        result = fetch_upcoming_fixtures(24, now=now)

    assert len(result) == 1, result
    assert result[0]["teams"]["home"]["name"] == "A"


def test_fetch_upcoming_fixtures_excludes_kickoff_before_now():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    ligue1_id = api_football_config.API_FOOTBALL_LEAGUE_IDS["Ligue1"]
    fixtures_payload = [
        _fixture("Started", "X", ligue1_id, "2026-06-01T09:00:00+00:00"),  # déjà commencé
        _fixture("Future", "Y", ligue1_id, "2026-06-01T11:00:00+00:00"),
    ]

    with patch("httpx.get", side_effect=_date_filtered_fake_get(fixtures_payload)):
        result = fetch_upcoming_fixtures(24, now=now)

    assert len(result) == 1, result
    assert result[0]["teams"]["home"]["name"] == "Future"


def test_fetch_upcoming_fixtures_one_request_per_calendar_date():
    now = datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["date"])
        return _FakeResponse({"response": [], "paging": {"current": 1, "total": 1}})

    with patch("httpx.get", side_effect=fake_get):
        fetch_upcoming_fixtures(48, now=now)  # couvre potentiellement 3 dates calendaires (1,2,3 juin)

    assert len(calls) == len(set(calls)), "une même date a été interrogée plusieurs fois"
    assert "2026-06-01" in calls


UNIT_TESTS = [
    test_fetch_upcoming_fixtures_filters_by_window_and_known_league,
    test_fetch_upcoming_fixtures_excludes_kickoff_before_now,
    test_fetch_upcoming_fixtures_one_request_per_calendar_date,
    test_future_fixtures_are_submitted_to_orchestrator,
    test_past_and_in_progress_fixtures_are_skipped_never_submitted,
    test_idempotent_across_two_runs_no_duplicate,
    test_shadow_version_produces_role_shadow_prediction_alongside_active,
    test_no_shadow_versions_means_zero_shadow_predictions,
    test_include_shadow_false_never_queries_or_logs_shadow,
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
