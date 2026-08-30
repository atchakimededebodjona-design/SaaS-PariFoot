"""
model_selection.py — Phase 6 : Model Selection Engine V1.

RECHERCHE + SHADOW UNIQUEMENT (voir scripts/model_selection_research.py et
scripts/model_selection_shadow.py). Aucune fonction de ce module n'écrit
dans model_predictions/model_versions/team_ratings, ne modifie
ModelVersion.status/is_active, et ne fait AUCUNE promotion — voir
app/ai/arena/promotion.py pour le mécanisme de promotion réel (LIVE et
offline), totalement distinct et jamais appelé ici.

=== Pourquoi un moteur séparé de promotion.py ===

promotion.py répond à "cette VERSION candidate d'UN model_type précis
peut-elle remplacer sa version active ?" (comparaison binaire, un seul
type de modèle à la fois, déjà câblée aux crons Railway via
`ModelVersion.status`). Ce module répond à une question différente :
"parmi TOUS les model_types disponibles (dixon_coles/elo/xgboost/
lightgbm), lequel est actuellement le plus fiable pour CE marché ?" — une
sélection MULTI-modèles, sur des FENÊTRES temporelles glissantes, qui ne
touche jamais `ModelVersion.status` et ne peut donc jamais être ramassée
par les crons de promotion/shadow XGBoost-LightGBM existants (voir
scheduler.py, evaluate_live_models.py) — isolation volontaire (§ "NE
JAMAIS remplacer le modèle de production automatiquement" du prompt
Phase 6).

=== Pipeline (diagramme du prompt Phase 6) ===

MODEL PREDICTIONS -> PERFORMANCE WINDOW -> MODEL EVALUATION (accuracy/
log_loss/brier, réutilise service.py) -> MODEL SELECTION (3 portes ci-
dessous) -> CANDIDATE MODEL.

=== Les 3 portes de sélection, dans l'ordre, jamais court-circuitées ===

1. SUFFICIENT DATA : un model_type n'est éligible que si TOUTES ses
   fenêtres ont sample_size >= min_sample_size (réutilise
   MIN_BENCHMARK_SAMPLE_SIZE, le seuil unique de tout l'Arena). Aucun
   model_type éligible -> status="insufficient_data".
2. STABLE PERFORMANCE : parmi les éligibles, le meilleur (log_loss le plus
   bas) doit l'être dans au moins `min_top_rank_fraction` des fenêtres ET
   son coefficient de variation du log_loss entre fenêtres doit rester
   <= `max_log_loss_cv` (une bonne moyenne sur une seule fenêtre chanceuse
   ne suffit jamais). Sinon -> status="unstable".
3. STATISTICALLY CREDIBLE : le candidat stable doit battre son dauphin de
   façon statistiquement significative (bootstrap PAIRÉ sur les mêmes
   matchs de la fenêtre de test tenue à l'écart, réutilise
   research.bootstrap_paired_diff telle quelle — jamais une deuxième
   implémentation). Sinon -> status="not_significant". Aucune paire
   fournie -> jamais "selected" par défaut (§4 du prompt : "refuser de
   sélectionner lorsque les données sont insuffisantes").
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlmodel import Session

from .ensemble import MIN_BENCHMARK_SAMPLE_SIZE
from .schemas import MarketMetrics
from .service import _model_predictions_markets
from . import research

KNOWN_SELECTION_MODEL_TYPES = ("dixon_coles", "elo", "xgboost", "lightgbm")

DEFAULT_MIN_WINDOWS = 3
DEFAULT_MAX_LOG_LOSS_CV = 0.25
DEFAULT_MIN_TOP_RANK_FRACTION = 0.5


def evaluate_model_window(
    session: Session,
    model_type: str,
    market: str,
    since: date,
    until: date,
    *,
    model_version_id: Optional[int] = None,
    dcwf: Optional["research.DCWalkForwardResult"] = None,
    league: Optional[str] = None,
) -> MarketMetrics:
    """
    MODEL EVALUATION du diagramme (§2 du prompt Phase 6) — métriques d'UN
    model_type sur UNE fenêtre [since, until] (bornes incluses). Seule
    fonction de ce module à toucher la session DB — jamais dupliquée par
    les scripts appelants (research/shadow), qui doivent tous deux passer
    par ici plutôt que reconstruire leur propre dispatch.

    `dixon_coles` : si `dcwf` (walk-forward en mémoire, voir
    research.build_dixon_coles_walk_forward) est fourni, ses observations
    dans la plage sont utilisées (research.dc_market_metrics_in_range) —
    seule source possible pour un historique BACKTESTÉ, car Dixon-Coles n'a
    aucune ligne source="backtest" en base (voir docstring research.py).
    Sans `dcwf`, retombe sur les prédictions LIVE de prediction_log
    (service._dixon_coles_markets) — utile pour une fenêtre "aujourd'hui".

    elo/xgboost/lightgbm : `model_version_id` explicite si fourni (pour
    comparer une version précise, indépendamment de is_active — même
    convention que ensemble.compute_market_weights), sinon aucune métrique
    n'est renvoyée (MarketMetrics vide) — jamais une version devinée.
    """
    if model_type == "dixon_coles":
        if dcwf is not None:
            return research.dc_market_metrics_in_range(dcwf, market, since, until)
        from .service import _dixon_coles_markets
        return _dixon_coles_markets(session, league=league, since=since, until=until)[market]

    if model_version_id is None:
        return MarketMetrics(sample_size=0)
    return _model_predictions_markets(session, model_type, model_version_id, league=league, since=since, until=until)[market]


@dataclass
class SelectionDecision:
    status: str  # "selected" | "insufficient_data" | "unstable" | "not_significant"
    market: str
    selected_model_type: Optional[str] = None
    runner_up_model_type: Optional[str] = None
    windows_evaluated: int = 0
    eligible_models: list[str] = field(default_factory=list)
    top_rank_counts: dict[str, int] = field(default_factory=dict)
    log_loss_cv: dict[str, float] = field(default_factory=dict)
    credibility: Optional[dict] = None  # sortie de research.bootstrap_paired_diff
    reason: str = ""


def _coefficient_of_variation(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    if mean <= 0:
        return None
    return statistics.pstdev(values) / mean


def select_candidate_model(
    window_results_by_model: dict[str, list[MarketMetrics]],
    market: str,
    *,
    min_sample_size: int = MIN_BENCHMARK_SAMPLE_SIZE,
    min_windows: int = DEFAULT_MIN_WINDOWS,
    max_log_loss_cv: float = DEFAULT_MAX_LOG_LOSS_CV,
    min_top_rank_fraction: float = DEFAULT_MIN_TOP_RANK_FRACTION,
    credibility_pairs_provider=None,
) -> SelectionDecision:
    """
    `window_results_by_model` : {model_type: [MarketMetrics par fenêtre,
    dans l'ordre chronologique]} — déjà calculées par l'appelant (voir
    evaluate_model_window ci-dessous), jamais recalculées ici (ce module ne
    touche jamais la session DB).

    `credibility_pairs_provider(candidate: str, runner_up: str) -> list[tuple[float, float]]` :
    callback fourni par l'appelant, appelé UNIQUEMENT une fois le candidat
    stable et son dauphin connus (porte 2 franchie) — retourne les paires
    (log_loss_dauphin, log_loss_candidat) PAR MATCH sur la fenêtre de TEST
    tenue à l'écart des fenêtres de stabilité (jamais les mêmes fenêtres
    qui ont servi aux portes 1/2 — anti-fuite). Un callback plutôt qu'une
    liste précalculée : ce module ne connaît le candidat/dauphin qu'APRÈS
    la porte 2, l'appelant ne doit donc calculer les paires qu'à ce moment,
    jamais pour toutes les paires de modèles possibles à l'avance.
    """
    n_windows = max((len(v) for v in window_results_by_model.values()), default=0)
    if n_windows < min_windows:
        return SelectionDecision(
            status="insufficient_data", market=market, windows_evaluated=n_windows,
            reason=f"Seulement {n_windows} fenêtre(s) disponible(s), {min_windows} requises au minimum.",
        )

    # --- Porte 1 : sufficient data --------------------------------------
    eligible = [
        mt for mt, windows in window_results_by_model.items()
        if len(windows) == n_windows and all(w.sample_size >= min_sample_size and w.log_loss is not None for w in windows)
    ]
    if not eligible:
        insufficient_detail = {
            mt: [w.sample_size for w in windows] for mt, windows in window_results_by_model.items()
        }
        return SelectionDecision(
            status="insufficient_data", market=market, windows_evaluated=n_windows,
            reason=f"Aucun modèle n'a sample_size >= {min_sample_size} sur les {n_windows} fenêtres "
                   f"(échantillons observés par modèle : {insufficient_detail}).",
        )

    # --- Porte 2 : stable performance ------------------------------------
    top_rank_counts: dict[str, int] = {mt: 0 for mt in eligible}
    for i in range(n_windows):
        window_log_losses = {mt: window_results_by_model[mt][i].log_loss for mt in eligible}
        best_mt = min(window_log_losses, key=window_log_losses.get)
        top_rank_counts[best_mt] += 1

    log_loss_cv = {
        mt: _coefficient_of_variation([w.log_loss for w in window_results_by_model[mt]]) for mt in eligible
    }

    ranked_by_top_count = sorted(eligible, key=lambda mt: (-top_rank_counts[mt], statistics.mean(
        w.log_loss for w in window_results_by_model[mt]
    )))
    stable_candidate = ranked_by_top_count[0]
    runner_up = ranked_by_top_count[1] if len(ranked_by_top_count) > 1 else None

    top_fraction = top_rank_counts[stable_candidate] / n_windows
    cv = log_loss_cv[stable_candidate]
    stable = top_fraction >= min_top_rank_fraction and (cv is None or cv <= max_log_loss_cv)

    if not stable:
        return SelectionDecision(
            status="unstable", market=market, windows_evaluated=n_windows, eligible_models=eligible,
            top_rank_counts=top_rank_counts, log_loss_cv=log_loss_cv,
            reason=(
                f"'{stable_candidate}' meilleur sur {top_rank_counts[stable_candidate]}/{n_windows} fenêtres "
                f"(besoin >= {min_top_rank_fraction:.0%}) et coefficient de variation du log_loss = "
                f"{cv if cv is not None else 'N/A'} (seuil {max_log_loss_cv}) — performance jugée instable, "
                "jamais sélectionné sur une seule fenêtre chanceuse."
            ),
        )

    credibility_pairs = credibility_pairs_provider(stable_candidate, runner_up) if (runner_up is not None and credibility_pairs_provider is not None) else None

    if runner_up is None or not credibility_pairs:
        return SelectionDecision(
            status="not_significant", market=market, windows_evaluated=n_windows, eligible_models=eligible,
            top_rank_counts=top_rank_counts, log_loss_cv=log_loss_cv,
            reason=(
                "Aucun dauphin comparable ou aucune paire de test disponible pour évaluer la significativité "
                f"statistique de '{stable_candidate}' — jamais sélectionné sans ce test (§4 du prompt)."
            ),
        )

    # --- Porte 3 : statistically credible --------------------------------
    bootstrap = research.bootstrap_paired_diff(credibility_pairs)
    # convention : pairs = (log_loss_runner_up, log_loss_candidate) -> mean_diff > 0 signifie candidat meilleur (log_loss plus bas)
    candidate_wins = bootstrap["significant"] and bootstrap["mean_diff"] is not None and bootstrap["mean_diff"] > 0

    if not candidate_wins:
        return SelectionDecision(
            status="not_significant", market=market, windows_evaluated=n_windows, eligible_models=eligible,
            top_rank_counts=top_rank_counts, log_loss_cv=log_loss_cv, credibility=bootstrap,
            runner_up_model_type=runner_up,
            reason=(
                f"'{stable_candidate}' n'est pas statistiquement meilleur que '{runner_up}' sur la fenêtre de "
                f"test (IC bootstrap sur delta log_loss : [{bootstrap['ci_low']}, {bootstrap['ci_high']}], "
                f"n={bootstrap['sample_size']}) — jamais sélectionné sur une différence non significative."
            ),
        )

    return SelectionDecision(
        status="selected", market=market, selected_model_type=stable_candidate, runner_up_model_type=runner_up,
        windows_evaluated=n_windows, eligible_models=eligible, top_rank_counts=top_rank_counts,
        log_loss_cv=log_loss_cv, credibility=bootstrap,
        reason=(
            f"'{stable_candidate}' meilleur sur {top_rank_counts[stable_candidate]}/{n_windows} fenêtres, "
            f"CV log_loss={cv}, et statistiquement meilleur que '{runner_up}' sur la fenêtre de test "
            f"(delta={bootstrap['mean_diff']}, IC=[{bootstrap['ci_low']}, {bootstrap['ci_high']}], n={bootstrap['sample_size']})."
        ),
    )
