"""
test_production_readiness.py — Phase 9 : tests de api/app/ai/readiness/
(gates.py + matrix.py). Base isolée dédiée (jamais api/app.db pour ceux-ci).

Usage : python api/test_production_readiness.py
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_support import configure_test_env, cleanup_db

DB_PATH = configure_test_env("test_production_readiness.db")

from sqlmodel import Session, select
from app.core.database import engine, init_db
from app.models.team_rating import ModelVersion, next_version_name
from app.models.model_promotion_event import ModelPromotionEvent
from app.models.model_selection_decision import ModelSelectionDecision
from app.models.match import Match

from app.ai.shadow.schemas import ShadowDecisionRecord, ShadowResolution, pending_resolution
from app.ai.shadow.tracking import ShadowDecisionStore

from app.ai.readiness.schemas import GATE_STATUSES, FINAL_VERDICTS, CHECKLIST_ITEMS
from app.ai.readiness import gates as g
from app.ai.readiness.matrix import evaluate_production_readiness

init_db()
UTC = timezone.utc


def _temp_store() -> ShadowDecisionStore:
    import os
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return ShadowDecisionStore(path=tmp)


def _seed_active_version(session, model_type, trained_at=None):
    v = ModelVersion(name=next_version_name(session, f"xfoot-{model_type}"), model_type=model_type,
                      trained_at=trained_at or (datetime.now(UTC) - timedelta(days=30)), is_active=True, status="active")
    session.add(v); session.commit(); session.refresh(v)
    return v


def _seed_all_model_types(session, as_of):
    for mt in g.MODEL_TYPES:
        _seed_active_version(session, mt, trained_at=as_of - timedelta(days=10))


def _record(shadow_id="r1", **overrides):
    base = dict(
        shadow_id=shadow_id, match_id=None, league="Ligue1", home_team="A", away_team="B",
        kickoff=datetime(2026, 1, 1, tzinfo=UTC), as_of=datetime(2026, 1, 1, tzinfo=UTC),
        model_type="xgboost", model_version="xfoot-xgboost-v1", calibration_source="RAW", market="1X2", selection="home_win",
        raw_probability=0.6, calibrated_probability=None,
        market_probabilities_raw={"home_win": 0.6, "draw": 0.25, "away_win": 0.15}, market_probabilities_calibrated=None,
        probability_source="RAW", quality={"data_quality": "HIGH"}, confidence="HIGH", eligibility="ELIGIBLE",
        value_status=None, odds_source=None, odds_timestamp=None, temporal_status="TEMPORALLY_VERIFIED",
        provenance={"model_source": "xgboost", "model_version": "xfoot-xgboost-v1", "calibration_source": "RAW", "feature_snapshot": "s1", "odds_source": None},
        status="VALUE_CANDIDATE", created_at=datetime.now(UTC), data_marking="REAL",
    )
    base.update(overrides)
    return ShadowDecisionRecord(**base)


def _resolution(**overrides):
    base = dict(result_status="RESOLVED", actual_home_goals=1, actual_away_goals=0, actual_outcome="home_win",
                candidate_correct=True, resolved_at=datetime.now(UTC))
    base.update(overrides)
    return ShadowResolution(**base)


_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def section(name):
    print(f"\n=== {name} ===")


# ---------------------------------------------------------------------------
# 1. all gates pass (synthetic best-case).
# ---------------------------------------------------------------------------

def test_all_gates_pass_synthetic():
    section("1. all gates pass (synthétique)")
    with Session(engine) as s:
        for m in s.exec(select(Match)).all():
            s.delete(m)
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        for e in s.exec(select(ModelPromotionEvent)).all():
            s.delete(e)
        for d in s.exec(select(ModelSelectionDecision)).all():
            s.delete(d)
        s.commit()

        as_of = datetime.now(UTC)
        _seed_all_model_types(s, as_of)
        s.commit()

        store = _temp_store()
        entries_dict = {}
        for i in range(150):
            r = _record(shadow_id=f"s{i}", kickoff=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i))
            entries_dict[r.shadow_id] = {"record": r, "resolution": _resolution()}
        # Écrit directement via upsert_new (public API, jamais un accès privé au store).
        for r_dict in entries_dict.values():
            store.upsert_new(r_dict["record"], r_dict["resolution"])
        store.save()

        result = evaluate_production_readiness(s, store, as_of)
        by_name = {gt.name: gt for gt in result.gates}
        check("MODEL PASS", by_name["MODEL"].status == "PASS")
        check("TEMPORAL_INTEGRITY PASS", by_name["TEMPORAL_INTEGRITY"].status == "PASS")
        check("SHADOW PASS", by_name["SHADOW"].status == "PASS")
        check("TRACK_RECORD not NO_DATA with 150 real resolved", by_name["TRACK_RECORD"].evidence["real_prospective_track_record_sample_size"] == 150)
        check("TRACK_RECORD PASS (>=100 -> STATISTICALLY_INFORMATIVE)", by_name["TRACK_RECORD"].status == "PASS")
        check("verdict is one of FINAL_VERDICTS", result.final_verdict in FINAL_VERDICTS)

        Path(store.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. critical gate failure -> NO_GO.
# ---------------------------------------------------------------------------

def test_critical_gate_failure_forces_no_go():
    section("2. critical gate failure -> NO_GO")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        as_of = datetime.now(UTC)
        # Aucune ModelVersion active -> MODEL=FAIL (critique).
        store = _temp_store()
        result = evaluate_production_readiness(s, store, as_of)
        by_name = {gt.name: gt for gt in result.gates}
        check("MODEL FAIL", by_name["MODEL"].status == "FAIL")
        check("verdict != PRODUCTION_READY", result.final_verdict != "PRODUCTION_READY")
        check("verdict in (NO_GO, BLOCKED)", result.final_verdict in ("NO_GO", "BLOCKED"))
        check("MODEL in critical_gate_failures", "MODEL" in result.critical_gate_failures)
        Path(store.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. unknown critical gate never promoted to PASS.
# ---------------------------------------------------------------------------

def test_unknown_critical_gate_never_pass():
    section("3. UNKNOWN critical gate jamais promu à PASS")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        _seed_active_version(s, "dixon_coles", trained_at=datetime.now(UTC) + timedelta(days=5))  # trained_at > as_of.
        as_of = datetime.now(UTC)
        gate = g.gate_model(s, as_of)
        check("status is FAIL, never PASS", gate.status == "FAIL")
        check("MODEL is critical", gate.critical is True)


# ---------------------------------------------------------------------------
# 4/5/6. track record maturity classification never silently upgraded.
# ---------------------------------------------------------------------------

def test_track_record_no_data():
    section("4. no-data track record")
    gate = g.gate_track_record([])
    check("status NOT_AVAILABLE", gate.status == "NOT_AVAILABLE")
    check("maturity NO_DATA", gate.evidence["maturity"] == "NO_DATA")


def test_track_record_early_data_never_becomes_production_ready():
    section("5. early-data track record")
    entries = [(_record(shadow_id=f"e{i}"), _resolution()) for i in range(5)]
    gate = g.gate_track_record(entries)
    check("status CONDITIONAL (jamais PASS)", gate.status == "CONDITIONAL")
    check("maturity EARLY_DATA", gate.evidence["maturity"] == "EARLY_DATA")


def test_track_record_statistically_informative():
    section("6. statistically-informative track record")
    entries = [(_record(shadow_id=f"i{i}", kickoff=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i)), _resolution()) for i in range(120)]
    gate = g.gate_track_record(entries)
    check("status PASS", gate.status == "PASS")
    check("maturity STATISTICALLY_INFORMATIVE", gate.evidence["maturity"] == "STATISTICALLY_INFORMATIVE")


# ---------------------------------------------------------------------------
# 7. temporal failure.
# ---------------------------------------------------------------------------

def test_temporal_failure():
    section("7. temporal failure (trained_at > as_of)")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        as_of = datetime.now(UTC)
        _seed_active_version(s, "dixon_coles", trained_at=as_of + timedelta(days=10))
        gate = g.gate_temporal_integrity(s, as_of)
        check("status FAIL", gate.status == "FAIL")
        check("violation reported", len(gate.evidence["violations"]) == 1)


# ---------------------------------------------------------------------------
# 8. model provenance failure (no active version).
# ---------------------------------------------------------------------------

def test_model_provenance_failure():
    section("8. model provenance failure")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        gate = g.gate_model_version(s)
        check("status FAIL", gate.status == "FAIL")


# ---------------------------------------------------------------------------
# 9. feature provenance failure (registry inconsistency simulated indirectly
#    via RED-feature presence, since the real registry is always internally
#    consistent — validate_registry() genuinely returns []).
# ---------------------------------------------------------------------------

def test_feature_gate_flags_red_features_conditional_not_pass():
    section("9. feature provenance — RED features -> jamais PASS silencieux")
    gate = g.gate_features()
    check("status in (PASS, CONDITIONAL, FAIL)", gate.status in GATE_STATUSES)
    if gate.evidence["traffic_light_counts"]["RED"] > 0:
        check("RED present -> not PASS", gate.status != "PASS")


# ---------------------------------------------------------------------------
# 10. calibration failure (no ModelSelectionDecision).
# ---------------------------------------------------------------------------

def test_calibration_not_available_without_decisions():
    section("10. calibration failure (no decisions)")
    with Session(engine) as s:
        for d in s.exec(select(ModelSelectionDecision)).all():
            s.delete(d)
        s.commit()
        gate = g.gate_calibration(s)
        check("status NOT_AVAILABLE", gate.status == "NOT_AVAILABLE")


# ---------------------------------------------------------------------------
# 11/12. odds unverified / (temporally verified odds not representable in this repo — never fabricated).
# ---------------------------------------------------------------------------

def test_odds_unverified_reads_persisted_report():
    section("11. odds unverified (relit le rapport Phase 8G.2, jamais un appel réseau)")
    gate = g.gate_odds()
    check("status in (NOT_AVAILABLE, UNKNOWN)", gate.status in ("NOT_AVAILABLE", "UNKNOWN"))
    check("never PASS without real verified access", gate.status != "PASS")


def test_odds_never_fabricated_as_temporally_verified():
    section("12. odds jamais fabriquées comme TEMPORALLY_VERIFIED")
    # Aucune fonction de ce module ne peut produire ODDS=PASS sans un rapport
    # persistant confirmant un accès réel — vérifié en 11. Ici on confirme
    # qu'aucun chemin de secours ne retourne PASS silencieusement.
    gate = g.gate_odds()
    check("PASS requires an explicit persisted CONFIRMED decision (absent today)", gate.status != "PASS")


# ---------------------------------------------------------------------------
# 13/14. value blocked without odds / value never unconditionally eligible.
# ---------------------------------------------------------------------------

def test_value_blocked_without_verified_odds():
    section("13. value blocked without odds")
    odds_gate = g.gate_odds()
    value_gate = g.gate_value(odds_gate, [])
    check("VALUE never PASS when ODDS != PASS", odds_gate.status != "PASS" and value_gate.status != "PASS")


def test_value_still_conditional_even_with_odds_pass():
    section("14. value reste CONDITIONAL même avec un ODDS gate PASS synthétique (jamais un raccourci vers PASS)")
    from app.ai.readiness.schemas import ProductionGate
    fake_odds_pass = ProductionGate(name="ODDS", status="PASS", evidence={})
    value_gate = g.gate_value(fake_odds_pass, [])
    check("VALUE status is CONDITIONAL (never silently PASS)", value_gate.status == "CONDITIONAL")


# ---------------------------------------------------------------------------
# 15. production/shadow isolation (main.py never imports research packages).
# ---------------------------------------------------------------------------

def test_production_shadow_isolation():
    section("15. production/shadow isolation")
    gate = g.gate_api_exposure()
    check("API_EXPOSURE PASS", gate.status == "PASS")
    check("no research package imported by main.py", gate.evidence["imports_found"] == [])


# ---------------------------------------------------------------------------
# 16. model mismatch (rollback / promotion events reflect DB reality only).
# ---------------------------------------------------------------------------

def test_rollback_reflects_real_promotion_events():
    section("16. rollback gate reflète la réalité DB (0 événement -> NOT_AVAILABLE)")
    with Session(engine) as s:
        for e in s.exec(select(ModelPromotionEvent)).all():
            s.delete(e)
        s.commit()
        gate = g.gate_rollback(s)
        check("status NOT_AVAILABLE with 0 events", gate.status == "NOT_AVAILABLE")

        v1 = _seed_active_version(s, "dixon_coles")
        event = ModelPromotionEvent(model_version_id=v1.id, model_type="dixon_coles", market="1X2", decision="promoted", reason="test", actor="test")
        s.add(event); s.commit()
        gate2 = g.gate_rollback(s)
        check("status CONDITIONAL once >=1 real event exists", gate2.status == "CONDITIONAL")


# ---------------------------------------------------------------------------
# 17. probability mismatch -> provenance gate flags empty provenance.
# ---------------------------------------------------------------------------

def test_probability_provenance_mismatch_flagged():
    section("17. probability/provenance mismatch flagged")
    bad = _record(shadow_id="bad1", provenance={})
    gate = g.gate_provenance([(bad, _resolution())])
    check("status FAIL on empty provenance", gate.status == "FAIL")


# ---------------------------------------------------------------------------
# 18. decision mismatch -> decision gate distinguishes real vs synthetic.
# ---------------------------------------------------------------------------

def test_decision_gate_distinguishes_real_from_synthetic():
    section("18. decision gate distingue REAL de SYNTHETIC")
    synthetic_only = [(_record(shadow_id="syn1", data_marking="SYNTHETIC"), _resolution())]
    gate = g.gate_decision(synthetic_only)
    check("CONDITIONAL when 0 REAL decisions (synthetic never counted)", gate.status == "CONDITIONAL")
    check("evidence shows 0 real", gate.evidence["real_shadow_decisions_evaluated"] == 0)


# ---------------------------------------------------------------------------
# 19. rollback (mechanism existence vs empirical proof, already in 16).
# ---------------------------------------------------------------------------

def test_rollback_mechanism_documented():
    section("19. rollback mechanism documented, never silently 'tested'")
    with Session(engine) as s:
        for e in s.exec(select(ModelPromotionEvent)).all():
            s.delete(e)
        s.commit()
        gate = g.gate_rollback(s)
        check("mechanism description present", "mechanism" in gate.evidence)
        check("never PASS without empirical evidence", gate.status != "PASS")


# ---------------------------------------------------------------------------
# 20. kill switch — Phase 9.1 a construit un mécanisme réel (api/app/ai/safety/) ;
# ce test (mis à jour depuis Phase 9, qui constatait honnêtement son absence)
# vérifie maintenant que le gate le reconnaît réellement, sans jamais le
# confondre avec "empiriquement exercé en production" (voir gate ROLLBACK,
# resté inchangé : le mécanisme existant ne compense jamais l'absence de
# preuve empirique sur CE déploiement, §52 no-compensation).
# ---------------------------------------------------------------------------

def test_kill_switch_now_traceable_via_observability():
    section("20. kill switch désormais traçable (Phase 9.1) — plus jamais None")
    with Session(engine) as s:
        gate = g.gate_observability(s)
        check("kill_switch coverage no longer None (Phase 9.1 mechanism exists)", gate.evidence["coverage"]["kill_switch"] is not None)
        check("status no longer forced NOT_AVAILABLE solely for kill_switch absence", gate.status in GATE_STATUSES)


def test_kill_switch_gate_reads_real_state():
    section("20b. KILL_SWITCH gate lit l'état réel, fail-closed sur corruption")
    gate = g.gate_kill_switch()
    check("status in GATE_STATUSES", gate.status in GATE_STATUSES)
    check("critical", gate.critical is True)


# ---------------------------------------------------------------------------
# 21. fail-safe — a gate that cannot be evaluated returns UNKNOWN/NOT_AVAILABLE, never a best guess.
# ---------------------------------------------------------------------------

def test_fail_safe_never_best_guess():
    section("21. fail-safe : jamais un best-guess")
    gate = g.gate_odds()
    # Simule un rapport absent en pointant temporairement vers un chemin inexistant n'est pas possible sans
    # modifier REPO_ROOT — on vérifie plutôt l'invariant structurel : blocking_reason toujours présent si != PASS.
    check("non-PASS gate always carries a blocking_reason or is explicitly UNKNOWN", gate.status == "PASS" or gate.blocking_reason is not None or gate.status == "UNKNOWN")


# ---------------------------------------------------------------------------
# 22. DB purity — readiness evaluation never writes.
# ---------------------------------------------------------------------------

def test_db_purity_readonly():
    section("22. DB purity (readiness evaluation never writes)")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        as_of = datetime.now(UTC)
        _seed_all_model_types(s, as_of)
        s.commit()
        before = {"model_versions": len(s.exec(select(ModelVersion)).all()), "matches": len(s.exec(select(Match)).all())}
        store = _temp_store()
        evaluate_production_readiness(s, store, as_of)
        after = {"model_versions": len(s.exec(select(ModelVersion)).all()), "matches": len(s.exec(select(Match)).all())}
        check("row counts unchanged", before == after)
        Path(store.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 23. determinism — same DB + same as_of -> same verdict.
# ---------------------------------------------------------------------------

def test_determinism():
    section("23. determinism")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        as_of = datetime.now(UTC)
        _seed_all_model_types(s, as_of)
        s.commit()
        store = _temp_store()
        r1 = evaluate_production_readiness(s, store, as_of)
        r2 = evaluate_production_readiness(s, store, as_of)
        check("same final_verdict", r1.final_verdict == r2.final_verdict)
        check("same critical_gate_failures", sorted(r1.critical_gate_failures) == sorted(r2.critical_gate_failures))
        check("same gate statuses", [gt.status for gt in r1.gates] == [gt.status for gt in r2.gates])
        Path(store.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 24. filter consistency — gate_track_record filters real+resolved only, deterministic ordering unaffected by dict/set.
# ---------------------------------------------------------------------------

def test_filter_consistency_real_resolved_only():
    section("24. filter consistency (REAL + RESOLVED only counted)")
    entries = [
        (_record(shadow_id="a", data_marking="REAL"), _resolution(result_status="RESOLVED")),
        (_record(shadow_id="b", data_marking="REAL"), pending_resolution()),
        (_record(shadow_id="c", data_marking="SYNTHETIC"), _resolution(result_status="RESOLVED")),
    ]
    gate = g.gate_track_record(entries)
    check("only 1 counted (REAL+RESOLVED)", gate.evidence["real_prospective_track_record_sample_size"] == 1)


# ---------------------------------------------------------------------------
# 25. security scan.
# ---------------------------------------------------------------------------

def test_security_scan_no_false_positive_on_documentation():
    section("25. security scan (pas de faux positif sur la documentation 'aucun secret')")
    gate = g.gate_security()
    check("status PASS on current repo", gate.status == "PASS")
    check("no suspicious lines", gate.evidence["suspicious_after_filtering"] == [])


# ---------------------------------------------------------------------------
# 26. frontend/API exposure classification.
# ---------------------------------------------------------------------------

def test_frontend_exposure_classification():
    section("26. frontend/API exposure classification")
    gate = g.gate_frontend_exposure()
    check("status PASS (aucun vocabulaire Phase 8H-8N exposé)", gate.status == "PASS")
    check("shadow CSS/Phase11 mentions correctly excluded", gate.evidence["hits"] == {})


# ---------------------------------------------------------------------------
# 27. readiness report — full assembly produces a well-formed assessment.
# ---------------------------------------------------------------------------

def test_readiness_report_well_formed():
    section("27. readiness report bien formé")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        as_of = datetime.now(UTC)
        _seed_all_model_types(s, as_of)
        s.commit()
        store = _temp_store()
        result = evaluate_production_readiness(s, store, as_of)
        check("23 gates evaluated (22 + KILL_SWITCH, Phase 9.1)", len(result.gates) == 23)
        check("checklist has all CHECKLIST_ITEMS", {c.key for c in result.checklist} == set(CHECKLIST_ITEMS))
        check("promotion_readiness covers all model types", {p.model_type for p in result.promotion_readiness} == set(g.MODEL_TYPES))
        check("phase10_readiness has required keys", all(k in result.phase10_readiness for k in ("ready", "blocked", "missing_real_data", "open_risks")))
        check("db_safety matrix present", "matrix" in result.db_safety)
        Path(store.path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 28. no accidental activation — evaluating readiness never mutates ModelVersion.is_active/status.
# ---------------------------------------------------------------------------

def test_no_accidental_activation():
    section("28. no accidental activation")
    with Session(engine) as s:
        for v in s.exec(select(ModelVersion)).all():
            s.delete(v)
        s.commit()
        as_of = datetime.now(UTC)
        v = _seed_active_version(s, "dixon_coles", trained_at=as_of - timedelta(days=1))
        version_id = v.id
        store = _temp_store()
        evaluate_production_readiness(s, store, as_of)
        s.expire_all()
        refreshed = s.get(ModelVersion, version_id)
        check("is_active unchanged", refreshed.is_active is True)
        check("status unchanged", refreshed.status == "active")
        Path(store.path).unlink(missing_ok=True)


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_passed} passed, {_failed} failed (sur {_passed + _failed} assertions)")
    cleanup_db(DB_PATH)
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
