"""
track_record.py — Phase 7 : Shadow Evaluation & Track Record V1.

Couche de LECTURE SEULE au-dessus des deux tables Phase 6
(model_selection_decisions, shadow_selection_predictions) — AUCUNE écriture
DB dans ce module. Réutilise intégralement les primitives statistiques déjà
construites en Phase 5.7 (research.py : bootstrap_paired_diff, mcnemar_test,
wilson_interval, actual_outcome, derive_calibration_verdict, obs_log_loss/
obs_brier) et le calcul de métriques de service.py
(_compute_market_metrics) — AUCUNE nouvelle formule statistique n'est créée
ici, uniquement une orchestration + une couche de requêtage.

=== Pourquoi aucune nouvelle table (§24 du prompt Phase 7) ===

Tout ce dont le Track Record a besoin est déjà présent :
- `ModelSelectionDecision` porte déjà market/as_of/status/selected_model_type/
  windows_evaluated/metrics(JSON)/calibration_choice/calibration_verdict —
  suffisant pour la distribution de sélection (§14), le stability tracking
  (§13, dérivé de `metrics["top_rank_counts"]` déjà stocké) et le calibration
  tracking (§15).
- `ShadowSelectionPrediction` porte déjà candidate_probs/production_probs/
  status/result_*/candidate_correct/production_correct — suffisant pour le
  Track Record cumulatif (§8), les fenêtres glissantes (§9) et la
  comparaison Production vs Shadow (§6/§17/§18/§19). La seule information
  manquante était la probabilité AVANT calibration (`candidate_probs_raw`,
  Phase 7, un unique champ additif — voir shadow_selection_prediction.py)
  pour le RAW vs CALIBRATED par prédiction individuelle (§16).

=== "Mêmes matchs" (§6) ===

compare_production_vs_shadow ne considère JAMAIS une ligne où l'un des deux
côtés (production/shadow) est absent — jamais une comparaison inventée sur
un échantillon partiel, même principe que shadow_comparison.py (Phase 11,
concept différent — ModelVersion.status="shadow" — mais même discipline
d'intersection sur clé naturelle, réutilisée ici en pattern, pas en code).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from .ensemble import MIN_BENCHMARK_SAMPLE_SIZE
from .schemas import MarketMetrics
from .service import _compute_market_metrics
from . import research
from app.models.model_selection_decision import ModelSelectionDecision
from app.models.shadow_selection_prediction import ShadowSelectionPrediction

CONCLUSION_RELATIVE_THRESHOLD = 0.01  # même seuil (1% relatif) que research.derive_calibration_verdict — documenté une seule fois


# ---------------------------------------------------------------------------
# 1. Adaptateurs — ShadowSelectionPrediction -> observation {p_true, probs, actual, correct}
# ---------------------------------------------------------------------------

def _shadow_observation(row: ShadowSelectionPrediction, use_raw: bool = False) -> Optional[dict]:
    """`use_raw=True` lit candidate_probs_raw (probabilité AVANT
    calibration) au lieu de candidate_probs (version finale) — voir §16 du
    prompt Phase 7. None si non résolue, ou si la probabilité brute demandée
    n'a jamais été stockée (calibration_applied="none" -> candidate_probs
    EST déjà la version brute, voir candidate_probs_raw=None dans ce cas)."""
    if row.status != "resolved" or row.result_home_goals is None or row.result_away_goals is None:
        return None
    if use_raw:
        probs_json = row.candidate_probs_raw or row.candidate_probs
    else:
        probs_json = row.candidate_probs
    try:
        probs = json.loads(probs_json)
    except (json.JSONDecodeError, TypeError):
        return None
    actual = research.actual_outcome(row.market, row.result_home_goals, row.result_away_goals)
    if actual not in probs:
        return None
    pick = max(probs, key=probs.get)
    return {"p_true": probs[actual], "probs": probs, "actual": actual, "correct": pick == actual}


def _production_observation(row: ShadowSelectionPrediction) -> Optional[dict]:
    if row.status != "resolved" or row.production_probs is None or row.result_home_goals is None:
        return None
    try:
        probs = json.loads(row.production_probs)
    except (json.JSONDecodeError, TypeError):
        return None
    actual = research.actual_outcome(row.market, row.result_home_goals, row.result_away_goals)
    if actual not in probs:
        return None
    pick = max(probs, key=probs.get)
    return {"p_true": probs[actual], "probs": probs, "actual": actual, "correct": pick == actual}


def _metrics_dict(metrics: MarketMetrics) -> dict:
    return {"sample_size": metrics.sample_size, "accuracy": metrics.accuracy,
            "log_loss": metrics.log_loss, "brier_score": metrics.brier_score}


def _query_resolved_rows(
    session: Session, market: str, *, since: Optional[date] = None, until: Optional[date] = None,
    last_n: Optional[int] = None, league: Optional[str] = None,
) -> list[ShadowSelectionPrediction]:
    stmt = select(ShadowSelectionPrediction).where(
        ShadowSelectionPrediction.market == market, ShadowSelectionPrediction.status == "resolved",
    )
    if since is not None:
        stmt = stmt.where(ShadowSelectionPrediction.match_date >= since)
    if until is not None:
        stmt = stmt.where(ShadowSelectionPrediction.match_date <= until)
    if league is not None:
        stmt = stmt.where(ShadowSelectionPrediction.league == league)
    stmt = stmt.order_by(ShadowSelectionPrediction.match_date, ShadowSelectionPrediction.id)
    rows = session.exec(stmt).all()
    if last_n is not None:
        rows = rows[-last_n:]
    return rows


# ---------------------------------------------------------------------------
# 2. Track Record — §8/§9/§20/§21/§22/§29 du prompt.
# ---------------------------------------------------------------------------

@dataclass
class TrackRecordResult:
    status: str  # "ok" | "insufficient_data" | "no_shadow_data"
    market: str
    sample_size: int = 0
    accuracy: Optional[float] = None
    log_loss: Optional[float] = None
    brier_score: Optional[float] = None
    accuracy_ci: tuple = (None, None)
    since: Optional[str] = None
    until: Optional[str] = None
    league: Optional[str] = None


def compute_track_record(
    session: Session, market: str, *, since: Optional[date] = None, until: Optional[date] = None,
    last_n: Optional[int] = None, league: Optional[str] = None,
    min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE, use_raw: bool = False,
) -> TrackRecordResult:
    """compute_track_record(...) du §29 — sample_size/accuracy/log_loss/
    brier/CI/status calculés à partir des OBSERVATIONS INDIVIDUELLES (jamais
    une moyenne de moyennes, §8) sur les prédictions shadow RÉSOLUES
    correspondant aux filtres."""
    rows = _query_resolved_rows(session, market, since=since, until=until, last_n=last_n, league=league)
    if not rows:
        return TrackRecordResult(status="no_shadow_data", market=market,
                                  since=str(since) if since else None, until=str(until) if until else None, league=league)

    observations = [o for o in (_shadow_observation(r, use_raw=use_raw) for r in rows) if o is not None]
    metrics = _compute_market_metrics(observations)
    status = "ok" if metrics.sample_size >= min_sample_size else "insufficient_data"
    ci = research.wilson_interval(metrics.correct_predictions or 0, metrics.sample_size) if metrics.sample_size > 0 else (None, None)

    return TrackRecordResult(
        status=status, market=market, sample_size=metrics.sample_size, accuracy=metrics.accuracy,
        log_loss=metrics.log_loss, brier_score=metrics.brier_score, accuracy_ci=ci,
        since=str(since) if since else None, until=str(until) if until else None, league=league,
    )


def compute_cumulative_track_record(
    session: Session, market: str, checkpoints: list[date], **filters,
) -> list[TrackRecordResult]:
    """§8 : une entrée par checkpoint (ex. Day1/Day7/Day30), CHACUNE
    recalculée depuis le début (`since` reste celui fourni dans `filters`,
    `until=checkpoint`) à partir des observations individuelles — jamais une
    moyenne cumulée de moyennes déjà agrégées par période."""
    return [compute_track_record(session, market, until=cp, **filters) for cp in sorted(checkpoints)]


# ---------------------------------------------------------------------------
# 3. Comparaison Production vs Shadow — §6/§17/§18/§19/§30/§46 du prompt.
# ---------------------------------------------------------------------------

@dataclass
class ComparisonResult:
    status: str  # "ok" | "insufficient_data" | "no_shadow_data"
    market: str
    sample_size: int = 0
    production: Optional[dict] = None
    shadow: Optional[dict] = None
    delta: Optional[dict] = None
    significance: Optional[dict] = None
    conclusion: str = "INSUFFICIENT_DATA"


def _derive_comparison_conclusion(bootstrap_ll: dict, production_log_loss: Optional[float]) -> str:
    """BETTER/EQUIVALENT/WORSE/NO_CLEAR_ADVANTAGE/INSUFFICIENT_DATA (§46) —
    même seuil de significativité PRATIQUE (1% relatif) que
    research.derive_calibration_verdict (Phase 6), documenté une seule fois
    (CONCLUSION_RELATIVE_THRESHOLD). Une différence statistiquement
    significative mais minuscule reste EQUIVALENT, jamais BETTER/WORSE."""
    if bootstrap_ll["sample_size"] == 0:
        return "INSUFFICIENT_DATA"
    if not bootstrap_ll["significant"]:
        return "NO_CLEAR_ADVANTAGE"
    if not production_log_loss:
        return "NO_CLEAR_ADVANTAGE"
    rel = bootstrap_ll["mean_diff"] / production_log_loss  # >0 : shadow a un log_loss plus bas (meilleur)
    if rel > CONCLUSION_RELATIVE_THRESHOLD:
        return "BETTER"
    if rel < -CONCLUSION_RELATIVE_THRESHOLD:
        return "WORSE"
    return "EQUIVALENT"


def compare_production_vs_shadow(
    session: Session, market: str, *, since: Optional[date] = None, until: Optional[date] = None,
    last_n: Optional[int] = None, league: Optional[str] = None, min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE,
) -> ComparisonResult:
    """
    §6 : n'utilise QUE les lignes où production ET shadow sont résolues —
    "si Shadow existe mais pas Production : ne pas inclure dans la
    comparaison principale ; si Production existe mais pas Shadow : ne pas
    inventer de Shadow" — les deux cas sont honorés en ne construisant les
    paires QUE lorsque les DEUX observations (`_shadow_observation`,
    `_production_observation`) réussissent pour la même ligne.

    §18 : delta = shadow - production. Pour log_loss/brier, négatif =
    shadow meilleur (plus bas) ; pour accuracy, positif = shadow meilleur
    (plus haut) — convention documentée explicitement dans le résultat
    (`delta["sign_convention"]`), jamais implicite.
    """
    rows = _query_resolved_rows(session, market, since=since, until=until, last_n=last_n, league=league)
    matched = [r for r in rows if r.production_probs is not None and r.production_correct is not None]

    pairs = []
    for r in matched:
        s_obs = _shadow_observation(r)
        p_obs = _production_observation(r)
        if s_obs is not None and p_obs is not None:
            pairs.append((s_obs, p_obs))

    if not pairs:
        return ComparisonResult(status="no_shadow_data", market=market, sample_size=0)

    shadow_observations = [s for s, _ in pairs]
    production_observations = [p for _, p in pairs]
    shadow_metrics = _compute_market_metrics(shadow_observations)
    production_metrics = _compute_market_metrics(production_observations)
    n = len(pairs)

    if n < min_sample_size:
        return ComparisonResult(
            status="insufficient_data", market=market, sample_size=n,
            production=_metrics_dict(production_metrics), shadow=_metrics_dict(shadow_metrics),
            conclusion="INSUFFICIENT_DATA",
        )

    delta = {
        "accuracy": round(shadow_metrics.accuracy - production_metrics.accuracy, 4)
        if shadow_metrics.accuracy is not None and production_metrics.accuracy is not None else None,
        "log_loss": round(shadow_metrics.log_loss - production_metrics.log_loss, 4)
        if shadow_metrics.log_loss is not None and production_metrics.log_loss is not None else None,
        "brier_score": round(shadow_metrics.brier_score - production_metrics.brier_score, 4)
        if shadow_metrics.brier_score is not None and production_metrics.brier_score is not None else None,
        "sign_convention": "shadow - production ; log_loss/brier : négatif = shadow meilleur ; accuracy : positif = shadow meilleur.",
    }

    pairs_ll = [(research.obs_log_loss(p), research.obs_log_loss(s)) for s, p in pairs]  # (production, shadow) -> mean_diff>0 si shadow meilleur
    pairs_brier = [(research.obs_brier(p), research.obs_brier(s)) for s, p in pairs]
    bootstrap_ll = research.bootstrap_paired_diff(pairs_ll)
    bootstrap_brier = research.bootstrap_paired_diff(pairs_brier)
    b = sum(1 for s, p in pairs if s["correct"] and not p["correct"])
    c = sum(1 for s, p in pairs if (not s["correct"]) and p["correct"])
    mcnemar = research.mcnemar_test(b, c)

    significance = {
        "bootstrap_log_loss_diff": bootstrap_ll, "bootstrap_brier_diff": bootstrap_brier, "mcnemar_accuracy": mcnemar,
        "shadow_accuracy_ci": research.wilson_interval(shadow_metrics.correct_predictions or 0, n),
        "production_accuracy_ci": research.wilson_interval(production_metrics.correct_predictions or 0, n),
    }

    conclusion = _derive_comparison_conclusion(bootstrap_ll, production_metrics.log_loss)

    return ComparisonResult(
        status="ok", market=market, sample_size=n, production=_metrics_dict(production_metrics),
        shadow=_metrics_dict(shadow_metrics), delta=delta, significance=significance, conclusion=conclusion,
    )


# ---------------------------------------------------------------------------
# 4. Distribution de sélection / stability tracking — §13/§14 du prompt.
# ---------------------------------------------------------------------------

def _decisions_query(session: Session, *, since: Optional[date] = None, until: Optional[date] = None, market: Optional[str] = None):
    stmt = select(ModelSelectionDecision)
    if since is not None:
        stmt = stmt.where(ModelSelectionDecision.as_of >= since)
    if until is not None:
        stmt = stmt.where(ModelSelectionDecision.as_of <= until)
    if market is not None:
        stmt = stmt.where(ModelSelectionDecision.market == market)
    return session.exec(stmt).all()


def compute_selection_distribution(session: Session, *, since: Optional[date] = None, until: Optional[date] = None, market: Optional[str] = None) -> dict:
    """§14 : distribution UNIQUEMENT sur les décisions status="selected" —
    les événements insufficient_data/unstable/not_significant ne sont
    JAMAIS attribués à un modèle ici (voir compute_stability_tracking pour
    leur suivi séparé)."""
    rows = [r for r in _decisions_query(session, since=since, until=until, market=market) if r.status == "selected"]
    if not rows:
        return {"status": "no_shadow_data", "total_selected": 0, "counts": {}, "distribution": {}}

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.selected_model_type] = counts.get(r.selected_model_type, 0) + 1
    total = len(rows)
    distribution = {mt: round(c / total, 4) for mt, c in counts.items()}
    return {"status": "ok", "total_selected": total, "counts": counts, "distribution": distribution}


def compute_stability_tracking(session: Session, *, since: Optional[date] = None, until: Optional[date] = None, market: Optional[str] = None) -> dict:
    """
    §13 : pour chaque modèle, combien de fois il a été le CANDIDAT STABLE
    (le gagnant de la porte 2 — voir model_selection.py) puis effectivement
    sélectionné, jugé instable, ou rejeté pour non-significativité. Le
    candidat implicite d'une décision non "selected" est dérivé de
    `metrics["top_rank_counts"]` (déjà stocké par model_selection_shadow.py
    — voir model_selection.SelectionDecision.top_rank_counts) : jamais une
    nouvelle colonne, jamais recalculé depuis les prédictions individuelles.
    Une décision "insufficient_data" n'a AUCUN candidat identifiable (porte
    1 échoue avant qu'un rang ne soit calculé) — jamais attribuée à un
    modèle, comptée séparément dans `unattributed`.
    """
    rows = _decisions_query(session, since=since, until=until, market=market)
    if not rows:
        return {"status": "no_shadow_data", "per_model": {}, "unattributed": {}}

    per_model: dict[str, dict[str, int]] = {}
    unattributed = {"insufficient_data": 0, "no_identifiable_candidate": 0}

    for r in rows:
        if r.status == "insufficient_data":
            unattributed["insufficient_data"] += 1
            continue

        implied_candidate = r.selected_model_type
        if implied_candidate is None:
            try:
                metrics = json.loads(r.metrics) if r.metrics else {}
            except (json.JSONDecodeError, TypeError):
                metrics = {}
            top_rank_counts = metrics.get("top_rank_counts") or {}
            if top_rank_counts:
                implied_candidate = max(top_rank_counts, key=top_rank_counts.get)

        if implied_candidate is None:
            unattributed["no_identifiable_candidate"] += 1
            continue

        bucket = per_model.setdefault(implied_candidate, {"selected": 0, "unstable": 0, "not_significant": 0})
        if r.status in bucket:
            bucket[r.status] += 1

    return {"status": "ok", "per_model": per_model, "unattributed": unattributed}


# ---------------------------------------------------------------------------
# 5. Calibration tracking — §15/§16 du prompt.
# ---------------------------------------------------------------------------

def compute_calibration_tracking(session: Session, *, since: Optional[date] = None, until: Optional[date] = None, market: Optional[str] = None) -> dict:
    """§15 : fréquence NONE/PLATT/ISOTONIC parmi les décisions
    "selected" (seules celles-ci portent une calibration_choice
    significative — une décision refusée n'a jamais de candidat à
    calibrer)."""
    rows = [r for r in _decisions_query(session, since=since, until=until, market=market) if r.status == "selected"]
    if not rows:
        return {"status": "no_shadow_data", "choice_counts": {}, "choice_frequency": {}}

    choice_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    for r in rows:
        choice_counts[r.calibration_choice] = choice_counts.get(r.calibration_choice, 0) + 1
        if r.calibration_verdict:
            verdict_counts[r.calibration_verdict] = verdict_counts.get(r.calibration_verdict, 0) + 1
    total = len(rows)
    choice_frequency = {k: round(v / total, 4) for k, v in choice_counts.items()}
    return {"status": "ok", "total_decisions": total, "choice_counts": choice_counts,
            "choice_frequency": choice_frequency, "verdict_counts": verdict_counts}


def compare_raw_vs_calibrated(
    session: Session, market: str, *, since: Optional[date] = None, until: Optional[date] = None,
    league: Optional[str] = None, min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE,
) -> dict:
    """§16 : RAW vs CALIBRATED, UNIQUEMENT sur les prédictions où une
    calibration a RÉELLEMENT été appliquée (calibration_applied != "none"
    ET candidate_probs_raw présent) — jamais une comparaison où "raw" et
    "calibrated" seraient en fait la même valeur."""
    rows = _query_resolved_rows(session, market, since=since, until=until, league=league)
    calibrated_rows = [r for r in rows if r.calibration_applied != "none" and r.candidate_probs_raw]
    if not calibrated_rows:
        return {"status": "no_shadow_data", "market": market, "sample_size": 0}

    raw_obs = [o for o in (_shadow_observation(r, use_raw=True) for r in calibrated_rows) if o is not None]
    calibrated_obs = [o for o in (_shadow_observation(r, use_raw=False) for r in calibrated_rows) if o is not None]
    raw_metrics = _compute_market_metrics(raw_obs)
    calibrated_metrics = _compute_market_metrics(calibrated_obs)

    if raw_metrics.sample_size < min_sample_size:
        return {"status": "insufficient_data", "market": market, "sample_size": raw_metrics.sample_size}

    verdict = research.derive_calibration_verdict(raw_metrics, calibrated_metrics, min_sample_size)
    return {
        "status": "ok", "market": market, "sample_size": raw_metrics.sample_size,
        "raw": _metrics_dict(raw_metrics), "calibrated": _metrics_dict(calibrated_metrics), "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# 6. Rapport persistant — §38 du prompt. Écriture fichier UNIQUEMENT.
# ---------------------------------------------------------------------------

def write_track_record_reports(result: dict, outdir, run_id: str) -> tuple:
    from pathlib import Path

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"shadow_track_record_{run_id}.json"
    md_path = outdir / f"shadow_track_record_{run_id}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_track_record_markdown_report(result), encoding="utf-8")
    return json_path, md_path


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_track_record_markdown_report(result: dict) -> str:
    """Formate `result` (assemblé par scripts/shadow_track_record.py) selon
    les tableaux obligatoires du §45 et les sections du §38 du prompt
    Phase 7. Pure fonction de mise en forme — rien n'est recalculé ici."""
    md = ["# XFOOT SHADOW EVALUATION & TRACK RECORD V1\n"]

    md.append("\n## 1. Résumé exécutif\n")
    md.append(f"\nRun id : `{result.get('run_id')}` — généré le {result.get('generated_at')}.\n")
    if result.get("status") == "no_shadow_data":
        md.append("\n**NO SHADOW DATA** — aucune prédiction shadow résolue n'existe encore. "
                   "Ce rapport ne contient donc aucune mesure de performance : rien n'est fabriqué.\n")

    md.append("\n## 2. Période et marchés couverts\n")
    md.append(f"\n- Période : {result.get('since') or 'depuis le début'} → {result.get('until') or 'aujourd’hui'}\n")
    md.append(f"- Marchés : {', '.join(result.get('markets', []))}\n")

    md.append("\n## 3. Production vs Shadow — Log Loss\n\n")
    md.append("| Market | N | Production LogLoss | Shadow LogLoss | Delta | Conclusion |\n")
    md.append("|---|---|---|---|---|---|\n")
    for market, comp in result.get("comparisons", {}).items():
        prod = comp.get("production") or {}
        shad = comp.get("shadow") or {}
        delta = (comp.get("delta") or {}).get("log_loss")
        md.append(f"| {market} | {comp.get('sample_size', 0)} | {_fmt(prod.get('log_loss'))} | "
                   f"{_fmt(shad.get('log_loss'))} | {_fmt(delta)} | {comp.get('conclusion')} |\n")

    md.append("\n## 4. Production vs Shadow — Accuracy\n\n")
    md.append("| Market | N | Production | Shadow | Delta | Significance |\n")
    md.append("|---|---|---|---|---|---|\n")
    for market, comp in result.get("comparisons", {}).items():
        prod = comp.get("production") or {}
        shad = comp.get("shadow") or {}
        delta = (comp.get("delta") or {}).get("accuracy")
        sig = (comp.get("significance") or {}).get("mcnemar_accuracy", {})
        md.append(f"| {market} | {comp.get('sample_size', 0)} | {_fmt(prod.get('accuracy'))} | "
                   f"{_fmt(shad.get('accuracy'))} | {_fmt(delta)} | significant={sig.get('significant')} |\n")

    md.append("\n## 5. Production vs Shadow — Brier\n\n")
    md.append("| Market | N | Production | Shadow | Delta | Conclusion |\n")
    md.append("|---|---|---|---|---|---|\n")
    for market, comp in result.get("comparisons", {}).items():
        prod = comp.get("production") or {}
        shad = comp.get("shadow") or {}
        delta = (comp.get("delta") or {}).get("brier_score")
        md.append(f"| {market} | {comp.get('sample_size', 0)} | {_fmt(prod.get('brier_score'))} | "
                   f"{_fmt(shad.get('brier_score'))} | {_fmt(delta)} | {comp.get('conclusion')} |\n")

    md.append("\n## 6. Selection distribution\n\n")
    md.append("| Model | Selected | Share |\n|---|---|---|\n")
    for market, dist in result.get("selection_distribution", {}).items():
        for mt, share in dist.get("distribution", {}).items():
            md.append(f"| {mt} [{market}] | {dist.get('counts', {}).get(mt)} | {_fmt(share)} |\n")

    md.append("\n## 7. Stability tracking\n\n")
    for market, stab in result.get("stability_tracking", {}).items():
        md.append(f"\n- **{market}** : {stab.get('per_model', {})} (non attribué : {stab.get('unattributed', {})})\n")

    md.append("\n## 8. Calibration\n\n")
    md.append("| Market | NONE | PLATT | ISOTONIC | Verdict (raw vs calibrated) |\n|---|---|---|---|---|\n")
    for market, calib in result.get("calibration_tracking", {}).items():
        freq = calib.get("choice_frequency", {})
        rvc = result.get("raw_vs_calibrated", {}).get(market, {})
        md.append(f"| {market} | {_fmt(freq.get('none'))} | {_fmt(freq.get('platt'))} | "
                   f"{_fmt(freq.get('isotonic'))} | {rvc.get('verdict', 'N/A')} |\n")

    md.append("\n## 9. Track Record cumulatif\n\n")
    for market, points in result.get("cumulative", {}).items():
        md.append(f"\n### {market}\n\n| Until | N | Accuracy | LogLoss | Brier |\n|---|---|---|---|---|\n")
        for p in points:
            md.append(f"| {p.get('until')} | {p.get('sample_size')} | {_fmt(p.get('accuracy'))} | "
                       f"{_fmt(p.get('log_loss'))} | {_fmt(p.get('brier_score'))} |\n")

    md.append("\n## 10. Limitations\n\n")
    for limit in result.get("limitations", []):
        md.append(f"\n- {limit}\n")

    md.append("\n## 11. Conclusion\n\n")
    md.append(f"\n{result.get('conclusion', '')}\n")

    md.append("\n---\n\nPHASE 7 — XFOOT SHADOW EVALUATION & TRACK RECORD V1 TERMINÉE. "
               "AUCUNE PROMOTION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")

    return "".join(md)
