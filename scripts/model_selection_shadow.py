"""
scripts/model_selection_shadow.py — Phase 6 : Model Selection Engine V1 +
Calibration Engine V1, MODE SHADOW (backtest).
=============================================================================

SHADOW UNIQUEMENT — ce script est le SEUL de la Phase 6 à écrire en base,
et UNIQUEMENT dans les deux tables dédiées créées par la migration
`a7c3e6f19b2d` : `model_selection_decisions` et `shadow_selection_predictions`
(voir api/app/models/model_selection_decision.py et
shadow_selection_prediction.py). AUCUNE écriture dans model_predictions,
model_versions, team_ratings, prediction_log, ModelVersion.status/is_active.
AUCUNE modification de api/model_artifacts/*.json. Ne touche jamais
scheduler.py/promotion.py/l'Ensemble/l'endpoint /predictions/* — voir
docstring de app/ai/arena/model_selection.py pour l'analyse complète des
crons Railway existants et pourquoi ce mécanisme reste totalement isolé
d'eux (aucune ligne ModelVersion.status="shadow" n'est jamais créée ici).

Mode "backtest" (Phase 6, inchangé) : les matchs "shadow" sont ceux de la
fenêtre de TEST du Model Selection Engine (déjà résolus, déjà connus) —
traités comme scripts/backtest_elo.py/research_ensemble.py pour
source="backtest" : la probabilité candidate est produite SANS connaître le
résultat (walk-forward), seule sa RÉSOLUTION (déjà connue) est immédiate.

Mode "live" (Phase 7, §26 du prompt) : `main_live()` ne fait AUCUN appel
réseau et n'ajoute AUCUN nouveau point d'intégration externe —
`fetch_upcoming_fixtures` (app/core/api_football_client.py) reste hors
périmètre. La source de fixtures est `model_predictions` où
`status="pending"` ET `match_date >= as_of` : exactement ce que la
production a DÉJÀ prédit (via ModelOrchestrator.predict_all, voir
scheduler.py) et pas encore résolu — jamais une fixture inventée, jamais un
nouveau mécanisme de découverte. La probabilité brute du candidat est lue
DIRECTEMENT dans sa propre ligne pending (aucune walk-forward pour un match
futur : elle a déjà été calculée par la production au moment de la
génération). Les lignes shadow live sont écrites `status="pending"` (§4 du
prompt : immuables tant que le résultat n'est pas connu) — `--resolve`
les résout plus tard, jamais avant qu'un vrai résultat existe.

`--production-model-type` (défaut "dixon_coles") : le modèle dont ce script
SNAPSHOT la prédiction déjà produite, pour comparaison ("COMPARE TO
PRODUCTION" du diagramme du prompt) — Dixon-Coles est le modèle réellement
servi par /predictions/* (voir api/main.py), donc le choix par défaut le
plus honnête de "ce que Production affichait". Snapshot en LECTURE SEULE
(prediction_log pour dixon_coles, model_predictions role="active" pour
elo/xgboost/lightgbm) — jamais recalculé, jamais modifié.

Usage (depuis la racine du dépôt) :
    # Mode backtest (Phase 6) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/model_selection_shadow.py \
        --market 1X2 [--production-model-type dixon_coles] [--n-windows 5] \
        [--min-sample-size 100] [--outdir reports/model_selection]

    # Mode live (Phase 7) — fixtures déjà pending en production, résultat inconnu :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/model_selection_shadow.py \
        --mode live --market 1X2 [--as-of 2026-08-29]

    # Résout les lignes shadow encore "pending" dont le résultat est maintenant connu
    # (model_predictions, prediction_log, OU match — quelle que soit la source) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/model_selection_shadow.py --resolve
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
_API_DIR = _SCRIPTS_DIR.parent / "api"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_API_DIR))

from sqlmodel import Session, select  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.models.prediction_log import PredictionLog  # noqa: E402
from app.models.model_prediction import ModelPrediction  # noqa: E402
from app.models.model_selection_decision import ModelSelectionDecision  # noqa: E402
from app.models.shadow_selection_prediction import ShadowSelectionPrediction  # noqa: E402
from app.ai.arena.ensemble import MIN_BENCHMARK_SAMPLE_SIZE  # noqa: E402
from app.ai.arena.promotion import get_active_version  # noqa: E402 (lecture seule : get_active_version, jamais apply_promotion/set_shadow)
from app.ai.arena import research, model_selection, calibration_engine  # noqa: E402

import walk_forward_ensemble as wfe  # noqa: E402
from research_ensemble import _fold_baseline_observations, _obs_log_loss, _payload_market_probs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("model_selection_shadow")


def _candidate_raw_probs(model_type, market, key, rows_by_model, dcwf):
    if model_type == "dixon_coles":
        pred = dcwf.predictions.get(key)
        return pred.probs.get(market) if pred is not None else None
    row = rows_by_model.get(model_type, {}).get(key)
    if row is None:
        return None
    from research_ensemble import _model_prediction_payload
    return _payload_market_probs(_model_prediction_payload(row), market)


def _production_snapshot(session, production_model_type, market, key):
    """Lecture seule — retourne (probs, model_version_id) ou (None, None) si
    aucune prédiction de production n'existe encore pour ce match."""
    league, match_date, home_team, away_team = key
    if production_model_type == "dixon_coles":
        row = session.exec(
            select(PredictionLog).where(
                PredictionLog.league == league, PredictionLog.match_date == match_date,
                PredictionLog.home_team == home_team, PredictionLog.away_team == away_team,
            )
        ).first()
        if row is None:
            return None, None
        try:
            payload = json.loads(row.payload)
        except (json.JSONDecodeError, TypeError):
            return None, None
        return _payload_market_probs(payload, market), None

    version = get_active_version(session, production_model_type)
    if version is None:
        return None, None
    row = session.exec(
        select(ModelPrediction).where(
            ModelPrediction.league == league, ModelPrediction.match_date == match_date,
            ModelPrediction.home_team == home_team, ModelPrediction.away_team == away_team,
            ModelPrediction.model_type == production_model_type, ModelPrediction.model_version_id == version.id,
            ModelPrediction.role == "active",
        )
    ).first()
    if row is None:
        return None, None
    from research_ensemble import _model_prediction_payload
    return _payload_market_probs(_model_prediction_payload(row), market), version.id


