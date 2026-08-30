# XFOOT EXTERNAL DATA SOURCE AUDIT V1

## 1. Executive Summary

Run id : `20260830_004747` — généré le 2026-08-30T00:47:47.750445+00:00. Recherche effectuée le 2026-08-30.

RÈGLE ABSOLUE : AUDIT UNIQUEMENT. Aucune intégration, aucune clé API, aucune modification production.

- 23 fournisseurs évalués sur 6 domaines (odds, injuries, suspensions, lineups, standings, weather).
- Verdicts : {'SHORTLIST': 2, 'CONSIDER': 16, 'DO_NOT_USE': 4, 'RECOMMENDED_FOR_MVP': 1}
- Aucune source n'est RECOMMENDED FOR MVP sans réserve (voir §29/§30) — 1 source(s) atteignent ce niveau, toujours avec une réserve documentée.

## 2. Current Xfoot Data Gaps

Confirmé par la Phase 8A (Data Intelligence & Feature Registry V1, reports/data/) : Xfoot n'a AUCUNE donnée de cote, blessure, suspension, composition ou météo. Le classement (standings) a depuis été reconstruit EN INTERNE (Phase 8B, verdict EQUIVALENT — aucun gain démontré vs baseline).

- Ligues en base locale (5) : Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA
- Ligues CSV source non chargées en base (6) : ChampionsLeague, ConferenceLeague, EuropaLeague, MLS, PrimeiraLiga, SaudiProLeague

## 3. Research Methodology

Recherche web menée via 4 threads parallèles (API-Football en profondeur ; alternatives odds ; alternatives injuries/suspensions/lineups ; alternatives standings/weather), sources officielles uniquement (documentation technique, pages tarifaires, CGU). Toute donnée non confirmable depuis une source officielle est marquée UNKNOWN / NEEDS CONFIRMATION — jamais devinée. Plusieurs pages officielles bloquant les requêtes automatisées (403 Cloudflare notamment sur api-football.com) ont été consultées via des captures archivées (Wayback Machine), avec la date de capture ET la date de vérification citées séparément.

## 4. Odds Providers

