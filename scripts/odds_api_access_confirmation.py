"""
scripts/odds_api_access_confirmation.py — Phase 8G.2 : XFOOT THE ODDS API
HISTORICAL ACCESS CONFIRMATION.
=============================================================================
DOCUMENTATION UNIQUEMENT. ZÉRO appel réseau vers The Odds API dans ce script
(ni /v4/sports, ni /v4/historical/..., ni aucun autre endpoint) — ce module
n'importe même pas fetch_sports/fetch_historical_odds_snapshot (aucun risque
d'appel accidentel, même pattern que scripts/odds_api_cost_audit.py).

Objectif UNIQUE (Phase 8G.2) : déterminer si le plan payant 20K ($30/mois)
donne réellement accès à l'endpoint Historical Odds — question laissée
NEEDS_REAL_QUERY par la Phase 8G.1.

Les constats de documentation officielle ci-dessous viennent de WebFetch
exécutés DANS cette session (jamais repris aveuglément d'un rapport
antérieur, §3 du prompt) :
  - https://the-odds-api.com/ (page d'accueil, tableau de tarifs)
  - https://the-odds-api.com/liveapi/guides/v4/ (guide technique — recherché
    explicitement pour toute mention nommant un tier précis)
  - https://the-odds-api.com/historical-odds-data/ (page dédiée Historical
    Odds — recherchée explicitement pour toute mention nommant un tier précis)
  - Recherche web ciblée (aucune page FAQ dédiée officielle trouvée)

Aucune requête Historical Odds payante n'est effectuée. Aucun crédit
consommé. Aucun abonnement acheté ou modifié.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/odds_api_access_confirmation.py
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from sqlmodel import Session  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402

import feature_engineering_walkforward as fewf  # noqa: E402  (réutilise snapshot_db_counts, jamais réimplémenté)

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("odds_api_access_confirmation")

# ---------------------------------------------------------------------------
# §1 : documentation re-vérifiée CETTE session (WebFetch), résumée ici en
# DONNÉES (jamais recalculée par le script — pure fonction de rapport, même
# discipline que api/app/ai/arena/research.py::render_markdown_report).
# ---------------------------------------------------------------------------

DOCS_CONSULTED = [
    {
        "url": "https://the-odds-api.com/",
        "finding": (
            "Tableau de tarifs : Starter (gratuit, 500 crédits/mois), 20K ($30/mois, 20 000 crédits/mois), "
            "100K ($59/mois, 100 000 crédits/mois), 5M ($119/mois), 15M ($249/mois). Le libellé \"Historical Odds\" "
            "apparaît IDENTIQUE sur CHAQUE carte de plan, y compris Starter — sans coche différenciée, astérisque, "
            "ni mention de prix additionnel visible dans le rendu texte de la page."
        ),
        "tier_specific_mention_found": False,
    },
    {
        "url": "https://the-odds-api.com/liveapi/guides/v4/",
        "finding": (
            "Confirme la formule de coût (10 x marchés x régions pour l'endpoint historique) et la phrase "
            "\"This endpoint is only available on paid usage plans\" — recherche explicite (cette session) de toute "
            "mention nommant un tier précis ($30, 20K, 100K, Starter...) en lien avec l'accès historique : "
            "AUCUNE trouvée."
        ),
        "tier_specific_mention_found": False,
    },
    {
        "url": "https://the-odds-api.com/historical-odds-data/",
        "finding": (
            "Confirme \"Historical data is only available on paid usage plans\" et \"Historical odds data is only "
            "available for paid subscriptions at this time\" — recherche explicite (cette session) de toute mention "
            "nommant un tier précis, y compris dans une éventuelle FAQ/section de bas de page : AUCUNE trouvée. "
            "Aucune mention \"commercial use\"/\"SaaS\" trouvée à proximité du texte Historical Odds sur cette page."
        ),
        "tier_specific_mention_found": False,
    },
    {
        "url": "web search (the-odds-api.com FAQ / help center)",
        "finding": (
            "Aucune page FAQ officielle dédiée trouvée. Les seuls résultats pertinents renvoient à la page d'accueil "
            "déjà consultée (même tableau ambigu) — aucune source indépendante supplémentaire."
        ),
        "tier_specific_mention_found": False,
    },
]

CONTRADICTION = {
    "claim_a": {
        "source": "https://the-odds-api.com/ (tableau de tarifs)",
        "text": "\"Historical Odds\" listé de façon identique sur CHAQUE plan, y compris Starter (gratuit).",
        "implication_if_taken_literally": "Suggérerait que même le plan Starter gratuit donne accès à Historical Odds.",
    },
    "claim_b_contradicts_a": {
        "source": "https://the-odds-api.com/historical-odds-data/ ET the-odds-api.com/liveapi/guides/v4/",
        "text": "\"Historical data is only available on paid usage plans\" / \"only available for paid subscriptions\".",
        "implication": "Exclut explicitement le plan Starter gratuit.",
    },
    "empirical_evidence_resolves_a_vs_b": {
        "source": "reports/odds_providers/odds_api_smoke_test_20260830_104639.json (Phase 8G, RÉEL)",
        "text": "Clé Starter (gratuit) : GET /v4/sports -> 200 OK (clé valide) ; GET /v4/historical/.../odds -> 401 Unauthorized, 0 crédit consommé.",
        "conclusion": "L'affirmation A (tableau de tarifs) est FAUSSE pour Starter, empiriquement démontrée par ce dépôt lui-même — donc son libellé identique sur les autres plans ne peut PAS être traité comme une confirmation fiable pour le plan 20K non plus.",
    },
    "remaining_gap": (
        "Aucune source officielle (documentation technique, page dédiée, ou FAQ trouvée) ne nomme EXPLICITEMENT "
        "le tier minimum requis pour Historical Odds. Le tableau de tarifs de la page d'accueil s'est avéré non "
        "fiable sur ce point précis (contredit empiriquement pour Starter). Cette question ne peut donc être "
        "résolue ni par une lecture supplémentaire de documentation, ni par un nouveau calcul — seule une "
        "confirmation du support officiel, ou un appel réel Historical Odds effectué APRÈS souscription au plan "
        "20K, pourrait trancher (les deux hors périmètre de cette phase, §2 du prompt : ne pas acheter, ne pas "
        "consommer de crédit)."
    ),
}

SUPPORT_MESSAGE_DRAFT = """Subject: Historical Odds access on the 20K plan — pre-purchase question