def _pick(probs: dict) -> str:
    return max(probs, key=probs.get)


# Phase 7 : centralisé dans research.py (réutilisé aussi par track_record.py) —
# jamais une deuxième implémentation de la même règle 1X2/BTTS/O-U.
_actual_outcome = research.actual_outcome


def _find_result_for_shadow_row(session: Session, row: ShadowSelectionPrediction) -> Optional[tuple[int, int]]:
    """§28 du prompt Phase 7 : cherche un résultat réel pour la ligne
    shadow, dans cet ordre — (1) model_predictions déjà résolu pour ce
    match (n'importe quel model_type : le score réel est le même pour
    tous, source la plus probable pour une fixture live résolue par
    fetch_daily_results.py) ; (2) prediction_log (Dixon-Coles, même
    script) ; (3) la table match (matchs backtest/historiques, seule
    source utilisée en Phase 6). Jamais fabriqué — None si aucune des
    trois n'a de résultat, la ligne reste `pending`."""
    from app.models.match import Match

    mp = session.exec(
        select(ModelPrediction).where(
            ModelPrediction.league == row.league, ModelPrediction.match_date == row.match_date,
            ModelPrediction.home_team == row.home_team, ModelPrediction.away_team == row.away_team,
            ModelPrediction.status == "resolved",
        )
    ).first()
    if mp is not None and mp.result_home_goals is not None:
        return mp.result_home_goals, mp.result_away_goals

    pl = session.exec(
        select(PredictionLog).where(
            PredictionLog.league == row.league, PredictionLog.match_date == row.match_date,
            PredictionLog.home_team == row.home_team, PredictionLog.away_team == row.away_team,
            PredictionLog.result_fetched_at.is_not(None),
        )
    ).first()
    if pl is not None and pl.result_home_goals is not None:
        return pl.result_home_goals, pl.result_away_goals

    match = session.exec(
        select(Match).where(
            Match.league == row.league, Match.date >= datetime.combine(row.match_date, datetime.min.time()),
            Match.date < datetime.combine(row.match_date, datetime.max.time()),
            Match.home_team == row.home_team, Match.away_team == row.away_team,
        )
    ).first()
    if match is not None:
        return match.home_goals, match.away_goals

    return None


