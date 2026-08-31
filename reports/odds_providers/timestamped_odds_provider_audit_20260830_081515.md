# XFOOT TIMESTAMPED ODDS PROVIDER DISCOVERY V1

## 1. Executive Summary

Run id : `20260830_081515` — généré le 2026-08-30T08:15:15.024726+00:00. Recherche effectuée le 2026-08-30.

RÈGLE ABSOLUE : RECHERCHE UNIQUEMENT. Aucun achat, aucune clé API, aucune intégration.

- 11 fournisseurs évalués.
- Verdicts : {'SHORTLIST': 2, 'CONSIDER': 7, 'DO_NOT_USE': 2}

## 2. Why Phase 8E Failed


football-data.co.uk (source Phase 8D) ne fournit AUCUN timestamp mesuré par observation de cote — seule une méthodologie de collecte documentée (jamais un instant précis) existe. Résultat Phase 8E : 100% des observations HISTORICAL_BUT_UNTIMESTAMPED, zéro TEMPORALLY_VERIFIED, quel que soit le cutoff testé. Cette phase cherche une source qui ÉVITE structurellement ce problème.

## 3. Requirements


Fournisseur idéal (§objectif) : historical odds + timestamped snapshots + bookmaker identification + market identification + league coverage + commercially usable data + historical reconstruction. Critère n°1 (§69, jamais négociable) : `can_reconstruct_snapshot`.

## 4. Providers Researched

