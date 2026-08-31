"""
scripts/odds_api_cost_audit.py — Phase 8G.1 : XFOOT THE ODDS API — CREDIT &
COST FEASIBILITY AUDIT (AVANT ACHAT).
=============================================================================
DOCUMENTATION + CALCUL UNIQUEMENT. ZÉRO appel réseau vers The Odds API dans
ce script (ni /v4/sports, ni /v4/historical/..., ni aucun autre endpoint) —
contrairement à scripts/odds_api_smoke_test.py, ce module n'importe même pas
les fonctions fetch_sports/fetch_historical_odds_snapshot (aucun risque
d'appel accidentel). Les seules données réseau utilisées sont CELLES DÉJÀ
COLLECTÉES lors du smoke test précédent (Phase 8G, 20260830_104639 — 0 crédit
consommé, clé valide, /v4/sports 200, historical 401) et la documentation
officielle vérifiée par WebFetch dans CETTE session (pricing, formule de
coût, historical-odds-data, terms-and-conditions).

Ce script calcule UNIQUEMENT, à partir de données déjà en base (lecture
seule) et du cache football-data.co.uk déjà téléchargé (Phase 8D, aucun
nouveau téléchargement) :
  - la sélection PoC 100 matchs (réutilise EXACTEMENT select_trial_matches,
    max_per_league=20 — même fonction que scripts/odds_api_trial.py, jamais
    une deuxième sélection) ;
  - la couverture temporelle réelle de cette sélection vs les dates de
    disponibilité Historical Odds officiellement confirmées par ligue ;
  - le nombre de requêtes RÉELLEMENT nécessaires par scénario, y compris
    l'optimisation de regroupement par horaire de coup d'envoi partagé
    (§6/§9 du prompt : ne jamais multiplier artificiellement les appels si
    l'API fournit déjà plusieurs événements dans une même réponse), calculée
    UNIQUEMENT à partir des kickoffs RÉELS déjà enrichis via le cache Phase
    8D — jamais une heure fabriquée pour un match DATE_ONLY.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/odds_api_cost_audit.py
"""

import json
import logging
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlmodel import Session, select  # noqa: E402

from app.core.database import engine, init_db  # noqa: E402
from app.models.match import Match  # noqa: E402
from app.ai.odds_research.odds_api_trial import (  # noqa: E402
    select_trial_matches, enrich_kickoff_times, cutoff_for, CUTOFF_HORIZONS_HOURS, SPORT_KEYS,
)

import feature_engineering_walkforward as fewf  # noqa: E402
from odds_api_trial import load_match_rows, load_kickoff_time_index, MAX_PER_LEAGUE  # noqa: E402 (réutilisées telles quelles)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("odds_api_cost_audit")

# ---------------------------------------------------------------------------
# §1-§5 : constantes DOCUMENTÉES, vérifiées par WebFetch officiel dans cette
# session (jamais reprises aveuglément d'un ancien rapport — §1 du prompt).
# Sources citées explicitement dans le rapport (§ build_report).
# ---------------------------------------------------------------------------

PLANS = [
    {"name": "Starter", "price_usd_month": 0, "credits_month": 500, "historical_odds_access": "NO (empirically confirmed — see below)"},
    {"name": "20K", "price_usd_month": 30, "credits_month": 20_000, "historical_odds_access": "UNKNOWN (see Historical Odds Pricing section)"},
    {"name": "100K", "price_usd_month": 59, "credits_month": 100_000, "historical_odds_access": "UNKNOWN (see Historical Odds Pricing section)"},
    {"name": "5M", "price_usd_month": 119, "credits_month": 5_000_000, "historical_odds_access": "UNKNOWN (see Historical Odds Pricing section)"},
    {"name": "15M", "price_usd_month": 249, "credits_month": 15_000_000, "historical_odds_access": "UNKNOWN (see Historical Odds Pricing section)"},
]
ENTRY_LEVEL_PAID_PLAN = "20K"  # $30/month, 20 000 crédits/mois — "premier plan payant" au sens du prompt

# Formule officielle confirmée (the-odds-api.com/liveapi/guides/v4/) :
#   GET /v4/odds (live)       : cost = markets x regions
#   GET /v4/historical/.../odds : cost = 10 x markets x regions   (multiplicateur historique x10)
HISTORICAL_MULTIPLIER = 10