def resolve_pending_shadow_predictions(session: Session) -> int:
    """Résout les lignes `status="pending"` dont le résultat est maintenant
    connu (model_predictions, prediction_log, OU match — voir
    _find_result_for_shadow_row) — ne mute JAMAIS une ligne déjà résolue
    (même invariant que prediction_logging.resolve_prediction, réimplémenté
    ici localement pour ne jamais dépendre d'un module ciblant
    model_predictions). Le modèle candidat, ses probabilités et la décision
    d'origine ne sont JAMAIS modifiés ici (§4 du prompt) — seuls les champs
    liés au résultat le sont."""
    pending = session.exec(select(ShadowSelectionPrediction).where(ShadowSelectionPrediction.status == "pending")).all()
    n = 0
    for row in pending:
        result = _find_result_for_shadow_row(session, row)
        if result is None:
            continue
        hg, ag = result
        candidate_probs = json.loads(row.candidate_probs)
        row.result_home_goals, row.result_away_goals = hg, ag
        actual = _actual_outcome(row.market, hg, ag)
        row.candidate_correct = _pick(candidate_probs) == actual
        if row.production_probs:
            row.production_correct = _pick(json.loads(row.production_probs)) == actual
        row.status = "resolved"
        row.resolved_at = datetime.now(timezone.utc)
        session.add(row)
        n += 1
    session.commit()
    return n


def _compute_decision_and_calibration(session: Session, market: str, n_windows: int, min_sample_size: int) -> dict:
    """Reconstruit la chaîne fenêtres -> décision -> calibration (§2 du
    prompt Phase 6, réutilisée sans changement en Phase 7) — extraite ici
    pour être appelée à l'IDENTIQUE par main() (mode backtest) ET
    main_live() (Phase 7, §26) : ne jamais dupliquer cette logique entre
    les deux modes (§1 du prompt Phase 7, "ne pas créer une seconde
    infrastructure parallèle"). Ne touche aucune table shadow (lecture
    seule sur model_predictions/match/prediction_log, réutilise
    walk_forward_ensemble.py/research.py tels quels)."""
    versions = {mt: wfe.latest_version(session, mt) for mt in wfe.BACKTEST_MODEL_TYPES}
    missing = [mt for mt, v in versions.items() if v is None]
    if missing:
        return {"status": "no_data", "missing": missing}

    rows_by_model = {mt: wfe._resolved_rows(session, mt, versions[mt].id) for mt in wfe.BACKTEST_MODEL_TYPES}
    common_keys = set(rows_by_model["elo"]) & set(rows_by_model["xgboost"]) & set(rows_by_model["lightgbm"])
    if not common_keys:
        return {"status": "no_overlap"}

    ordered_keys = sorted(common_keys, key=lambda k: (k[1], k[0], k[2], k[3]))
    windows = wfe._make_folds(ordered_keys, n_windows)
    if len(windows) < 2:
        return {"status": "insufficient_history"}

    dcwf = research.build_dixon_coles_walk_forward(windows, min_train_matches=research.MIN_DC_TRAIN_MATCHES)
    stability_windows = windows[:-1]
    test_window = windows[-1]
    test_window_index = len(windows) - 1

    window_results_by_model = {mt: [] for mt in model_selection.KNOWN_SELECTION_MODEL_TYPES}
    for w_keys in stability_windows:
        since, until = w_keys[0][1], w_keys[-1][1]
        for mt in model_selection.KNOWN_SELECTION_MODEL_TYPES:
            version = versions.get(mt)
            window_results_by_model[mt].append(model_selection.evaluate_model_window(
                session, mt, market, since, until, model_version_id=version.id if version else None, dcwf=dcwf,
            ))

    def _credibility_pairs(candidate, runner_up, market=market):
        cand_obs = _fold_baseline_observations(candidate, market, test_window, test_window_index, rows_by_model, dcwf)
        other_obs = _fold_baseline_observations(runner_up, market, test_window, test_window_index, rows_by_model, dcwf)
        common = sorted(set(cand_obs) & set(other_obs))
        return [(_obs_log_loss(other_obs[k]), _obs_log_loss(cand_obs[k])) for k in common]

    decision = model_selection.select_candidate_model(
        window_results_by_model, market, min_sample_size=min_sample_size, credibility_pairs_provider=_credibility_pairs,
    )
    logger.info(f"[{market}] {decision.status} — {decision.reason}")

    calibration_choice, calibration_verdict = "none", None
    calib_result = None
    train_obs_list = []
    if decision.status == "selected":
        train_obs = {}
        for w_idx, w_keys in enumerate(stability_windows):
            train_obs.update(_fold_baseline_observations(decision.selected_model_type, market, w_keys, w_idx, rows_by_model, dcwf))
        train_obs_list = [train_obs[k] for k in sorted(train_obs)]
        test_obs = _fold_baseline_observations(decision.selected_model_type, market, test_window, test_window_index, rows_by_model, dcwf)
        test_obs_list = [test_obs[k] for k in sorted(test_obs)]
        calib_result = calibration_engine.evaluate_calibration(train_obs_list, test_obs_list, min_sample_size)
        calibration_choice, calibration_verdict = calib_result.choice, calib_result.verdict

    return {
        "status": "ok", "decision": decision, "calib_result": calib_result, "train_obs_list": train_obs_list,
        "calibration_choice": calibration_choice, "calibration_verdict": calibration_verdict,
        "versions": versions, "rows_by_model": rows_by_model, "dcwf": dcwf, "ordered_keys": ordered_keys,
        "windows": windows, "stability_windows": stability_windows,
        "test_window": test_window, "test_window_index": test_window_index,
    }


