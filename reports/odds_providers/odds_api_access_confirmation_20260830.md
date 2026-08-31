# XFOOT THE ODDS API — HISTORICAL ACCESS CONFIRMATION

## 1. Executive Summary

Run id : `20260830_111154` — généré le 2026-08-30T11:11:54.403192+00:00. RÈGLE : NO PURCHASE, NO PLAN CHANGE, NO HISTORICAL ODDS REQUEST, 0 CREDITS CONSUMED THIS PHASE.

Question : Le plan payant 20K à $30/mois permet-il réellement d'utiliser Historical Odds ?

**Décision : SUPPORT_REQUIRED** (confiance : LOW-MEDIUM)

## 2. Prior Phase Context (8G / 8G.1)


- Phase 8G.1 verdict : NEEDS_REAL_QUERY (reports/odds_providers/odds_api_cost_audit_20260830.json)
- Phase 8G smoke test : reports/odds_providers/odds_api_smoke_test_20260830_104639.json
- Recommended PoC scenario (100 matches, 3 markets, 1 cutoff, kickoff-batched): 1080 credits (~8.1% of 20K quota with +50% margin). Maximal scenario (3 markets x 5 cutoffs): 5400 credits worst-effective, well within 20K quota.

## 3. Documentation Consulted (this session)


- **https://the-odds-api.com/**
  Tableau de tarifs : Starter (gratuit, 500 crédits/mois), 20K ($30/mois, 20 000 crédits/mois), 100K ($59/mois, 100 000 crédits/mois), 5M ($119/mois), 15M ($249/mois). Le libellé "Historical Odds" apparaît IDENTIQUE sur CHAQUE carte de plan, y compris Starter — sans coche différenciée, astérisque, ni mention de prix additionnel visible dans le rendu texte de la page.
  Tier-specific mention found: False

- **https://the-odds-api.com/liveapi/guides/v4/**
  Confirme la formule de coût (10 x marchés x régions pour l'endpoint historique) et la phrase "This endpoint is only available on paid usage plans" — recherche explicite (cette session) de toute mention nommant un tier précis ($30, 20K, 100K, Starter...) en lien avec l'accès historique : AUCUNE trouvée.
  Tier-specific mention found: False

- **https://the-odds-api.com/historical-odds-data/**
  Confirme "Historical data is only available on paid usage plans" et "Historical odds data is only available for paid subscriptions at this time" — recherche explicite (cette session) de toute mention nommant un tier précis, y compris dans une éventuelle FAQ/section de bas de page : AUCUNE trouvée. Aucune mention "commercial use"/"SaaS" trouvée à proximité du texte Historical Odds sur cette page.
  Tier-specific mention found: False

- **web search (the-odds-api.com FAQ / help center)**
  Aucune page FAQ officielle dédiée trouvée. Les seuls résultats pertinents renvoient à la page d'accueil déjà consultée (même tableau ambigu) — aucune source indépendante supplémentaire.
  Tier-specific mention found: False

## 4. Contradiction Identified


**Claim A** (https://the-odds-api.com/ (tableau de tarifs)) : "Historical Odds" listé de façon identique sur CHAQUE plan, y compris Starter (gratuit).

Suggérerait que même le plan Starter gratuit donne accès à Historical Odds.

**Claim B contradicts A** (https://the-odds-api.com/historical-odds-data/ ET the-odds-api.com/liveapi/guides/v4/) : "Historical data is only available on paid usage plans" / "only available for paid subscriptions".

Exclut explicitement le plan Starter gratuit.

**Empirical evidence** (reports/odds_providers/odds_api_smoke_test_20260830_104639.json (Phase 8G, RÉEL)) : Clé Starter (gratuit) : GET /v4/sports -> 200 OK (clé valide) ; GET /v4/historical/.../odds -> 401 Unauthorized, 0 crédit consommé.

L'affirmation A (tableau de tarifs) est FAUSSE pour Starter, empiriquement démontrée par ce dépôt lui-même — donc son libellé identique sur les autres plans ne peut PAS être traité comme une confirmation fiable pour le plan 20K non plus.

**Remaining gap** : Aucune source officielle (documentation technique, page dédiée, ou FAQ trouvée) ne nomme EXPLICITEMENT le tier minimum requis pour Historical Odds. Le tableau de tarifs de la page d'accueil s'est avéré non fiable sur ce point précis (contredit empiriquement pour Starter). Cette question ne peut donc être résolue ni par une lecture supplémentaire de documentation, ni par un nouveau calcul — seule une confirmation du support officiel, ou un appel réel Historical Odds effectué APRÈS souscription au plan 20K, pourrait trancher (les deux hors périmètre de cette phase, §2 du prompt : ne pas acheter, ne pas consommer de crédit).

## 5. Support Question — Prepared, NOT Sent


Status : **DRAFTED_NOT_SENT**. Draft saved at `reports/odds_providers/odds_api_support_message_draft.txt`.

```
Subject: Historical Odds access on the 20K plan — pre-purchase question

Hi,

Before subscribing, I'd like to confirm a few points about Historical Odds access on the 20K credits/month plan ($30/month):

1. Does the $30/month 20K credits plan include access to the Historical Odds API endpoint, including historical snapshots using previous_timestamp and next_timestamp?
2. Is Historical Odds available on the 20K plan?
3. Is there any additional Historical Odds fee beyond the plan's monthly price?
4. Are historical snapshots billed at the standard 10x credit multiplier (10 x markets x regions), same as documented for the historical odds endpoint in general?
5. Does the 20K plan allow commercial SaaS usage for derived betting features (e.g. features computed from historical odds and used inside a paid product), consistent with your general Terms and Conditions on commercial use and no-resale-as-standalone-data-product?

For context: our current Starter (free) key returns 401 Unauthorized on GET /v4/historical/sports/{sport}/odds while GET /v4/sports succeeds normally, which is why we're asking before upgrading.

Thanks in advance for clarifying.

```

## 6. Response Received


None — message not sent this phase, per instructions.

## 7. Confidence Level


LOW-MEDIUM

## 8. Decision


**SUPPORT_REQUIRED**

Trois sources officielles distinctes (page d'accueil, guide technique v4, page dédiée Historical Odds) et une recherche web ciblée pour une FAQ ont été consultées CETTE session : aucune ne nomme explicitement le tier minimum requis pour Historical Odds. La seule affirmation apparemment favorable au plan 20K (le libellé "Historical Odds" listé sur sa carte de tarif) provient de la MÊME page dont le libellé identique sur Starter est empiriquement FAUX (401 réel, Phase 8G) — elle ne peut donc pas être utilisée comme confirmation technique pour le plan 20K non plus (§3 du prompt : ne pas transformer une indication marketing en confirmation technique). Ni CONFIRMED_20K ni CONFIRMED_HIGHER_PLAN_REQUIRED ne sont justifiables avec les preuves actuellement disponibles.

---

PHASE 8G.2 — XFOOT THE ODDS API HISTORICAL ACCESS CONFIRMATION TERMINÉE. AUCUN ACHAT EFFECTUÉ. AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