Hi,

Before subscribing, I'd like to confirm a few points about Historical Odds access on the 20K credits/month plan ($30/month):

1. Does the $30/month 20K credits plan include access to the Historical Odds API endpoint, including historical snapshots using previous_timestamp and next_timestamp?
2. Is Historical Odds available on the 20K plan?
3. Is there any additional Historical Odds fee beyond the plan's monthly price?
4. Are historical snapshots billed at the standard 10x credit multiplier (10 x markets x regions), same as documented for the historical odds endpoint in general?
5. Does the 20K plan allow commercial SaaS usage for derived betting features (e.g. features computed from historical odds and used inside a paid product), consistent with your general Terms and Conditions on commercial use and no-resale-as-standalone-data-product?

For context: our current Starter (free) key returns 401 Unauthorized on GET /v4/historical/sports/{sport}/odds while GET /v4/sports succeeds normally, which is why we're asking before upgrading.

Thanks in advance for clarifying.
"""


def snapshot_db_rows(session) -> dict:
    return fewf.snapshot_db_counts(session)


def build_result() -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now(timezone.utc).isoformat()

    decision = "SUPPORT_REQUIRED"
    confidence = "LOW-MEDIUM"
    reason = (
        "Trois sources officielles distinctes (page d'accueil, guide technique v4, page dédiée Historical Odds) et "
        "une recherche web ciblée pour une FAQ ont été consultées CETTE session : aucune ne nomme explicitement le "
        "tier minimum requis pour Historical Odds. La seule affirmation apparemment favorable au plan 20K (le "
        "libellé \"Historical Odds\" listé sur sa carte de tarif) provient de la MÊME page dont le libellé identique "
        "sur Starter est empiriquement FAUX (401 réel, Phase 8G) — elle ne peut donc pas être utilisée comme "
        "confirmation technique pour le plan 20K non plus (§3 du prompt : ne pas transformer une indication "
        "marketing en confirmation technique). Ni CONFIRMED_20K ni CONFIRMED_HIGHER_PLAN_REQUIRED ne sont "
        "justifiables avec les preuves actuellement disponibles."
    )

    return {
        "run_id": run_id, "generated_at": generated_at, "phase": "8G.2", "kind": "historical_access_confirmation",
        "rule": "NO PURCHASE, NO PLAN CHANGE, NO HISTORICAL ODDS REQUEST, 0 CREDITS CONSUMED THIS PHASE.",
        "question": "Le plan payant 20K à $30/mois permet-il réellement d'utiliser Historical Odds ?",
        "prior_phase_context": {
            "phase_8g1_verdict": "NEEDS_REAL_QUERY",
            "phase_8g1_report": "reports/odds_providers/odds_api_cost_audit_20260830.json",
            "phase_8g_smoke_test_report": "reports/odds_providers/odds_api_smoke_test_20260830_104639.json",
            "cost_summary": "Recommended PoC scenario (100 matches, 3 markets, 1 cutoff, kickoff-batched): 1080 credits (~8.1% of 20K quota with +50% margin). Maximal scenario (3 markets x 5 cutoffs): 5400 credits worst-effective, well within 20K quota.",
        },
        "documentation_consulted": DOCS_CONSULTED,
        "contradiction": CONTRADICTION,
        "support_message_status": "DRAFTED_NOT_SENT",
        "support_message_draft": SUPPORT_MESSAGE_DRAFT,
        "support_message_draft_file": "reports/odds_providers/odds_api_support_message_draft.txt",
        "response_received": None,
        "confidence_level": confidence,
        "decision": decision,
        "decision_reason": reason,
    }


def render_markdown(result: dict) -> str:
    md = ["# XFOOT THE ODDS API — HISTORICAL ACCESS CONFIRMATION\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}. RÈGLE : {result['rule']}\n")
    md.append(f"\nQuestion : {result['question']}\n\n**Décision : {result['decision']}** (confiance : {result['confidence_level']})\n")

    md.append("\n## 2. Prior Phase Context (8G / 8G.1)\n\n")
    ctx = result["prior_phase_context"]
    md.append(f"\n- Phase 8G.1 verdict : {ctx['phase_8g1_verdict']} ({ctx['phase_8g1_report']})\n"
               f"- Phase 8G smoke test : {ctx['phase_8g_smoke_test_report']}\n- {ctx['cost_summary']}\n")

    md.append("\n## 3. Documentation Consulted (this session)\n\n")
    for d in result["documentation_consulted"]:
        md.append(f"\n- **{d['url']}**\n  {d['finding']}\n  Tier-specific mention found: {d['tier_specific_mention_found']}\n")

    md.append("\n## 4. Contradiction Identified\n\n")
    c = result["contradiction"]
    md.append(f"\n**Claim A** ({c['claim_a']['source']}) : {c['claim_a']['text']}\n\n{c['claim_a']['implication_if_taken_literally']}\n")
    md.append(f"\n**Claim B contradicts A** ({c['claim_b_contradicts_a']['source']}) : {c['claim_b_contradicts_a']['text']}\n\n{c['claim_b_contradicts_a']['implication']}\n")
    e = c["empirical_evidence_resolves_a_vs_b"]
    md.append(f"\n**Empirical evidence** ({e['source']}) : {e['text']}\n\n{e['conclusion']}\n")
    md.append(f"\n**Remaining gap** : {c['remaining_gap']}\n")

    md.append("\n## 5. Support Question — Prepared, NOT Sent\n\n")
    md.append(f"\nStatus : **{result['support_message_status']}**. Draft saved at `{result['support_message_draft_file']}`.\n\n```\n{result['support_message_draft']}\n```\n")

    md.append("\n## 6. Response Received\n\n")
    md.append(f"\n{result['response_received'] or 'None — message not sent this phase, per instructions.'}\n")

    md.append("\n## 7. Confidence Level\n\n")
    md.append(f"\n{result['confidence_level']}\n")

    md.append("\n## 8. Decision\n\n")
    md.append(f"\n**{result['decision']}**\n\n{result['decision_reason']}\n")

    md.append("\n---\n\nPHASE 8G.2 — XFOOT THE ODDS API HISTORICAL ACCESS CONFIRMATION TERMINÉE. "
               "AUCUN ACHAT EFFECTUÉ. AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def main() -> dict:
    init_db()
    with Session(engine) as session:
        db_before = snapshot_db_rows(session)

    logger.info("Aucun appel réseau vers The Odds API dans ce script — analyse documentaire uniquement (déjà vérifiée cette session).")
    result = build_result()

    with Session(engine) as session:
        db_after = snapshot_db_rows(session)
    result["db_safety"] = {"before": db_before, "after": db_after, "unchanged": db_before == db_after}

    git_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True,
    ).stdout
    result["git_status_porcelain"] = git_status

    outdir = Path(__file__).resolve().parent.parent / "reports" / "odds_providers"
    outdir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    draft_path = outdir / "odds_api_support_message_draft.txt"
    draft_path.write_text(SUPPORT_MESSAGE_DRAFT, encoding="utf-8")

    json_path = outdir / f"odds_api_access_confirmation_{date_str}.json"
    md_path = outdir / f"odds_api_access_confirmation_{date_str}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    logger.info("Rapports écrits : %s / %s / %s", json_path, md_path, draft_path)

    print("\n" + "=" * 80)
    print(f"Décision : {result['decision']} (confiance : {result['confidence_level']})")
    print("Production inchangée. DB inchangée. 0 crédit Historical Odds consommé. Aucun abonnement acheté.")
    print("\ngit status --porcelain :")
    print(result["git_status_porcelain"] or "(clean)")
    print("PHASE 8G.2 — XFOOT THE ODDS API HISTORICAL ACCESS CONFIRMATION TERMINÉE. "
          "AUCUN ACHAT EFFECTUÉ. AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


if __name__ == "__main__":
    main()
    sys.exit(0)
