"""
provider_audit.py — Phase 8F : XFOOT TIMESTAMPED ODDS PROVIDER DISCOVERY V1.

Structures de RECHERCHE UNIQUEMENT — aucun client API, aucun secret, aucune
table DB, aucun appel réseau. Étend api/app/ai/external_data/scorecard.py
(Phase 8C, réutilisé tel quel pour les enums génériques déjà validées :
COST_CATEGORIES, COMMERCIAL_USAGE_STATUSES, LEAKAGE_RISKS,
INTEGRATION_COMPLEXITIES, VERDICTS) avec les dimensions SPÉCIFIQUES au
critère temporel qui est le cœur de cette phase (§2/§69 du prompt) : capacité
à reconstruire un snapshot d'odds à un instant précis AVANT le kickoff.

Le critère n°1 (§69) n'est JAMAIS le coût, la popularité ou le nombre de
bookmakers — c'est exclusivement `can_reconstruct_snapshot` (§2) et
`snapshot_model` (§6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.ai.external_data.scorecard import (  # noqa: F401 — réutilisées telles quelles (§62 : pas de deuxième architecture)
    COST_CATEGORIES, COMMERCIAL_USAGE_STATUSES, LEAKAGE_RISKS,
    INTEGRATION_COMPLEXITIES, VERDICTS, COVERAGE_SCORES,
)

# ---------------------------------------------------------------------------
# Vocabulaire spécifique Phase 8F
# ---------------------------------------------------------------------------

YES_PARTIAL_NO_UNKNOWN = ("YES", "PARTIAL", "NO", "UNKNOWN")  # §2, §15, §21 (forme générique)

SNAPSHOT_MODELS = (  # §6
    "TRUE_SNAPSHOT_HISTORY", "TIMESTAMPED_HISTORICAL", "OPEN_CLOSE_ONLY",
    "HISTORICAL_UNTIMESTAMPED", "CURRENT_ONLY", "UNKNOWN",
)

TIMESTAMP_ORIGINS = ("BOOKMAKER_TIMESTAMP", "PROVIDER_INGESTION_TIMESTAMP", "UNKNOWN", "N/A")  # §14

TIMESTAMP_GRANULARITIES = ("SECOND", "MINUTE", "HOUR", "DATE_ONLY", "UNKNOWN")  # §12

ACCESS_MODELS = ("API_QUERY", "BULK_ARCHIVE", "BOTH", "UNKNOWN")  # §27

MOVEMENT_STATUSES = ("MOVEMENT_AVAILABLE", "MOVEMENT_NOT_AVAILABLE", "UNKNOWN")  # §16

LEAGUE_COVERAGE_ANSWERS = ("FULL", "PARTIAL", "NONE", "UNKNOWN")  # §21

CUTOFF_HORIZONS = ("T-24h", "T-12h", "T-6h", "T-3h", "T-1h")  # §15

XFOOT_LEAGUES = (
    "Bundesliga", "Ligue1", "PremierLeague", "SerieA", "LaLiga",
    "ChampionsLeague", "ConferenceLeague", "EuropaLeague", "MLS", "PrimeiraLiga", "SaudiProLeague",
)


@dataclass
class TimestampedOddsProvider:
    name: str

    # §2 — critère principal
    can_reconstruct_snapshot: str          # YES | PARTIAL | NO | UNKNOWN
    snapshot_model: str                    # §6 : A-F -> SNAPSHOT_MODELS

    # §12-§14 — sémantique du timestamp
    timestamp_granularity: str             # SECOND|MINUTE|HOUR|DATE_ONLY|UNKNOWN
    timestamp_semantics_notes: str         # texte : publication/ingestion/snapshot/kickoff/closing — jamais supposé
    timestamp_origin: str                  # BOOKMAKER_TIMESTAMP | PROVIDER_INGESTION_TIMESTAMP | UNKNOWN | N/A

    # §15 — reconstruction par cutoff
    cutoff_reconstruction: dict[str, str]  # {"T-24h": "YES"|"PARTIAL"|"NO"|"UNKNOWN", ...} pour CUTOFF_HORIZONS

    # §16-§20
    movement_status: str                   # MOVEMENT_AVAILABLE | MOVEMENT_NOT_AVAILABLE | UNKNOWN
    opening_definition: str                # §17 : ce que "opening" signifie réellement chez ce fournisseur
    closing_definition: str                # §18
    consensus_capability_notes: str        # §19/§20 : le consensus est-il lui-même reconstructible à un cutoff ?

    # §7-§8 — profondeur historique
    historical_first_year: Optional[int]
    historical_last_year: Optional[int]
    historical_depth_notes: str            # annoncée vs réellement vérifiée (§7)
    match_level_query: str                 # YES|PARTIAL|NO|UNKNOWN — §8 : peut-on interroger un match précis ?

    # §9-§11
    bookmaker_granularity_notes: str       # §9
    markets_notes: str                     # §10 : 1X2/BTTS/O-U 2.5 — jamais élargi sans nécessité
    odds_format_notes: str                 # §11

    # §21-§22 — couverture ligues (les 11 ligues Xfoot)
    league_coverage: dict[str, str]        # {league: FULL|PARTIAL|NONE|UNKNOWN}

    # §23-§28
    id_stability_notes: str                # §23-§25 : team/league/season IDs stables ou noms seulement
    access_model: str                      # API_QUERY|BULK_ARCHIVE|BOTH|UNKNOWN
    rate_limits_notes: str                 # §28

    # §29-§33
    cost_category: str                     # réutilise COST_CATEGORIES (Phase 8C)
    cost_notes: str
    commercial_usage: str                  # réutilise COMMERCIAL_USAGE_STATUSES
    storage_rights_notes: str
    redistribution_notes: str
    retention_notes: str                   # §33

    # §34-§38
    data_quality_notes: str
    reliability_notes: str
    latency_notes: str                     # real-time|near-real-time|minutes|hourly|daily|unknown
    api_maturity: str                      # LOW|MEDIUM|HIGH
    integration_complexity: str            # réutilise INTEGRATION_COMPLEXITIES

    # §39-§42
    lock_in_notes: str
    trial_availability: str                # texte : essai/sample/démo annoncé officiellement, ou "NONE"

    # Scores qualitatifs (§47/§48/§49) — réutilise COVERAGE_SCORES (EXCELLENT|GOOD|PARTIAL|POOR|UNKNOWN)
    coverage_score: str
    temporal_score: str                    # critère principal §48 : snapshot timestamp reconstructible
    leakage_risk: str                      # réutilise LEAKAGE_RISKS

    duplicate_of_existing: bool = False
    duplicate_notes: str = ""
    sources: list[str] = field(default_factory=list)
    verified_date: Optional[str] = None
    notes: Optional[str] = None
    verdict: str = "CONSIDER"              # réutilise VERDICTS (Phase 8C)


# ---------------------------------------------------------------------------
# Validation (§63 : tester la classification des fournisseurs, cutoffs, etc.)
# ---------------------------------------------------------------------------

def validate_provider(p: TimestampedOddsProvider) -> list[str]:
    problems = []
    if p.can_reconstruct_snapshot not in YES_PARTIAL_NO_UNKNOWN:
        problems.append(f"{p.name}: can_reconstruct_snapshot invalide '{p.can_reconstruct_snapshot}'")
    if p.snapshot_model not in SNAPSHOT_MODELS:
        problems.append(f"{p.name}: snapshot_model invalide '{p.snapshot_model}'")
    if p.timestamp_granularity not in TIMESTAMP_GRANULARITIES:
        problems.append(f"{p.name}: timestamp_granularity invalide '{p.timestamp_granularity}'")
    if p.timestamp_origin not in TIMESTAMP_ORIGINS:
        problems.append(f"{p.name}: timestamp_origin invalide '{p.timestamp_origin}'")
    if p.movement_status not in MOVEMENT_STATUSES:
        problems.append(f"{p.name}: movement_status invalide '{p.movement_status}'")
    if p.match_level_query not in YES_PARTIAL_NO_UNKNOWN:
        problems.append(f"{p.name}: match_level_query invalide '{p.match_level_query}'")
    if p.access_model not in ACCESS_MODELS:
        problems.append(f"{p.name}: access_model invalide '{p.access_model}'")
    if p.cost_category not in COST_CATEGORIES:
        problems.append(f"{p.name}: cost_category invalide '{p.cost_category}'")
    if p.commercial_usage not in COMMERCIAL_USAGE_STATUSES:
        problems.append(f"{p.name}: commercial_usage invalide '{p.commercial_usage}'")
    if p.integration_complexity not in INTEGRATION_COMPLEXITIES:
        problems.append(f"{p.name}: integration_complexity invalide '{p.integration_complexity}'")
    if p.coverage_score not in COVERAGE_SCORES:
        problems.append(f"{p.name}: coverage_score invalide '{p.coverage_score}'")
    if p.temporal_score not in COVERAGE_SCORES:
        problems.append(f"{p.name}: temporal_score invalide '{p.temporal_score}'")
    if p.leakage_risk not in LEAKAGE_RISKS:
        problems.append(f"{p.name}: leakage_risk invalide '{p.leakage_risk}'")
    if p.api_maturity not in ("LOW", "MEDIUM", "HIGH"):
        problems.append(f"{p.name}: api_maturity invalide '{p.api_maturity}'")
    if p.verdict not in VERDICTS:
        problems.append(f"{p.name}: verdict invalide '{p.verdict}'")

    for h in CUTOFF_HORIZONS:
        if h not in p.cutoff_reconstruction:
            problems.append(f"{p.name}: cutoff_reconstruction manque '{h}'")
        elif p.cutoff_reconstruction[h] not in YES_PARTIAL_NO_UNKNOWN:
            problems.append(f"{p.name}: cutoff_reconstruction['{h}'] invalide '{p.cutoff_reconstruction[h]}'")

    for lg in XFOOT_LEAGUES:
        if lg not in p.league_coverage:
            problems.append(f"{p.name}: league_coverage manque '{lg}'")
        elif p.league_coverage[lg] not in LEAGUE_COVERAGE_ANSWERS:
            problems.append(f"{p.name}: league_coverage['{lg}'] invalide '{p.league_coverage[lg]}'")

    if not p.sources:
        problems.append(f"{p.name}: aucune source citée")
    if not p.verified_date:
        problems.append(f"{p.name}: verified_date manquante")

    # §69 : le verdict ne peut JAMAIS dépasser CONSIDER si le critère n°1 (timestamp) n'est pas YES.
    if p.verdict in ("SHORTLIST", "RECOMMENDED_FOR_MVP") and p.can_reconstruct_snapshot != "YES":
        problems.append(f"{p.name}: verdict={p.verdict} mais can_reconstruct_snapshot={p.can_reconstruct_snapshot} (§69 : le critère temporel prime toujours)")
    if p.verdict == "RECOMMENDED_FOR_MVP" and p.snapshot_model not in ("TRUE_SNAPSHOT_HISTORY", "TIMESTAMPED_HISTORICAL"):
        problems.append(f"{p.name}: RECOMMENDED_FOR_MVP mais snapshot_model={p.snapshot_model}")
    if p.verdict == "RECOMMENDED_FOR_MVP" and p.commercial_usage != "ALLOWED":
        problems.append(f"{p.name}: RECOMMENDED_FOR_MVP mais commercial_usage={p.commercial_usage}")

    return problems


# ---------------------------------------------------------------------------
# Décision — §69 : critère n°1 = reconstruction temporelle, jamais le coût
# ---------------------------------------------------------------------------

def decide_verdict(
    *, can_reconstruct_snapshot: str, snapshot_model: str, coverage_score: str,
    temporal_score: str, leakage_risk: str, commercial_usage: str,
    duplicate_of_existing: bool = False,
) -> str:
    """
    Ordre de priorité STRICT (§69) : (1) reconstruction temporelle avant
    cutoff, (2) profondeur historique + couverture ligues (approximées ici
    par coverage_score/temporal_score, déjà synthétisés par l'appelant),
    (3) droits commerciaux, (4) coût — jamais l'inverse.
    """
    if commercial_usage == "RESTRICTED":
        return "DO_NOT_USE"
    if can_reconstruct_snapshot in ("NO", "UNKNOWN"):
        return "DO_NOT_USE" if can_reconstruct_snapshot == "NO" and leakage_risk == "HIGH" else "CONSIDER"
    if snapshot_model in ("HISTORICAL_UNTIMESTAMPED", "CURRENT_ONLY", "UNKNOWN"):
        return "CONSIDER"
    if duplicate_of_existing:
        return "CONSIDER"
    if commercial_usage in ("UNKNOWN",) or temporal_score == "UNKNOWN" or coverage_score == "UNKNOWN":
        return "CONSIDER"

    # can_reconstruct_snapshot == "YES" ou "PARTIAL" à partir d'ici.
    if commercial_usage == "LEGAL_REVIEW_REQUIRED":
        return "SHORTLIST" if temporal_score in ("EXCELLENT", "GOOD") else "CONSIDER"

    if can_reconstruct_snapshot == "YES" and snapshot_model in ("TRUE_SNAPSHOT_HISTORY", "TIMESTAMPED_HISTORICAL") \
            and temporal_score in ("EXCELLENT", "GOOD") and leakage_risk in ("LOW", "MEDIUM") \
            and commercial_usage == "ALLOWED":
        if temporal_score == "EXCELLENT" and coverage_score in ("EXCELLENT", "GOOD") and leakage_risk == "LOW":
            return "RECOMMENDED_FOR_MVP"
        return "SHORTLIST"

    return "CONSIDER"