def main(market: str = "1X2", production_model_type: str = "dixon_coles", n_windows: int = 5,
         min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE, outdir: str = "reports/model_selection"):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    init_db()
    with Session(engine) as session:
        ctx = _compute_decision_and_calibration(session, market, n_windows, min_sample_size)
        if ctx["status"] != "ok":
            return {"status": ctx["status"]}

        decision = ctx["decision"]
        calib_result = ctx["calib_result"]
        train_obs_list = ctx["train_obs_list"]
        calibration_choice = ctx["calibration_choice"]
        calibration_verdict = ctx["calibration_verdict"]
        rows_by_model = ctx["rows_by_model"]
        dcwf = ctx["dcwf"]
        ordered_keys = ctx["ordered_keys"]
        test_window = ctx["test_window"]

        # --- Persistance (§8 du prompt : "enregistrer ses décisions en mode shadow") ---
        decision_row = ModelSelectionDecision(
            run_id=run_id, mode="shadow", market=market, as_of=ordered_keys[-1][1], status=decision.status,
            selected_model_type=decision.selected_model_type, runner_up_model_type=decision.runner_up_model_type,
            windows_evaluated=decision.windows_evaluated,
            metrics=json.dumps({"top_rank_counts": decision.top_rank_counts, "log_loss_cv": decision.log_loss_cv,
                                 "credibility": decision.credibility}, default=str),
            reason=decision.reason, calibration_choice=calibration_choice, calibration_verdict=calibration_verdict,
        )
        session.add(decision_row)
        session.commit()
        session.refresh(decision_row)
        logger.info(f"ModelSelectionDecision #{decision_row.id} persistée (mode=shadow, status={decision.status}).")

        n_shadow_predictions = 0
        if decision.status == "selected":
            for key in test_window:
                raw_probs = _candidate_raw_probs(decision.selected_model_type, market, key, rows_by_model, dcwf)
                if raw_probs is None:
                    continue
                candidate_probs = calibration_engine.produce_candidate_probability(raw_probs, calib_result, train_obs_list)

                production_probs, production_version_id = _production_snapshot(session, production_model_type, market, key)

                league, match_date, home_team, away_team = key
                any_row = rows_by_model["elo"][key]
                hg, ag = any_row.result_home_goals, any_row.result_away_goals
                actual = _actual_outcome(market, hg, ag)

                existing = session.exec(
                    select(ShadowSelectionPrediction).where(
                        ShadowSelectionPrediction.league == league, ShadowSelectionPrediction.match_date == match_date,
                        ShadowSelectionPrediction.home_team == home_team, ShadowSelectionPrediction.away_team == away_team,
                        ShadowSelectionPrediction.market == market,
                    )
                ).first()
                if existing is not None:
                    continue  # idempotent — jamais une prédiction shadow déjà écrite n'est réécrite

                row = ShadowSelectionPrediction(
                    selection_decision_id=decision_row.id, league=league, match_date=match_date,
                    home_team=home_team, away_team=away_team, market=market,
                    candidate_model_type=decision.selected_model_type, calibration_applied=calibration_choice,
                    candidate_probs=json.dumps(candidate_probs),
                    candidate_probs_raw=json.dumps(raw_probs) if calibration_choice != "none" else None,
                    production_model_type=production_model_type if production_probs is not None else None,
                    production_model_version_id=production_version_id,
                    production_probs=json.dumps(production_probs) if production_probs is not None else None,
                    status="resolved", result_home_goals=hg, result_away_goals=ag,
                    candidate_correct=(_pick(candidate_probs) == actual),
                    production_correct=(_pick(production_probs) == actual) if production_probs is not None else None,
                    resolved_at=datetime.now(timezone.utc),
                )
                session.add(row)
                n_shadow_predictions += 1
            session.commit()
        logger.info(f"{n_shadow_predictions} ShadowSelectionPrediction(s) écrite(s).")

        candidate_correct_n = production_correct_n = compared_n = 0
        if n_shadow_predictions:
            rows = session.exec(
                select(ShadowSelectionPrediction).where(ShadowSelectionPrediction.selection_decision_id == decision_row.id)
            ).all()
            for r in rows:
                if r.production_correct is not None:
                    compared_n += 1
                    candidate_correct_n += int(bool(r.candidate_correct))
                    production_correct_n += int(bool(r.production_correct))

        result = {
            "status": "ok", "run_id": run_id, "market": market, "decision_status": decision.status,
            "selected_model_type": decision.selected_model_type, "shadow_predictions_written": n_shadow_predictions,
            "compared_to_production": compared_n, "candidate_correct": candidate_correct_n, "production_correct": production_correct_n,
        }

    logger.info(f"Comparaison à la production (échantillon avec snapshot production disponible) : "
                f"{result['compared_to_production']} matchs, candidat correct={result['candidate_correct']}, "
                f"production correct={result['production_correct']}.")
    print("\nPHASE 6 — XFOOT MODEL SELECTION ENGINE V1 + CALIBRATION ENGINE V1 TERMINÉE (SHADOW). "
          "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. AUCUN MODÈLE DE PRODUCTION REMPLACÉ.")
    return result


