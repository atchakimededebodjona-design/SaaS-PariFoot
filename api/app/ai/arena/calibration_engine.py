"""
calibration_engine.py — Phase 6 : Calibration Engine V1.

RECHERCHE + SHADOW UNIQUEMENT — voir docstring de model_selection.py pour
les garanties d'isolation (aucune écriture DB, aucun impact production).

Orchestration fine au-dessus des primitives déjà construites en Phase 5.7
(app/ai/arena/research.py::platt_calibrate/isotonic_calibrate/
redistribute_pick_probability/apply_pick_calibration/
expected_calibration_error/derive_calibration_verdict) — AUCUNE
réimplémentation des maths de calibration ici, uniquement la logique de
décision "faut-il calibrer, et avec quelle méthode ?".

=== Choix de la méthode (Platt vs Isotonic) SANS toucher au test ===

`train_obs` (typiquement une fenêtre de validation, dans l'ORDRE
CHRONOLOGIQUE — condition requise par _internal_time_split ci-dessous) est
lui-même scindé en deux parts temporelles (70/30 par défaut) : la première
sert à AJUSTER Platt/Isotonic, la seconde à choisir lequel des deux
généralise le mieux — jamais le fold de test, qui ne sert qu'à MESURER le
résultat final (même discipline anti-fuite que scripts/research_ensemble.py
pour la sélection de stratégie/température, Phase 5.7). Si l'échantillon est
trop réduit pour ce choix interne, ou si train_obs n'a qu'une seule classe
observée (§16 du prompt Phase 5.7, toujours en vigueur ici), AUCUNE
calibration n'est forcée : `choice="none"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .schemas import MarketMetrics
from .service import _compute_market_metrics
from . import research

MIN_CHOICE_SAMPLE_SIZE = 10  # échantillon minimal pour départager Platt/Isotonic en interne — sous ce seuil, aucune calibration n'est forcée


@dataclass
class CalibrationResult:
    choice: str  # "none" | "platt" | "isotonic"
    verdict: str  # "HELPFUL" | "NEUTRAL" | "HARMFUL" | "INSUFFICIENT_DATA"
    raw_metrics: MarketMetrics
    platt_metrics: Optional[MarketMetrics]
    isotonic_metrics: Optional[MarketMetrics]
    raw_ece: Optional[float]
    platt_ece: Optional[float]
    isotonic_ece: Optional[float]
    train_sample_size: int
    test_sample_size: int


def _internal_time_split(obs: list[dict], fit_fraction: float = 0.7) -> tuple[list[dict], list[dict]]:
    """Découpe CHRONOLOGIQUE (jamais aléatoire) de `obs` — l'appelant doit
    fournir des observations déjà triées par date de match (c'est le cas de
    toute fenêtre construite par scripts/model_selection_research.py)."""
    cut = max(1, int(len(obs) * fit_fraction))
    return obs[:cut], obs[cut:]


def evaluate_calibration(train_obs: list[dict], test_obs: list[dict], min_sample_size: int) -> CalibrationResult:
    """
    `train_obs` : observations de la fenêtre de VALIDATION (jamais de test),
    dans l'ordre chronologique — sert à la fois à choisir Platt vs Isotonic
    (split interne, voir _internal_time_split) et, une fois le choix fixé, à
    ajuster la version finale appliquée à `test_obs`.

    `test_obs` : observations de la fenêtre de TEST — jamais utilisées pour
    ajuster ni choisir quoi que ce soit, uniquement pour MESURER RAW vs
    PLATT vs ISOTONIC (les trois toujours rapportés, pour transparence,
    même si `choice` n'en retient qu'un).
    """
    raw_metrics = _compute_market_metrics(test_obs)
    raw_ece = research.expected_calibration_error(raw_metrics.calibration)

    if len(train_obs) < min_sample_size or raw_metrics.sample_size < min_sample_size:
        return CalibrationResult(
            choice="none", verdict="INSUFFICIENT_DATA", raw_metrics=raw_metrics,
            platt_metrics=None, isotonic_metrics=None, raw_ece=raw_ece, platt_ece=None, isotonic_ece=None,
            train_sample_size=len(train_obs), test_sample_size=raw_metrics.sample_size,
        )

    train_correct = [o["correct"] for o in train_obs]
    if len(set(train_correct)) < 2:
        # une seule classe observée sur train -> Platt/Isotonic non forcés (§16, Phase 5.7)
        return CalibrationResult(
            choice="none", verdict="NEUTRAL", raw_metrics=raw_metrics,
            platt_metrics=None, isotonic_metrics=None, raw_ece=raw_ece, platt_ece=None, isotonic_ece=None,
            train_sample_size=len(train_obs), test_sample_size=raw_metrics.sample_size,
        )

    # --- Choix Platt vs Isotonic sur un split interne de train_obs (jamais test_obs) ---
    fit_obs, choice_obs = _internal_time_split(train_obs)
    method_choice = "none"
    if len(choice_obs) >= MIN_CHOICE_SAMPLE_SIZE and len(set(o["correct"] for o in fit_obs)) >= 2:
        fit_conf = [max(o["probs"].values()) for o in fit_obs]
        fit_correct = [o["correct"] for o in fit_obs]
        choice_conf = [max(o["probs"].values()) for o in choice_obs]

        platt_choice_probs = research.platt_calibrate(fit_conf, fit_correct, choice_conf)
        iso_choice_probs = research.isotonic_calibrate(fit_conf, fit_correct, choice_conf)
        platt_choice_metrics = _compute_market_metrics(research.apply_pick_calibration(choice_obs, platt_choice_probs))
        iso_choice_metrics = _compute_market_metrics(research.apply_pick_calibration(choice_obs, iso_choice_probs))

        candidates = {"platt": platt_choice_metrics.log_loss, "isotonic": iso_choice_metrics.log_loss}
        candidates = {k: v for k, v in candidates.items() if v is not None}
        raw_choice_ll = _compute_market_metrics(choice_obs).log_loss
        if raw_choice_ll is not None:
            candidates["none"] = raw_choice_ll
        if candidates:
            method_choice = min(candidates, key=candidates.get)

    # --- Version finale : ajustée sur TOUT train_obs, mesurée sur test_obs ---
    train_conf = [max(o["probs"].values()) for o in train_obs]
    test_conf = [max(o["probs"].values()) for o in test_obs]

    platt_test_probs = research.platt_calibrate(train_conf, train_correct, test_conf)
    platt_metrics = _compute_market_metrics(research.apply_pick_calibration(test_obs, platt_test_probs))

    iso_test_probs = research.isotonic_calibrate(train_conf, train_correct, test_conf)
    isotonic_metrics = _compute_market_metrics(research.apply_pick_calibration(test_obs, iso_test_probs))

    chosen_metrics = {"none": raw_metrics, "platt": platt_metrics, "isotonic": isotonic_metrics}[method_choice]
    verdict = research.derive_calibration_verdict(raw_metrics, chosen_metrics, min_sample_size)

    return CalibrationResult(
        choice=method_choice, verdict=verdict, raw_metrics=raw_metrics,
        platt_metrics=platt_metrics, isotonic_metrics=isotonic_metrics,
        raw_ece=raw_ece,
        platt_ece=research.expected_calibration_error(platt_metrics.calibration),
        isotonic_ece=research.expected_calibration_error(isotonic_metrics.calibration),
        train_sample_size=len(train_obs), test_sample_size=raw_metrics.sample_size,
    )


def produce_candidate_probability(raw_probs: dict[str, float], calibration_result: CalibrationResult, train_obs: list[dict]) -> dict[str, float]:
    """Applique `calibration_result.choice` à UNE probabilité brute (ex. un
    match shadow non encore résolu) — réajuste Platt/Isotonic sur
    `train_obs` (les MÊMES observations que celles passées à
    evaluate_calibration, jamais des données futures) puis applique au seul
    point `raw_probs`. `choice="none"` -> retourne `raw_probs` inchangé."""
    if calibration_result.choice == "none" or not train_obs:
        return dict(raw_probs)

    train_conf = [max(o["probs"].values()) for o in train_obs]
    train_correct = [o["correct"] for o in train_obs]
    pick_conf = max(raw_probs.values())

    if calibration_result.choice == "platt":
        calibrated = research.platt_calibrate(train_conf, train_correct, [pick_conf])
    else:
        calibrated = research.isotonic_calibrate(train_conf, train_correct, [pick_conf])

    return research.redistribute_pick_probability(raw_probs, calibrated[0])
