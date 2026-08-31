"""
scripts/timestamped_odds_provider_audit.py — Phase 8F : XFOOT TIMESTAMPED
ODDS PROVIDER DISCOVERY V1.
=============================================================================
RECHERCHE UNIQUEMENT. Aucun compte créé, aucune clé API, aucun secret,
aucun appel réseau vers un fournisseur. Les faits encodés ci-dessous
proviennent d'une recherche web menée le 2026-08-30 (documentation
officielle, pages tarifaires, CGU — citées avec URL et date de vérification)
— jamais inventés. Toute donnée non confirmable depuis une source officielle
est explicitement marquée "UNKNOWN / NEEDS CONFIRMATION".

`verdict` n'est JAMAIS assigné à la main : calculé par
app.ai.odds_research.provider_audit.decide_verdict() à partir des faits
déclarés — le critère n°1 (§69) est TOUJOURS can_reconstruct_snapshot, jamais
le coût, la popularité ou le nombre de bookmakers.

Usage : python scripts/timestamped_odds_provider_audit.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app.ai.odds_research.provider_audit import (  # noqa: E402
    TimestampedOddsProvider, validate_provider, decide_verdict,
    CUTOFF_HORIZONS, XFOOT_LEAGUES,
)

VERIFIED_DATE = "2026-08-30"


def _mk(name: str, **kw) -> TimestampedOddsProvider:
    verdict = decide_verdict(
        can_reconstruct_snapshot=kw["can_reconstruct_snapshot"], snapshot_model=kw["snapshot_model"],
        coverage_score=kw["coverage_score"], temporal_score=kw["temporal_score"],
        leakage_risk=kw["leakage_risk"], commercial_usage=kw["commercial_usage"],
        duplicate_of_existing=kw.get("duplicate_of_existing", False),
    )
    return TimestampedOddsProvider(name=name, verified_date=VERIFIED_DATE, verdict=verdict, **kw)


def _leagues(**overrides) -> dict:
    base = {lg: "UNKNOWN" for lg in XFOOT_LEAGUES}
    base.update(overrides)
    return base


def _cutoffs(value: str, **overrides) -> dict:
    base = {h: value for h in CUTOFF_HORIZONS}
    base.update(overrides)
    return base


PROVIDERS: list[TimestampedOddsProvider] = [

    _mk(
        "The Odds API — Historical Sports Odds API",
        can_reconstruct_snapshot="YES",
        snapshot_model="TRUE_SNAPSHOT_HISTORY",
        timestamp_granularity="SECOND",
        timestamp_semantics_notes=(
            "Deux champs distincts confirmés : `timestamp` (niveau réponse) = capture la plus proche <= à la "
            "date demandée, aligné sur la grille de polling (5-10 min, secondes=00) — clairement un artefact "
            "de snapshot. `last_update` (niveau bookmaker, et niveau marché pour les marchés additionnels) = "
            "horodatage à la seconde près, DIFFÉRENT du timestamp de snapshot, propre à chaque bookmaker."
        ),
        timestamp_origin="UNKNOWN",
        cutoff_reconstruction=_cutoffs("YES"),
        movement_status="MOVEMENT_AVAILABLE",
        opening_definition="Non explicitement défini comme 'opening bookmaker' — dépend du 1er snapshot disponible depuis l'activation de la couverture (par ligue).",
        closing_definition="Dernier snapshot avant le début effectif de l'événement (paramètre `date` <= kickoff).",
        consensus_capability_notes=(
            "Architecture point-in-time (paramètre `date` -> snapshot le plus proche <=, navigation "
            "previous_timestamp/next_timestamp) : permet de sélectionner uniquement les bookmakers dont "
            "last_update <= cutoff avant de calculer un consensus — exactement le modèle SAFE CONSENSUS de "
            "Phase 8E, reconstructible."
        ),
        historical_first_year=2020, historical_last_year=2026,
        historical_depth_notes=(
            "PAS uniforme : table officielle 'Earliest Historical Timestamps' par ligue. 5 grands championnats "
            "+ CL/EL/MLS/Portugal : depuis 2020-06 à 2020-07. Conference League : depuis 2022-10 (logique, "
            "compétition créée 2021-22). Saudi Pro League : depuis 2026-02 SEULEMENT (~6.5 mois de profondeur "
            "au 30/08/2026). AUCUNE ligue ne couvre 2019-08 -> 2020-06 (10 mois du dataset Xfoot manquants pour toutes)."
        ),
        match_level_query="YES",
        bookmaker_granularity_notes="Cotes ventilées par bookmaker individuel, chacun avec son propre last_update. ~20 bookmakers UK/EU pertinents pour le foot listés officiellement (Bet365, Pinnacle, Unibet, William Hill, Betfair, Winamax FR/DE, etc.).",
        markets_notes=(
            "1X2 (h2h) : confirmé, marché 'Featured'. O/U 2.5 : marché 'totals' confirmé mais couverture "
            "explicite pour bookmakers foot européens NON garantie par la doc ('mainly US sports' — mention "
            "possiblement datée) -> UNKNOWN pour la ligne 2.5 précisément. BTTS : confirmé disponible pour le "
            "foot (clé 'btts') mais classé marché 'Additional' -> profondeur historique limitée à mai 2023 "
            "seulement (pas les 6 ans de h2h/totals), couverture bookmaker EU incertaine."
        ),
        odds_format_notes="Décimal natif.",
        league_coverage=_leagues(
            Bundesliga="FULL", Ligue1="FULL", PremierLeague="FULL", SerieA="FULL", LaLiga="FULL",
            ChampionsLeague="FULL", EuropaLeague="FULL", ConferenceLeague="PARTIAL", MLS="FULL",
            PrimeiraLiga="FULL", SaudiProLeague="PARTIAL",
        ),
        id_stability_notes="Clés de sport stables (ex. soccer_epl, soccer_germany_bundesliga) ; eventId par match pour navigation ciblée.",
        access_model="API_QUERY",
        rate_limits_notes="Système de crédits (pas de req/min documenté séparément) ; coût historique = 10 crédits/région/marché (endpoint groupé) ou /événement (endpoint additionnel).",
        cost_category="LOW",
        cost_notes="Free=500 crédits/mois ; 20K=$30/mois ; 100K=$59/mois ; 5M=$119/mois ; 15M=$249/mois. Reconfirmé stable au 2026-08-30 (aucun changement vs Phase 8C).",
        commercial_usage="ALLOWED",
        storage_rights_notes="CGU (terms-and-conditions.html) : usage commercial explicitement autorisé dans un produit à valeur ajoutée (pas de revente du flux brut).",
        redistribution_notes="Interdit : revendre/réempaqueter comme produit de données autonome. Usage interne à un moteur de prédiction conforme.",
        retention_notes="AUCUNE clause trouvée sur la durée de rétention d'un snapshot téléchargé (CGU muettes, recherche exhaustive des mots-clés retain/storage/store/delete/cache/persist) — UNKNOWN, à clarifier par écrit avec le support.",
        data_quality_notes="Non vérifié empiriquement (pas d'appel API réel effectué, hors périmètre §1).",
        reliability_notes="Documentation technique complète et cohérente (v4), pages produit à jour.",
        latency_notes="Grille de 5-10 min pour les snapshots ; last_update à la seconde pour les mises à jour bookmaker individuelles.",
        api_maturity="HIGH",
        integration_complexity="LOW",
        lock_in_notes="Fournisseur unique pour cette architecture précise ; une abstraction OddsProvider permettrait un remplacement futur (non implémentée dans cette phase, §39).",
        trial_availability="Offre Free (500 crédits/mois) permet un pilote à très petite échelle (voir §36 Trial Plan).",
        coverage_score="GOOD",
        temporal_score="GOOD",
        leakage_risk="LOW",
        sources=[
            "https://the-odds-api.com/liveapi/guides/v4/",
            "https://the-odds-api.com/historical-odds-data/",
            "https://the-odds-api.com/sports-odds-data/sports-apis.html",
            "https://the-odds-api.com/sports-odds-data/betting-markets.html",
            "https://the-odds-api.com/sports-odds-data/bookmaker-apis.html",
            "https://the-odds-api.com/terms-and-conditions.html",
        ],
        notes=(
            "Meilleur candidat identifié dans cette phase : seule architecture confirmée en interrogation "
            "point-in-time réellement adaptée à la reconstruction T-24h..T-1h SANS fuite (chaque requête ne "
            "retourne jamais un snapshot postérieur à la date demandée). temporal_score plafonné à GOOD (pas "
            "EXCELLENT) tant que la sémantique exacte de `last_update` (bookmaker vs ingestion) n'est pas "
            "confirmée noir sur blanc par le support."
        ),
    ),

    _mk(
        "Betfair Historical Data Service",
        can_reconstruct_snapshot="YES",
        snapshot_model="TRUE_SNAPSHOT_HISTORY",
        timestamp_granularity="SECOND",
        timestamp_semantics_notes=(
            "Champ `pt` ('Publish Time') confirmé officiellement (schéma ESASwaggerSchema.json) : "
            "'the number of milliseconds since 1970-01-01T00:00:00 GMT that the changes were generated' — "
            "horodatage SYSTÈME DE L'EXCHANGE au moment où le changement de marché a été généré. Précision "
            "fine exacte (matched-bet vs génération du message de changement) non confirmée par citation "
            "officielle exacte."
        ),
        timestamp_origin="BOOKMAKER_TIMESTAMP",
        cutoff_reconstruction=_cutoffs("YES", **{"T-1h": "PARTIAL"}),
        movement_status="MOVEMENT_AVAILABLE",
        opening_definition="Premier prix échangé disponible dans l'historique (dépend du tier : Basic=granularité 1 min).",
        closing_definition="Dernier prix échangé avant le début de l'événement, disponible à tous les tiers.",
        consensus_capability_notes="N/A — Betfair est un exchange (prix unique déterminé par l'offre/demande des parieurs), pas un agrégat multi-bookmaker. Pas de 'consensus' au sens Phase 8E, mais un signal de marché différent, potentiellement complémentaire.",
        historical_first_year=2016, historical_last_year=2026,
        historical_depth_notes="Fichiers de mapping événement->compétition confirmés officiellement 'from 2018-2022 only for Soccer, Tennis, Cricket, Golf, and Other Sports' (fournis 'as is', complétude non garantie). Portée au-delà de 2022 non confirmée explicitement pour le foot.",
        match_level_query="YES",
        bookmaker_granularity_notes="N/A (exchange, pas de notion de bookmaker multiple) — un seul flux de marché par sélection.",
        markets_notes="Match Odds (1X2), Over/Under, BTTS disponibles comme marchés Exchange distincts — confirmé par l'existence de ces marchés sur la plateforme Betfair, pas vérifié spécifiquement dans le service Historical Data pour la profondeur de chacun.",
        odds_format_notes="Décimal natif (convention Betfair standard).",
        league_coverage=_leagues(),  # liste précise des championnats non obtenue (nécessite connexion compte) -> UNKNOWN partout, honnête
        id_stability_notes="IDs d'événement/marché Betfair stables (marketId/selectionId), mais mapping compétition->ligue Xfoot non vérifié.",
        access_model="BULK_ARCHIVE",
        rate_limits_notes="UNKNOWN / NEEDS CONFIRMATION (nécessite connexion à un compte).",
        cost_category="UNKNOWN",
        cost_notes="Structure confirmée à 3 tiers (Basic gratuit 1min ; Advanced payant 1s ; Pro payant ~50ms tick) mais AUCUN montant chiffré public trouvé — connexion compte requise pour voir les prix réels. Un chiffre de frais d'activation Application Key commerciale (~£5000) circule sur des sources tierces NON officielles — non retenu ici.",
        commercial_usage="LEGAL_REVIEW_REQUIRED",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        retention_notes="UNKNOWN / NEEDS CONFIRMATION.",
        data_quality_notes="Non vérifié (pas d'accès compte).",
        reliability_notes="Documentation technique établie (Betfair Exchange API bien connue de l'industrie), mais Historical Data Service spécifiquement moins documenté publiquement.",
        latency_notes="N/A (produit d'archive, pas de latence temps réel applicable).",
        api_maturity="MEDIUM",
        integration_complexity="HIGH",
        lock_in_notes="Fournisseur unique pour ce niveau de granularité (tick ~50ms) parmi tous les candidats étudiés dans Xfoot à ce jour.",
        trial_availability="Tier Basic gratuit (granularité 1 min) permettrait un premier test SI le blocage territorial est levé.",
        coverage_score="PARTIAL",
        temporal_score="GOOD",
        leakage_risk="LOW",
        sources=[
            "https://support.developer.betfair.com/hc/en-us/articles/115003864531",
            "https://github.com/betfair/stream-api-sample-code (schéma ESASwaggerSchema.json, champ pt)",
            "https://historicdata.betfair.com/ (accès direct nécessitant compte)",
        ],
        notes=(
            "RISQUE MAJEUR NON RÉSOLU : deux tentatives (Phase 8C puis 8F) de récupération directe des CGU "
            "générales Betfair (territoires interdits) ont échoué (403 Cloudflare). Plusieurs sources "
            "SECONDAIRES convergentes (5 sites indépendants de tracking des restrictions géographiques) "
            "indiquent que la FRANCE figure parmi les territoires interdits. Ce signal est TROP FORT pour être "
            "ignoré mais TROP INCERTAIN (non confirmé sur pièce officielle) pour être traité comme une "
            "certitude -> commercial_usage=LEGAL_REVIEW_REQUIRED, jamais RESTRICTED ni ALLOWED sans "
            "vérification directe (contact support Betfair ou test d'inscription avec IP française réelle)."
        ),
    ),

    _mk(
        "OpticOdds (marque sœur d'OddsJam)",
        can_reconstruct_snapshot="PARTIAL",
        snapshot_model="TRUE_SNAPSHOT_HISTORY",
        timestamp_granularity="SECOND",
        timestamp_semantics_notes="Format ISO 8601 confirmé (api-faq officielle). Endpoint historique documenté comme retournant 'un tableau de chaque changement de prix, verrouillage, déverrouillage et événement de règlement, avec horodatage' — semble suivre les événements de marché réels plutôt qu'une grille de polling fixe, mais AUCUNE définition officielle explicite bookmaker vs ingestion trouvée.",
        timestamp_origin="UNKNOWN",
        cutoff_reconstruction=_cutoffs("UNKNOWN"),
        movement_status="MOVEMENT_AVAILABLE",
        opening_definition="UNKNOWN / NEEDS CONFIRMATION.",
        closing_definition="UNKNOWN / NEEDS CONFIRMATION.",
        consensus_capability_notes="UNKNOWN — architecture par changement de prix individuel suggère que c'est possible en théorie, mais non confirmé officiellement.",
        historical_first_year=None, historical_last_year=None,
        historical_depth_notes="Page marketing revendique 'plusieurs années d'historique de prix complet pour les ligues majeures et bookmakers de niveau 1' — AUCUN chiffre précis en années, AUCUNE mention spécifique au football. Ne peut PAS être vérifié plus précisément sans contact commercial.",
        match_level_query="YES",
        bookmaker_granularity_notes="Confirmé par bookmaker ('sportsbook=...' paramètre), '200+ sportsbooks' revendiqués.",
        markets_notes="Non détaillé spécifiquement pour le foot (1X2/BTTS/O-U) dans les pages consultées — 'foot' listé comme sport couvert parmi '25+ sports, 400+ ligues' sans détail marché par marché.",
        odds_format_notes="UNKNOWN / NEEDS CONFIRMATION.",
        league_coverage=_leagues(),  # aucune confirmation ligue par ligue -> UNKNOWN partout, honnête
        id_stability_notes="UNKNOWN / NEEDS CONFIRMATION.",
        access_model="API_QUERY",
        rate_limits_notes="10 requêtes/15 secondes confirmé (api-faq officielle) pour l'endpoint historique.",
        cost_category="UNKNOWN",
        cost_notes="AUCUN prix public — toutes les pages (pricing, historical-odds) renvoient vers un formulaire de contact commercial.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        retention_notes="UNKNOWN / NEEDS CONFIRMATION.",
        data_quality_notes="Page marketing revendique une infrastructure ingérant '>1M mises à jour de cotes/seconde' — non vérifiable indépendamment.",
        reliability_notes="Doc technique développeur réelle trouvée (developer.opticodds.com), pas seulement une page marketing — signal positif de maturité.",
        latency_notes="Near-real-time revendiqué (infrastructure haute fréquence).",
        api_maturity="MEDIUM",
        integration_complexity="MEDIUM",
        lock_in_notes="UNKNOWN — fournisseur récemment découvert, pas d'historique d'usage dans l'industrie sports-analytics documenté ici.",
        trial_availability="NONE confirmé publiquement — accès semble nécessiter un contact commercial dès le départ.",
        coverage_score="UNKNOWN",
        temporal_score="GOOD",
        leakage_risk="MEDIUM",
        sources=[
            "https://developer.opticodds.com/docs/odds-api-getting-started-guide",
            "https://developer.opticodds.com/docs/api-faq",
            "https://opticodds.com/historical-odds",
            "https://opticodds.com/pricing",
        ],
        notes=(
            "Découverte de cette phase (recherche élargie, absent de Phase 8C) — architecture la plus "
            "prometteuse après The Odds API et Betfair (vrai endpoint 'chaque changement de prix, avec "
            "timestamp'), mais bloqué par un nombre trop élevé d'inconnues (couverture ligue par ligue, "
            "prix, sémantique exacte du timestamp, droits commerciaux) pour dépasser CONSIDER en l'état — "
            "nécessiterait un contact commercial direct pour lever ces inconnues avant toute décision. Note "
            "non officielle (presse spécialisée, non vérifiée) : litige commercial en cours (Swish Analytics "
            "vs OddsJam/OpticOdds, allégation de vol de données) — à surveiller, sans impact sur la "
            "classification factuelle ci-dessus."
        ),
    ),

    _mk(
        "Sportmonks — Premium Odds Feed (historique)",
        can_reconstruct_snapshot="PARTIAL",
        snapshot_model="TIMESTAMPED_HISTORICAL",
        timestamp_granularity="SECOND",
        timestamp_semantics_notes="Champ `bookmaker_update` confirmé officiellement : 'the timestamp of the bookmakers latest update' — c'est un vrai BOOKMAKER_TIMESTAMP (positif), mais rétention limitée à 7 jours après le début du match (confirmé sur DEUX endroits de la doc officielle : la page endpoint 'GET All Historical Odds' ET la FAQ officielle).",
        timestamp_origin="BOOKMAKER_TIMESTAMP",
        cutoff_reconstruction=_cutoffs("NO"),  # 7 jours de rétention = structurellement inutilisable pour un backtest 2019-2026
        movement_status="MOVEMENT_AVAILABLE",
        opening_definition="Première valeur dans la fenêtre de rétention de 7 jours — pas nécessairement l'ouverture réelle du marché si celle-ci a eu lieu plus de 7 jours avant le match.",
        closing_definition="Dernière valeur avant/juste après le coup d'envoi, dans la même fenêtre de 7 jours.",
        consensus_capability_notes="Techniquement reconstructible (bookmaker_update par observation) mais UNIQUEMENT dans la fenêtre de 7 jours — inutile pour un cutoff théorique sur un match de plusieurs mois/années dans le passé.",
        historical_first_year=None, historical_last_year=None,
        historical_depth_notes="CONFIRMÉ INSUFFISANT pour Xfoot : rétention de 7 jours après le match SEULEMENT (deux sources officielles concordantes) — pas une archive pluriannuelle malgré le nom 'Historical Odds'.",
        match_level_query="YES",
        bookmaker_granularity_notes="Par bookmaker, confirmé (140+ bookmakers via TXOdds selon Phase 8C).",
        markets_notes="1X2, BTTS, O/U 2.5 confirmés (42 marchés au total, Phase 8C) — mais sans intérêt pratique vu la rétention de 7 jours.",
        odds_format_notes="Décimal (Phase 8C).",
        league_coverage=_leagues(),  # dépend du plan choisi, non re-vérifié ligue par ligue dans cette phase
        id_stability_notes="IDs Sportmonks stables (fixture_id, league_id, season_id) — Phase 8C.",
        access_model="API_QUERY",
        rate_limits_notes="2000-5000 req/h selon plan (Phase 8C).",
        cost_category="MEDIUM",
        cost_notes="Starter €29/mois à Pro €249/mois + add-on Premium Odds Feed €129/mois (Phase 8C, non re-vérifié dans cette phase).",
        commercial_usage="ALLOWED",
        storage_rights_notes="Licence commerciale confirmée sur tous les plans payants (Phase 8C).",
        redistribution_notes="Revente brute interdite, usage produit interne autorisé (Phase 8C).",
        retention_notes="7 jours après le match côté fournisseur — Xfoot devrait capturer et stocker LUI-MÊME en continu pour construire son propre historique, le fournisseur ne le fait pas à sa place.",
        data_quality_notes="Non vérifié empiriquement.",
        reliability_notes="Documentation complète (Phase 8C/8F).",
        latency_notes="Standard ~10 min avant match, Premium ~1 min (Phase 8C).",
        api_maturity="HIGH",
        integration_complexity="MEDIUM",
        lock_in_notes="N/A — écarté pour la profondeur historique, pas pour un verrou fournisseur.",
        trial_availability="Essai gratuit 14 jours (Phase 8C) — suffisant pour vérifier bookmaker_update en conditions réelles, mais PAS pour un backtest historique profond.",
        coverage_score="GOOD",
        temporal_score="POOR",
        leakage_risk="MEDIUM",
        duplicate_of_existing=True,
        duplicate_notes="Reconfirme le finding déjà établi en Phase 8C (rétention 7 jours) — cette phase ajoute uniquement la confirmation que bookmaker_update est un vrai timestamp bookmaker, ce qui ne change pas le verdict global (profondeur toujours insuffisante).",
        sources=[
            "https://docs.sportmonks.com/v3/endpoints-and-entities/endpoints/premium-odds-feed/premium-pre-match-odds/get-all-historical-odds",
            "https://docs.sportmonks.com/v3/faq/odds",
        ],
        notes="Le SEUL fournisseur de cette liste avec un timestamp bookmaker confirmé ET une profondeur structurellement insuffisante en même temps — cas d'école de la distinction §55 'historical odds' vs 'timestamped snapshot history' : ici on a le timestamp mais PAS l'historique profond, l'inverse du problème football-data.co.uk.",
    ),

    _mk(
        "Sportradar — Odds Comparison API",
        can_reconstruct_snapshot="NO",
        snapshot_model="CURRENT_ONLY",
        timestamp_granularity="UNKNOWN",
        timestamp_semantics_notes="Le mécanisme le plus proche d'un historique est le 'Sport Event [Markets] Change Log', documenté officiellement comme retournant 'a list of sport events with odds changes in the last 5 minutes' — fenêtre glissante de polling, PAS une archive interrogeable sur des mois/années.",
        timestamp_origin="UNKNOWN",
        cutoff_reconstruction=_cutoffs("NO"),
        movement_status="UNKNOWN",
        opening_definition="N/A — pas de notion d'archive historique dans ce produit.",
        closing_definition="N/A",
        consensus_capability_notes="N/A pour un usage historique (le produit n'est pas conçu pour du backtesting).",
        historical_first_year=None, historical_last_year=None,
        historical_depth_notes="Fenêtre de change-log <= 24h confirmée (changelog officiel, extension de 5min à 24h+ notée à partir de juin 2023 pour certains endpoints) — structurellement PAS un produit d'archive.",
        match_level_query="NO",
        bookmaker_granularity_notes="Agrégat de 100+ bookmakers tiers (Odds Comparison), granularité par bookmaker au niveau LIVE uniquement.",
        markets_notes="Large couverture de marchés en direct (OC Prematch/Live/Futures/Player Props) — non pertinent pour un historique.",
        odds_format_notes="UNKNOWN / NEEDS CONFIRMATION.",
        league_coverage=_leagues(
            Bundesliga="FULL", Ligue1="FULL", PremierLeague="FULL", SerieA="FULL", LaLiga="FULL", MLS="FULL",
        ),  # couverture confirmée large pour le direct, mais non pertinente pour l'historique -> reste UNKNOWN pour les 5 autres ligues
        id_stability_notes="IDs Sportradar stables (Sport ID 1 = Soccer, mapping documenté) — mais non pertinent sans historique.",
        access_model="API_QUERY",
        rate_limits_notes="UNKNOWN / NEEDS CONFIRMATION (essai 30 jours puis contact commercial).",
        cost_category="ENTERPRISE",
        cost_notes="Confirmé officiellement non public — essai 30 jours puis vente entreprise. Chiffres tiers (1250$-10000$+/mois) NON officiels, non retenus.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        retention_notes="UNKNOWN / NEEDS CONFIRMATION.",
        data_quality_notes="UNKNOWN.",
        reliability_notes="Documentation développeur complète et professionnelle (developer.sportradar.com).",
        latency_notes="Near-real-time (produit conçu pour le direct).",
        api_maturity="HIGH",
        integration_complexity="HIGH",
        lock_in_notes="N/A — écarté sur le critère temporel avant toute considération de lock-in.",
        trial_availability="Essai gratuit 30 jours confirmé officiellement — mais ne changerait rien à l'absence structurelle d'archive historique.",
        coverage_score="GOOD",
        temporal_score="POOR",
        leakage_risk="MEDIUM",
        sources=[
            "https://developer.sportradar.com/odds/",
            "https://developer.sportradar.com/odds/reference/odds-comparison-overview (change log)",
        ],
        notes="Écarté du critère n°1 (§69) : ce produit est conçu pour du polling en direct, jamais pour de la reconstruction historique — même avec un essai gratuit, l'architecture ne le permet pas.",
    ),

    _mk(
        "Stats Perform / Opta — Bet Trading Data",
        can_reconstruct_snapshot="NO",
        snapshot_model="UNKNOWN",
        timestamp_granularity="UNKNOWN",
        timestamp_semantics_notes="N/A — ce produit fournit des DONNÉES ÉVÉNEMENTIELLES (tirs, passes, xG) utilisées par des bookmakers tiers pour FABRIQUER leurs propres cotes ; ce n'est PAS un flux de cotes de marché en tant que tel.",
        timestamp_origin="N/A",
        cutoff_reconstruction=_cutoffs("UNKNOWN"),
        movement_status="UNKNOWN",
        opening_definition="N/A", closing_definition="N/A",
        consensus_capability_notes="N/A — hors périmètre (pas un produit de cotes).",
        historical_first_year=None, historical_last_year=None,
        historical_depth_notes="N/A — hors périmètre.",
        match_level_query="UNKNOWN",
        bookmaker_granularity_notes="N/A.",
        markets_notes="N/A — ce n'est pas un fournisseur de cotes de marché.",
        odds_format_notes="N/A",
        league_coverage=_leagues(),
        id_stability_notes="UNKNOWN.",
        access_model="UNKNOWN",
        rate_limits_notes="UNKNOWN.",
        cost_category="ENTERPRISE",
        cost_notes="Confirmé enterprise-only / sur devis (FAQ officielle Pricing & Licensing).",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN.", redistribution_notes="UNKNOWN.", retention_notes="UNKNOWN.",
        data_quality_notes="UNKNOWN.", reliability_notes="Marque établie (Opta), documentation produit officielle existe.",
        latency_notes="UNKNOWN.", api_maturity="MEDIUM", integration_complexity="HIGH",
        lock_in_notes="N/A.", trial_availability="Demo sur demande (« Request a product demo »), pas un essai libre-service.",
        coverage_score="UNKNOWN", temporal_score="UNKNOWN", leakage_risk="UNKNOWN",
        sources=["https://www.statsperform.com/products/", "https://www.statsperform.com/stats-perform-faqs-pricing-and-licensing/"],
        notes="HORS PÉRIMÈTRE : Stats Perform vend des données événementielles servant à FABRIQUER des cotes chez des bookmakers clients, pas un flux de cotes de marché historique horodaté. Exclu du classement des fournisseurs de cotes.",
    ),

    _mk(
        "BetsAPI — Event Odds",
        can_reconstruct_snapshot="NO",
        snapshot_model="CURRENT_ONLY",
        timestamp_granularity="UNKNOWN",
        timestamp_semantics_notes="Seul champ temporel documenté : `odds_update` = 'the last time we checked the market (will be gone after the event is finished)' — timestamp d'ingestion TRANSITOIRE, supprimé après la fin du match. Structurellement incompatible avec un archivage historique.",
        timestamp_origin="PROVIDER_INGESTION_TIMESTAMP",
        cutoff_reconstruction=_cutoffs("NO"),
        movement_status="MOVEMENT_NOT_AVAILABLE",
        opening_definition="UNKNOWN.", closing_definition="UNKNOWN.",
        consensus_capability_notes="N/A — aucun historique disponible pour reconstruire quoi que ce soit après le match.",
        historical_first_year=None, historical_last_year=None,
        historical_depth_notes="AUCUNE — le seul timestamp disponible disparaît à la fin du match, confirmé officiellement.",
        match_level_query="NO",
        bookmaker_granularity_notes="Packages spécifiques par bookmaker existent (Bet365, BWin, Betfair, Betway, SboBet) pour les cotes courantes — non pertinent sans historique.",
        markets_notes="Non détaillé pour l'historique (N/A).",
        odds_format_notes="UNKNOWN.",
        league_coverage=_leagues(),
        id_stability_notes="UNKNOWN.", access_model="API_QUERY", rate_limits_notes="UNKNOWN.",
        cost_category="LOW", cost_notes="À partir de ~10$/mois (Phase 8F, page pricing officielle) — mais pour les cotes courantes, pas un historique exploitable.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN.", redistribution_notes="UNKNOWN.", retention_notes="Aucune (timestamp supprimé après le match).",
        data_quality_notes="UNKNOWN.", reliability_notes="Documentation officielle existante mais peu détaillée sur l'historique.",
        latency_notes="UNKNOWN.", api_maturity="LOW", integration_complexity="MEDIUM",
        lock_in_notes="N/A.", trial_availability="UNKNOWN.",
        coverage_score="UNKNOWN", temporal_score="POOR", leakage_risk="HIGH",
        sources=["https://betsapi.com/docs/events/odds.html", "https://betsapi.com/docs/events/odds_summary.html", "https://betsapi.com/docs/pricing.html"],
        notes="Écarté : le seul mécanisme temporel documenté est explicitement transitoire (supprimé après le match) — structurellement inutilisable pour reconstruire un cutoff pré-match sur des matchs passés.",
    ),

    _mk(
        "RapidAPI — agrégateurs de cotes (JsonOdds et similaires)",
        can_reconstruct_snapshot="NO",
        snapshot_model="CURRENT_ONLY",
        timestamp_granularity="UNKNOWN",
        timestamp_semantics_notes="JsonOdds (le plus documenté du lot) expose un champ `LastUpdated` = 'the last time these odds were updated' — fraîcheur des cotes COURANTES, aucune section historique documentée officiellement.",
        timestamp_origin="PROVIDER_INGESTION_TIMESTAMP",
        cutoff_reconstruction=_cutoffs("UNKNOWN"),
        movement_status="UNKNOWN",
        opening_definition="UNKNOWN.", closing_definition="UNKNOWN.",
        consensus_capability_notes="UNKNOWN.",
        historical_first_year=None, historical_last_year=None,
        historical_depth_notes="Aucune preuve officielle d'un entrepôt historique horodaté trouvée pour aucun des fournisseurs de cette catégorie explorés.",
        match_level_query="UNKNOWN",
        bookmaker_granularity_notes="Variable selon le fournisseur, non systématiquement documenté.",
        markets_notes="Non vérifié en détail (hors périmètre vu l'absence d'historique).",
        odds_format_notes="UNKNOWN.",
        league_coverage=_leagues(),
        id_stability_notes="UNKNOWN.", access_model="API_QUERY", rate_limits_notes="UNKNOWN.",
        cost_category="UNKNOWN", cost_notes="Variable selon le fournisseur, peu documenté officiellement.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN.", redistribution_notes="UNKNOWN.", retention_notes="UNKNOWN.",
        data_quality_notes="Fournisseurs de niche marketplace — fiabilité non garantie, à traiter avec prudence.",
        reliability_notes="Documentation généralement mince ; JsonOdds a une doc officielle propre mais limitée.",
        latency_notes="UNKNOWN.", api_maturity="LOW", integration_complexity="MEDIUM",
        lock_in_notes="N/A.", trial_availability="UNKNOWN.",
        coverage_score="UNKNOWN", temporal_score="UNKNOWN", leakage_risk="UNKNOWN",
        sources=["https://jsonodds.com/documentation/", "https://rapidapi.com/collection/football-soccer-apis"],
        notes="Catégorie explorée en largeur (§3 du prompt), rien de crédible trouvé au-delà de ce que The Odds API/OpticOdds documentent déjà officiellement — non individualisé davantage faute de documentation exploitable.",
    ),

    _mk(
        "Smarkets / Matchbook (exchanges alternatifs)",
        can_reconstruct_snapshot="NO",
        snapshot_model="UNKNOWN",
        timestamp_granularity="UNKNOWN",
        timestamp_semantics_notes="Aucun produit d'archive historique identifié chez l'un ou l'autre — seules des API de trading EN DIRECT (order book, placement d'ordres) sont documentées officiellement.",
        timestamp_origin="N/A",
        cutoff_reconstruction=_cutoffs("NO"),
        movement_status="UNKNOWN",
        opening_definition="N/A", closing_definition="N/A",
        consensus_capability_notes="N/A.",
        historical_first_year=None, historical_last_year=None,
        historical_depth_notes="Aucun produit d'archive historique trouvé (Smarkets : docs.smarkets.com, help.smarkets.com — API trading live uniquement. Matchbook : developers.matchbook.com — API trading live uniquement, confirmé 'any registered customer can begin using the API immediately').",
        match_level_query="NO",
        bookmaker_granularity_notes="N/A (exchanges, pas de multi-bookmaker).",
        markets_notes="Marchés de trading standards (Match Odds etc.) mais sans historique interrogeable.",
        odds_format_notes="Décimal (convention exchange standard).",
        league_coverage=_leagues(),
        id_stability_notes="UNKNOWN.", access_model="UNKNOWN", rate_limits_notes="UNKNOWN.",
        cost_category="UNKNOWN", cost_notes="Comptes enregistrés gratuits pour l'API de trading (confirmé Matchbook) — sans objet pour un historique inexistant.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN.", redistribution_notes="UNKNOWN.", retention_notes="N/A.",
        data_quality_notes="N/A.", reliability_notes="Documentation trading live existante pour les deux.",
        latency_notes="Real-time (produits de trading).", api_maturity="MEDIUM", integration_complexity="HIGH",
        lock_in_notes="N/A.", trial_availability="Comptes enregistrés gratuits (accès immédiat confirmé côté Matchbook) — mais pour du trading live, pas un historique.",
        coverage_score="UNKNOWN", temporal_score="UNKNOWN", leakage_risk="UNKNOWN",
        sources=["https://docs.smarkets.com", "https://developers.matchbook.com"],
        notes="Aucun des deux ne propose, à ce jour, un produit équivalent au Betfair Historical Data Service. Un agrégateur tiers (OddsPapi) reconstruit lui-même un historique Matchbook par ses propres moyens — preuve indirecte que Matchbook ne le fournit pas nativement.",
    ),

    # Fournisseurs déjà établis en Phase 8C/8D/8E — reportés ici pour le tableau de décision complet,
    # jamais réévalués depuis zéro (§1 : réutiliser le travail existant).
    _mk(
        "football-data.co.uk (référence — Phase 8D/8E)",
        can_reconstruct_snapshot="NO", snapshot_model="HISTORICAL_UNTIMESTAMPED",
        timestamp_granularity="DATE_ONLY", timestamp_semantics_notes="AUCUN timestamp par observation de cote — confirmé exhaustivement en Phase 8E (seule colonne temporelle = heure de coup d'envoi du match).",
        timestamp_origin="N/A", cutoff_reconstruction=_cutoffs("NO"),
        movement_status="MOVEMENT_NOT_AVAILABLE",
        opening_definition="Première valeur persistée, PAS une ouverture de marché prouvée (Phase 8E, §9).",
        closing_definition="Valeur étiquetée 'C' par la source, sans timestamp mesuré (Phase 8E, §10).",
        consensus_capability_notes="Colonne Avg pré-calculée par la source, provenance temporelle opaque — jamais un consensus SAFE reconstructible (Phase 8E, §13).",
        historical_first_year=1993, historical_last_year=2026,
        historical_depth_notes="FULL_HISTORY réelle (décennies) mais SANS AUCUN timestamp exploitable — confirmé Phase 8D/8E.",
        match_level_query="YES",
        bookmaker_granularity_notes="Multi-bookmaker (Bet365 et autres), mais sans timestamp individuel.",
        markets_notes="1X2, O/U 2.5 ; BTTS absent (confirmé Phase 8C).",
        odds_format_notes="Décimal.",
        league_coverage=_leagues(Bundesliga="FULL", Ligue1="FULL", PremierLeague="FULL", SerieA="FULL", LaLiga="FULL", MLS="FULL", ChampionsLeague="NONE", ConferenceLeague="NONE", EuropaLeague="NONE", PrimeiraLiga="UNKNOWN", SaudiProLeague="NONE"),
        id_stability_notes="Noms d'équipe seulement (mais identiques à la convention Xfoot, voir Phase 8D).",
        access_model="BULK_ARCHIVE", rate_limits_notes="N/A (fichiers CSV statiques).",
        cost_category="FREE", cost_notes="Gratuit (Phase 8D).",
        commercial_usage="UNKNOWN", storage_rights_notes="Aucune licence explicite trouvée (Phase 8C).",
        redistribution_notes="UNKNOWN.", retention_notes="N/A (téléchargement direct).",
        data_quality_notes="Bonne qualité mais aucun timestamp (Phase 8D/8E).",
        reliability_notes="Utilisé depuis des années dans la recherche académique.",
        latency_notes="Hebdomadaire (Phase 8D).", api_maturity="LOW", integration_complexity="LOW",
        lock_in_notes="Déjà utilisé (cache local Phase 8D) — sans lock-in réel (fichiers statiques).",
        trial_availability="N/A (gratuit, pas d'essai nécessaire).",
        coverage_score="PARTIAL", temporal_score="POOR", leakage_risk="HIGH",
        duplicate_of_existing=True, duplicate_notes="Déjà entièrement audité en Phase 8D (intégration) et Phase 8E (intégrité temporelle) — reporté ici uniquement pour le tableau de décision comparatif complet.",
        sources=["reports/odds/odds_backtest_20260830_020051.md (Phase 8D)", "reports/odds_integrity/odds_integrity_20260830_021211.md (Phase 8E)"],
        notes="Résultat déjà établi : HISTORICAL_BUT_UNTIMESTAMPED à 100%. Cas de référence de cette phase — le nouveau critère (§2) cherche précisément ce que cette source n'a jamais eu.",
    ),

    _mk(
        "Pinnacle (API publique)", can_reconstruct_snapshot="UNKNOWN", snapshot_model="UNKNOWN",
        timestamp_granularity="UNKNOWN", timestamp_semantics_notes="N/A — API publique fermée.",
        timestamp_origin="UNKNOWN", cutoff_reconstruction=_cutoffs("UNKNOWN"),
        movement_status="UNKNOWN", opening_definition="UNKNOWN.", closing_definition="UNKNOWN.",
        consensus_capability_notes="N/A.", historical_first_year=None, historical_last_year=None,
        historical_depth_notes="Non applicable — accès public fermé depuis le 23 juillet 2025 (Phase 8C).",
        match_level_query="UNKNOWN", bookmaker_granularity_notes="N/A.", markets_notes="N/A.",
        odds_format_notes="UNKNOWN.", league_coverage=_leagues(),
        id_stability_notes="UNKNOWN.", access_model="UNKNOWN", rate_limits_notes="UNKNOWN.",
        cost_category="UNKNOWN", cost_notes="API fermée, accès uniquement sur dossier commercial (Phase 8C).",
        commercial_usage="UNKNOWN", storage_rights_notes="UNKNOWN.", redistribution_notes="UNKNOWN.", retention_notes="UNKNOWN.",
        data_quality_notes="UNKNOWN.", reliability_notes="Référence qualité marché historique (réputation), mais inaccessible.",
        latency_notes="UNKNOWN.", api_maturity="MEDIUM", integration_complexity="HIGH",
        lock_in_notes="N/A.", trial_availability="NONE (accès fermé).",
        coverage_score="UNKNOWN", temporal_score="UNKNOWN", leakage_risk="UNKNOWN",
        duplicate_of_existing=True, duplicate_notes="Déjà établi Phase 8C — non ré-exploré (aucune évolution attendue de la fermeture d'accès).",
        sources=["https://github.com/pinnacleapi/pinnacleapi-documentation"],
        notes="Non ré-audité en profondeur cette phase — statut fermé déjà confirmé Phase 8C.",
    ),
]


def validate_all() -> list[str]:
    problems = []
    for p in PROVIDERS:
        problems.extend(validate_provider(p))
    return problems


# ---------------------------------------------------------------------------
# Table de reconstruction par cutoff (§44)
# ---------------------------------------------------------------------------

def build_temporal_scorecard(providers: list[TimestampedOddsProvider]) -> list[dict]:
    rows = []
    for p in providers:
        rows.append({
            "provider": p.name,
            **{h: p.cutoff_reconstruction[h] for h in CUTOFF_HORIZONS},
            "timestamp_semantics": p.timestamp_origin,
            "verdict": p.verdict,
        })
    return rows


def build_league_coverage_table(providers: list[TimestampedOddsProvider]) -> list[dict]:
    rows = []
    for p in providers:
        row = {"provider": p.name}
        row.update({lg: p.league_coverage[lg] for lg in XFOOT_LEAGUES})
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def render_markdown(result: dict) -> str:
    md = ["# XFOOT TIMESTAMPED ODDS PROVIDER DISCOVERY V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}. Recherche effectuée le {VERIFIED_DATE}.\n")
    md.append("\nRÈGLE ABSOLUE : RECHERCHE UNIQUEMENT. Aucun achat, aucune clé API, aucune intégration.\n")
    md.append(f"\n- {len(result['providers'])} fournisseurs évalués.\n- Verdicts : {result['verdict_counts']}\n")

    md.append("\n## 2. Why Phase 8E Failed\n\n")
    md.append(
        "\nfootball-data.co.uk (source Phase 8D) ne fournit AUCUN timestamp mesuré par observation de cote — "
        "seule une méthodologie de collecte documentée (jamais un instant précis) existe. Résultat Phase 8E : "
        "100% des observations HISTORICAL_BUT_UNTIMESTAMPED, zéro TEMPORALLY_VERIFIED, quel que soit le "
        "cutoff testé. Cette phase cherche une source qui ÉVITE structurellement ce problème.\n"
    )

    md.append("\n## 3. Requirements\n\n")
    md.append("\nFournisseur idéal (§objectif) : historical odds + timestamped snapshots + bookmaker identification + market identification + league coverage + commercially usable data + historical reconstruction. Critère n°1 (§69, jamais négociable) : `can_reconstruct_snapshot`.\n")

    md.append("\n## 4. Providers Researched\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** — can_reconstruct_snapshot={p['can_reconstruct_snapshot']}, snapshot_model={p['snapshot_model']}, verdict={p['verdict']}\n")

    md.append("\n## 5. Snapshot History\n\n")
    md.append("| Provider | Snapshot Model |\n|---|---|\n")
    for p in result["providers"]:
        md.append(f"| {p['name']} | {p['snapshot_model']} |\n")

    md.append("\n## 6. Timestamp Quality\n\n")
    md.append("| Provider | Granularity | Origin | Semantics |\n|---|---|---|---|\n")
    for p in result["providers"]:
        md.append(f"| {p['name']} | {p['timestamp_granularity']} | {p['timestamp_origin']} | {p['timestamp_semantics_notes'][:120]}... |\n")

    md.append("\n## 7. Timestamp Semantics\n\n")
    md.append("\nDistinction BOOKMAKER_TIMESTAMP vs PROVIDER_INGESTION_TIMESTAMP appliquée systématiquement (§14) — voir §6. Seuls Betfair (`pt`) et Sportmonks (`bookmaker_update`) ont un timestamp bookmaker CONFIRMÉ officiellement. The Odds API (`last_update`) et OpticOdds : origine non confirmée explicitement (UNKNOWN).\n")

    md.append("\n## 8. Cutoff Reconstruction\n\n")
    md.append("| Provider | T-24h | T-12h | T-6h | T-3h | T-1h |\n|---|---|---|---|---|---|\n")
    for row in result["temporal_scorecard"]:
        md.append(f"| {row['provider']} | {row['T-24h']} | {row['T-12h']} | {row['T-6h']} | {row['T-3h']} | {row['T-1h']} |\n")

    md.append("\n## 9. Opening Odds\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['opening_definition']}\n")

    md.append("\n## 10. Closing Odds\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['closing_definition']}\n")

    md.append("\n## 11. Odds Movement\n\n")
    md.append("| Provider | Movement |\n|---|---|\n")
    for p in result["providers"]:
        md.append(f"| {p['name']} | {p['movement_status']} |\n")

    md.append("\n## 12. Market Consensus\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['consensus_capability_notes']}\n")

    md.append("\n## 13. League Coverage\n\n")
    md.append("| Provider | " + " | ".join(XFOOT_LEAGUES) + " |\n|" + "---|" * (len(XFOOT_LEAGUES) + 1) + "\n")
    for row in result["league_coverage"]:
        md.append(f"| {row['provider']} | " + " | ".join(row[lg] for lg in XFOOT_LEAGUES) + " |\n")

    md.append("\n## 14. Historical Depth\n\n")
    md.append("| Provider | First Year | Last Year | Notes |\n|---|---|---|---|\n")
    for p in result["providers"]:
        md.append(f"| {p['name']} | {p['historical_first_year'] or 'N/A'} | {p['historical_last_year'] or 'N/A'} | {p['historical_depth_notes'][:150]} |\n")

    md.append("\n## 15. Match IDs\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['id_stability_notes']}\n")

    md.append("\n## 16. Season IDs\n\n")
    md.append("\nVoir §15 — la plupart des fournisseurs modernes (The Odds API, Sportmonks) exposent des clés stables par ligue/événement. Betfair : IDs de marché/sélection stables, mapping compétition non confirmé.\n")

    md.append("\n## 17. API Access\n\n")
    md.append("| Provider | Access Model | Integration Complexity |\n|---|---|---|\n")
    for p in result["providers"]:
        md.append(f"| {p['name']} | {p['access_model']} | {p['integration_complexity']} |\n")

    md.append("\n## 18. Rate Limits\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['rate_limits_notes']}\n")

    md.append("\n## 19. Cost\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** ({p['cost_category']}) : {p['cost_notes']}\n")

    md.append("\n## 20. Commercial Rights\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['commercial_usage']} — {p['storage_rights_notes']}\n")

    md.append("\n## 21. Storage Rights\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['retention_notes']}\n")

    md.append("\n## 22. Redistribution\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['redistribution_notes']}\n")

    md.append("\n## 23. Data Quality\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['data_quality_notes']}\n")

    md.append("\n## 24. Reliability\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['reliability_notes']}\n")

    md.append("\n## 25. Latency\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['latency_notes']}\n")

    md.append("\n## 26. Integration Complexity\n\n")
    md.append("| Provider | Complexity | API Maturity |\n|---|---|---|\n")
    for p in result["providers"]:
        md.append(f"| {p['name']} | {p['integration_complexity']} | {p['api_maturity']} |\n")

    md.append("\n## 27. Provider Lock-in\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['lock_in_notes']}\n")

    md.append("\n## 28. Trial Availability\n\n")
    for p in result["providers"]:
        md.append(f"- **{p['name']}** : {p['trial_availability']}\n")

    md.append("\n## 29. Provider Scorecards\n\n")
    md.append("| Provider | Snapshot History | Timestamp | Historical Depth | 1X2 | BTTS | O/U | League Coverage | Movement | Cost | Commercial | Verdict |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for p in result["providers"]:
        md.append(
            f"| {p['name']} | {p['snapshot_model']} | {p['timestamp_origin']} | "
            f"{p['historical_first_year'] or '?'}-{p['historical_last_year'] or '?'} | voir §12 | voir §12 | voir §12 | "
            f"{p['coverage_score']} | {p['movement_status']} | {p['cost_category']} | {p['commercial_usage']} | **{p['verdict']}** |\n"
        )

    md.append("\n## 30. Top 3 Candidates\n\n")
    md.append(result["top3_text"])

    md.append("\n## 31. Best Temporal Quality\n\n" + result["best_temporal"] + "\n")
    md.append("\n## 32. Best Coverage\n\n" + result["best_coverage"] + "\n")
    md.append("\n## 33. Best Price/Value\n\n" + result["best_price_value"] + "\n")
    md.append("\n## 34. Best Overall\n\n" + result["best_overall"] + "\n")

    md.append("\n## 35. Recommendation\n\n")
    md.append(f"\n**{result['recommendation']}**\n\n{result['recommendation_notes']}\n")

    md.append("\n## 36. Trial Plan\n\n")
    for item in result["trial_plan"]:
        md.append(f"- {item}\n")
    md.append("\n**NE PAS lancer ce plan dans cette phase (§53).**\n")

    md.append("\n## 37. Limitations\n\n")
    for item in result["limitations"]:
        md.append(f"- {item}\n")

    md.append("\n## 38. Phase 8G Recommendation\n\n")
    for item in result["recommendations_phase_8g"]:
        md.append(f"- {item}\n")

    md.append("\n---\n\n### DECISION TABLE (§67)\n\n")
    md.append("| Provider | Snapshot | Timestamp | History | Coverage | Commercial | Cost | Leakage | Verdict |\n|---|---|---|---|---|---|---|---|---|\n")
    for p in result["providers"]:
        md.append(
            f"| {p['name']} | {p['snapshot_model']} | {p['timestamp_origin']} | "
            f"{p['historical_first_year'] or '?'}-{p['historical_last_year'] or '?'} | {p['coverage_score']} | "
            f"{p['commercial_usage']} | {p['cost_category']} | {p['leakage_risk']} | **{p['verdict']}** |\n"
        )

    md.append("\n---\n\n### TIMESTAMPED ODDS SOURCE\n\n" + result["timestamped_source_status"] + "\n")
    md.append("\n### TOP CANDIDATE\n\n" + result["top_candidate"] + "\n")
    md.append("\n### TRIAL\n\n" + result["trial_status"] + "\n")
    md.append("\n### VALUE ENGINE\n\nNOT BUILT.\n")
    md.append("\n### PRODUCTION\n\nNO CHANGES.\n")

    md.append("\n---\n\nPHASE 8F — XFOOT TIMESTAMPED ODDS PROVIDER DISCOVERY V1 TERMINÉE. "
               "AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. "
               "EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def write_reports(result: dict, outdir: Path, run_id: str) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"timestamped_odds_provider_audit_{run_id}.json"
    md_path = outdir / f"timestamped_odds_provider_audit_{run_id}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def main():
    problems = validate_all()
    if problems:
        raise RuntimeError(f"Incohérences détectées — corriger avant de publier le rapport : {problems}")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
    from sqlmodel import Session, select, func  # noqa: E402
    from app.core.database import engine, init_db  # noqa: E402
    from app.models.match import Match, MatchStats  # noqa: E402
    from app.models.model_prediction import ModelPrediction  # noqa: E402
    from app.models.prediction_log import PredictionLog  # noqa: E402
    from app.models.team_rating import ModelVersion, TeamRating  # noqa: E402

    init_db()
    tables = {"match": Match, "match_stats": MatchStats, "model_predictions": ModelPrediction,
              "model_versions": ModelVersion, "prediction_log": PredictionLog, "team_ratings": TeamRating}

    def snapshot(session):
        return {name: session.exec(select(func.count()).select_from(m)).one() for name, m in tables.items()}

    with Session(engine) as session:
        db_before = snapshot(session)

    providers_dicts = [vars(p) for p in PROVIDERS]
    verdict_counts: dict[str, int] = {}
    for p in providers_dicts:
        verdict_counts[p["verdict"]] = verdict_counts.get(p["verdict"], 0) + 1

    top3_text = (
        "1. **The Odds API** — meilleure architecture globale : requête point-in-time réelle "
        "(previous_timestamp/next_timestamp), toutes les 11 ligues listées (2 partielles), tarification claire "
        "et basse, droits commerciaux confirmés. Faiblesse : sémantique exacte de last_update non confirmée, "
        "BTTS/O-U incertains, 10 mois du dataset Xfoot (08/2019-06/2020) resteront toujours non couverts.\n\n"
        "2. **Betfair Historical Data Service** — meilleure qualité temporelle techniquement (timestamp "
        "bookmaker natif `pt`, granularité tick ~50ms en tier Pro) mais bloqué par un risque juridique non "
        "résolu (France potentiellement territoire interdit) et une tarification totalement opaque.\n\n"
        "3. **OpticOdds (OddsJam)** — découverte de cette phase, architecture prometteuse (flux de chaque "
        "changement de prix avec timestamp) mais trop d'inconnues bloquantes (couverture, prix, sémantique) "
        "pour dépasser CONSIDER sans contact commercial direct.\n"
    )

    result = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers_dicts,
        "verdict_counts": verdict_counts,
        "temporal_scorecard": build_temporal_scorecard(PROVIDERS),
        "league_coverage": build_league_coverage_table(PROVIDERS),
        "top3_text": top3_text,
        "best_temporal": "Betfair Historical Data Service (timestamp bookmaker natif confirmé, granularité tick) — sous réserve de la résolution du risque légal France.",
        "best_coverage": "The Odds API (11/11 ligues listées, 9 avec profondeur complète depuis 2020).",
        "best_price_value": "The Odds API (à partir de $30/mois, tier gratuit pour un premier test).",
        "best_overall": "The Odds API — meilleur équilibre architecture/couverture/coût/droits commerciaux, sans blocage légal identifié (contrairement à Betfair).",
        "recommendation": "PROCEED_TO_PROVIDER_TRIAL",
        "recommendation_notes": (
            "The Odds API dispose d'une architecture suffisamment documentée et d'un tier gratuit permettant "
            "une validation réelle à très petite échelle (voir §36) sans engagement financier significatif. "
            "Betfair reste une option à explorer EN PARALLÈLE mais uniquement après résolution du doute légal "
            "(contact direct du support Betfair). Aucun fournisseur n'est déclaré 'validé pour production' "
            "(§règle absolue)."
        ),
        "trial_plan": [
            "Portée : 100 à 500 matchs (2-3 ligues parmi Premier League/LaLiga/Bundesliga, les mieux couvertes historiquement).",
            "Cutoffs testés : T-24h, T-12h, T-6h, T-3h, T-1h par match (5 appels historiques ciblés par match, pas d'énumération complète).",
            "Marchés : 1X2 en priorité (h2h, marché Featured, coût connu) ; O/U 2.5 en secondaire si le budget crédits le permet.",
            "Budget crédits estimé : 500 matchs x 5 cutoffs x 1 région x 1 marché x 10 crédits = 25 000 crédits — dépasse le tier gratuit (500 crédits), nécessiterait le tier $30/mois (20K crédits, proche) ou une réduction d'échelle (ex. 20 matchs x 5 cutoffs = 1000 crédits, à peine au-dessus du gratuit).",
            "Objectif du pilote : confirmer empiriquement la sémantique de last_update (comparer à des mouvements de cote connus) et la couverture réelle des bookmakers EU pour O/U 2.5 — lever les deux inconnues bloquantes identifiées en §6/§Markets.",
            "Bookmaker-level : conserver chaque bookmaker séparément (jamais un consensus pré-agrégé) pour permettre un safe_consensus reconstructible (méthode Phase 8E, réutilisable telle quelle).",
        ],
        "limitations": [
            "Recherche documentaire uniquement — aucun appel API réel effectué (interdiction explicite §1/§58), donc plusieurs points restent UNKNOWN qu'un vrai test lèverait rapidement.",
            "Betfair : le texte exact des CGU (territoires interdits) n'a jamais pu être récupéré directement (403 systématique sur 2 tentatives, 2 phases différentes) — s'appuie sur des sources secondaires convergentes mais non officielles.",
            "OpticOdds : découverte tardive dans cette recherche, profondeur d'investigation moindre que The Odds API/Betfair — mériterait une phase de recherche dédiée si retenu.",
            "Aucune vérification empirique de la couverture O/U 2.5 (ligne précise) pour les bookmakers européens de foot chez The Odds API — la doc contient une mise en garde possiblement datée ('mainly US sports').",
            "10 mois du dataset Xfoot (2019-08 à 2020-06) ne seront JAMAIS couverts par The Odds API ni par aucun candidat identifié dans cette recherche (aucun ne descend avant 2016 pour Betfair, aucun avant juin 2020 pour The Odds API) — toute future intégration devra accepter cette perte de couverture rétroactive.",
        ],
        "recommendations_phase_8g": [
            "Contacter directement le support The Odds API par écrit pour confirmer : (a) sémantique exacte de last_update, (b) politique de rétention des snapshots téléchargés, (c) couverture O/U 2.5 réelle pour les bookmakers EU de foot — avant tout pilote payant.",
            "Contacter directement le support Betfair pour confirmer explicitement le statut de la France dans les territoires interdits — condition bloquante avant toute exploration supplémentaire de ce fournisseur.",
            "Si les deux points ci-dessus sont levés favorablement : exécuter le Trial Plan (§36) sur le tier gratuit ou $30/mois de The Odds API, jamais avant.",
            "Ne pas construire le Value Engine avant qu'un signal réellement TEMPORALLY_VERIFIED (Phase 8E) soit obtenu sur des données neuves.",
        ],
        "timestamped_source_status": "🟡 PARTIAL",
        "top_candidate": "The Odds API (sous réserve de confirmation des points §6/§Markets/§21 via contact direct)",
        "trial_status": "PROPOSED",
        "db_counts_before": db_before,
    }

    with Session(engine) as session:
        db_after = snapshot(session)
    result["db_counts_after"] = db_after
    result["db_unchanged"] = db_before == db_after

    outdir = Path(__file__).resolve().parent.parent / "reports" / "odds_providers"
    json_path, md_path = write_reports(result, outdir, result["run_id"])
    print(f"Rapport écrit : {json_path} / {md_path}")
    print("\n" + "=" * 80)
    print("PHASE 8F — XFOOT TIMESTAMPED ODDS PROVIDER DISCOVERY V1 TERMINÉE.")
    print("AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


if __name__ == "__main__":
    main()