- **The Odds API — Historical Sports Odds API** — can_reconstruct_snapshot=YES, snapshot_model=TRUE_SNAPSHOT_HISTORY, verdict=SHORTLIST
- **Betfair Historical Data Service** — can_reconstruct_snapshot=YES, snapshot_model=TRUE_SNAPSHOT_HISTORY, verdict=SHORTLIST
- **OpticOdds (marque sœur d'OddsJam)** — can_reconstruct_snapshot=PARTIAL, snapshot_model=TRUE_SNAPSHOT_HISTORY, verdict=CONSIDER
- **Sportmonks — Premium Odds Feed (historique)** — can_reconstruct_snapshot=PARTIAL, snapshot_model=TIMESTAMPED_HISTORICAL, verdict=CONSIDER
- **Sportradar — Odds Comparison API** — can_reconstruct_snapshot=NO, snapshot_model=CURRENT_ONLY, verdict=CONSIDER
- **Stats Perform / Opta — Bet Trading Data** — can_reconstruct_snapshot=NO, snapshot_model=UNKNOWN, verdict=CONSIDER
- **BetsAPI — Event Odds** — can_reconstruct_snapshot=NO, snapshot_model=CURRENT_ONLY, verdict=DO_NOT_USE
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** — can_reconstruct_snapshot=NO, snapshot_model=CURRENT_ONLY, verdict=CONSIDER
- **Smarkets / Matchbook (exchanges alternatifs)** — can_reconstruct_snapshot=NO, snapshot_model=UNKNOWN, verdict=CONSIDER
- **football-data.co.uk (référence — Phase 8D/8E)** — can_reconstruct_snapshot=NO, snapshot_model=HISTORICAL_UNTIMESTAMPED, verdict=DO_NOT_USE
- **Pinnacle (API publique)** — can_reconstruct_snapshot=UNKNOWN, snapshot_model=UNKNOWN, verdict=CONSIDER

## 5. Snapshot History

| Provider | Snapshot Model |
|---|---|
| The Odds API — Historical Sports Odds API | TRUE_SNAPSHOT_HISTORY |
| Betfair Historical Data Service | TRUE_SNAPSHOT_HISTORY |
| OpticOdds (marque sœur d'OddsJam) | TRUE_SNAPSHOT_HISTORY |
| Sportmonks — Premium Odds Feed (historique) | TIMESTAMPED_HISTORICAL |
| Sportradar — Odds Comparison API | CURRENT_ONLY |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN |
| BetsAPI — Event Odds | CURRENT_ONLY |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | CURRENT_ONLY |
| Smarkets / Matchbook (exchanges alternatifs) | UNKNOWN |
| football-data.co.uk (référence — Phase 8D/8E) | HISTORICAL_UNTIMESTAMPED |
| Pinnacle (API publique) | UNKNOWN |

## 6. Timestamp Quality

| Provider | Granularity | Origin | Semantics |
|---|---|---|---|
| The Odds API — Historical Sports Odds API | SECOND | UNKNOWN | Deux champs distincts confirmés : `timestamp` (niveau réponse) = capture la plus proche <= à la date demandée, aligné su... |
| Betfair Historical Data Service | SECOND | BOOKMAKER_TIMESTAMP | Champ `pt` ('Publish Time') confirmé officiellement (schéma ESASwaggerSchema.json) : 'the number of milliseconds since 1... |
| OpticOdds (marque sœur d'OddsJam) | SECOND | UNKNOWN | Format ISO 8601 confirmé (api-faq officielle). Endpoint historique documenté comme retournant 'un tableau de chaque chan... |
| Sportmonks — Premium Odds Feed (historique) | SECOND | BOOKMAKER_TIMESTAMP | Champ `bookmaker_update` confirmé officiellement : 'the timestamp of the bookmakers latest update' — c'est un vrai BOOKM... |
| Sportradar — Odds Comparison API | UNKNOWN | UNKNOWN | Le mécanisme le plus proche d'un historique est le 'Sport Event [Markets] Change Log', documenté officiellement comme re... |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN | N/A | N/A — ce produit fournit des DONNÉES ÉVÉNEMENTIELLES (tirs, passes, xG) utilisées par des bookmakers tiers pour FABRIQUE... |
| BetsAPI — Event Odds | UNKNOWN | PROVIDER_INGESTION_TIMESTAMP | Seul champ temporel documenté : `odds_update` = 'the last time we checked the market (will be gone after the event is fi... |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | UNKNOWN | PROVIDER_INGESTION_TIMESTAMP | JsonOdds (le plus documenté du lot) expose un champ `LastUpdated` = 'the last time these odds were updated' — fraîcheur ... |
| Smarkets / Matchbook (exchanges alternatifs) | UNKNOWN | N/A | Aucun produit d'archive historique identifié chez l'un ou l'autre — seules des API de trading EN DIRECT (order book, pla... |
| football-data.co.uk (référence — Phase 8D/8E) | DATE_ONLY | N/A | AUCUN timestamp par observation de cote — confirmé exhaustivement en Phase 8E (seule colonne temporelle = heure de coup ... |
| Pinnacle (API publique) | UNKNOWN | UNKNOWN | N/A — API publique fermée.... |

## 7. Timestamp Semantics


Distinction BOOKMAKER_TIMESTAMP vs PROVIDER_INGESTION_TIMESTAMP appliquée systématiquement (§14) — voir §6. Seuls Betfair (`pt`) et Sportmonks (`bookmaker_update`) ont un timestamp bookmaker CONFIRMÉ officiellement. The Odds API (`last_update`) et OpticOdds : origine non confirmée explicitement (UNKNOWN).

## 8. Cutoff Reconstruction

| Provider | T-24h | T-12h | T-6h | T-3h | T-1h |
|---|---|---|---|---|---|
| The Odds API — Historical Sports Odds API | YES | YES | YES | YES | YES |
| Betfair Historical Data Service | YES | YES | YES | YES | PARTIAL |
| OpticOdds (marque sœur d'OddsJam) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Sportmonks — Premium Odds Feed (historique) | NO | NO | NO | NO | NO |
| Sportradar — Odds Comparison API | NO | NO | NO | NO | NO |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| BetsAPI — Event Odds | NO | NO | NO | NO | NO |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Smarkets / Matchbook (exchanges alternatifs) | NO | NO | NO | NO | NO |
| football-data.co.uk (référence — Phase 8D/8E) | NO | NO | NO | NO | NO |
| Pinnacle (API publique) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |

## 9. Opening Odds

- **The Odds API — Historical Sports Odds API** : Non explicitement défini comme 'opening bookmaker' — dépend du 1er snapshot disponible depuis l'activation de la couverture (par ligue).
- **Betfair Historical Data Service** : Premier prix échangé disponible dans l'historique (dépend du tier : Basic=granularité 1 min).
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN / NEEDS CONFIRMATION.
- **Sportmonks — Premium Odds Feed (historique)** : Première valeur dans la fenêtre de rétention de 7 jours — pas nécessairement l'ouverture réelle du marché si celle-ci a eu lieu plus de 7 jours avant le match.
- **Sportradar — Odds Comparison API** : N/A — pas de notion d'archive historique dans ce produit.
- **Stats Perform / Opta — Bet Trading Data** : N/A
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : N/A
- **football-data.co.uk (référence — Phase 8D/8E)** : Première valeur persistée, PAS une ouverture de marché prouvée (Phase 8E, §9).
- **Pinnacle (API publique)** : UNKNOWN.

## 10. Closing Odds

- **The Odds API — Historical Sports Odds API** : Dernier snapshot avant le début effectif de l'événement (paramètre `date` <= kickoff).
- **Betfair Historical Data Service** : Dernier prix échangé avant le début de l'événement, disponible à tous les tiers.
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN / NEEDS CONFIRMATION.
- **Sportmonks — Premium Odds Feed (historique)** : Dernière valeur avant/juste après le coup d'envoi, dans la même fenêtre de 7 jours.
- **Sportradar — Odds Comparison API** : N/A
- **Stats Perform / Opta — Bet Trading Data** : N/A
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : N/A
- **football-data.co.uk (référence — Phase 8D/8E)** : Valeur étiquetée 'C' par la source, sans timestamp mesuré (Phase 8E, §10).
- **Pinnacle (API publique)** : UNKNOWN.

## 11. Odds Movement

| Provider | Movement |
|---|---|
| The Odds API — Historical Sports Odds API | MOVEMENT_AVAILABLE |
| Betfair Historical Data Service | MOVEMENT_AVAILABLE |
| OpticOdds (marque sœur d'OddsJam) | MOVEMENT_AVAILABLE |
| Sportmonks — Premium Odds Feed (historique) | MOVEMENT_AVAILABLE |
| Sportradar — Odds Comparison API | UNKNOWN |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN |
| BetsAPI — Event Odds | MOVEMENT_NOT_AVAILABLE |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | UNKNOWN |
| Smarkets / Matchbook (exchanges alternatifs) | UNKNOWN |
| football-data.co.uk (référence — Phase 8D/8E) | MOVEMENT_NOT_AVAILABLE |
| Pinnacle (API publique) | UNKNOWN |

## 12. Market Consensus

- **The Odds API — Historical Sports Odds API** : Architecture point-in-time (paramètre `date` -> snapshot le plus proche <=, navigation previous_timestamp/next_timestamp) : permet de sélectionner uniquement les bookmakers dont last_update <= cutoff avant de calculer un consensus — exactement le modèle SAFE CONSENSUS de Phase 8E, reconstructible.
- **Betfair Historical Data Service** : N/A — Betfair est un exchange (prix unique déterminé par l'offre/demande des parieurs), pas un agrégat multi-bookmaker. Pas de 'consensus' au sens Phase 8E, mais un signal de marché différent, potentiellement complémentaire.
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN — architecture par changement de prix individuel suggère que c'est possible en théorie, mais non confirmé officiellement.
- **Sportmonks — Premium Odds Feed (historique)** : Techniquement reconstructible (bookmaker_update par observation) mais UNIQUEMENT dans la fenêtre de 7 jours — inutile pour un cutoff théorique sur un match de plusieurs mois/années dans le passé.
- **Sportradar — Odds Comparison API** : N/A pour un usage historique (le produit n'est pas conçu pour du backtesting).
- **Stats Perform / Opta — Bet Trading Data** : N/A — hors périmètre (pas un produit de cotes).
- **BetsAPI — Event Odds** : N/A — aucun historique disponible pour reconstruire quoi que ce soit après le match.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : N/A.
- **football-data.co.uk (référence — Phase 8D/8E)** : Colonne Avg pré-calculée par la source, provenance temporelle opaque — jamais un consensus SAFE reconstructible (Phase 8E, §13).
- **Pinnacle (API publique)** : N/A.

## 13. League Coverage

| Provider | Bundesliga | Ligue1 | PremierLeague | SerieA | LaLiga | ChampionsLeague | ConferenceLeague | EuropaLeague | MLS | PrimeiraLiga | SaudiProLeague |
|---|---|---|---|---|---|---|---|---|---|---|---|
| The Odds API — Historical Sports Odds API | FULL | FULL | FULL | FULL | FULL | FULL | PARTIAL | FULL | FULL | FULL | PARTIAL |
| Betfair Historical Data Service | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| OpticOdds (marque sœur d'OddsJam) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Sportmonks — Premium Odds Feed (historique) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Sportradar — Odds Comparison API | FULL | FULL | FULL | FULL | FULL | UNKNOWN | UNKNOWN | UNKNOWN | FULL | UNKNOWN | UNKNOWN |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| BetsAPI — Event Odds | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Smarkets / Matchbook (exchanges alternatifs) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| football-data.co.uk (référence — Phase 8D/8E) | FULL | FULL | FULL | FULL | FULL | NONE | NONE | NONE | FULL | UNKNOWN | NONE |
| Pinnacle (API publique) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |

## 14. Historical Depth

| Provider | First Year | Last Year | Notes |
|---|---|---|---|
| The Odds API — Historical Sports Odds API | 2020 | 2026 | PAS uniforme : table officielle 'Earliest Historical Timestamps' par ligue. 5 grands championnats + CL/EL/MLS/Portugal : depuis 2020-06 à 2020-07. Con |
| Betfair Historical Data Service | 2016 | 2026 | Fichiers de mapping événement->compétition confirmés officiellement 'from 2018-2022 only for Soccer, Tennis, Cricket, Golf, and Other Sports' (fournis |
| OpticOdds (marque sœur d'OddsJam) | N/A | N/A | Page marketing revendique 'plusieurs années d'historique de prix complet pour les ligues majeures et bookmakers de niveau 1' — AUCUN chiffre précis en |
| Sportmonks — Premium Odds Feed (historique) | N/A | N/A | CONFIRMÉ INSUFFISANT pour Xfoot : rétention de 7 jours après le match SEULEMENT (deux sources officielles concordantes) — pas une archive pluriannuell |
| Sportradar — Odds Comparison API | N/A | N/A | Fenêtre de change-log <= 24h confirmée (changelog officiel, extension de 5min à 24h+ notée à partir de juin 2023 pour certains endpoints) — structurel |
| Stats Perform / Opta — Bet Trading Data | N/A | N/A | N/A — hors périmètre. |
| BetsAPI — Event Odds | N/A | N/A | AUCUNE — le seul timestamp disponible disparaît à la fin du match, confirmé officiellement. |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | N/A | N/A | Aucune preuve officielle d'un entrepôt historique horodaté trouvée pour aucun des fournisseurs de cette catégorie explorés. |
| Smarkets / Matchbook (exchanges alternatifs) | N/A | N/A | Aucun produit d'archive historique trouvé (Smarkets : docs.smarkets.com, help.smarkets.com — API trading live uniquement. Matchbook : developers.match |
| football-data.co.uk (référence — Phase 8D/8E) | 1993 | 2026 | FULL_HISTORY réelle (décennies) mais SANS AUCUN timestamp exploitable — confirmé Phase 8D/8E. |
| Pinnacle (API publique) | N/A | N/A | Non applicable — accès public fermé depuis le 23 juillet 2025 (Phase 8C). |

## 15. Match IDs

- **The Odds API — Historical Sports Odds API** : Clés de sport stables (ex. soccer_epl, soccer_germany_bundesliga) ; eventId par match pour navigation ciblée.
- **Betfair Historical Data Service** : IDs d'événement/marché Betfair stables (marketId/selectionId), mais mapping compétition->ligue Xfoot non vérifié.
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN / NEEDS CONFIRMATION.
- **Sportmonks — Premium Odds Feed (historique)** : IDs Sportmonks stables (fixture_id, league_id, season_id) — Phase 8C.
- **Sportradar — Odds Comparison API** : IDs Sportradar stables (Sport ID 1 = Soccer, mapping documenté) — mais non pertinent sans historique.
- **Stats Perform / Opta — Bet Trading Data** : UNKNOWN.
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : UNKNOWN.
- **football-data.co.uk (référence — Phase 8D/8E)** : Noms d'équipe seulement (mais identiques à la convention Xfoot, voir Phase 8D).
- **Pinnacle (API publique)** : UNKNOWN.

## 16. Season IDs


Voir §15 — la plupart des fournisseurs modernes (The Odds API, Sportmonks) exposent des clés stables par ligue/événement. Betfair : IDs de marché/sélection stables, mapping compétition non confirmé.

## 17. API Access

| Provider | Access Model | Integration Complexity |
|---|---|---|
| The Odds API — Historical Sports Odds API | API_QUERY | LOW |
| Betfair Historical Data Service | BULK_ARCHIVE | HIGH |
| OpticOdds (marque sœur d'OddsJam) | API_QUERY | MEDIUM |
| Sportmonks — Premium Odds Feed (historique) | API_QUERY | MEDIUM |
| Sportradar — Odds Comparison API | API_QUERY | HIGH |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN | HIGH |
| BetsAPI — Event Odds | API_QUERY | MEDIUM |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | API_QUERY | MEDIUM |
| Smarkets / Matchbook (exchanges alternatifs) | UNKNOWN | HIGH |
| football-data.co.uk (référence — Phase 8D/8E) | BULK_ARCHIVE | LOW |
| Pinnacle (API publique) | UNKNOWN | HIGH |

## 18. Rate Limits

- **The Odds API — Historical Sports Odds API** : Système de crédits (pas de req/min documenté séparément) ; coût historique = 10 crédits/région/marché (endpoint groupé) ou /événement (endpoint additionnel).
- **Betfair Historical Data Service** : UNKNOWN / NEEDS CONFIRMATION (nécessite connexion à un compte).
- **OpticOdds (marque sœur d'OddsJam)** : 10 requêtes/15 secondes confirmé (api-faq officielle) pour l'endpoint historique.
- **Sportmonks — Premium Odds Feed (historique)** : 2000-5000 req/h selon plan (Phase 8C).
- **Sportradar — Odds Comparison API** : UNKNOWN / NEEDS CONFIRMATION (essai 30 jours puis contact commercial).
- **Stats Perform / Opta — Bet Trading Data** : UNKNOWN.
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : UNKNOWN.
- **football-data.co.uk (référence — Phase 8D/8E)** : N/A (fichiers CSV statiques).
- **Pinnacle (API publique)** : UNKNOWN.

## 19. Cost

- **The Odds API — Historical Sports Odds API** (LOW) : Free=500 crédits/mois ; 20K=$30/mois ; 100K=$59/mois ; 5M=$119/mois ; 15M=$249/mois. Reconfirmé stable au 2026-08-30 (aucun changement vs Phase 8C).
- **Betfair Historical Data Service** (UNKNOWN) : Structure confirmée à 3 tiers (Basic gratuit 1min ; Advanced payant 1s ; Pro payant ~50ms tick) mais AUCUN montant chiffré public trouvé — connexion compte requise pour voir les prix réels. Un chiffre de frais d'activation Application Key commerciale (~£5000) circule sur des sources tierces NON officielles — non retenu ici.
- **OpticOdds (marque sœur d'OddsJam)** (UNKNOWN) : AUCUN prix public — toutes les pages (pricing, historical-odds) renvoient vers un formulaire de contact commercial.
- **Sportmonks — Premium Odds Feed (historique)** (MEDIUM) : Starter €29/mois à Pro €249/mois + add-on Premium Odds Feed €129/mois (Phase 8C, non re-vérifié dans cette phase).
- **Sportradar — Odds Comparison API** (ENTERPRISE) : Confirmé officiellement non public — essai 30 jours puis vente entreprise. Chiffres tiers (1250$-10000$+/mois) NON officiels, non retenus.
- **Stats Perform / Opta — Bet Trading Data** (ENTERPRISE) : Confirmé enterprise-only / sur devis (FAQ officielle Pricing & Licensing).
- **BetsAPI — Event Odds** (LOW) : À partir de ~10$/mois (Phase 8F, page pricing officielle) — mais pour les cotes courantes, pas un historique exploitable.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** (UNKNOWN) : Variable selon le fournisseur, peu documenté officiellement.
- **Smarkets / Matchbook (exchanges alternatifs)** (UNKNOWN) : Comptes enregistrés gratuits pour l'API de trading (confirmé Matchbook) — sans objet pour un historique inexistant.
- **football-data.co.uk (référence — Phase 8D/8E)** (FREE) : Gratuit (Phase 8D).
- **Pinnacle (API publique)** (UNKNOWN) : API fermée, accès uniquement sur dossier commercial (Phase 8C).

## 20. Commercial Rights

- **The Odds API — Historical Sports Odds API** : ALLOWED — CGU (terms-and-conditions.html) : usage commercial explicitement autorisé dans un produit à valeur ajoutée (pas de revente du flux brut).
- **Betfair Historical Data Service** : LEGAL_REVIEW_REQUIRED — UNKNOWN / NEEDS CONFIRMATION.
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN — UNKNOWN / NEEDS CONFIRMATION.
- **Sportmonks — Premium Odds Feed (historique)** : ALLOWED — Licence commerciale confirmée sur tous les plans payants (Phase 8C).
- **Sportradar — Odds Comparison API** : UNKNOWN — UNKNOWN / NEEDS CONFIRMATION.
- **Stats Perform / Opta — Bet Trading Data** : UNKNOWN — UNKNOWN.
- **BetsAPI — Event Odds** : UNKNOWN — UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN — UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : UNKNOWN — UNKNOWN.
- **football-data.co.uk (référence — Phase 8D/8E)** : UNKNOWN — Aucune licence explicite trouvée (Phase 8C).
- **Pinnacle (API publique)** : UNKNOWN — UNKNOWN.

## 21. Storage Rights

- **The Odds API — Historical Sports Odds API** : AUCUNE clause trouvée sur la durée de rétention d'un snapshot téléchargé (CGU muettes, recherche exhaustive des mots-clés retain/storage/store/delete/cache/persist) — UNKNOWN, à clarifier par écrit avec le support.
- **Betfair Historical Data Service** : UNKNOWN / NEEDS CONFIRMATION.
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN / NEEDS CONFIRMATION.
- **Sportmonks — Premium Odds Feed (historique)** : 7 jours après le match côté fournisseur — Xfoot devrait capturer et stocker LUI-MÊME en continu pour construire son propre historique, le fournisseur ne le fait pas à sa place.
- **Sportradar — Odds Comparison API** : UNKNOWN / NEEDS CONFIRMATION.
- **Stats Perform / Opta — Bet Trading Data** : UNKNOWN.
- **BetsAPI — Event Odds** : Aucune (timestamp supprimé après le match).
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : N/A.
- **football-data.co.uk (référence — Phase 8D/8E)** : N/A (téléchargement direct).
- **Pinnacle (API publique)** : UNKNOWN.

## 22. Redistribution

- **The Odds API — Historical Sports Odds API** : Interdit : revendre/réempaqueter comme produit de données autonome. Usage interne à un moteur de prédiction conforme.
- **Betfair Historical Data Service** : UNKNOWN / NEEDS CONFIRMATION.
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN / NEEDS CONFIRMATION.
- **Sportmonks — Premium Odds Feed (historique)** : Revente brute interdite, usage produit interne autorisé (Phase 8C).
- **Sportradar — Odds Comparison API** : UNKNOWN / NEEDS CONFIRMATION.
- **Stats Perform / Opta — Bet Trading Data** : UNKNOWN.
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : UNKNOWN.
- **football-data.co.uk (référence — Phase 8D/8E)** : UNKNOWN.
- **Pinnacle (API publique)** : UNKNOWN.

## 23. Data Quality

- **The Odds API — Historical Sports Odds API** : Non vérifié empiriquement (pas d'appel API réel effectué, hors périmètre §1).
- **Betfair Historical Data Service** : Non vérifié (pas d'accès compte).
- **OpticOdds (marque sœur d'OddsJam)** : Page marketing revendique une infrastructure ingérant '>1M mises à jour de cotes/seconde' — non vérifiable indépendamment.
- **Sportmonks — Premium Odds Feed (historique)** : Non vérifié empiriquement.
- **Sportradar — Odds Comparison API** : UNKNOWN.
- **Stats Perform / Opta — Bet Trading Data** : UNKNOWN.
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : Fournisseurs de niche marketplace — fiabilité non garantie, à traiter avec prudence.
- **Smarkets / Matchbook (exchanges alternatifs)** : N/A.
- **football-data.co.uk (référence — Phase 8D/8E)** : Bonne qualité mais aucun timestamp (Phase 8D/8E).
- **Pinnacle (API publique)** : UNKNOWN.

## 24. Reliability

- **The Odds API — Historical Sports Odds API** : Documentation technique complète et cohérente (v4), pages produit à jour.
- **Betfair Historical Data Service** : Documentation technique établie (Betfair Exchange API bien connue de l'industrie), mais Historical Data Service spécifiquement moins documenté publiquement.
- **OpticOdds (marque sœur d'OddsJam)** : Doc technique développeur réelle trouvée (developer.opticodds.com), pas seulement une page marketing — signal positif de maturité.
- **Sportmonks — Premium Odds Feed (historique)** : Documentation complète (Phase 8C/8F).
- **Sportradar — Odds Comparison API** : Documentation développeur complète et professionnelle (developer.sportradar.com).
- **Stats Perform / Opta — Bet Trading Data** : Marque établie (Opta), documentation produit officielle existe.
- **BetsAPI — Event Odds** : Documentation officielle existante mais peu détaillée sur l'historique.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : Documentation généralement mince ; JsonOdds a une doc officielle propre mais limitée.
- **Smarkets / Matchbook (exchanges alternatifs)** : Documentation trading live existante pour les deux.
- **football-data.co.uk (référence — Phase 8D/8E)** : Utilisé depuis des années dans la recherche académique.
- **Pinnacle (API publique)** : Référence qualité marché historique (réputation), mais inaccessible.

## 25. Latency

- **The Odds API — Historical Sports Odds API** : Grille de 5-10 min pour les snapshots ; last_update à la seconde pour les mises à jour bookmaker individuelles.
- **Betfair Historical Data Service** : N/A (produit d'archive, pas de latence temps réel applicable).
- **OpticOdds (marque sœur d'OddsJam)** : Near-real-time revendiqué (infrastructure haute fréquence).
- **Sportmonks — Premium Odds Feed (historique)** : Standard ~10 min avant match, Premium ~1 min (Phase 8C).
- **Sportradar — Odds Comparison API** : Near-real-time (produit conçu pour le direct).
- **Stats Perform / Opta — Bet Trading Data** : UNKNOWN.
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : Real-time (produits de trading).
- **football-data.co.uk (référence — Phase 8D/8E)** : Hebdomadaire (Phase 8D).
- **Pinnacle (API publique)** : UNKNOWN.

## 26. Integration Complexity

| Provider | Complexity | API Maturity |
|---|---|---|
| The Odds API — Historical Sports Odds API | LOW | HIGH |
| Betfair Historical Data Service | HIGH | MEDIUM |
| OpticOdds (marque sœur d'OddsJam) | MEDIUM | MEDIUM |
| Sportmonks — Premium Odds Feed (historique) | MEDIUM | HIGH |
| Sportradar — Odds Comparison API | HIGH | HIGH |
| Stats Perform / Opta — Bet Trading Data | HIGH | MEDIUM |
| BetsAPI — Event Odds | MEDIUM | LOW |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | MEDIUM | LOW |
| Smarkets / Matchbook (exchanges alternatifs) | HIGH | MEDIUM |
| football-data.co.uk (référence — Phase 8D/8E) | LOW | LOW |
| Pinnacle (API publique) | HIGH | MEDIUM |

## 27. Provider Lock-in

- **The Odds API — Historical Sports Odds API** : Fournisseur unique pour cette architecture précise ; une abstraction OddsProvider permettrait un remplacement futur (non implémentée dans cette phase, §39).
- **Betfair Historical Data Service** : Fournisseur unique pour ce niveau de granularité (tick ~50ms) parmi tous les candidats étudiés dans Xfoot à ce jour.
- **OpticOdds (marque sœur d'OddsJam)** : UNKNOWN — fournisseur récemment découvert, pas d'historique d'usage dans l'industrie sports-analytics documenté ici.
- **Sportmonks — Premium Odds Feed (historique)** : N/A — écarté pour la profondeur historique, pas pour un verrou fournisseur.
- **Sportradar — Odds Comparison API** : N/A — écarté sur le critère temporel avant toute considération de lock-in.
- **Stats Perform / Opta — Bet Trading Data** : N/A.
- **BetsAPI — Event Odds** : N/A.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : N/A.
- **Smarkets / Matchbook (exchanges alternatifs)** : N/A.
- **football-data.co.uk (référence — Phase 8D/8E)** : Déjà utilisé (cache local Phase 8D) — sans lock-in réel (fichiers statiques).
- **Pinnacle (API publique)** : N/A.

## 28. Trial Availability

- **The Odds API — Historical Sports Odds API** : Offre Free (500 crédits/mois) permet un pilote à très petite échelle (voir §36 Trial Plan).
- **Betfair Historical Data Service** : Tier Basic gratuit (granularité 1 min) permettrait un premier test SI le blocage territorial est levé.
- **OpticOdds (marque sœur d'OddsJam)** : NONE confirmé publiquement — accès semble nécessiter un contact commercial dès le départ.
- **Sportmonks — Premium Odds Feed (historique)** : Essai gratuit 14 jours (Phase 8C) — suffisant pour vérifier bookmaker_update en conditions réelles, mais PAS pour un backtest historique profond.
- **Sportradar — Odds Comparison API** : Essai gratuit 30 jours confirmé officiellement — mais ne changerait rien à l'absence structurelle d'archive historique.
- **Stats Perform / Opta — Bet Trading Data** : Demo sur demande (« Request a product demo »), pas un essai libre-service.
- **BetsAPI — Event Odds** : UNKNOWN.
- **RapidAPI — agrégateurs de cotes (JsonOdds et similaires)** : UNKNOWN.
- **Smarkets / Matchbook (exchanges alternatifs)** : Comptes enregistrés gratuits (accès immédiat confirmé côté Matchbook) — mais pour du trading live, pas un historique.
- **football-data.co.uk (référence — Phase 8D/8E)** : N/A (gratuit, pas d'essai nécessaire).
- **Pinnacle (API publique)** : NONE (accès fermé).

## 29. Provider Scorecards

| Provider | Snapshot History | Timestamp | Historical Depth | 1X2 | BTTS | O/U | League Coverage | Movement | Cost | Commercial | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| The Odds API — Historical Sports Odds API | TRUE_SNAPSHOT_HISTORY | UNKNOWN | 2020-2026 | voir §12 | voir §12 | voir §12 | GOOD | MOVEMENT_AVAILABLE | LOW | ALLOWED | **SHORTLIST** |
| Betfair Historical Data Service | TRUE_SNAPSHOT_HISTORY | BOOKMAKER_TIMESTAMP | 2016-2026 | voir §12 | voir §12 | voir §12 | PARTIAL | MOVEMENT_AVAILABLE | UNKNOWN | LEGAL_REVIEW_REQUIRED | **SHORTLIST** |
| OpticOdds (marque sœur d'OddsJam) | TRUE_SNAPSHOT_HISTORY | UNKNOWN | ?-? | voir §12 | voir §12 | voir §12 | UNKNOWN | MOVEMENT_AVAILABLE | UNKNOWN | UNKNOWN | **CONSIDER** |
| Sportmonks — Premium Odds Feed (historique) | TIMESTAMPED_HISTORICAL | BOOKMAKER_TIMESTAMP | ?-? | voir §12 | voir §12 | voir §12 | GOOD | MOVEMENT_AVAILABLE | MEDIUM | ALLOWED | **CONSIDER** |
| Sportradar — Odds Comparison API | CURRENT_ONLY | UNKNOWN | ?-? | voir §12 | voir §12 | voir §12 | GOOD | UNKNOWN | ENTERPRISE | UNKNOWN | **CONSIDER** |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN | N/A | ?-? | voir §12 | voir §12 | voir §12 | UNKNOWN | UNKNOWN | ENTERPRISE | UNKNOWN | **CONSIDER** |
| BetsAPI — Event Odds | CURRENT_ONLY | PROVIDER_INGESTION_TIMESTAMP | ?-? | voir §12 | voir §12 | voir §12 | UNKNOWN | MOVEMENT_NOT_AVAILABLE | LOW | UNKNOWN | **DO_NOT_USE** |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | CURRENT_ONLY | PROVIDER_INGESTION_TIMESTAMP | ?-? | voir §12 | voir §12 | voir §12 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **CONSIDER** |
| Smarkets / Matchbook (exchanges alternatifs) | UNKNOWN | N/A | ?-? | voir §12 | voir §12 | voir §12 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **CONSIDER** |
| football-data.co.uk (référence — Phase 8D/8E) | HISTORICAL_UNTIMESTAMPED | N/A | 1993-2026 | voir §12 | voir §12 | voir §12 | PARTIAL | MOVEMENT_NOT_AVAILABLE | FREE | UNKNOWN | **DO_NOT_USE** |
| Pinnacle (API publique) | UNKNOWN | UNKNOWN | ?-? | voir §12 | voir §12 | voir §12 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **CONSIDER** |

## 30. Top 3 Candidates

1. **The Odds API** — meilleure architecture globale : requête point-in-time réelle (previous_timestamp/next_timestamp), toutes les 11 ligues listées (2 partielles), tarification claire et basse, droits commerciaux confirmés. Faiblesse : sémantique exacte de last_update non confirmée, BTTS/O-U incertains, 10 mois du dataset Xfoot (08/2019-06/2020) resteront toujours non couverts.

2. **Betfair Historical Data Service** — meilleure qualité temporelle techniquement (timestamp bookmaker natif `pt`, granularité tick ~50ms en tier Pro) mais bloqué par un risque juridique non résolu (France potentiellement territoire interdit) et une tarification totalement opaque.

3. **OpticOdds (OddsJam)** — découverte de cette phase, architecture prometteuse (flux de chaque changement de prix avec timestamp) mais trop d'inconnues bloquantes (couverture, prix, sémantique) pour dépasser CONSIDER sans contact commercial direct.

## 31. Best Temporal Quality

Betfair Historical Data Service (timestamp bookmaker natif confirmé, granularité tick) — sous réserve de la résolution du risque légal France.

## 32. Best Coverage

The Odds API (11/11 ligues listées, 9 avec profondeur complète depuis 2020).

## 33. Best Price/Value

The Odds API (à partir de $30/mois, tier gratuit pour un premier test).

## 34. Best Overall

The Odds API — meilleur équilibre architecture/couverture/coût/droits commerciaux, sans blocage légal identifié (contrairement à Betfair).

## 35. Recommendation


**PROCEED_TO_PROVIDER_TRIAL**

The Odds API dispose d'une architecture suffisamment documentée et d'un tier gratuit permettant une validation réelle à très petite échelle (voir §36) sans engagement financier significatif. Betfair reste une option à explorer EN PARALLÈLE mais uniquement après résolution du doute légal (contact direct du support Betfair). Aucun fournisseur n'est déclaré 'validé pour production' (§règle absolue).

## 36. Trial Plan

- Portée : 100 à 500 matchs (2-3 ligues parmi Premier League/LaLiga/Bundesliga, les mieux couvertes historiquement).
- Cutoffs testés : T-24h, T-12h, T-6h, T-3h, T-1h par match (5 appels historiques ciblés par match, pas d'énumération complète).
- Marchés : 1X2 en priorité (h2h, marché Featured, coût connu) ; O/U 2.5 en secondaire si le budget crédits le permet.
- Budget crédits estimé : 500 matchs x 5 cutoffs x 1 région x 1 marché x 10 crédits = 25 000 crédits — dépasse le tier gratuit (500 crédits), nécessiterait le tier $30/mois (20K crédits, proche) ou une réduction d'échelle (ex. 20 matchs x 5 cutoffs = 1000 crédits, à peine au-dessus du gratuit).
- Objectif du pilote : confirmer empiriquement la sémantique de last_update (comparer à des mouvements de cote connus) et la couverture réelle des bookmakers EU pour O/U 2.5 — lever les deux inconnues bloquantes identifiées en §6/§Markets.
- Bookmaker-level : conserver chaque bookmaker séparément (jamais un consensus pré-agrégé) pour permettre un safe_consensus reconstructible (méthode Phase 8E, réutilisable telle quelle).

**NE PAS lancer ce plan dans cette phase (§53).**

## 37. Limitations

- Recherche documentaire uniquement — aucun appel API réel effectué (interdiction explicite §1/§58), donc plusieurs points restent UNKNOWN qu'un vrai test lèverait rapidement.
- Betfair : le texte exact des CGU (territoires interdits) n'a jamais pu être récupéré directement (403 systématique sur 2 tentatives, 2 phases différentes) — s'appuie sur des sources secondaires convergentes mais non officielles.
- OpticOdds : découverte tardive dans cette recherche, profondeur d'investigation moindre que The Odds API/Betfair — mériterait une phase de recherche dédiée si retenu.
- Aucune vérification empirique de la couverture O/U 2.5 (ligne précise) pour les bookmakers européens de foot chez The Odds API — la doc contient une mise en garde possiblement datée ('mainly US sports').
- 10 mois du dataset Xfoot (2019-08 à 2020-06) ne seront JAMAIS couverts par The Odds API ni par aucun candidat identifié dans cette recherche (aucun ne descend avant 2016 pour Betfair, aucun avant juin 2020 pour The Odds API) — toute future intégration devra accepter cette perte de couverture rétroactive.

## 38. Phase 8G Recommendation

- Contacter directement le support The Odds API par écrit pour confirmer : (a) sémantique exacte de last_update, (b) politique de rétention des snapshots téléchargés, (c) couverture O/U 2.5 réelle pour les bookmakers EU de foot — avant tout pilote payant.
- Contacter directement le support Betfair pour confirmer explicitement le statut de la France dans les territoires interdits — condition bloquante avant toute exploration supplémentaire de ce fournisseur.
- Si les deux points ci-dessus sont levés favorablement : exécuter le Trial Plan (§36) sur le tier gratuit ou $30/mois de The Odds API, jamais avant.
- Ne pas construire le Value Engine avant qu'un signal réellement TEMPORALLY_VERIFIED (Phase 8E) soit obtenu sur des données neuves.

---

### DECISION TABLE (§67)

| Provider | Snapshot | Timestamp | History | Coverage | Commercial | Cost | Leakage | Verdict |
|---|---|---|---|---|---|---|---|---|
| The Odds API — Historical Sports Odds API | TRUE_SNAPSHOT_HISTORY | UNKNOWN | 2020-2026 | GOOD | ALLOWED | LOW | LOW | **SHORTLIST** |
| Betfair Historical Data Service | TRUE_SNAPSHOT_HISTORY | BOOKMAKER_TIMESTAMP | 2016-2026 | PARTIAL | LEGAL_REVIEW_REQUIRED | UNKNOWN | LOW | **SHORTLIST** |
| OpticOdds (marque sœur d'OddsJam) | TRUE_SNAPSHOT_HISTORY | UNKNOWN | ?-? | UNKNOWN | UNKNOWN | UNKNOWN | MEDIUM | **CONSIDER** |
| Sportmonks — Premium Odds Feed (historique) | TIMESTAMPED_HISTORICAL | BOOKMAKER_TIMESTAMP | ?-? | GOOD | ALLOWED | MEDIUM | MEDIUM | **CONSIDER** |
| Sportradar — Odds Comparison API | CURRENT_ONLY | UNKNOWN | ?-? | GOOD | UNKNOWN | ENTERPRISE | MEDIUM | **CONSIDER** |
| Stats Perform / Opta — Bet Trading Data | UNKNOWN | N/A | ?-? | UNKNOWN | UNKNOWN | ENTERPRISE | UNKNOWN | **CONSIDER** |
| BetsAPI — Event Odds | CURRENT_ONLY | PROVIDER_INGESTION_TIMESTAMP | ?-? | UNKNOWN | UNKNOWN | LOW | HIGH | **DO_NOT_USE** |
| RapidAPI — agrégateurs de cotes (JsonOdds et similaires) | CURRENT_ONLY | PROVIDER_INGESTION_TIMESTAMP | ?-? | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **CONSIDER** |
| Smarkets / Matchbook (exchanges alternatifs) | UNKNOWN | N/A | ?-? | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **CONSIDER** |
| football-data.co.uk (référence — Phase 8D/8E) | HISTORICAL_UNTIMESTAMPED | N/A | 1993-2026 | PARTIAL | UNKNOWN | FREE | HIGH | **DO_NOT_USE** |
| Pinnacle (API publique) | UNKNOWN | UNKNOWN | ?-? | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | **CONSIDER** |

---

### TIMESTAMPED ODDS SOURCE

🟡 PARTIAL

### TOP CANDIDATE

The Odds API (sous réserve de confirmation des points §6/§Markets/§21 via contact direct)

### TRIAL

PROPOSED

### VALUE ENGINE

NOT BUILT.

### PRODUCTION

NO CHANGES.

---

PHASE 8F — XFOOT TIMESTAMPED ODDS PROVIDER DISCOVERY V1 TERMINÉE. AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