# ---------------------------------------------------------------------------
# Mode LIVE — Phase 7, §3/§4/§26 du prompt. Prédictions shadow sur de vraies
# fixtures pas encore résolues, écrites `status="pending"` (jamais résolues
# immédiatement, contrairement au mode backtest ci-dessus).
# ---------------------------------------------------------------------------

def _find_upcoming_fixture_keys(session: Session, as_of: date) -> list[tuple]:
    """§26 : source de fixtures = ce que la production a DÉJÀ prédit et pas
    encore résolu (model_predictions, status="pending"), jamais un nouvel
    appel réseau. Filtré à match_date >= as_of pour exclure les lignes
    pending ORPHELINES (jamais résolues par fetch_daily_results.py,
    antérieures à as_of) — jamais traitées comme des fixtures à venir (voir
    docstring module, audit empirique de la base locale)."""
    rows = session.exec(
        select(
            ModelPrediction.league, ModelPrediction.match_date, ModelPrediction.home_team, ModelPrediction.away_team,
        ).where(ModelPrediction.status == "pending", ModelPrediction.match_date >= as_of).distinct()
    ).all()
    keys = {tuple(r) for r in rows}
    return sorted(keys, key=lambda k: (k[1], k[0], k[2], k[3]))


def _live_candidate_raw_probs(session: Session, model_type: str, market: str, key: tuple, versions: dict) -> Optional[dict]:
    """Contrairement au mode backtest (_candidate_raw_probs, qui lit
    rows_by_model construit sur l'historique déjà résolu), le mode live lit
    DIRECTEMENT la prédiction pending déjà produite par la production pour
    CE match précis (ModelOrchestrator.predict_all, voir scheduler.py) —
    aucun réentraînement, aucune walk-forward pour un match futur : la
    prédiction candidate ne peut exister que si la production l'a déjà
    calculée (jamais fabriquée, voir §3 : "si aucun modèle ne peut être
    inventé, shadow_status = INSUFFICIENT_DATA")."""
    league, match_date, home_team, away_team = key
    if model_type == "dixon_coles":
        row = session.exec(
            select(PredictionLog).where(
                PredictionLog.league == league, PredictionLog.match_date == match_date,
                PredictionLog.home_team == home_team, PredictionLog.away_team == away_team,
            )
        ).first()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload)
        except (json.JSONDecodeError, TypeError):
            return None
        return _payload_market_probs(payload, market)

    version = versions.get(model_type)
    if version is None:
        return None
    row = session.exec(
        select(ModelPrediction).where(
            ModelPrediction.league == league, ModelPrediction.match_date == match_date,
            ModelPrediction.home_team == home_team, ModelPrediction.away_team == away_team,
            ModelPrediction.model_type == model_type, ModelPrediction.model_version_id == version.id,
        )
    ).first()
    if row is None:
        return None
    from research_ensemble import _model_prediction_payload
    return _payload_market_probs(_model_prediction_payload(row), market)


