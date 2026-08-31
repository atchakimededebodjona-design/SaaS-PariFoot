# XFOOT THE ODDS API — COST & CREDIT FEASIBILITY AUDIT

## 1. Executive Summary

Run id : `20260830_110556` — généré le 2026-08-30T11:05:56.258322+00:00. RÈGLE : NO PURCHASE, NO PLAN CHANGE, NO HISTORICAL ODDS REQUEST, NO NEW SMOKE TEST, 0 CREDITS CONSUMED THIS PHASE.

**Décision finale : NEEDS_REAL_QUERY**

Le calcul de coût lui-même est FAVORABLE : le scénario recommandé (100 matchs, 3 marchés, 1 cutoff, regroupement par kickoff réel appliqué) nécessite 1080 crédits, soit 8.1% du quota mensuel du plan 20K même avec +50% de marge — largement soutenable. MAIS la documentation officielle ne confirme PAS explicitement que le plan d'entrée de gamme (20K, $30/mois) inclut réellement l'accès Historical Odds (la page dédiée dit seulement 'paid usage plans' sans nommer le tier minimum, et la page d'accueil liste 'Historical Odds' de façon identique sur CHAQUE plan y compris Starter — ce qui contredit notre propre 401 empirique sur la clé Starter actuelle). Cette phase interdit explicitement toute nouvelle requête réseau pour lever cette inconnue. Recommandation : si un achat est envisagé, vérifier ce point PRÉCIS (contact support The Odds API ou un premier appel historique réel sur le plan 20K une fois souscrit) avant tout usage en profondeur — le coût n'est PAS le facteur bloquant, l'ACCÈS au tier minimum l'est potentiellement.

## 2. Current Account Status


Preuve issue du smoke test Phase 8G (reports/odds_providers/odds_api_smoke_test_20260830_104639.json) : /v4/sports -> 200, historical -> 401, crédits consommés ce run-là : 0. Starter (free) tier key: standard API accessible, Historical Odds endpoint returns 401 (access not included in current plan).

## 3. Official Pricing

| Plan | Price/month (USD) | Credits/month | Historical Odds (label on pricing page) |
|---|---|---|---|
| Starter | 0 | 500 | NO (empirically confirmed — see below) |
| 20K | 30 | 20,000 | UNKNOWN (see Historical Odds Pricing section) |
| 100K | 59 | 100,000 | UNKNOWN (see Historical Odds Pricing section) |
| 5M | 119 | 5,000,000 | UNKNOWN (see Historical Odds Pricing section) |
| 15M | 249 | 15,000,000 | UNKNOWN (see Historical Odds Pricing section) |

Entry-level PAID plan ("premier plan payant") : **20K**.

## 4. Historical Odds Pricing


- Live formula : `cost = markets x regions`
- Historical formula : `cost = 10 x markets x regions`
- Source : https://the-odds-api.com/liveapi/guides/v4/ (verified via WebFetch this session)
- Minimum tier confirmed : UNKNOWN — official docs state 'Historical data is only available on paid usage plans' without naming the minimum tier explicitly; homepage lists 'Historical Odds' as a feature label on every plan card (Starter included), which CONTRADICTS the dedicated historical-odds-data page AND our own empirical 401 on the Starter/free key used in the prior smoke test. Treating the dedicated page + empirical evidence as authoritative: Starter = NO. First paid tier (20K, $30/mo) = UNKNOWN, not contradicted by any source, but not explicitly confirmed either.

## 5. Credit Formula


`credits = 10 x markets x regions`, PER REQUEST — verified via official docs this session, NOT reused blindly from a prior report.

## 6. Xfoot Dataset


- Total matches (all leagues) : 12459
- Earliest : 2019-08-09T00:00:00 — Latest : 2026-05-24T00:00:00

| League | N matches | Earliest | Latest | Historical floor | Covered | Not covered | Coverage % |
|---|---|---|---|---|---|---|---|
| PremierLeague | 2660 | 2019-08-09T00:00:00 | 2026-05-24T00:00:00 | 2020-06-06T10:05:00+00:00 | 2372 | 288 | 89.17 |
| Ligue1 | 2337 | 2019-08-09T00:00:00 | 2026-05-17T00:00:00 | 2020-07-16T00:55:00+00:00 | 2058 | 279 | 88.06 |
| Bundesliga | 2142 | 2019-08-16T00:00:00 | 2026-05-16T00:00:00 | 2020-06-06T10:05:00+00:00 | 1875 | 267 | 87.54 |
| SerieA | 2660 | 2019-08-24T00:00:00 | 2026-05-24T00:00:00 | 2020-06-06T10:05:00+00:00 | 2404 | 256 | 90.38 |
| LaLiga | 2660 | 2019-08-16T00:00:00 | 2026-05-24T00:00:00 | 2020-06-06T10:05:00+00:00 | 2390 | 270 | 89.85 |

## 7. PoC Dataset


N = 100 (max 20/ligue) — sélection réutilisée telle quelle de app.ai.odds_research.odds_api_trial.select_trial_matches (identique à scripts/odds_api_trial.py, aucune resélection).

| League | Selected | Earliest | Latest | Real kickoff (cache) | Date-only fallback | Covered (>=floor) |
|---|---|---|---|---|---|---|
| Bundesliga | 20 | 2026-05-03 | 2026-05-16 | 20 | 0 | 20/20 |
| LaLiga | 20 | 2026-05-17 | 2026-05-24 | 20 | 0 | 20/20 |
| Ligue1 | 20 | 2026-05-08 | 2026-05-17 | 20 | 0 | 20/20 |
| PremierLeague | 20 | 2026-05-15 | 2026-05-24 | 20 | 0 | 20/20 |
| SerieA | 20 | 2026-05-17 | 2026-05-24 | 20 | 0 | 20/20 |

## 8. League Coverage

| League | Available | Historical | Market coverage | Confidence |
|---|---|---|---|---|
| PremierLeague | YES (confirmed Phase 8F/8G — SPORT_KEYS mapping + smoke test /v4/sports 200 for SerieA; other 4 not re-verified this phase per no-new-network-call rule) | 2020-06-06T10:05:00+00:00 | UNKNOWN / NEEDS_REAL_QUERY (1X2/h2h presumed available per general Odds API market catalogue; BTTS/O-U 2.5 historical availability NOT confirmed by documentation fetched this session) | MEDIUM (availability + floor date from official docs; market-level and real-time coverage not empirically verified for this phase) |
| Ligue1 | YES (confirmed Phase 8F/8G — SPORT_KEYS mapping + smoke test /v4/sports 200 for SerieA; other 4 not re-verified this phase per no-new-network-call rule) | 2020-07-16T00:55:00+00:00 | UNKNOWN / NEEDS_REAL_QUERY (1X2/h2h presumed available per general Odds API market catalogue; BTTS/O-U 2.5 historical availability NOT confirmed by documentation fetched this session) | MEDIUM (availability + floor date from official docs; market-level and real-time coverage not empirically verified for this phase) |
| Bundesliga | YES (confirmed Phase 8F/8G — SPORT_KEYS mapping + smoke test /v4/sports 200 for SerieA; other 4 not re-verified this phase per no-new-network-call rule) | 2020-06-06T10:05:00+00:00 | UNKNOWN / NEEDS_REAL_QUERY (1X2/h2h presumed available per general Odds API market catalogue; BTTS/O-U 2.5 historical availability NOT confirmed by documentation fetched this session) | MEDIUM (availability + floor date from official docs; market-level and real-time coverage not empirically verified for this phase) |
| SerieA | YES (confirmed Phase 8F/8G — SPORT_KEYS mapping + smoke test /v4/sports 200 for SerieA; other 4 not re-verified this phase per no-new-network-call rule) | 2020-06-06T10:05:00+00:00 | UNKNOWN / NEEDS_REAL_QUERY (1X2/h2h presumed available per general Odds API market catalogue; BTTS/O-U 2.5 historical availability NOT confirmed by documentation fetched this session) | MEDIUM (availability + floor date from official docs; market-level and real-time coverage not empirically verified for this phase) |
| LaLiga | YES (confirmed Phase 8F/8G — SPORT_KEYS mapping + smoke test /v4/sports 200 for SerieA; other 4 not re-verified this phase per no-new-network-call rule) | 2020-06-06T10:05:00+00:00 | UNKNOWN / NEEDS_REAL_QUERY (1X2/h2h presumed available per general Odds API market catalogue; BTTS/O-U 2.5 historical availability NOT confirmed by documentation fetched this session) | MEDIUM (availability + floor date from official docs; market-level and real-time coverage not empirically verified for this phase) |

## 9. Market Coverage


Markets tested in this cost model : ('1X2', 'BTTS', 'OU25'). 1X2/h2h presence is standard across the provider's catalogue (not historically league-specific per docs fetched). BTTS and Over/Under 2.5 historical availability is **UNKNOWN / NEEDS_REAL_QUERY** — no official documentation confirming per-market historical coverage was found this session, and no query was made to verify (forbidden this phase). Cost model bills per REQUESTED market key regardless of actual per-bookmaker availability (standard Odds API billing behavior), so the credit numbers below are valid upper-bound cost estimates even if some bookmakers don't return every market.

## 10. Minimal Scenario


100 matchs (échantillon PoC réel), 1X2 uniquement, 1 région (eu), 1 cutoff (T-6h, représentatif)

- Requests (worst case, no batching) : 100
- Requests (after real-kickoff batching) : 36
- Credits (worst case) : 1000
- Credits (after batching) : 360

## 11. Recommended Scenario


100 matchs (échantillon PoC réel), 1X2 + BTTS(si dispo) + O/U2.5(si dispo), 1 région (eu), 1 cutoff (T-6h)

- Markets : 3
- Requests (worst case) : 100 — Requests (after batching) : 36
- Credits (worst case) : 3000
- **Credits (after batching) : 1080**

## 12. Maximum Reasonable Scenario


100 matchs (échantillon PoC réel), 3 marchés, 5 cutoffs (T-24h/12h/6h/3h/1h), 1 région (eu)

- Credits (worst case, no batching) : 15000
- Credits (after real-kickoff batching) : 5400

## 13. Safety Margins


Applied on top of the RECOMMENDED scenario (batched) :

| Margin | Credits needed | % of entry-plan (20K) monthly quota |
|---|---|---|
| +10% | 1188 | 5.94% |
| +25% | 1350 | 6.75% |
| +50% | 1620 | 8.1% |

## 14. Historical Depth


Per-league floors confirmed officially (WebFetch, this session) : {'PremierLeague': '2020-06-06T10:05:00+00:00', 'Bundesliga': '2020-06-06T10:05:00+00:00', 'SerieA': '2020-06-06T10:05:00+00:00', 'LaLiga': '2020-06-06T10:05:00+00:00', 'Ligue1': '2020-07-16T00:55:00+00:00'}
Compared against Xfoot's own match history (section 6 above) — coverage percentages shown per league; never claimed as 100% without the computed figure.

## 15. Commercial Conditions


- **commercial_saas_use** : ALLOWED — 'use of our data in websites, mobile apps, dashboards, analytical tools, and other user-facing applications, including commercial use' (terms-and-conditions.html, verified this session).

- **redistribution** : PROHIBITED as a standalone data product (own API/data feed/downloadable files) — does not restrict internal derived features/ML use per the fetched clause.

- **storage_caching** : Not explicitly addressed — no stated restriction on operational retention found in the fetched terms.

- **research_vs_commercial_distinction** : NONE STATED — same restriction (no resale as standalone product) applies regardless of research/production context.

- **verdict** : ALLOWED for Xfoot's intended use (internal feature derivation + display in the product), subject to never reselling raw odds as a standalone feed. LEGAL_REVIEW_REQUIRED only if Xfoot later considers redistributing raw odds directly.

## 16. Risks


- Minimum paid tier actually granting Historical Odds access is NOT explicitly confirmed by official docs (homepage listing contradicts the dedicated Historical Odds page and our own empirical 401 on the Starter key).
- BTTS / Over-Under 2.5 historical market availability is unconfirmed — recommended-scenario cost assumes all 3 markets are requestable, which may overstate cost if some are unsupported historically (billing would then simply fail/return less data for that market, not overcharge, but this hasn't been verified).
- Request-batching-by-shared-kickoff figures depend on football-data.co.uk cache coverage, which is itself incomplete (some PoC matches remain DATE_ONLY) — real achievable batching could differ from this estimate.

## 17. Unknowns


- Exact minimum paid tier for Historical Odds access : UNKNOWN.
- BTTS / O-U 2.5 historical market availability per league : UNKNOWN / NEEDS_REAL_QUERY.
- Whether /v4/sports lists all 5 priority leagues (only SerieA re-verified in the Phase 8G smoke test) : ASSUMED YES per Phase 8F/8G SPORT_KEYS documentation, not re-verified this phase (no new network calls allowed).

## 18. Recommendation


**NEEDS_REAL_QUERY**

Le calcul de coût lui-même est FAVORABLE : le scénario recommandé (100 matchs, 3 marchés, 1 cutoff, regroupement par kickoff réel appliqué) nécessite 1080 crédits, soit 8.1% du quota mensuel du plan 20K même avec +50% de marge — largement soutenable. MAIS la documentation officielle ne confirme PAS explicitement que le plan d'entrée de gamme (20K, $30/mois) inclut réellement l'accès Historical Odds (la page dédiée dit seulement 'paid usage plans' sans nommer le tier minimum, et la page d'accueil liste 'Historical Odds' de façon identique sur CHAQUE plan y compris Starter — ce qui contredit notre propre 401 empirique sur la clé Starter actuelle). Cette phase interdit explicitement toute nouvelle requête réseau pour lever cette inconnue. Recommandation : si un achat est envisagé, vérifier ce point PRÉCIS (contact support The Odds API ou un premier appel historique réel sur le plan 20K une fois souscrit) avant tout usage en profondeur — le coût n'est PAS le facteur bloquant, l'ACCÈS au tier minimum l'est potentiellement.

---

### SCORECARD

| Criterion | Result | Evidence | Verdict |
|---|---|---|---|
| Historical access | 401 on current Starter/free key | Phase 8G smoke test 20260830_104639 | BLOCKED on current plan |
| Credit cost | 1080 credits for recommended PoC scenario | This audit, official formula | LOW (well within entry-paid-plan quota) |
| 1X2 | Standard market, cost = 10 credits/request/region | Official pricing docs | CONFIRMED |
| BTTS | Historical availability unconfirmed | No official doc found this session | UNKNOWN |
| O/U 2.5 | Historical availability unconfirmed | No official doc found this session | UNKNOWN |
| League coverage | 5/5 leagues have a confirmed historical floor date | the-odds-api.com/historical-odds-data/ | CONFIRMED |
| Historical depth | 2020-06-06 (4 leagues) / 2020-07-16 (Ligue1) vs Xfoot data back to 2019 | This audit (section 6/14) | PARTIAL (see coverage %) |
| Commercial use | Allowed, no resale as standalone product | terms-and-conditions.html | ALLOWED |
| PoC affordability | 8.1% of 20K plan quota even at +50% margin | This audit | AFFORDABLE |

---

### DATABASE SAFETY


Before : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

After : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Unchanged : **True**

---

PHASE 8G.1 — XFOOT THE ODDS API COST & CREDIT FEASIBILITY AUDIT TERMINÉE. AUCUN ACHAT EFFECTUÉ. AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