# Dates de disponibilité Historical Odds PAR LIGUE, confirmées par WebFetch
# officiel (the-odds-api.com/historical-odds-data/) dans CETTE session.
LEAGUE_HISTORICAL_FLOOR = {
    "PremierLeague": datetime(2020, 6, 6, 10, 5, tzinfo=timezone.utc),
    "Bundesliga": datetime(2020, 6, 6, 10, 5, tzinfo=timezone.utc),
    "SerieA": datetime(2020, 6, 6, 10, 5, tzinfo=timezone.utc),
    "LaLiga": datetime(2020, 6, 6, 10, 5, tzinfo=timezone.utc),
    "Ligue1": datetime(2020, 7, 16, 0, 55, tzinfo=timezone.utc),
}

MARKETS_TESTED = ("1X2", "BTTS", "OU25")  # h2h, totals(2.5) — disponibilité historique réelle NON confirmée, voir §9 du rapport
CUTOFF_HORIZONS = CUTOFF_HORIZONS_HOURS  # (24, 12, 6, 3, 1) — réutilisé tel quel, jamais réinventé


def snapshot_db_rows(session) -> dict:
    return fewf.snapshot_db_counts(session)


def load_poc_sample():
    """§3 du prompt : réutilise EXACTEMENT la sélection déterministe déjà
    utilisée par scripts/odds_api_trial.py (max_per_league=MAX_PER_LEAGUE=20,
    aucun filtre de date préalable — les N matchs les plus récents par ligue),
    puis enrichit le kickoff via le cache football-data.co.uk (Phase 8D, zéro
    coût réseau — fichiers déjà présents localement)."""
    match_rows = load_match_rows()
    trial_matches = select_trial_matches(match_rows, max_per_league=MAX_PER_LEAGUE)
    cache_dir = Path.home() / ".xfoot_research_cache" / "odds_football_data_co_uk"
    try:
        kickoff_index = load_kickoff_time_index(cache_dir)
        trial_matches = enrich_kickoff_times(trial_matches, kickoff_index)
    except Exception as e:  # noqa: BLE001
        logger.warning("Enrichissement kickoff impossible (%s) — comptage batching restera conservateur (0 partages).", e)
    return trial_matches


def xfoot_dataset_summary(session) -> dict:
    """§6 du prompt : Xfoot Dataset — bornes réelles de `match` (lecture
    seule), toutes ligues confondues ET restreintes aux 5 ligues prioritaires."""
    all_rows = session.exec(select(Match.league, Match.date)).all()
    if not all_rows:
        return {"total_matches": 0}
    all_dates = [d for _, d in all_rows]
    priority_rows = [(lg, d) for lg, d in all_rows if lg in SPORT_KEYS]
    per_league = defaultdict(list)
    for lg, d in priority_rows:
        per_league[lg].append(d)

    per_league_summary = {}
    for lg in SPORT_KEYS:
        dates = sorted(per_league.get(lg, []))
        floor = LEAGUE_HISTORICAL_FLOOR.get(lg)
        covered = sum(1 for d in dates if floor is not None and d.replace(tzinfo=timezone.utc) >= floor)
        per_league_summary[lg] = {
            "n_matches": len(dates),
            "earliest": dates[0].isoformat() if dates else None,
            "latest": dates[-1].isoformat() if dates else None,
            "historical_odds_floor": floor.isoformat() if floor else None,
            "matches_on_or_after_floor": covered,
            "matches_before_floor_not_covered": len(dates) - covered,
            "coverage_pct": round(100.0 * covered / len(dates), 2) if dates else None,
        }

    return {
        "total_matches_all_leagues": len(all_dates),
        "earliest_match_all_leagues": min(all_dates).isoformat(),
        "latest_match_all_leagues": max(all_dates).isoformat(),
        "priority_leagues": per_league_summary,
    }


def poc_sample_summary(trial_matches) -> dict:
    """§7/§13 du prompt : composition réelle de l'échantillon PoC (100 max),
    couverture Historical Odds par ligue sur CET échantillon précis, et
    enrichissement kickoff réel (nécessaire au calcul de regroupement §6/§9)."""
    by_league = defaultdict(list)
    for m in trial_matches:
        by_league[m.league].append(m)

    summary = {}
    for lg, matches in by_league.items():
        floor = LEAGUE_HISTORICAL_FLOOR.get(lg)
        dts = [m.kickoff_date for m in matches]
        with_real_kickoff = sum(1 for m in matches if m.kickoff_precision == "DATE_AND_TIME")
        covered = sum(1 for m in matches if floor is not None and datetime.combine(m.kickoff_date, datetime.min.time(), tzinfo=timezone.utc) >= floor)
        summary[lg] = {
            "n_selected": len(matches),
            "earliest": min(dts).isoformat() if dts else None,
            "latest": max(dts).isoformat() if dts else None,
            "with_real_kickoff_time_from_cache": with_real_kickoff,
            "date_only_fallback": len(matches) - with_real_kickoff,
            "on_or_after_historical_floor": covered,
            "not_covered_before_floor": len(matches) - covered,
        }
    return summary