7 fournisseurs étudiés (voir §24/ODDS pour le détail chiffré) : API-Football (déjà intégré, tous plans dont Free, mais 7 jours d'historique rétroactif seulement), The Odds API (marché h2h/totals confirmés, BTTS non confirmé, historique payant depuis juin 2020), Betfair Exchange (bourse d'échange, pas un bookmaker classique, risque géographique France non résolu), Pinnacle (API publique fermée depuis juillet 2025, accès enterprise seulement), football-data.co.uk (CSV gratuit, 1X2 + O/U 2.5 sur certaines ligues, licence non explicite), Sportmonks (42 marchés dont 1X2/BTTS/O-U, mais rétention de 7 jours seulement), OddsPortal (aucune API officielle, scraping explicitement interdit par les CGU — écarté d'office).

## 5. Odds Historical Availability

Classification par fournisseur : FULL_HISTORY confirmé uniquement pour Betfair Historical Data Service (depuis 2016, granularité jusqu'à 50ms) et football-data.co.uk (depuis les années 1990 selon ligue, gratuit). PARTIAL_HISTORY pour API-Football (7 jours glissants seulement — inadapté à un backtest sur les 12459 matchs déjà en base) et The Odds API (depuis juin 2020 seulement, ~19 mois du dataset Xfoot resteraient non couverts). CURRENT_ONLY pour Sportmonks Premium Odds Feed (rétention 7 jours post-match). AUCUN fournisseur combine FULL_HISTORY + couverture des 11 ligues + usage commercial sans réserve.

## 6. Odds Timestamp

API-Football : champ `update` par cote (ISO). The Odds API : snapshots historiques toutes les 5-10 minutes (produit payant séparé). football-data.co.uk : deux snapshots documentés par match (pré-clôture collecté vendredi/mardi après-midi, clôture identifiée par un suffixe "C" sur le code bookmaker) — pas un flux continu, mais deux points temporels clairs et exploitables pour une règle de cutoff. Betfair Historical : jusqu'à 50ms de granularité (tier Pro). Aucun de ces timestamps n'a été vérifié en conditions réelles (appel API live) dans cette phase — voir §52.

## 7. Odds Movement

Reconstruction opening → movement → pre-match → closing : possible en théorie chez Betfair Historical (granularité fine) et The Odds API (snapshots réguliers, produit payant) ; PARTIELLEMENT possible chez football-data.co.uk (seulement 2 points : pré-clôture et clôture, pas de mouvement intermédiaire) ; NON disponible chez API-Football (fenêtre de 7 jours, mise à jour toutes les 3h, pas conçu comme un entrepôt de mouvement) ; NON disponible chez Sportmonks Odds standard (rétention trop courte). Aucune reconstruction complète et vérifiée n'a été testée empiriquement — audit documentaire uniquement (§54).

## 8. Injury Providers

5 fournisseurs étudiés : API-Football (déjà intégré, tous plans, données depuis avril 2021 seulement — endpoint récent), Sportmonks (module "sidelined", historique via start_date/end_date, tous plans payants), Sportradar (schéma exact non documenté publiquement, accès enterprise), Opta/Stats Perform (produits marketés mais documentation technique fermée, enterprise-only), SportsData.io (produit "Replay" prometteur pour l'horodatage mais portée foot/blessures non confirmée). CONSTAT COMMUN AUX 5 : aucun n'expose de champ `reported_at`/`published_at` documenté publiquement — voir §61/§16.

## 9. Suspension Providers

Aucun fournisseur dédié spécifiquement aux suspensions n'a été identifié dans cette recherche — l'information est systématiquement bundlée avec les blessures (ex. API-Football : champ `reason` texte libre incluant "Suspended" parmi d'autres motifs, sans structuration séparée type durée/compétition/motif). Sportmonks "sidelined" couvre également ce cas via `type_id`. Aucune source n'offre une modélisation dédiée (durée exacte, instance disciplinaire, appel en cours) distincte du domaine injuries.

## 10. Lineup Providers

7 entrées étudiées (voir §24/LINEUPS). Point le plus important de ce domaine : **la distinction PREDICTED vs CONFIRMED (§18/§62) n'est explicitement documentée que chez Sportmonks** (add-on "Premium Expected Lineups", précision annoncée 75-88% selon compétition, composition confirmée marquée `lineup_confirmed=true` ~1h avant coup d'envoi). API-Football ne fait AUCUNE distinction (un seul jeu de données) et documente explicitement une disponibilité de 20 à 40 minutes avant le match SEULEMENT, avec certaines compétitions où la composition n'est disponible qu'APRÈS le match — un point de fuite documenté par le fournisseur lui-même. Sportradar/Opta : accès enterprise, détail non public. FotMob/Transfermarkt : aucune API officielle, scraping déconseillé.

## 11. Standings Providers

3 fournisseurs étudiés en plus de la reconstruction interne Xfoot (déjà bâtie, Phase 8B) : API-Football (aucun paramètre `date`, classement le plus récent seulement — ne permet PAS de reconstruction historique), football-data.org (reconstruit lui-même le classement à partir des résultats de matchs, en retirant explicitement les pénalités de points historiques — donc un recalcul comparable à celui de Xfoot, en moins complet), Sportmonks (endpoint "Standing Correction" suggérant la conservation des pénalités officielles — seul avantage structurel identifié sur la solution interne, non vérifié empiriquement).

## 12. Weather Providers

4 fournisseurs étudiés : Visual Crossing (meilleur candidat global — historique complet depuis 1970, observations réelles, gratuit jusqu'à 1000 enregistrements/jour), OpenWeatherMap (historique complet depuis 1979 via des produits séparés de l'offre "current/forecast", mais tarification officielle non extractible/confirmée), Meteostat (licence la plus permissive — CC BY 4.0, données gratuites, mais intégration la plus complexe : fichiers Parquet + mapping par station météo plutôt que lat/lon direct), WeatherAPI.com (RÉSERVE MAJEURE : ses données "historiques" sont en réalité des prévisions archivées, pas des observations réelles — à éviter pour un usage ML rétrospectif sans validation empirique de l'écart). Coût transversal à tous : aucun fournisseur ne fournit nativement une correspondance stade→coordonnées GPS, à construire et maintenir en interne.

## 13. League Coverage

5 ligues en base locale : Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA. 6 ligues CSV non chargées : ChampionsLeague, ConferenceLeague, EuropaLeague, MLS, PrimeiraLiga, SaudiProLeague.

Aucun fournisseur odds gratuit ne couvre les 11 ligues (football-data.co.uk : 6/11, absent sur Saudi Pro League + 3 coupes européennes). API-Football (déjà intégré) revendique une couverture marketing complète sur les 11, mais la couverture RÉELLE varie par saison (`coverage.odds`/`coverage.injuries`) et n'a pas pu être vérifiée saison-par-saison sans clé API live — UNKNOWN pour la profondeur historique par ligue.

## 14. Historical Coverage

Aucune source odds ne couvre historiquement l'intégralité 2019-2026 du dataset Xfoot avec une couverture complète des 11 ligues. football-data.co.uk est FULL_HISTORY sur 6/11 ligues (gratuit). The Odds API ne remonte qu'à juin 2020 (manque ~10 mois du dataset). API-Football (déjà intégré) n'offre que 7 jours d'historique rétroactif — inutilisable pour le backtest sur les matchs déjà en base.

## 15. Timestamp Quality

Meilleure qualité de timestamp trouvée : Visual Crossing/Meteostat/OpenWeatherMap (weather, observations horaires réelles), Betfair Historical (odds, jusqu'à 50ms), football-data.co.uk (2 snapshots documentés : pré-clôture/clôture). Pire : injuries/lineups — AUCUN fournisseur étudié ne documente de champ `reported_at`/`published_at` fiable permettant de garantir qu'une information était bien connue avant un instant T donné.

## 16. Leakage Risk

Voir champ `leakage_risk` par fournisseur (§24). Point critique : API-Football Lineups et Standings sont classés HIGH (compositions parfois disponibles seulement après le match pour certaines compétitions ; aucun paramètre `date` sur /standings). Injuries (tous fournisseurs) : MEDIUM par défaut, faute de timestamp de publication documenté.

## 17. Data Quality

WeatherAPI.com : réserve qualité majeure — son "historique" est en réalité des prévisions archivées, pas des observations réelles (seul cas de ce type identifié). football-data.org standings : recalcul lui-même (retire les pénalités de points), pas une archive officielle. Les autres sources n'ont pas révélé de problème de qualité structurel équivalent dans la documentation consultée.

## 18. API Quality

Tous les fournisseurs candidats retenus (hors Betfair/exchange) sont REST/JSON. Aucun ne documente de support GraphQL. Rate limits documentés pour API-Football (100 req/j Free à 1,5M Custom), The Odds API (système de crédits), football-data.org (10-120 req/min selon plan), Sportmonks (2000-5000 req/h selon plan). Aucun webhook documenté chez API-Football.

## 19. Pricing

- **API-Football / API-Sports — Odds** : Odds inclus sur TOUS les plans dont Free ($0/mois, 100 req/j, 10 req/min) selon la page pricing officielle ("All our plans include all competitions and endpoints"). Vérifié 2026-08-30 via capture archivée (site bloque les requêtes automatisées non-navigateur).
- **The Odds API (the-odds-api.com)** : Free 500 crédits/mois ; 20K=$30/mois ; 100K=$59/mois ; 5M=$119/mois ; 15M=$249/mois (page pricing officielle, vérifié 2026-08-30). Coût historique ×10 crédits/requête.
- **Betfair Exchange API + Historical Data Service** : Delayed App Key gratuite (dev). Live App Key : £499 d'activation (non remboursable) + £999 licence éditeur si certification requise. Historical Data Service : paliers Basic/Advanced/Pro, prix exacts non confirmés officiellement (source secondaire, fetch direct bloqué 403).
- **Pinnacle (API publique)** : API publique fermée depuis le 23 juillet 2025 ; accès désormais réservé aux partenariats commerciaux sur dossier (contact api@pinnacle.com). Aucune tarification publique.
- **football-data.co.uk (CSV gratuit)** : Gratuit (notes.txt / data.php, vérifié 2026-08-30).
- **Sportmonks — Odds (Premium Odds Feed)** : Starter €29/mois (5 ligues) à Pro €249/mois (120 ligues) + add-on Premium Odds Feed €129/mois + add-on Odds&Predictions €15/mois. Enterprise sur devis (historique inclus, portée non détaillée).
- **OddsPortal** : N/A — pas de produit API commercialisé.
- **API-Football / API-Sports — Injuries** : Inclus sur tous les plans dont Free (page pricing officielle, vérifié 2026-08-30).
- **Sportmonks — Injuries/Suspensions (module sidelined)** : Starter €29/mois à Enterprise sur devis (voir grille Odds ci-dessus, même fournisseur/mêmes plans).
- **Sportradar — Injuries (Rosters/Lineups/Transfers/Injuries)** : AUCUNE tarification publique confirmée officiellement. Estimations tierces NON officielles évoquent 1300-4990€/mois voire 10000+$/mois sur engagement annuel — à traiter comme purement indicatif.
- **Opta / Stats Perform — Injuries & Lineups** : 100% vente commerciale, aucun tarif public. FAQ officielle précise travailler avec "many startups and smaller organisations" mais sans grille tarifaire self-service.
- **SportsData.io (via RapidAPI / direct) — Injuries & Lineups** : Estimation tierce NON officielle ~500-1000+$/mois — accès production nécessite contact commercial, aucun tarif public confirmé.
- **API-Football / API-Sports — Lineups** : Inclus sur tous les plans dont Free.
- **Sportmonks — Lineups + Predicted Lineups (add-on)** : Grille standard (voir Odds ci-dessus) + add-on "Premium Expected Lineups" dont le tarif exact n'est pas publié — UNKNOWN.
- **FotMob (aucune API officielle)** : N/A — aucun produit API commercialisé.
- **Transfermarkt (aucune API officielle confirmée)** : N/A.
- **API-Football / API-Sports — Standings** : Inclus sur tous les plans dont Free.
- **football-data.org — Standings** : Free (0€, 12 compétitions, 10 req/min) à Pro (199€/mois, 100 compétitions). Grille officielle vérifiée 2026-08-30.
- **Sportmonks — Standings (avec Standing Correction)** : Growth €99/mois (30 ligues) probablement suffisant en volume ; add-on historique €29 (portée exacte non détaillée).
- **Visual Crossing Weather API** : Free : 1000 enregistrements/jour, usage commercial ET non-commercial explicitement autorisé. Pay-as-you-go : 0,0001$/enregistrement. Professional 35$/mois, Corporate 150$/mois.
- **OpenWeatherMap — History API / History Bulk** : Page pricing officielle rendue en JavaScript, non extractible par l'outil de recherche — tarif exact NON CONFIRMÉ officiellement. Free tier confirmé à 1000 appels/jour (toutes API confondues). Un tarif de 0,0015$/appel au-delà est rapporté par plusieurs trackers tiers NON officiels, à ne pas considérer comme confirmé.
- **Meteostat** : Données bulk gratuites (fichiers Parquet annuels, sans clé API). Accès JSON hébergé via RapidAPI : 500 appels/mois gratuits, tarif premium au-delà non confirmé -> UNKNOWN pour ce mode d'accès spécifique.
- **WeatherAPI.com** : Free (100K appels/mois, historique limité à 1 jour) ; Starter 7$/mois (7 jours d'historique) ; Pro+ 25$/mois (historique complet depuis 2010) ; Business 65$/mois.

## 20. Commercial Rights

API-Football (déjà intégré, tous domaines) : LEGAL_REVIEW_REQUIRED — clause explicite sur les "betting platforms" nécessitant potentiellement des licences additionnelles, ambiguë pour un SaaS de pronostics comme Xfoot. Sportmonks : licence commerciale explicitement confirmée sur tous les plans payants, la plus claire de cette recherche. Betfair : accès en pure consommation de données explicitement NON PERMIS via la clé live. OddsPortal/FotMob : usage commercial explicitement INTERDIT.

## 21. Storage Rights

Aucune clause trouvée interdisant EXPLICITEMENT le stockage local d'un cache/historique chez API-Football ou Sportmonks (silence, pas une autorisation écrite non plus) — à faire confirmer par le support de chaque fournisseur avant tout stockage à long terme destiné au futur Track Record.

## 22. Redistribution Rights

Aucun fournisseur étudié n'a été confirmé comme autorisant explicitement l'AFFICHAGE de ses données brutes aux utilisateurs finaux de Xfoot (USER-FACING DISPLAY) sans réserve — tous les usages confirmés le sont pour un usage INTERNAL USE (alimenter un modèle/produit), jamais pour republier la donnée brute telle quelle.

## 23. Integration Complexity

LOW : API-Football (déjà intégré), The Odds API, football-data.co.uk, football-data.org, Visual Crossing, WeatherAPI.com. MEDIUM : Sportmonks (tous domaines), OpenWeatherMap (offre éclatée), SportsData.io. HIGH : Betfair (compte + certification), Meteostat (parsing Parquet + mapping station), Sportradar/Opta (cycle commercial + intégration enterprise), FotMob/Transfermarkt/OddsPortal (aucune voie officielle).

## 24. Provider Scorecards

### PROVIDERS

| Provider | Domain | Coverage | History | Timestamp | Leakage | Cost | Commercial | Integration | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| API-Football / API-Sports — Odds | odds | GOOD | PARTIAL_HISTORY | OK | LOW | FREE | LEGAL_REVIEW_REQUIRED | LOW | **SHORTLIST** |
| The Odds API (the-odds-api.com) | odds | PARTIAL | PARTIAL_HISTORY | OK | LOW | LOW | ALLOWED | LOW | **CONSIDER** |
| Betfair Exchange API + Historical Data Service | odds | UNKNOWN | FULL_HISTORY | OK | LOW | MEDIUM | LEGAL_REVIEW_REQUIRED | HIGH | **CONSIDER** |
| Pinnacle (API publique) | odds | UNKNOWN | UNKNOWN | incertain | UNKNOWN | UNKNOWN | UNKNOWN | HIGH | **CONSIDER** |
| football-data.co.uk (CSV gratuit) | odds | PARTIAL | FULL_HISTORY | OK | LOW | FREE | UNKNOWN | LOW | **CONSIDER** |
| Sportmonks — Odds (Premium Odds Feed) | odds | UNKNOWN | CURRENT_ONLY | OK | MEDIUM | MEDIUM | ALLOWED | MEDIUM | **CONSIDER** |
| OddsPortal | odds | UNKNOWN | UNKNOWN | OK | UNKNOWN | UNKNOWN | RESTRICTED | HIGH | **DO_NOT_USE** |
| API-Football / API-Sports — Injuries | injuries, suspensions | UNKNOWN | PARTIAL_HISTORY | OK | MEDIUM | FREE | LEGAL_REVIEW_REQUIRED | LOW | **CONSIDER** |
| Sportmonks — Injuries/Suspensions (module sidelined) | injuries, suspensions | PARTIAL | PARTIAL_HISTORY | OK | MEDIUM | MEDIUM | ALLOWED | MEDIUM | **CONSIDER** |
| Sportradar — Injuries (Rosters/Lineups/Transfers/Injuries) | injuries, suspensions, lineups | UNKNOWN | UNKNOWN | incertain | UNKNOWN | ENTERPRISE | UNKNOWN | HIGH | **CONSIDER** |
| Opta / Stats Perform — Injuries & Lineups | injuries, lineups | UNKNOWN | UNKNOWN | incertain | UNKNOWN | ENTERPRISE | UNKNOWN | HIGH | **CONSIDER** |
| SportsData.io (via RapidAPI / direct) — Injuries & Lineups | injuries, lineups | POOR | PARTIAL_HISTORY | OK | MEDIUM | UNKNOWN | UNKNOWN | MEDIUM | **CONSIDER** |
| API-Football / API-Sports — Lineups | lineups | GOOD | CURRENT_ONLY | OK | HIGH | FREE | LEGAL_REVIEW_REQUIRED | LOW | **DO_NOT_USE** |
| Sportmonks — Lineups + Predicted Lineups (add-on) | lineups | PARTIAL | CURRENT_ONLY | OK | MEDIUM | MEDIUM | ALLOWED | MEDIUM | **CONSIDER** |
| FotMob (aucune API officielle) | lineups, injuries | UNKNOWN | UNKNOWN | OK | UNKNOWN | UNKNOWN | RESTRICTED | HIGH | **DO_NOT_USE** |
| Transfermarkt (aucune API officielle confirmée) | lineups, injuries | UNKNOWN | UNKNOWN | OK | UNKNOWN | UNKNOWN | RESTRICTED | HIGH | **DO_NOT_USE** |
| API-Football / API-Sports — Standings | standings | GOOD | CURRENT_ONLY | OK | HIGH | FREE | LEGAL_REVIEW_REQUIRED | LOW | **CONSIDER** |
| football-data.org — Standings | standings | PARTIAL | PARTIAL_HISTORY | OK | LOW | FREE | UNKNOWN | LOW | **CONSIDER** |
| Sportmonks — Standings (avec Standing Correction) | standings | PARTIAL | PARTIAL_HISTORY | OK | LOW | MEDIUM | ALLOWED | MEDIUM | **CONSIDER** |
| Visual Crossing Weather API | weather | EXCELLENT | FULL_HISTORY | OK | LOW | FREE | ALLOWED | LOW | **RECOMMENDED_FOR_MVP** |
| OpenWeatherMap — History API / History Bulk | weather | GOOD | FULL_HISTORY | OK | LOW | UNKNOWN | UNKNOWN | MEDIUM | **CONSIDER** |
| Meteostat | weather | GOOD | FULL_HISTORY | OK | LOW | FREE | ALLOWED | HIGH | **SHORTLIST** |
| WeatherAPI.com | weather | PARTIAL | PARTIAL_HISTORY | OK | MEDIUM | LOW | ALLOWED | LOW | **CONSIDER** |

## 25. Domain Priority

| Domain | Potential Value | Availability | Historical Quality | Leakage Risk | Cost | Priority |
|---|---|---|---|---|---|---|
| odds | HIGH (candidat Value Engine futur — jamais "odds=vérité", §60) | PARTIAL — aucune source ne couvre les 11 ligues Xfoot ET un historique profond ET un usage commercial sans réserve simultanément | PARTIAL — football-data.co.uk (gratuit, 6/11 ligues, historique complet) est la seule combinaison FULL_HISTORY+FREE, mais couverture ligues incomplète | LOW à MEDIUM selon fournisseur (timestamps généralement documentés) | FREE à MEDIUM (hors Betfair/Pinnacle, incertains) | P0 (confirmé) — mais AUCUN fournisseur ne permet un backtest walk-forward complet sur les 11 ligues Xfoot dès aujourd'hui |
| injuries | MEDIUM (hypothèse non testée, §61 — qualité du timing/statut plus importante que le volume) | PARTIAL — API-Football (déjà intégré) depuis avril 2021 seulement ; alternatives enterprise (Sportradar/Opta) hors budget probable | POOR — AUCUN fournisseur étudié ne documente un timestamp `reported_at`/`published_at` fiable | MEDIUM à HIGH (documenté explicitement, §16) | FREE (API-Football) à ENTERPRISE (Sportradar/Opta) | P1 (dégradé par rapport à la priorité initiale P1, en raison de l'absence de timestamp de publication fiable sur toutes les sources étudiées) |
| suspensions | LOW-MEDIUM (souvent bundlé avec injuries, information partiellement structurée) | PARTIAL — capturé de façon non structurée dans le champ `reason` d'API-Football ("Suspended") | POOR (même limite que injuries) | MEDIUM à HIGH | FREE à ENTERPRISE | P2 (aucune source dédiée structurée trouvée — dépend entièrement du domaine injuries) |
| lineups | MEDIUM — mais risque structurel de fuite élevé pour la variante "confirmée" (§62) | PARTIAL — API-Football (20-40 min avant, parfois après le match), Sportmonks (distinction predicted/confirmed, la seule trouvée) | POOR/UNKNOWN — aucun fournisseur ne conserve d'historique des compositions PRÉDITES passées | HIGH pour "confirmed" (trop proche du kickoff, parfois après) ; MEDIUM pour "predicted" (Sportmonks) | FREE (API-Football) à MEDIUM (Sportmonks add-on) | P1 — mais seule la variante PREDICTED (Sportmonks) est structurellement utilisable en anti-fuite ; la variante CONFIRMED (API-Football) est à haut risque |
| standings | LOW — la reconstruction interne Xfoot (Phase 8B) est DÉJÀ construite et testée (verdict EQUIVALENT, aucun gain démontré) | GOOD (interne) — externe: API-Football (aucune reconstruction historique), football-data.org (recalcul identique à l'interne), Sportmonks (avantage potentiel via corrections de pénalités, non confirmé) | GOOD (interne, déjà testé anti-fuite) — externe majoritairement équivalent ou inférieur | LOW (interne) ; HIGH pour API-Football (pas de paramètre date) ; LOW pour football-data.org/Sportmonks | FREE (déjà construit en interne) | P2 (démoté) — aucun fournisseur externe n'apporte un avantage clairement supérieur à la solution interne déjà validée, sauf piste Sportmonks (pénalités de points) à vérifier ponctuellement |
| weather | UNKNOWN/LOW_PRIORITY — littérature sport-analytics : effets généralement faibles et inconsistants, non spécifique à Xfoot | GOOD (Visual Crossing : historique complet, gratuit jusqu'à 1000 enregistrements/jour) | GOOD (observations réelles chez Visual Crossing/OpenWeatherMap/Meteostat) — MEDIUM chez WeatherAPI.com (prévisions archivées, pas des observations) | LOW | FREE à LOW | P2 — disponible et peu coûteux, mais valeur prédictive non démontrée ; candidat à un test walk-forward futur à faible coût, jamais une priorité immédiate |

## 26. Duplication Analysis

- **API-Football / API-Sports — Standings** : DUPLIQUE l'information déjà produite en interne par Xfoot (Phase 8B, build_league_standing_features) — et en moins bien (pas de reconstruction point-in-time du tout ici).
- **football-data.org — Standings** : Recalcule la MÊME information que la reconstruction interne Xfoot déjà bâtie (Phase 8B) — avec en plus une couverture ligues incomplète et sans gestion des pénalités de points (explicitement retirées par le fournisseur lui-même).

Aucune autre source ne duplique une information déjà produite par Xfoot (odds/injuries/suspensions/lineups/weather = zéro donnée interne existante, confirmé Phase 8A).

## 27. Feature Value Hypotheses

- **ODDS** : "Les probabilités implicites pré-match apportent une information complémentaire au modèle Xfoot." — hypothèse NON démontrée, nécessite un test walk-forward (§45).
- **INJURIES** : "Les absences de joueurs clés apportent une information complémentaire aux ratings existants." — hypothèse NON démontrée ; la qualité du timing/statut prime sur le volume (§61).
- **LINEUPS** : "Une composition prédite fiable (Sportmonks, ~75-88% de précision annoncée) améliore la prédiction par rapport aux seuls ratings d'équipe." — NON démontrée, ET la variante prédite reste moins fiable que la variante confirmée par construction, à mettre en balance avec le risque de fuite de cette dernière.
- **STANDINGS** : déjà testée en interne (Phase 8B) — verdict EQUIVALENT. Hypothèse d'un gain via les pénalités de points (Sportmonks) reste ouverte mais non testée.
- **WEATHER** : "Des conditions météo extrêmes (vent fort, pluie battante) affectent le rythme de jeu et donc le résultat." — hypothèse faible selon la littérature générale, non testée pour Xfoot.

## 28. Historical Reconstruction

| Provider | Historical Reconstruction |
|---|---|
| API-Football / API-Sports — Odds | PARTIAL |
| The Odds API (the-odds-api.com) | PARTIAL |
| Betfair Exchange API + Historical Data Service | YES |
| Pinnacle (API publique) | UNKNOWN |
| football-data.co.uk (CSV gratuit) | YES |
| Sportmonks — Odds (Premium Odds Feed) | NO |
| OddsPortal | UNKNOWN |
| API-Football / API-Sports — Injuries | PARTIAL |
| Sportmonks — Injuries/Suspensions (module sidelined) | PARTIAL |
| Sportradar — Injuries (Rosters/Lineups/Transfers/Injuries) | UNKNOWN |
| Opta / Stats Perform — Injuries & Lineups | UNKNOWN |
| SportsData.io (via RapidAPI / direct) — Injuries & Lineups | PARTIAL |
| API-Football / API-Sports — Lineups | NO |
| Sportmonks — Lineups + Predicted Lineups (add-on) | NO |
| FotMob (aucune API officielle) | UNKNOWN |
| Transfermarkt (aucune API officielle confirmée) | UNKNOWN |
| API-Football / API-Sports — Standings | NO |
| football-data.org — Standings | PARTIAL |
| Sportmonks — Standings (avec Standing Correction) | PARTIAL |
| Visual Crossing Weather API | YES |
| OpenWeatherMap — History API / History Bulk | YES |
| Meteostat | YES |
| WeatherAPI.com | PARTIAL |

## 29. Top Providers

**ODDS** — BEST DATA QUALITY : Betfair Historical Data Service (FULL_HISTORY depuis 2016, granularité fine) MAIS risque légal France non résolu (LEGAL_REVIEW_REQUIRED avant toute exploration). BEST PRICE/VALUE : football-data.co.uk (gratuit, historique complet, 6/11 ligues). BEST OVERALL (prospectif) : API-Football (déjà intégré, LOW intégration) SOUS RÉSERVE de revue légale (clause "betting platforms") et de sa limite de 7 jours d'historique rétroactif.

**INJURIES** — Aucun BEST DATA QUALITY clair (aucune source ne documente de timestamp de publication fiable). BEST PRICE/VALUE : API-Football (déjà intégré, gratuit) malgré son historique partiel (depuis avril 2021) et sa réserve légale. Sportmonks en second choix (termes commerciaux plus clairs).

**LINEUPS** — BEST DATA QUALITY (anti-fuite) : Sportmonks (seule source distinguant PREDICTED/CONFIRMED). BEST PRICE/VALUE : API-Football (gratuit) mais leakage_risk=HIGH — À NE PAS UTILISER pour un usage pré-match rigoureux tant que la fenêtre de publication n'est pas confirmée sûre par compétition.

**STANDINGS** — Aucun fournisseur externe ne surpasse clairement la reconstruction interne Xfoot (Phase 8B). Seule piste : Sportmonks (corrections de pénalités de points), à vérifier ponctuellement.

**WEATHER** — BEST OVERALL : Visual Crossing (historique complet, gratuit jusqu'à 1000 enregistrements/jour, intégration simple, observations réelles).

## 30. MVP Recommendation

Aucun MVP externe n'est recommandé à l'issue de cette phase (§65 : ne pas conclure prématurément). Si une Phase 8D devait explorer une intégration réelle, la piste la moins risquée à tester en premier serait : **1 fournisseur Odds (football-data.co.uk, gratuit, historique complet, 6/11 ligues) UNIQUEMENT pour une validation walk-forward hors ligne** — pas d'intégration live, pas de clé API production, juste un test de la valeur du signal sur les 6 ligues couvertes, avant toute dépense. Ceci N'EST PAS une recommandation d'achat ni d'intégration — seulement l'option la moins coûteuse et la moins risquée pour un premier test empirique de l'hypothèse §44 ODDS.
## 31. Limitations

- Recherche documentaire (sources officielles/pages tarifaires/CGU), jamais une clé API live n'a été utilisée pour vérifier une couverture ligue-par-ligue ou saison-par-saison en direct (interdiction explicite de cette phase, §52) — plusieurs points restent marqués UNKNOWN pour cette raison précise.
- api-football.com bloque les requêtes automatisées non-navigateur (Cloudflare 403) — les pages officielles ont été consultées via des captures archivées (Wayback Machine), datées séparément de la date de vérification (2026-08-30).
- Betfair : le texte exact des CGU générales (restriction géographique France) n'a pas pu être confirmé par fetch direct (403) — s'appuie sur une source indexée tierce, à reconfirmer directement.
- Transfermarkt : absence d'API officielle confirmée par déduction (aucun programme développeur trouvé, écosystème de wrappers non officiels) mais le texte exact des CGU anti-scraping n'a pas pu être cité verbatim (page inaccessible à l'outil de recherche), contrairement à FotMob.
- Plusieurs tarifs (Sportradar, Opta/Stats Perform, add-on Sportmonks Premium Expected Lineups, OpenWeatherMap au-delà du tier gratuit) sont non-publics ou non confirmables officiellement — jamais estimés ni inventés dans ce rapport, systématiquement marqués UNKNOWN.
- Aucune vérification empirique (essai gratuit, appel API réel) n'a été effectuée pour aucun fournisseur — cette phase est un audit documentaire, pas un test technique (hors périmètre §52/§54).

## 32. Recommendations Phase 8D

- Si Xfoot souhaite avancer sur ODDS : lever d'abord le doute légal (clause "betting platform" d'API-Football, restriction géographique Betfair) avant toute dépense — un ticket support/juridique, pas du code.
- Vérifier en direct (clé API-Football existante, gratuite) la couverture réelle `seasons[].coverage.odds`/`.injuries` pour les 11 ligues Xfoot — c'est un simple appel GET /leagues, pas une intégration, et lèverait plusieurs UNKNOWN de ce rapport à coût nul.
- Si un test walk-forward ODDS est un jour mené : le limiter aux 6 ligues football-data.co.uk (gratuit, historique complet) plutôt que de payer pour une couverture plus large avant d'avoir confirmé un signal réel.
- LINEUPS : n'explorer que la variante Sportmonks "Predicted Lineups" (seule à distinguer prédite/confirmée) — ne jamais utiliser une composition "confirmée" API-Football comme feature pré-match sans avoir vérifié, compétition par compétition, qu'elle est bien disponible AVANT le coup d'envoi.
- STANDINGS : ne pas investir davantage sauf vérification ponctuelle de l'endpoint Sportmonks "Standing Correction" (essai gratuit 14 jours) pour le cas spécifique des pénalités de points, seul avantage structurel identifié sur la reconstruction interne déjà validée.
- WEATHER : Visual Crossing est le candidat le moins coûteux à tester si Xfoot veut valider empiriquement l'hypothèse §44 — mais ce n'est PAS une priorité au vu de la littérature existante.

---

### ODDS

| Provider | 1X2 | BTTS | O/U | History | Timestamp | Movement | Coverage | Cost | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| API-Football / API-Sports — Odds | oui | voir notes | voir notes | PARTIAL_HISTORY | voir notes | voir timestamp_quality | GOOD | FREE | SHORTLIST |
| The Odds API (the-odds-api.com) | oui | voir notes | voir notes | PARTIAL_HISTORY | voir notes | voir timestamp_quality | PARTIAL | LOW | CONSIDER |
| Betfair Exchange API + Historical Data Service | oui | voir notes | voir notes | FULL_HISTORY | voir notes | voir timestamp_quality | UNKNOWN | MEDIUM | CONSIDER |
| Pinnacle (API publique) | oui | voir notes | voir notes | UNKNOWN | UNKNOWN | voir timestamp_quality | UNKNOWN | UNKNOWN | CONSIDER |
| football-data.co.uk (CSV gratuit) | oui | voir notes | voir notes | FULL_HISTORY | voir notes | voir timestamp_quality | PARTIAL | FREE | CONSIDER |
| Sportmonks — Odds (Premium Odds Feed) | voir notes | voir notes | voir notes | CURRENT_ONLY | voir notes | voir timestamp_quality | UNKNOWN | MEDIUM | CONSIDER |
| OddsPortal | oui | voir notes | voir notes | UNKNOWN | voir notes | voir timestamp_quality | UNKNOWN | UNKNOWN | DO_NOT_USE |

### INJURIES

| Provider | History | Timestamp | Player Status | Coverage | Cost | Leakage | Verdict |
|---|---|---|---|---|---|---|---|
| API-Football / API-Sports — Injuries | PARTIAL_HISTORY | PAS de reported_at documenté | voir notes | UNKNOWN | FREE | MEDIUM | CONSIDER |
| Sportmonks — Injuries/Suspensions (module sidelined) | PARTIAL_HISTORY | PAS de reported_at documenté | voir notes | PARTIAL | MEDIUM | MEDIUM | CONSIDER |
| Sportradar — Injuries (Rosters/Lineups/Transfers/Injuries) | UNKNOWN | PAS de reported_at documenté | voir notes | UNKNOWN | ENTERPRISE | UNKNOWN | CONSIDER |
| Opta / Stats Perform — Injuries & Lineups | UNKNOWN | PAS de reported_at documenté | voir notes | UNKNOWN | ENTERPRISE | UNKNOWN | CONSIDER |
| SportsData.io (via RapidAPI / direct) — Injuries & Lineups | PARTIAL_HISTORY | PAS de reported_at documenté | voir notes | POOR | UNKNOWN | MEDIUM | CONSIDER |
| FotMob (aucune API officielle) | UNKNOWN | PAS de reported_at documenté | voir notes | UNKNOWN | UNKNOWN | UNKNOWN | DO_NOT_USE |
| Transfermarkt (aucune API officielle confirmée) | UNKNOWN | PAS de reported_at documenté | voir notes | UNKNOWN | UNKNOWN | UNKNOWN | DO_NOT_USE |

### LINEUPS

| Provider | Predicted | Confirmed | Timestamp | Historical | Coverage | Cost | Verdict |
|---|---|---|---|---|---|---|---|
| Sportradar — Injuries (Rosters/Lineups/Transfers/Injuries) | NON confirmé | UNKNOWN | voir notes | UNKNOWN | UNKNOWN | ENTERPRISE | CONSIDER |
| Opta / Stats Perform — Injuries & Lineups | NON confirmé | UNKNOWN | voir notes | UNKNOWN | UNKNOWN | ENTERPRISE | CONSIDER |
| SportsData.io (via RapidAPI / direct) — Injuries & Lineups | NON confirmé | OUI | voir notes | PARTIAL_HISTORY | POOR | UNKNOWN | CONSIDER |
| API-Football / API-Sports — Lineups | NON confirmé | OUI | 20-40min avant (parfois après) | CURRENT_ONLY | GOOD | FREE | DO_NOT_USE |
| Sportmonks — Lineups + Predicted Lineups (add-on) | OUI (distinct) | OUI | voir notes | CURRENT_ONLY | PARTIAL | MEDIUM | CONSIDER |
| FotMob (aucune API officielle) | NON confirmé | UNKNOWN | voir notes | UNKNOWN | UNKNOWN | UNKNOWN | DO_NOT_USE |
| Transfermarkt (aucune API officielle confirmée) | NON confirmé | UNKNOWN | voir notes | UNKNOWN | UNKNOWN | UNKNOWN | DO_NOT_USE |

### PRIORITY

| Domain | Potential Value | Data Quality | History | Leakage | Cost | Priority |
|---|---|---|---|---|---|---|
| odds | HIGH (candidat Value Engine futur — jamais "odds=vérité", §60) | PARTIAL — football-data.co.uk (gratuit, 6/11 ligues, historique complet) est la seule combinaison FULL_HISTORY+FREE, mais couverture ligues incomplète | PARTIAL — football-data.co.uk (gratuit, 6/11 ligues, historique complet) est la seule combinaison FULL_HISTORY+FREE, mais couverture ligues incomplète | LOW à MEDIUM selon fournisseur (timestamps généralement documentés) | FREE à MEDIUM (hors Betfair/Pinnacle, incertains) | P0 (confirmé) — mais AUCUN fournisseur ne permet un backtest walk-forward complet sur les 11 ligues Xfoot dès aujourd'hui |
| injuries | MEDIUM (hypothèse non testée, §61 — qualité du timing/statut plus importante que le volume) | POOR — AUCUN fournisseur étudié ne documente un timestamp `reported_at`/`published_at` fiable | POOR — AUCUN fournisseur étudié ne documente un timestamp `reported_at`/`published_at` fiable | MEDIUM à HIGH (documenté explicitement, §16) | FREE (API-Football) à ENTERPRISE (Sportradar/Opta) | P1 (dégradé par rapport à la priorité initiale P1, en raison de l'absence de timestamp de publication fiable sur toutes les sources étudiées) |
| suspensions | LOW-MEDIUM (souvent bundlé avec injuries, information partiellement structurée) | POOR (même limite que injuries) | POOR (même limite que injuries) | MEDIUM à HIGH | FREE à ENTERPRISE | P2 (aucune source dédiée structurée trouvée — dépend entièrement du domaine injuries) |
| lineups | MEDIUM — mais risque structurel de fuite élevé pour la variante "confirmée" (§62) | POOR/UNKNOWN — aucun fournisseur ne conserve d'historique des compositions PRÉDITES passées | POOR/UNKNOWN — aucun fournisseur ne conserve d'historique des compositions PRÉDITES passées | HIGH pour "confirmed" (trop proche du kickoff, parfois après) ; MEDIUM pour "predicted" (Sportmonks) | FREE (API-Football) à MEDIUM (Sportmonks add-on) | P1 — mais seule la variante PREDICTED (Sportmonks) est structurellement utilisable en anti-fuite ; la variante CONFIRMED (API-Football) est à haut risque |
| standings | LOW — la reconstruction interne Xfoot (Phase 8B) est DÉJÀ construite et testée (verdict EQUIVALENT, aucun gain démontré) | GOOD (interne, déjà testé anti-fuite) — externe majoritairement équivalent ou inférieur | GOOD (interne, déjà testé anti-fuite) — externe majoritairement équivalent ou inférieur | LOW (interne) ; HIGH pour API-Football (pas de paramètre date) ; LOW pour football-data.org/Sportmonks | FREE (déjà construit en interne) | P2 (démoté) — aucun fournisseur externe n'apporte un avantage clairement supérieur à la solution interne déjà validée, sauf piste Sportmonks (pénalités de points) à vérifier ponctuellement |
| weather | UNKNOWN/LOW_PRIORITY — littérature sport-analytics : effets généralement faibles et inconsistants, non spécifique à Xfoot | GOOD (observations réelles chez Visual Crossing/OpenWeatherMap/Meteostat) — MEDIUM chez WeatherAPI.com (prévisions archivées, pas des observations) | GOOD (observations réelles chez Visual Crossing/OpenWeatherMap/Meteostat) — MEDIUM chez WeatherAPI.com (prévisions archivées, pas des observations) | LOW | FREE à LOW | P2 — disponible et peu coûteux, mais valeur prédictive non démontrée ; candidat à un test walk-forward futur à faible coût, jamais une priorité immédiate |

---

## Database Safety (§56)


Compteurs AVANT : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Compteurs APRÈS : {'match': 12459, 'match_stats': 12459, 'model_predictions': 3610, 'model_versions': 15, 'prediction_log': 9, 'team_ratings': 568}

Identiques : True

## Production Isolation (§52)

Aucun client API de production créé. Aucun secret ajouté. Aucune migration. Aucune modification de model_predictions/prediction_log/model_versions/team_ratings/match/match_stats/scheduler/endpoints/Dashboard/Arena/frontend/modèles ML.

---

PHASE 8C — XFOOT EXTERNAL DATA SOURCE AUDIT V1 TERMINÉE. AUCUNE INTÉGRATION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.
