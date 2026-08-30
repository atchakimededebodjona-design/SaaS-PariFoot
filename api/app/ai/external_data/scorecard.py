"""
scorecard.py — Phase 8C : XFOOT EXTERNAL DATA SOURCE AUDIT V1.

Structures de RECHERCHE UNIQUEMENT — aucun client API, aucun secret, aucune
table DB, aucun appel réseau. Un ProviderRecord encode les faits recueillis
manuellement (recherche web, documentation officielle) pour un fournisseur de
données externe candidat ; les fonctions ci-dessous ne font que valider la
cohérence interne de ces faits et en dériver un verdict — jamais l'inverse
(le verdict ne peut jamais contredire les faits qui le justifient).

Aucune fonction ici ne décide qu'une source DOIT être intégrée : `verdict`
est une classification de recherche (DO_NOT_USE/CONSIDER/SHORTLIST/
RECOMMENDED_FOR_MVP), jamais une action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

DOMAINS = ("odds", "injuries", "suspensions", "lineups", "standings", "weather")

HISTORY_CLASSES = ("FULL_HISTORY", "PARTIAL_HISTORY", "CURRENT_ONLY", "UNKNOWN")
LEAKAGE_RISKS = ("LOW", "MEDIUM", "HIGH", "UNKNOWN")
COST_CATEGORIES = ("FREE", "LOW", "MEDIUM", "HIGH", "ENTERPRISE", "UNKNOWN")
COVERAGE_SCORES = ("EXCELLENT", "GOOD", "PARTIAL", "POOR", "UNKNOWN")
INTEGRATION_COMPLEXITIES = ("LOW", "MEDIUM", "HIGH")
COMMERCIAL_USAGE_STATUSES = ("ALLOWED", "RESTRICTED", "LEGAL_REVIEW_REQUIRED", "UNKNOWN")
RECONSTRUCTION_ANSWERS = ("YES", "PARTIAL", "NO", "UNKNOWN")
VERDICTS = ("DO_NOT_USE", "CONSIDER", "SHORTLIST", "RECOMMENDED_FOR_MVP")


@dataclass
class ProviderRecord:
    name: str
    domains: list[str]                       # sous-ensemble de DOMAINS couverts par ce fournisseur
    coverage_notes: str                       # couverture ligues Xfoot (5 en DB + 6 CSV non chargées), en texte
    coverage_score: str                       # EXCELLENT|GOOD|PARTIAL|POOR|UNKNOWN — qualitatif, jamais un faux %
    history: str                              # FULL_HISTORY|PARTIAL_HISTORY|CURRENT_ONLY|UNKNOWN
    timestamp_quality: str                    # texte : quels timestamps sont exposés (opening/closing/reported_at...)
    leakage_risk: str                         # LOW|MEDIUM|HIGH|UNKNOWN
    cost_category: str                        # FREE|LOW|MEDIUM|HIGH|ENTERPRISE|UNKNOWN
    cost_notes: str                           # prix exact + date de vérification, ou "UNKNOWN"
    commercial_usage: str                     # ALLOWED|RESTRICTED|LEGAL_REVIEW_REQUIRED|UNKNOWN
    storage_rights_notes: str                 # peut-on conserver raw/derived/snapshots ?
    redistribution_notes: str                 # affichage aux utilisateurs finaux possible ?
    integration_complexity: str               # LOW|MEDIUM|HIGH
    historical_reconstruction: str            # YES|PARTIAL|NO|UNKNOWN — §46/§47
    duplicate_of_existing: bool                # cette source apporte-t-elle une info déjà présente dans Xfoot ?
    duplicate_notes: str
    sources: list[str] = field(default_factory=list)   # URLs officielles utilisées
    verified_date: Optional[str] = None        # date réelle de vérification (recherche web)
    notes: Optional[str] = None
    verdict: str = "CONSIDER"                  # DO_NOT_USE|CONSIDER|SHORTLIST|RECOMMENDED_FOR_MVP


def validate_provider(p: ProviderRecord) -> list[str]:
    """Cohérence interne — jamais de valeur hors énumération, jamais un
    verdict RECOMMENDED_FOR_MVP qui contredirait les faits déclarés (§60 :
    aucune source ne devient RECOMMENDED_FOR_MVP uniquement parce qu'elle est
    moins chère)."""
    problems = []
    for d in p.domains:
        if d not in DOMAINS:
            problems.append(f"{p.name}: domaine inconnu '{d}'")
    if p.history not in HISTORY_CLASSES:
        problems.append(f"{p.name}: history invalide '{p.history}'")
    if p.leakage_risk not in LEAKAGE_RISKS:
        problems.append(f"{p.name}: leakage_risk invalide '{p.leakage_risk}'")
    if p.cost_category not in COST_CATEGORIES:
        problems.append(f"{p.name}: cost_category invalide '{p.cost_category}'")
    if p.coverage_score not in COVERAGE_SCORES:
        problems.append(f"{p.name}: coverage_score invalide '{p.coverage_score}'")
    if p.integration_complexity not in INTEGRATION_COMPLEXITIES:
        problems.append(f"{p.name}: integration_complexity invalide '{p.integration_complexity}'")
    if p.commercial_usage not in COMMERCIAL_USAGE_STATUSES:
        problems.append(f"{p.name}: commercial_usage invalide '{p.commercial_usage}'")
    if p.historical_reconstruction not in RECONSTRUCTION_ANSWERS:
        problems.append(f"{p.name}: historical_reconstruction invalide '{p.historical_reconstruction}'")
    if p.verdict not in VERDICTS:
        problems.append(f"{p.name}: verdict invalide '{p.verdict}'")
    if not p.sources:
        problems.append(f"{p.name}: aucune source citée")
    if not p.verified_date:
        problems.append(f"{p.name}: verified_date manquante")

    # §60/§64 : un verdict RECOMMENDED_FOR_MVP doit être justifié par les faits, jamais par le seul prix.
    if p.verdict == "RECOMMENDED_FOR_MVP":
        if p.history == "CURRENT_ONLY":
            problems.append(f"{p.name}: RECOMMENDED_FOR_MVP mais history=CURRENT_ONLY (aucun backtest walk-forward possible)")
        if p.leakage_risk in ("HIGH", "UNKNOWN"):
            problems.append(f"{p.name}: RECOMMENDED_FOR_MVP mais leakage_risk={p.leakage_risk}")
        if p.commercial_usage in ("RESTRICTED", "LEGAL_REVIEW_REQUIRED", "UNKNOWN"):
            problems.append(f"{p.name}: RECOMMENDED_FOR_MVP mais commercial_usage={p.commercial_usage}")
        if p.historical_reconstruction not in ("YES", "PARTIAL"):
            problems.append(f"{p.name}: RECOMMENDED_FOR_MVP mais historical_reconstruction={p.historical_reconstruction}")

    # §65 : timestamp insuffisant -> doit être documenté comme risque de fuite, jamais LOW silencieusement.
    if "UNKNOWN" in p.timestamp_quality.upper() and p.leakage_risk == "LOW":
        problems.append(f"{p.name}: timestamp_quality signale une incertitude mais leakage_risk=LOW")

    return problems


def derive_leakage_risk(history: str, has_reported_at_timestamp: bool) -> str:
    """Dérivation par défaut (§65) : CURRENT_ONLY ou timestamp de publication
    absent -> au mieux MEDIUM, jamais LOW. Un fournisseur peut documenter un
    risque plus élevé manuellement (ProviderRecord.leakage_risk n'est pas
    recalculé automatiquement, ceci n'est qu'un GARDE-FOU utilisable par les
    tests / le script d'assemblage, pas une contrainte imposée à la donnée)."""
    if history == "UNKNOWN":
        return "UNKNOWN"
    if history == "CURRENT_ONLY" or not has_reported_at_timestamp:
        return "MEDIUM" if history != "CURRENT_ONLY" else "HIGH"
    if history == "FULL_HISTORY":
        return "LOW"
    return "MEDIUM"


def decide_verdict(
    *, coverage_score: str, history: str, leakage_risk: str, cost_category: str,
    commercial_usage: str, integration_complexity: str, historical_reconstruction: str,
    duplicate_of_existing: bool,
) -> str:
    """
    Classification de recherche à 4 niveaux (§59), dérivée des faits — jamais
    du seul prix (§60). Une source dupliquant une information déjà produite
    par Xfoot en interne (§43) ne peut jamais dépasser CONSIDER, quelle que
    soit sa qualité par ailleurs — la question n'est alors plus "est-ce une
    bonne source" mais "apporte-t-elle quelque chose de nouveau".
    """
    if commercial_usage in ("RESTRICTED",):
        return "DO_NOT_USE"
    if history == "UNKNOWN" or leakage_risk == "UNKNOWN" or commercial_usage == "UNKNOWN":
        return "CONSIDER"  # incertitude factuelle -> jamais un verdict engageant
    if duplicate_of_existing:
        return "CONSIDER"
    if history == "CURRENT_ONLY" or leakage_risk == "HIGH" or historical_reconstruction == "NO":
        return "DO_NOT_USE" if leakage_risk == "HIGH" else "CONSIDER"
    if commercial_usage == "LEGAL_REVIEW_REQUIRED":
        return "SHORTLIST" if coverage_score in ("EXCELLENT", "GOOD") else "CONSIDER"

    strong_facts = (
        coverage_score in ("EXCELLENT", "GOOD")
        and history in ("FULL_HISTORY", "PARTIAL_HISTORY")
        and leakage_risk in ("LOW", "MEDIUM")
        and historical_reconstruction in ("YES", "PARTIAL")
        and commercial_usage == "ALLOWED"
    )
    if not strong_facts:
        return "CONSIDER"

    if history == "FULL_HISTORY" and leakage_risk == "LOW" and integration_complexity in ("LOW", "MEDIUM") \
            and historical_reconstruction == "YES" and coverage_score == "EXCELLENT":
        return "RECOMMENDED_FOR_MVP"

    return "SHORTLIST"