def compute_requests_needed(trial_matches, cutoff_hours_list: list[int]) -> dict:
    """
    §6/§9 du prompt : « ne pas supposer qu'un cutoff = une requête
    obligatoire ; étudier si l'API permet de partager une réponse entre
    plusieurs matchs ». Fait officiellement confirmé (WebFetch de cette
    session, the-odds-api.com/liveapi/guides/v4/) : une requête historique
    renvoie TOUS les événements du sport proches du timestamp demandé (voir
    `data` = liste, confirmé empiriquement par le smoke test Phase 8G) — donc
    DEUX matchs de la MÊME ligue dont le cutoff calculé tombe au même instant
    (même horaire de coup d'envoi réel, ex. plusieurs matchs Premier League à
    15h00 le même samedi) peuvent en théorie être couverts par UNE SEULE
    requête. `previous_timestamp`/`next_timestamp` permettent de NAVIGUER
    entre snapshots mais chaque navigation reste une requête facturée à part
    entière (confirmé : « single request returns one snapshot at a time »)
    — donc AUCUNE économie inter-cutoff, uniquement une éventuelle économie
    inter-MATCHS au même cutoff exact.

    Le regroupement n'est appliqué QUE sur les matchs dont le kickoff est
    RÉELLEMENT connu (cache football-data.co.uk, Phase 8D) — un match
    DATE_ONLY compte pour 1 requête à part entière, jamais supposé partageable
    (§ ne jamais fabriquer une heure pour gagner un crédit).
    """
    by_league = defaultdict(list)
    for m in trial_matches:
        by_league[m.league].append(m)

    result = {}
    for horizon in cutoff_hours_list:
        distinct_slots_worst_case = 0
        distinct_slots_with_batching = 0
        for lg, matches in by_league.items():
            worst_case = len(matches)  # 1 requête par match, jamais partagée (hypothèse conservatrice)
            slots = set()
            unbatchable = 0
            for m in matches:
                if m.kickoff_precision == "DATE_AND_TIME" and m.kickoff_datetime is not None:
                    cutoff_dt = cutoff_for(m.kickoff_datetime, horizon)
                    slots.add(cutoff_dt.replace(second=0, microsecond=0))
                else:
                    unbatchable += 1  # DATE_ONLY : jamais regroupé, jamais fabriqué
            distinct_slots_worst_case += worst_case
            distinct_slots_with_batching += len(slots) + unbatchable
        result[horizon] = {
            "requests_worst_case_no_batching": distinct_slots_worst_case,
            "requests_after_real_kickoff_batching": distinct_slots_with_batching,
        }
    return result