def main_live(market: str = "1X2", production_model_type: str = "dixon_coles", n_windows: int = 5,
              min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE, as_of: Optional[date] = None):
    """§26 du prompt Phase 7 : job shadow LIVE — lit les fixtures (déjà
    connues de la production, jamais un nouvel appel réseau), lit les
    modèles, exécute la sélection (même moteur que le mode backtest,
    _compute_decision_and_calibration), calcule la calibration, écrit
    UNIQUEMENT dans les tables shadow, ne modifie JAMAIS la production.
    Persiste `status="pending"` (§4 : immuable jusqu'à résolution réelle)."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    as_of = as_of or datetime.now(timezone.utc).date()

    init_db()
    with Session(engine) as session:
        upcoming = _find_upcoming_fixture_keys(session, as_of)
        if not upcoming:
            logger.info(f"[{market}] (live) Aucune fixture à venir éligible (model_predictions pending, "
                        f"match_date >= {as_of.isoformat()}) — NO SHADOW DATA, rien n'est fabriqué.")
            print("\nPHASE 7 — XFOOT SHADOW EVALUATION & TRACK RECORD V1 (SHADOW LIVE) : NO SHADOW DATA "
                  f"(aucune fixture éligible pour {market}). AUCUNE PROMOTION PRODUCTION EFFECTUÉE.")
            return {"status": "no_upcoming_fixtures", "market": market, "as_of": as_of.isoformat()}

        ctx = _compute_decision_and_calibration(session, market, n_windows, min_sample_size)
        if ctx["status"] != "ok":
            logger.error(f"[{market}] (live) {ctx['status']}")
            return {"status": ctx["status"], "market": market}

        decision = ctx["decision"]
        calib_result = ctx["calib_result"]
        train_obs_list = ctx["train_obs_list"]
        calibration_choice = ctx["calibration_choice"]
        calibration_verdict = ctx["calibration_verdict"]
        versions = ctx["versions"]
        logger.info(f"[{market}] (live) {decision.status} — {decision.reason} — {len(upcoming)} fixture(s) éligible(s).")

        decision_row = ModelSelectionDecision(
            run_id=run_id, mode="shadow", market=market, as_of=as_of, status=decision.status,
            selected_model_type=decision.selected_model_type, runner_up_model_type=decision.runner_up_model_type,
            windows_evaluated=decision.windows_evaluated,
            metrics=json.dumps({"top_rank_counts": decision.top_rank_counts, "log_loss_cv": decision.log_loss_cv,
                                 "credibility": decision.credibility}, default=str),
            reason=decision.reason, calibration_choice=calibration_choice, calibration_verdict=calibration_verdict,
        )
        session.add(decision_row)
        session.commit()
        session.refresh(decision_row)
        logger.info(f"ModelSelectionDecision #{decision_row.id} persistée (mode=shadow, live, status={decision.status}).")

        n_written = 0
        if decision.status == "selected":
            for key in upcoming:
                league, match_date, home_team, away_team = key
                existing = session.exec(
                    select(ShadowSelectionPrediction).where(
                        ShadowSelectionPrediction.league == league, ShadowSelectionPrediction.match_date == match_date,
                        ShadowSelectionPrediction.home_team == home_team, ShadowSelectionPrediction.away_team == away_team,
                        ShadowSelectionPrediction.market == market,
                    )
                ).first()
                if existing is not None:
                    continue  # idempotent (§27) — jamais réécrit, même si la décision a changé depuis un run précédent

                raw_probs = _live_candidate_raw_probs(session, decision.selected_model_type, market, key, versions)
                if raw_probs is None:
                    continue  # le candidat sélectionné n'a lui-même pas encore de prédiction pending pour ce match -- jamais fabriqué

                candidate_probs = calibration_engine.produce_candidate_probability(raw_probs, calib_result, train_obs_list)
                production_probs, production_version_id = _production_snapshot(session, production_model_type, market, key)

                row = ShadowSelectionPrediction(
                    selection_decision_id=decision_row.id, league=league, match_date=match_date,
                    home_team=home_team, away_team=away_team, market=market,
                    candidate_model_type=decision.selected_model_type, calibration_applied=calibration_choice,
                    candidate_probs=json.dumps(candidate_probs),
                    candidate_probs_raw=json.dumps(raw_probs) if calibration_choice != "none" else None,
                    production_model_type=production_model_type if production_probs is not None else None,
                    production_model_version_id=production_version_id,
                    production_probs=json.dumps(production_probs) if production_probs is not None else None,
                    status="pending",  # §4/§28 : résultat inconnu -- immuable jusqu'à résolution réelle (--resolve)
                )
                session.add(row)
                n_written += 1
            session.commit()
        logger.info(f"{n_written} ShadowSelectionPrediction(s) LIVE écrite(s) (status=pending, en attente de résultat).")

        result = {
            "status": "ok", "run_id": run_id, "market": market, "as_of": as_of.isoformat(),
            "decision_status": decision.status, "selected_model_type": decision.selected_model_type,
            "upcoming_fixtures_found": len(upcoming), "shadow_predictions_written": n_written,
        }

    print("\nPHASE 7 — XFOOT SHADOW EVALUATION & TRACK RECORD V1 (SHADOW LIVE) TERMINÉE. "
          "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. AUCUN MODÈLE DE PRODUCTION REMPLACÉ.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", type=str, default="backtest", choices=["backtest", "live"])
    parser.add_argument("--market", type=str, default="1X2", choices=["1X2", "BTTS", "OVER_UNDER_2_5"])
    parser.add_argument("--production-model-type", type=str, default="dixon_coles")
    parser.add_argument("--n-windows", type=int, default=5)
    parser.add_argument("--min-sample-size", type=int, default=MIN_BENCHMARK_SAMPLE_SIZE)
    parser.add_argument("--outdir", type=str, default=str(_SCRIPTS_DIR.parent / "reports" / "model_selection"))
    parser.add_argument("--as-of", type=str, default=None, help="Mode live uniquement : date de référence (YYYY-MM-DD), défaut aujourd'hui UTC.")
    parser.add_argument("--resolve", action="store_true", help="Résout les lignes shadow encore pending, ne recalcule rien.")
    args = parser.parse_args()

    if args.resolve:
        init_db()
        with Session(engine) as session:
            n = resolve_pending_shadow_predictions(session)
        logger.info(f"{n} ligne(s) shadow résolue(s).")
        sys.exit(0)

    if args.mode == "live":
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        main_live(market=args.market, production_model_type=args.production_model_type, n_windows=args.n_windows,
                  min_sample_size=args.min_sample_size, as_of=as_of)
    else:
        main(market=args.market, production_model_type=args.production_model_type, n_windows=args.n_windows,
             min_sample_size=args.min_sample_size, outdir=args.outdir)
    sys.exit(0)