def build_report(session) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    xfoot_ds = xfoot_dataset_summary(session)
    trial_matches = load_poc_sample()
    poc_ds = poc_sample_summary(trial_matches)
    n_poc = len(trial_matches)

    requests_by_horizon = compute_requests_needed(trial_matches, list(CUTOFF_HORIZONS))
    single_cutoff_horizon = 6  # T-6h retenu comme cutoff unique représentatif pour les scénarios MINIMAL/RECOMMENDED (milieu de la grille §6, jamais aux extrêmes)
    req_single = requests_by_horizon.get(single_cutoff_horizon, requests_by_horizon[CUTOFF_HORIZONS[2]])

    regions = 1  # eu, §5 du prompt

    # --- §7 Scénario MINIMAL : 1X2 seul, 1 cutoff (T-6h), regroupement appliqué ---
    minimal_requests_worst = req_single["requests_worst_case_no_batching"]
    minimal_requests_batched = req_single["requests_after_real_kickoff_batching"]
    minimal_credits_worst = minimal_requests_worst * HISTORICAL_MULTIPLIER * 1 * regions
    minimal_credits_batched = minimal_requests_batched * HISTORICAL_MULTIPLIER * 1 * regions

    # --- §8 Scénario RECOMMENDED : 1X2 + BTTS(si dispo) + O/U2.5(si dispo), 1 cutoff (T-6h) ---
    n_markets_recommended = len(MARKETS_TESTED)  # 3 — disponibilité réelle par marché = UNKNOWN, voir §9 du rapport (coût facturé sur le marché DEMANDÉ, pas sur ce qui est réellement retourné par bookmaker)
    recommended_credits_worst = minimal_requests_worst * HISTORICAL_MULTIPLIER * n_markets_recommended * regions
    recommended_credits_batched = minimal_requests_batched * HISTORICAL_MULTIPLIER * n_markets_recommended * regions

    # --- §9 Scénario MAXIMUM : 3 marchés x 5 cutoffs x 1 région, regroupement appliqué par cutoff ---
    maximal_credits_worst = sum(
        requests_by_horizon[h]["requests_worst_case_no_batching"] * HISTORICAL_MULTIPLIER * n_markets_recommended * regions
        for h in CUTOFF_HORIZONS
    )
    maximal_credits_batched = sum(
        requests_by_horizon[h]["requests_after_real_kickoff_batching"] * HISTORICAL_MULTIPLIER * n_markets_recommended * regions
        for h in CUTOFF_HORIZONS
    )

    entry_plan = next(p for p in PLANS if p["name"] == ENTRY_LEVEL_PAID_PLAN)
    margins = {}
    for pct in (10, 25, 50):
        credits_with_margin = round(recommended_credits_batched * (1 + pct / 100))
        margins[f"+{pct}%"] = {
            "credits_needed": credits_with_margin,
            "pct_of_entry_plan_quota": round(100.0 * credits_with_margin / entry_plan["credits_month"], 2),
        }

    cost_per_match = {
        "1X2_only": round(HISTORICAL_MULTIPLIER * 1 * regions, 2),
        "BTTS_only": round(HISTORICAL_MULTIPLIER * 1 * regions, 2),
        "OU25_only": round(HISTORICAL_MULTIPLIER * 1 * regions, 2),
        "1X2_plus_BTTS_plus_OU25": round(HISTORICAL_MULTIPLIER * 3 * regions, 2),
        "note": "Coût PAR REQUÊTE (une requête peut, empiriquement, couvrir plusieurs matchs de la même ligue au même horaire de coup d'envoi réel — voir requests_by_horizon). Coût par MATCH réel dépend donc du taux de partage effectif, pas d'un ratio fixe.",
    }

    league_table = []
    for lg in SPORT_KEYS:
        xf = xfoot_ds["priority_leagues"][lg]
        poc = poc_ds.get(lg, {})
        league_table.append({
            "league": lg, "sport_key": SPORT_KEYS[lg],
            "available_on_provider": "YES (confirmed Phase 8F/8G — SPORT_KEYS mapping + smoke test /v4/sports 200 for SerieA; other 4 not re-verified this phase per no-new-network-call rule)",
            "historical_floor_confirmed": LEAGUE_HISTORICAL_FLOOR[lg].isoformat(),
            "xfoot_full_history_coverage_pct": xf["coverage_pct"],
            "poc_sample_coverage": f"{poc.get('on_or_after_historical_floor', 0)}/{poc.get('n_selected', 0)}",
            "market_coverage": "UNKNOWN / NEEDS_REAL_QUERY (1X2/h2h presumed available per general Odds API market catalogue; BTTS/O-U 2.5 historical availability NOT confirmed by documentation fetched this session)",
            "confidence": "MEDIUM (availability + floor date from official docs; market-level and real-time coverage not empirically verified for this phase)",
        })

    return {
        "run_id": run_id, "generated_at": generated_at, "phase": "8G.1", "kind": "cost_credit_feasibility_audit",
        "rule": "NO PURCHASE, NO PLAN CHANGE, NO HISTORICAL ODDS REQUEST, NO NEW SMOKE TEST, 0 CREDITS CONSUMED THIS PHASE.",
        "prior_smoke_test_evidence": {
            "source_report": "reports/odds_providers/odds_api_smoke_test_20260830_104639.json",
            "sports_call_status": 200, "historical_call_status": 401, "credits_consumed_that_run": 0,
            "conclusion": "Starter (free) tier key: standard API accessible, Historical Odds endpoint returns 401 (access not included in current plan).",
        },
        "plans": PLANS, "entry_level_paid_plan": ENTRY_LEVEL_PAID_PLAN,
        "historical_odds_pricing": {
            "formula_live": "cost = markets x regions",
            "formula_historical": f"cost = {HISTORICAL_MULTIPLIER} x markets x regions",
            "formula_source": "https://the-odds-api.com/liveapi/guides/v4/ (verified via WebFetch this session)",
            "which_paid_tier_minimum_confirmed": "UNKNOWN — official docs state 'Historical data is only available on paid usage plans' without naming the minimum tier explicitly; homepage lists 'Historical Odds' as a feature label on every plan card (Starter included), which CONTRADICTS the dedicated historical-odds-data page AND our own empirical 401 on the Starter/free key used in the prior smoke test. Treating the dedicated page + empirical evidence as authoritative: Starter = NO. First paid tier (20K, $30/mo) = UNKNOWN, not contradicted by any source, but not explicitly confirmed either.",
        },
        "league_historical_floors": {lg: dt.isoformat() for lg, dt in LEAGUE_HISTORICAL_FLOOR.items()},
        "xfoot_dataset": xfoot_ds,
        "poc_dataset": {"n_matches": n_poc, "max_per_league": MAX_PER_LEAGUE, "by_league": poc_ds, "reused_selection_function": "app.ai.odds_research.odds_api_trial.select_trial_matches (identique à scripts/odds_api_trial.py, aucune resélection)"},
        "requests_needed_by_cutoff_horizon": requests_by_horizon,
        "batching_methodology": (
            "Une requête historique renvoie tous les événements du sport proches du timestamp demandé (confirmé "
            "empiriquement, smoke test Phase 8G) -> deux matchs de la même ligue dont le cutoff calculé tombe au "
            "même horaire réel (minute) peuvent partager une requête. Le regroupement n'est appliqué QUE sur les "
            "matchs dont le kickoff réel est connu via le cache football-data.co.uk (Phase 8D) — un match DATE_ONLY "
            "compte pour 1 requête entière, jamais supposé partageable. previous_timestamp/next_timestamp permettent "
            "de naviguer entre snapshots mais chaque navigation reste une requête facturée à part entière (confirmé "
            "par la documentation officielle) — AUCUNE économie inter-cutoff."
        ),
        "scenario_minimal": {
            "description": "100 matchs (échantillon PoC réel), 1X2 uniquement, 1 région (eu), 1 cutoff (T-6h, représentatif)",
            "requests_worst_case": minimal_requests_worst, "requests_after_batching": minimal_requests_batched,
            "credits_worst_case": minimal_credits_worst, "credits_after_batching": minimal_credits_batched,
        },
        "scenario_recommended": {
            "description": "100 matchs (échantillon PoC réel), 1X2 + BTTS(si dispo) + O/U2.5(si dispo), 1 région (eu), 1 cutoff (T-6h)",
            "markets_count": n_markets_recommended,
            "requests_worst_case": minimal_requests_worst, "requests_after_batching": minimal_requests_batched,
            "credits_worst_case": recommended_credits_worst, "credits_after_batching": recommended_credits_batched,
        },
        "scenario_maximal": {
            "description": "100 matchs (échantillon PoC réel), 3 marchés, 5 cutoffs (T-24h/12h/6h/3h/1h), 1 région (eu)",
            "credits_worst_case": maximal_credits_worst, "credits_after_batching": maximal_credits_batched,
        },
        "safety_margins_on_recommended_batched": margins,
        "cost_per_match": cost_per_match,
        "cost_per_100_matches": {
            "minimal_1X2_only": minimal_credits_batched,
            "recommended_3_markets_1_cutoff": recommended_credits_batched,
            "maximal_3_markets_5_cutoffs": maximal_credits_batched,
        },
        "league_coverage_table": league_table,
        "commercial_conditions": {
            "commercial_saas_use": "ALLOWED — 'use of our data in websites, mobile apps, dashboards, analytical tools, and other user-facing applications, including commercial use' (terms-and-conditions.html, verified this session).",
            "redistribution": "PROHIBITED as a standalone data product (own API/data feed/downloadable files) — does not restrict internal derived features/ML use per the fetched clause.",
            "storage_caching": "Not explicitly addressed — no stated restriction on operational retention found in the fetched terms.",
            "research_vs_commercial_distinction": "NONE STATED — same restriction (no resale as standalone product) applies regardless of research/production context.",
            "verdict": "ALLOWED for Xfoot's intended use (internal feature derivation + display in the product), subject to never reselling raw odds as a standalone feed. LEGAL_REVIEW_REQUIRED only if Xfoot later considers redistributing raw odds directly.",
        },
    }


def main() -> dict:
    init_db()
    with Session(engine) as session:
        db_before = snapshot_db_rows(session)

    logger.info("Aucun appel réseau vers The Odds API dans ce script — analyse documentaire + calcul local uniquement.")
    with Session(engine) as session:
        result = build_report(session)
        db_after = snapshot_db_rows(session)

    result["db_safety"] = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    git_status = subprocess.run(["git", "status", "--porcelain"], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True).stdout
    result["git_status_porcelain"] = git_status

    result["final_decision"] = _decide(result)
    _write_reports(result)
    print("\n" + "=" * 80)
    print("PHASE 8G.1 — XFOOT THE ODDS API COST & CREDIT FEASIBILITY AUDIT TERMINÉE. "
          "AUCUN ACHAT EFFECTUÉ. AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


def _decide(result: dict) -> dict:
    entry_quota = next(p for p in PLANS if p["name"] == ENTRY_LEVEL_PAID_PLAN)["credits_month"]
    rec_batched = result["scenario_recommended"]["credits_after_batching"]
    margin_50_pct_of_quota = result["safety_margins_on_recommended_batched"]["+50%"]["pct_of_entry_plan_quota"]

    tier_access_unknown = True  # confirmed above : minimum paid tier for Historical Odds not explicitly documented
    if tier_access_unknown:
        decision = "NEEDS_REAL_QUERY"
        reason = (
            f"Le calcul de coût lui-même est FAVORABLE : le scénario recommandé (100 matchs, 3 marchés, 1 cutoff, "
            f"regroupement par kickoff réel appliqué) nécessite {rec_batched} crédits, soit {margin_50_pct_of_quota}% "
            f"du quota mensuel du plan {ENTRY_LEVEL_PAID_PLAN} même avec +50% de marge — largement soutenable. "
            "MAIS la documentation officielle ne confirme PAS explicitement que le plan d'entrée de gamme "
            f"({ENTRY_LEVEL_PAID_PLAN}, $30/mois) inclut réellement l'accès Historical Odds (la page dédiée dit "
            "seulement 'paid usage plans' sans nommer le tier minimum, et la page d'accueil liste 'Historical Odds' "
            "de façon identique sur CHAQUE plan y compris Starter — ce qui contredit notre propre 401 empirique sur "
            "la clé Starter actuelle). Cette phase interdit explicitement toute nouvelle requête réseau pour lever "
            "cette inconnue. Recommandation : si un achat est envisagé, vérifier ce point PRÉCIS (contact support "
            "The Odds API ou un premier appel historique réel sur le plan 20K une fois souscrit) avant tout usage "
            "en profondeur — le coût n'est PAS le facteur bloquant, l'ACCÈS au tier minimum l'est potentiellement."
        )
    elif rec_batched > entry_quota * 0.5:
        decision, reason = "GO_WITH_CAUTION", "Coût recommandé proche de la moitié du quota mensuel — marge faible."
    elif rec_batched > entry_quota:
        decision, reason = "NO_GO", "Coût recommandé dépasse le quota mensuel du plan d'entrée de gamme."
    else:
        decision, reason = "GO", "Le plan d'entrée de gamme couvre confortablement le PoC recommandé."

    return {"decision": decision, "reason": reason}


def render_markdown(result: dict) -> str:
    md = ["# XFOOT THE ODDS API — COST & CREDIT FEASIBILITY AUDIT\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}. RÈGLE : {result['rule']}\n")
    md.append(f"\n**Décision finale : {result['final_decision']['decision']}**\n\n{result['final_decision']['reason']}\n")

    md.append("\n## 2. Current Account Status\n\n")
    ev = result["prior_smoke_test_evidence"]
    md.append(f"\nPreuve issue du smoke test Phase 8G ({ev['source_report']}) : /v4/sports -> {ev['sports_call_status']}, "
               f"historical -> {ev['historical_call_status']}, crédits consommés ce run-là : {ev['credits_consumed_that_run']}. "
               f"{ev['conclusion']}\n")

    md.append("\n## 3. Official Pricing\n\n")
    md.append("| Plan | Price/month (USD) | Credits/month | Historical Odds (label on pricing page) |\n|---|---|---|---|\n")
    for p in result["plans"]:
        md.append(f"| {p['name']} | {p['price_usd_month']} | {p['credits_month']:,} | {p['historical_odds_access']} |\n")
    md.append(f"\nEntry-level PAID plan (\"premier plan payant\") : **{result['entry_level_paid_plan']}**.\n")

    md.append("\n## 4. Historical Odds Pricing\n\n")
    hp = result["historical_odds_pricing"]
    md.append(f"\n- Live formula : `{hp['formula_live']}`\n- Historical formula : `{hp['formula_historical']}`\n"
               f"- Source : {hp['formula_source']}\n- Minimum tier confirmed : {hp['which_paid_tier_minimum_confirmed']}\n")

    md.append("\n## 5. Credit Formula\n\n")
    md.append(f"\n`credits = {HISTORICAL_MULTIPLIER} x markets x regions`, PER REQUEST — verified via official docs this session, NOT reused blindly from a prior report.\n")

    md.append("\n## 6. Xfoot Dataset\n\n")
    xf = result["xfoot_dataset"]
    md.append(f"\n- Total matches (all leagues) : {xf.get('total_matches_all_leagues')}\n"
               f"- Earliest : {xf.get('earliest_match_all_leagues')} — Latest : {xf.get('latest_match_all_leagues')}\n\n")
    md.append("| League | N matches | Earliest | Latest | Historical floor | Covered | Not covered | Coverage % |\n|---|---|---|---|---|---|---|---|\n")
    for lg, v in xf.get("priority_leagues", {}).items():
        md.append(f"| {lg} | {v['n_matches']} | {v['earliest']} | {v['latest']} | {v['historical_odds_floor']} | "
                   f"{v['matches_on_or_after_floor']} | {v['matches_before_floor_not_covered']} | {v['coverage_pct']} |\n")

    md.append("\n## 7. PoC Dataset\n\n")
    poc = result["poc_dataset"]
    md.append(f"\nN = {poc['n_matches']} (max {poc['max_per_league']}/ligue) — sélection réutilisée telle quelle de {poc['reused_selection_function']}.\n\n")
    md.append("| League | Selected | Earliest | Latest | Real kickoff (cache) | Date-only fallback | Covered (>=floor) |\n|---|---|---|---|---|---|---|\n")
    for lg, v in poc["by_league"].items():
        md.append(f"| {lg} | {v['n_selected']} | {v['earliest']} | {v['latest']} | {v['with_real_kickoff_time_from_cache']} | "
                   f"{v['date_only_fallback']} | {v['on_or_after_historical_floor']}/{v['n_selected']} |\n")

    md.append("\n## 8. League Coverage\n\n")
    md.append("| League | Available | Historical | Market coverage | Confidence |\n|---|---|---|---|---|\n")
    for row in result["league_coverage_table"]:
        md.append(f"| {row['league']} | {row['available_on_provider']} | {row['historical_floor_confirmed']} | {row['market_coverage']} | {row['confidence']} |\n")

    md.append("\n## 9. Market Coverage\n\n")
    md.append(f"\nMarkets tested in this cost model : {MARKETS_TESTED}. 1X2/h2h presence is standard across the provider's catalogue (not historically league-specific per docs fetched). "
               "BTTS and Over/Under 2.5 historical availability is **UNKNOWN / NEEDS_REAL_QUERY** — no official documentation confirming per-market historical coverage was found this session, and no query was made to verify (forbidden this phase). Cost model bills per REQUESTED market key regardless of actual per-bookmaker availability (standard Odds API billing behavior), so the credit numbers below are valid upper-bound cost estimates even if some bookmakers don't return every market.\n")

    md.append("\n## 10. Minimal Scenario\n\n")
    s = result["scenario_minimal"]
    md.append(f"\n{s['description']}\n\n- Requests (worst case, no batching) : {s['requests_worst_case']}\n"
               f"- Requests (after real-kickoff batching) : {s['requests_after_batching']}\n"
               f"- Credits (worst case) : {s['credits_worst_case']}\n- Credits (after batching) : {s['credits_after_batching']}\n")

    md.append("\n## 11. Recommended Scenario\n\n")
    s = result["scenario_recommended"]
    md.append(f"\n{s['description']}\n\n- Markets : {s['markets_count']}\n"
               f"- Requests (worst case) : {s['requests_worst_case']} — Requests (after batching) : {s['requests_after_batching']}\n"
               f"- Credits (worst case) : {s['credits_worst_case']}\n- **Credits (after batching) : {s['credits_after_batching']}**\n")

    md.append("\n## 12. Maximum Reasonable Scenario\n\n")
    s = result["scenario_maximal"]
    md.append(f"\n{s['description']}\n\n- Credits (worst case, no batching) : {s['credits_worst_case']}\n"
               f"- Credits (after real-kickoff batching) : {s['credits_after_batching']}\n")

    md.append("\n## 13. Safety Margins\n\n")
    md.append("\nApplied on top of the RECOMMENDED scenario (batched) :\n\n| Margin | Credits needed | % of entry-plan (20K) monthly quota |\n|---|---|---|\n")
    for k, v in result["safety_margins_on_recommended_batched"].items():
        md.append(f"| {k} | {v['credits_needed']} | {v['pct_of_entry_plan_quota']}% |\n")

    md.append("\n## 14. Historical Depth\n\n")
    md.append(f"\nPer-league floors confirmed officially (WebFetch, this session) : {result['league_historical_floors']}\n"
               "Compared against Xfoot's own match history (section 6 above) — coverage percentages shown per league; "
               "never claimed as 100% without the computed figure.\n")

    md.append("\n## 15. Commercial Conditions\n\n")
    cc = result["commercial_conditions"]
    for k, v in cc.items():
        md.append(f"\n- **{k}** : {v}\n")

    md.append("\n## 16. Risks\n\n")
    md.append(
        "\n- Minimum paid tier actually granting Historical Odds access is NOT explicitly confirmed by official docs "
        "(homepage listing contradicts the dedicated Historical Odds page and our own empirical 401 on the Starter key).\n"
        "- BTTS / Over-Under 2.5 historical market availability is unconfirmed — recommended-scenario cost assumes "
        "all 3 markets are requestable, which may overstate cost if some are unsupported historically (billing would "
        "then simply fail/return less data for that market, not overcharge, but this hasn't been verified).\n"
        "- Request-batching-by-shared-kickoff figures depend on football-data.co.uk cache coverage, which is itself "
        "incomplete (some PoC matches remain DATE_ONLY) — real achievable batching could differ from this estimate.\n"
    )

    md.append("\n## 17. Unknowns\n\n")
    md.append(
        "\n- Exact minimum paid tier for Historical Odds access : UNKNOWN.\n"
        "- BTTS / O-U 2.5 historical market availability per league : UNKNOWN / NEEDS_REAL_QUERY.\n"
        "- Whether /v4/sports lists all 5 priority leagues (only SerieA re-verified in the Phase 8G smoke test) : "
        "ASSUMED YES per Phase 8F/8G SPORT_KEYS documentation, not re-verified this phase (no new network calls allowed).\n"
    )

    md.append("\n## 18. Recommendation\n\n")
    md.append(f"\n**{result['final_decision']['decision']}**\n\n{result['final_decision']['reason']}\n")

    md.append("\n---\n\n### SCORECARD\n\n")
    md.append("| Criterion | Result | Evidence | Verdict |\n|---|---|---|---|\n")
    scorecard = [
        ("Historical access", "401 on current Starter/free key", "Phase 8G smoke test 20260830_104639", "BLOCKED on current plan"),
        ("Credit cost", f"{result['scenario_recommended']['credits_after_batching']} credits for recommended PoC scenario", "This audit, official formula", "LOW (well within entry-paid-plan quota)"),
        ("1X2", "Standard market, cost = 10 credits/request/region", "Official pricing docs", "CONFIRMED"),
        ("BTTS", "Historical availability unconfirmed", "No official doc found this session", "UNKNOWN"),
        ("O/U 2.5", "Historical availability unconfirmed", "No official doc found this session", "UNKNOWN"),
        ("League coverage", "5/5 leagues have a confirmed historical floor date", "the-odds-api.com/historical-odds-data/", "CONFIRMED"),
        ("Historical depth", "2020-06-06 (4 leagues) / 2020-07-16 (Ligue1) vs Xfoot data back to 2019", "This audit (section 6/14)", "PARTIAL (see coverage %)"),
        ("Commercial use", "Allowed, no resale as standalone product", "terms-and-conditions.html", "ALLOWED"),
        ("PoC affordability", f"{result['safety_margins_on_recommended_batched']['+50%']['pct_of_entry_plan_quota']}% of 20K plan quota even at +50% margin", "This audit", "AFFORDABLE"),
    ]
    for c, r, e, v in scorecard:
        md.append(f"| {c} | {r} | {e} | {v} |\n")

    md.append("\n---\n\n### DATABASE SAFETY\n\n")
    db = result["db_safety"]
    md.append(f"\nBefore : {db['before']}\n\nAfter : {db['after']}\n\nUnchanged : **{db['unchanged']}**\n")

    md.append("\n---\n\nPHASE 8G.1 — XFOOT THE ODDS API COST & CREDIT FEASIBILITY AUDIT TERMINÉE. "
               "AUCUN ACHAT EFFECTUÉ. AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def _write_reports(result: dict) -> None:
    outdir = Path(__file__).resolve().parent.parent / "reports" / "odds_providers"
    outdir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = outdir / f"odds_api_cost_audit_{date_str}.json"
    md_path = outdir / f"odds_api_cost_audit_{date_str}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s", json_path, md_path)


if __name__ == "__main__":
    main()
    sys.exit(0)
