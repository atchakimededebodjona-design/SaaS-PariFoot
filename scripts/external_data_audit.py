"""
scripts/external_data_audit.py — Phase 8C : XFOOT EXTERNAL DATA SOURCE AUDIT V1.
================================================================================
RECHERCHE / DUE DILIGENCE UNIQUEMENT. Aucun appel réseau vers un fournisseur
externe, aucune clé API créée ou utilisée, aucun secret. Les faits encodés
ci-dessous proviennent d'une recherche web menée le 2026-08-30 (documentation
officielle et pages tarifaires des fournisseurs, citées avec URL et date de
vérification) — jamais inventés. Toute donnée non confirmable depuis une
source officielle est explicitement marquée "UNKNOWN / NEEDS CONFIRMATION"
dans les champs texte correspondants (jamais silencieusement omise).

`verdict` n'est JAMAIS assigné à la main : il est calculé par
app.ai.external_data.scorecard.decide_verdict() à partir des faits déclarés
(§60 : aucune source ne devient RECOMMENDED_FOR_MVP uniquement parce qu'elle
est moins chère) — la seule façon de changer un verdict est de corriger le
fait qui le sous-tend, jamais le verdict lui-même.

Usage (depuis la racine du dépôt) :
    python scripts/external_data_audit.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app.ai.external_data.scorecard import (  # noqa: E402
    ProviderRecord, validate_provider, decide_verdict,
)

VERIFIED_DATE = "2026-08-30"
XFOOT_LEAGUES_DB = ["Bundesliga", "LaLiga", "Ligue1", "PremierLeague", "SerieA"]
XFOOT_LEAGUES_CSV_ONLY = ["ChampionsLeague", "ConferenceLeague", "EuropaLeague", "MLS", "PrimeiraLiga", "SaudiProLeague"]
XFOOT_LEAGUES_ALL = XFOOT_LEAGUES_DB + XFOOT_LEAGUES_CSV_ONLY  # 11 ligues, API-Football league IDs


def _mk(name: str, domains: list[str], **kw) -> ProviderRecord:
    verdict = decide_verdict(
        coverage_score=kw["coverage_score"], history=kw["history"], leakage_risk=kw["leakage_risk"],
        cost_category=kw["cost_category"], commercial_usage=kw["commercial_usage"],
        integration_complexity=kw["integration_complexity"],
        historical_reconstruction=kw["historical_reconstruction"],
        duplicate_of_existing=kw.get("duplicate_of_existing", False),
    )
    return ProviderRecord(name=name, domains=domains, verified_date=VERIFIED_DATE, verdict=verdict, **kw)


# =============================================================================
# ODDS (§6-§13, §60)
# =============================================================================

PROVIDERS: list[ProviderRecord] = [

    _mk(
        "API-Football / API-Sports — Odds", ["odds"],
        coverage_notes=(
            "Page marketing /coverage (capture archivée 2025-04-20) : les 11 ligues Xfoot cochées sur la "
            "colonne Odds. MAIS le spec OpenAPI officiel documente que la couverture réelle est calculée "
            "PAR SAISON (`seasons[].coverage.odds`) et varie (ex. Premier League saison 2010 : odds=false) "
            "— la vue marketing simplifiée ne reflète que l'état actuel, pas l'historique par saison. "
            "Vérification saison-par-saison des 11 ligues NON effectuée (nécessite clé API live)."
        ),
        coverage_score="GOOD",
        history="PARTIAL_HISTORY",
        timestamp_quality=(
            "Champ `update` (timestamp ISO) par cote. MAIS fenêtre de disponibilité documentée : cotes "
            "publiées 1 à 14 jours avant le match, seulement 7 JOURS D'HISTORIQUE CONSERVÉS après coup — "
            "PAS un entrepôt permanent. Reconstruction rétroactive des cotes 2019-2026 IMPOSSIBLE via cet "
            "endpoint ; seule une collecte prospective (à partir d'aujourd'hui) est possible."
        ),
        leakage_risk="LOW",
        cost_category="FREE",
        cost_notes=(
            "Odds inclus sur TOUS les plans dont Free ($0/mois, 100 req/j, 10 req/min) selon la page "
            "pricing officielle (\"All our plans include all competitions and endpoints\"). Vérifié 2026-08-30 "
            "via capture archivée (site bloque les requêtes automatisées non-navigateur)."
        ),
        commercial_usage="LEGAL_REVIEW_REQUIRED",
        storage_rights_notes=(
            "CGU (api-football.com/terms, MàJ 21 mai 2025) : revente du flux brut interdite, mais construire "
            "un produit à valeur ajoutée (SaaS) est explicitement autorisé (\"create different projects such "
            "as applications, websites...\"). AUCUNE clause explicite trouvée sur la durée de rétention "
            "autorisée d'un cache local — silence, pas une interdiction, mais pas une autorisation écrite non plus."
        ),
        redistribution_notes=(
            "CGU : \"use of our data for betting platforms […] may require additional licenses from the "
            "relevant rights holders\" — Xfoot étant un SaaS de pronostics (pas un opérateur de paris), la "
            "frontière avec \"betting platform\" au sens de cette clause est AMBIGUË -> LEGAL_REVIEW_REQUIRED."
        ),
        integration_complexity="LOW",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=False,
        duplicate_notes="Aucune donnée de cote nulle part dans Xfoot actuellement (confirmé Phase 8A) — information réellement nouvelle si collectée.",
        sources=[
            "https://www.api-football.com/public/doc/openapi.yaml (spec officiel v3.9.3, capture Wayback 2025-11-02)",
            "https://www.api-football.com/pricing (capture Wayback 2026-03-14)",
            "https://www.api-football.com/coverage (capture Wayback, Last-Update 2025-04-20)",
            "https://www.api-football.com/terms (capture Wayback 2026-02-01, MàJ 21 mai 2025)",
        ],
        notes=(
            "Fournisseur DÉJÀ utilisé par Xfoot (fixtures/résultats/live-scores) — zéro coût d'onboarding "
            "vendeur. Mais la fenêtre de 7 jours d'historique odds rend cette source INUTILISABLE pour "
            "backtester les 12459 matchs 2019-2026 déjà en base ; seule une collecte prospective (walk-forward "
            "à partir d'aujourd'hui) serait possible, sur un historique qui resterait court pendant des mois."
        ),
    ),

    _mk(
        "The Odds API (the-odds-api.com)", ["odds"],
        coverage_notes=(
            "5 grands championnats confirmés. Couverture contradictoire entre deux pages officielles pour "
            "MLS/Saudi Pro League/Primeira Liga/Conference League — UNKNOWN / NEEDS CONFIRMATION (nécessite "
            "un appel live à GET /v4/sports avec une clé gratuite)."
        ),
        coverage_score="PARTIAL",
        history="PARTIAL_HISTORY",
        timestamp_quality=(
            "Endpoint historique séparé (payant), snapshots toutes les 10 min (5 min depuis sept. 2022), "
            "disponible depuis le 6 juin 2020 SEULEMENT — ne couvre pas 2019, ne couvre donc pas le début "
            "du dataset Xfoot (2019-08-09)."
        ),
        leakage_risk="LOW",
        cost_category="LOW",
        cost_notes="Free 500 crédits/mois ; 20K=$30/mois ; 100K=$59/mois ; 5M=$119/mois ; 15M=$249/mois (page pricing officielle, vérifié 2026-08-30). Coût historique ×10 crédits/requête.",
        commercial_usage="ALLOWED",
        storage_rights_notes="CGU officielles : usage commercial autorisé dans une app/dashboard destinée aux utilisateurs finaux.",
        redistribution_notes="Interdit explicitement : revendre/réemballer/redistribuer les données \"as a standalone data product\" — usage interne à un produit Xfoot à valeur ajoutée reste conforme.",
        integration_complexity="LOW",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication (Xfoot n'a aucune donnée de cote).",
        sources=[
            "https://the-odds-api.com/liveapi/guides/v4/",
            "https://the-odds-api.com/historical-odds-data/",
            "https://the-odds-api.com/terms-and-conditions.html",
        ],
        notes="Marché BTTS non confirmé dans la doc. Ne couvre que depuis juin 2020 — 19 mois du dataset Xfoot (2019-08 à 2020-06) resteraient sans cotes historiques même avec cette source.",
    ),

    _mk(
        "Betfair Exchange API + Historical Data Service", ["odds"],
        coverage_notes="Large couverture marchés (Match Odds, O/U 2.5, BTTS via marchés séparés sur l'Exchange) mais couverture ligues Xfoot précise NON vérifiée.",
        coverage_score="UNKNOWN",
        history="FULL_HISTORY",
        timestamp_quality="Historical Data Service : granularité jusqu'à 50ms (tier Pro) depuis 2016 — la plus fine des sources étudiées, mais nécessite un compte Betfair vérifié.",
        leakage_risk="LOW",
        cost_category="MEDIUM",
        cost_notes="Delayed App Key gratuite (dev). Live App Key : £499 d'activation (non remboursable) + £999 licence éditeur si certification requise. Historical Data Service : paliers Basic/Advanced/Pro, prix exacts non confirmés officiellement (source secondaire, fetch direct bloqué 403).",
        commercial_usage="LEGAL_REVIEW_REQUIRED",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION — CGU non accessibles en fetch direct (403).",
        redistribution_notes="CGU officielles confirmées : \"Read-only access via the Live App Key isn't permitted\" — un usage de pure consommation de données (sans activité de pari réelle) semble explicitement EXCLU par cette clause.",
        integration_complexity="HIGH",
        historical_reconstruction="YES",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=[
            "https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access",
            "https://developer.betfair.com/historical-data-services-api/ (accès direct bloqué 403, info via sources secondaires à recouper)",
        ],
        notes=(
            "RISQUE MAJEUR NON RÉSOLU : une source indexée (CGU générales Betfair) suggère que la FRANCE "
            "figure parmi les \"territoires interdits\" — le fetch direct de la page officielle a échoué "
            "(403), ce point n'a PU ÊTRE NI CONFIRMÉ NI INFIRMÉ formellement. Pour une société opérée depuis "
            "la France, ceci pourrait disqualifier purement et simplement ce fournisseur (API live, service "
            "historique, et même l'ouverture d'un compte). LEGAL_REVIEW_REQUIRED avant toute exploration "
            "supplémentaire — ne pas engager de frais tant que ce point n'est pas tranché."
        ),
    ),

    _mk(
        "Pinnacle (API publique)", ["odds"],
        coverage_notes="UNKNOWN — API fermée au public.",
        coverage_score="UNKNOWN",
        history="UNKNOWN",
        timestamp_quality="UNKNOWN / NEEDS CONFIRMATION",
        leakage_risk="UNKNOWN",
        cost_category="UNKNOWN",
        cost_notes="API publique fermée depuis le 23 juillet 2025 ; accès désormais réservé aux partenariats commerciaux sur dossier (contact api@pinnacle.com). Aucune tarification publique.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        integration_complexity="HIGH",
        historical_reconstruction="UNKNOWN",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://github.com/pinnacleapi/pinnacleapi-documentation (doc résiduelle, accès API fermé — statut de fermeture corroboré par plusieurs sources tierces, aucune page officielle pinnacle.com directement re-vérifiable)"],
        notes="Cotes Pinnacle réputées \"sharp\"/référence marché — mais non accessibles en self-service. Toute cote \"Pinnacle\" obtenue via un agrégateur tiers doit être considérée avec prudence quant à sa fraîcheur/fiabilité.",
    ),

    _mk(
        "football-data.co.uk (CSV gratuit)", ["odds"],
        coverage_notes=(
            "Confirmé couvert (page data.php) : 5 grands championnats + MLS (6/11 ligues Xfoot). "
            "CONFIRMÉ ABSENT : Saudi Pro League, Champions League, Europa League, Conference League (5/11 ligues Xfoot)."
        ),
        coverage_score="PARTIAL",
        history="FULL_HISTORY",
        timestamp_quality="Deux snapshots par match : pré-clôture (collecté vendredi/mardi après-midi) et clôture (colonnes suffixées \"C\", ex. B365CH) — pas un flux continu ni une cote d'ouverture au sens strict, mais deux points temporels documentés et exploitables.",
        leakage_risk="LOW",
        cost_category="FREE",
        cost_notes="Gratuit (notes.txt / data.php, vérifié 2026-08-30).",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION — aucune licence/CGU explicite trouvée sur les pages consultées.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        integration_complexity="LOW",
        historical_reconstruction="YES",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication — seule source GRATUITE avec un historique de cotes pluriannuel réellement exploitable trouvée dans cette recherche.",
        sources=["https://www.football-data.co.uk/notes.txt", "https://www.football-data.co.uk/data.php"],
        notes="Meilleur rapport coût/historique pour les 6 ligues couvertes ; ne résout pas le besoin sur les 5 ligues manquantes (dont 3 compétitions européennes que Xfoot suit). Mise à jour hebdomadaire seulement — inadapté à un usage live, adapté à l'entraînement de modèle rétrospectif.",
    ),

    _mk(
        "Sportmonks — Odds (Premium Odds Feed)", ["odds"],
        coverage_notes="Sélection de ligues selon plan (2300+ ligues au catalogue total) — couverture précise des 11 ligues Xfoot NON vérifiée ligue par ligue.",
        coverage_score="UNKNOWN",
        history="CURRENT_ONLY",
        timestamp_quality="Historique des cotes pré-match conservé SEULEMENT jusqu'à 7 jours après le début du match (FAQ officielle) — pas un entrepôt pluriannuel, inadapté au backtesting sur 2019-2026.",
        leakage_risk="MEDIUM",
        cost_category="MEDIUM",
        cost_notes="Starter €29/mois (5 ligues) à Pro €249/mois (120 ligues) + add-on Premium Odds Feed €129/mois + add-on Odds&Predictions €15/mois. Enterprise sur devis (historique inclus, portée non détaillée).",
        commercial_usage="ALLOWED",
        storage_rights_notes="Tous les plans payants incluent une licence commerciale (CGU officielles).",
        redistribution_notes="Revente des données brutes interdite sans accord préalable ; usage produit interne autorisé.",
        integration_complexity="MEDIUM",
        historical_reconstruction="NO",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://docs.sportmonks.com/v3/faq/odds", "https://www.sportmonks.com/football-api/plans-pricing/"],
        notes="42 marchés couverts (1X2/BTTS/O-U inclus) mais l'historique de 7 jours en fait un outil de monitoring pré-match, pas un entrepôt de backtesting exploitable pour Xfoot.",
    ),

    _mk(
        "OddsPortal", ["odds"],
        coverage_notes="Large couverture web, mais aucune API officielle.",
        coverage_score="UNKNOWN",
        history="UNKNOWN",
        timestamp_quality="N/A — pas d'API.",
        leakage_risk="UNKNOWN",
        cost_category="UNKNOWN",
        cost_notes="N/A — pas de produit API commercialisé.",
        commercial_usage="RESTRICTED",
        storage_rights_notes="CGU officielles (oddsportal.com/terms/, art. 2.10/2.11) : interdiction explicite d'extraction/scraping d'une partie substantielle de la base de données sans consentement exprès.",
        redistribution_notes="CGU art. 2.2 : interdiction d'usage commercial du site/contenu sans autorisation.",
        integration_complexity="HIGH",
        historical_reconstruction="UNKNOWN",
        duplicate_of_existing=False,
        duplicate_notes="N/A.",
        sources=["https://www.oddsportal.com/terms/"],
        notes="AUCUNE voie d'accès conforme identifiée (pas d'API officielle, scraping explicitement interdit par les CGU) — écarté d'office, ne pas explorer davantage (§65 : ne pas recommander le scraping).",
    ),

    # =========================================================================
    # INJURIES / SUSPENSIONS (§14-§17, §61)
    # =========================================================================

    _mk(
        "API-Football / API-Sports — Injuries", ["injuries", "suspensions"],
        coverage_notes="Page marketing /coverage n'a PAS de colonne Injuries dédiée — couverture par ligue non vérifiable via cette page. Coverage réelle exposée via `seasons[].coverage.injuries` (spec OpenAPI), variable par saison/ligue, non vérifiée live pour les 11 ligues.",
        coverage_score="UNKNOWN",
        history="PARTIAL_HISTORY",
        timestamp_quality="Rattaché à un `fixture` précis (donc une date passée connue) mais AUCUN champ `reported_at`/`published_at` documenté — impossible de savoir avec certitude quand l'information est devenue publique, seulement à quel match elle se rapporte. Données disponibles seulement depuis avril 2021 (endpoint récent, cite la doc officielle) — ne couvre pas 2019-2021 du dataset Xfoot.",
        leakage_risk="MEDIUM",
        cost_category="FREE",
        cost_notes="Inclus sur tous les plans dont Free (page pricing officielle, vérifié 2026-08-30).",
        commercial_usage="LEGAL_REVIEW_REQUIRED",
        storage_rights_notes="Mêmes CGU que Odds ci-dessus (même fournisseur) — revente brute interdite, produit à valeur ajoutée autorisé.",
        redistribution_notes="Mêmes réserves que Odds (clause \"betting platforms\").",
        integration_complexity="LOW",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication (Xfoot n'a aucune donnée de blessure).",
        sources=["https://www.api-football.com/public/doc/openapi.yaml (section /injuries, capture Wayback 2025-11-02)"],
        notes="Types documentés : \"Missing Fixture\" / \"Questionable\". Champ `reason` texte libre (\"Broken ankle\", \"Suspended\", etc.) — couvre donc PARTIELLEMENT les suspensions aussi, sans distinction structurée.",
    ),

    _mk(
        "Sportmonks — Injuries/Suspensions (module sidelined)", ["injuries", "suspensions"],
        coverage_notes="Inclus sur tous les plans payants — couverture ligue précise dépend du plan choisi (30 ligues sur Growth €99/mois probablement suffisant pour les 11 de Xfoot, non confirmé ligue par ligue).",
        coverage_score="PARTIAL",
        history="PARTIAL_HISTORY",
        timestamp_quality="`start_date`/`end_date` par période d'indisponibilité (`sidelinedHistory` = historique complet). AUCUN champ `reported_at`/`published_at` documenté — même limite que API-Football.",
        leakage_risk="MEDIUM",
        cost_category="MEDIUM",
        cost_notes="Starter €29/mois à Enterprise sur devis (voir grille Odds ci-dessus, même fournisseur/mêmes plans).",
        commercial_usage="ALLOWED",
        storage_rights_notes="CGU officielles (sportmonks.com/terms-of-service) : usage commercial autorisé sur tous les plans payants, revente des données brutes interdite sans accord.",
        redistribution_notes="Stockage/exploitation pour son propre produit autorisé ; reproduction/distribution des \"services\" interdite sans permission.",
        integration_complexity="MEDIUM",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://www.sportmonks.com/glossary/injuries-and-suspensions/", "https://www.sportmonks.com/terms-of-service/"],
        notes="Termes commerciaux plus explicites/permissifs que API-Football pour ce domaine (licence commerciale confirmée sans réserve \"betting platform\").",
    ),

    _mk(
        "Sportradar — Injuries (Rosters/Lineups/Transfers/Injuries)", ["injuries", "suspensions", "lineups"],
        coverage_notes="UNKNOWN — schéma exact et couverture par ligue non documentés publiquement (accès sandbox/contrat requis).",
        coverage_score="UNKNOWN",
        history="UNKNOWN",
        timestamp_quality="UNKNOWN / NEEDS CONFIRMATION — documentation détaillée derrière accès contractuel.",
        leakage_risk="UNKNOWN",
        cost_category="ENTERPRISE",
        cost_notes="AUCUNE tarification publique confirmée officiellement. Estimations tierces NON officielles évoquent 1300-4990€/mois voire 10000+$/mois sur engagement annuel — à traiter comme purement indicatif.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="Gérées au cas par cas par contrat — UNKNOWN sans engagement commercial.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        integration_complexity="HIGH",
        historical_reconstruction="UNKNOWN",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=[
            "https://developer.sportradar.com/soccer/docs/soccer-ig-rosters-lineups-transfers",
            "https://developer.sportradar.com/soccer/reference/soccer-faq",
        ],
        notes="Modèle B2B (bookmakers, diffuseurs, grands médias) avec cycle de vente commercial — probablement disproportionné pour une petite SaaS comme Xfoot, sans exclusion officielle explicite d'un seuil de taille.",
    ),

    _mk(
        "Opta / Stats Perform — Injuries & Lineups", ["injuries", "lineups"],
        coverage_notes="3900+ compétitions revendiquées (marketing) — couverture précise des 11 ligues Xfoot non vérifiable (doc technique fermée, accès contractuel requis).",
        coverage_score="UNKNOWN",
        history="UNKNOWN",
        timestamp_quality="UNKNOWN / NEEDS CONFIRMATION — documentation détaillée (developers.statsperform.com) inaccessible sans compte/contrat.",
        leakage_risk="UNKNOWN",
        cost_category="ENTERPRISE",
        cost_notes="100% vente commerciale, aucun tarif public. FAQ officielle précise travailler avec \"many startups and smaller organisations\" mais sans grille tarifaire self-service.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="Licences personnalisées par compétition/pays — UNKNOWN en détail sans contrat.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        integration_complexity="HIGH",
        historical_reconstruction="UNKNOWN",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://www.statsperform.com/products/opta-data-feeds/", "https://www.statsperform.com/stats-perform-faqs-pricing-and-licensing/"],
        notes="Marketing mentionne changements de statut de blessure + confirmations de compositions, mais schéma exact non vérifiable publiquement.",
    ),

    _mk(
        "SportsData.io (via RapidAPI / direct) — Injuries & Lineups", ["injuries", "lineups"],
        coverage_notes="Essai gratuit limité à la Champions League uniquement ; accès production = contact commercial obligatoire.",
        coverage_score="POOR",
        history="PARTIAL_HISTORY",
        timestamp_quality="Produit \"Replay\" annoncé comme conservant les données \"exactement comme elles se sont produites\" (horodatage réel) — le signal le plus prometteur trouvé dans cette recherche pour l'anti-leakage, mais historiquement orienté sports US (NFL/NBA/MLB) ; portée exacte pour foot/blessures NON CONFIRMÉE.",
        leakage_risk="MEDIUM",
        cost_category="UNKNOWN",
        cost_notes="Estimation tierce NON officielle ~500-1000+$/mois — accès production nécessite contact commercial, aucun tarif public confirmé.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        integration_complexity="MEDIUM",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://sportsdata.io/soccer-api", "https://sportsdata.io/cart/free-trial/soccer"],
        notes="Produit Replay à vérifier directement par contact commercial avant tout jugement définitif — piste la plus intéressante du marketplace RapidAPI pour l'anti-leakage, mais non confirmée pour le football.",
    ),

    # =========================================================================
    # LINEUPS (§18-§19, §62)
    # =========================================================================

    _mk(
        "API-Football / API-Sports — Lineups", ["lineups"],
        coverage_notes="Marketing /coverage montre les 11 ligues cochées pour Lineups, mais dépend de `leagues[].coverage` par compétition (variable, non vérifié saison par saison).",
        coverage_score="GOOD",
        history="CURRENT_ONLY",
        timestamp_quality=(
            "Pas de distinction PREDICTED/CONFIRMED (un seul jeu de données). Citation officielle exacte : "
            "\"Lineups are available between 20 and 40 minutes before the fixture when the competition covers "
            "this feature […] for some competitions the lineups are not available before the fixture, in this "
            "case, they are updated and available after the match\". Ceci est PLUS TARDIF que le standard "
            "~60 min avant coup d'envoi et, pour certaines compétitions, disponible SEULEMENT APRÈS le match."
        ),
        leakage_risk="HIGH",
        cost_category="FREE",
        cost_notes="Inclus sur tous les plans dont Free.",
        commercial_usage="LEGAL_REVIEW_REQUIRED",
        storage_rights_notes="Mêmes CGU que Odds/Injuries (même fournisseur).",
        redistribution_notes="Mêmes réserves \"betting platform\".",
        integration_complexity="LOW",
        historical_reconstruction="NO",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://www.api-football.com/public/doc/openapi.yaml (section /fixtures/lineups)"],
        notes="§19 du prompt : une composition confirmée après kickoff = REJECTED par construction. Cette source expose exactement ce risque pour certaines compétitions — leakage_risk=HIGH délibérément, jamais LOW malgré le coût nul.",
    ),

    _mk(
        "Sportmonks — Lineups + Predicted Lineups (add-on)", ["lineups"],
        coverage_notes="Dépend du plan (5 à 2300+ ligues) — couverture précise des 11 ligues Xfoot non vérifiée.",
        coverage_score="PARTIAL",
        history="CURRENT_ONLY",
        timestamp_quality=(
            "SEUL fournisseur étudié distinguant EXPLICITEMENT composition PRÉDITE (algorithmique, publiée en "
            "amont, précision annoncée 75-88% selon compétition) de composition CONFIRMÉE (`lineup_confirmed="
            "true`, ~1h avant coup d'envoi). Historique des prédictions passées NON documenté — impossible de "
            "vérifier une composition prédite telle qu'elle existait à une date T passée."
        ),
        leakage_risk="MEDIUM",
        cost_category="MEDIUM",
        cost_notes="Grille standard (voir Odds ci-dessus) + add-on \"Premium Expected Lineups\" dont le tarif exact n'est pas publié — UNKNOWN.",
        commercial_usage="ALLOWED",
        storage_rights_notes="Mêmes CGU que Injuries Sportmonks ci-dessus.",
        redistribution_notes="Idem.",
        integration_complexity="MEDIUM",
        historical_reconstruction="NO",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=[
            "https://docs.sportmonks.com/v3/tutorials-and-guides/tutorials/includes/predicted-lineups",
            "https://docs.sportmonks.com/v3/tutorials-and-guides/tutorials/includes/lineups",
        ],
        notes="Le seul fournisseur du marché qui répond structurellement à l'exigence §19 (PREDICTED vs CONFIRMED) — mais l'absence d'historique des prédictions passées empêche un backtest walk-forward rigoureux tant que Xfoot n'a pas lui-même constitué son propre historique en collectant prospectivement.",
    ),

    _mk(
        "FotMob (aucune API officielle)", ["lineups", "injuries"],
        coverage_notes="N/A — pas d'API.",
        coverage_score="UNKNOWN",
        history="UNKNOWN",
        timestamp_quality="N/A.",
        leakage_risk="UNKNOWN",
        cost_category="UNKNOWN",
        cost_notes="N/A — aucun produit API commercialisé.",
        commercial_usage="RESTRICTED",
        storage_rights_notes=(
            "CGU officielles (fotmob.com/tos.txt, citation exacte) : \"Use of the data, content, or any "
            "information displayed on FotMob for any purpose, including but not limited to scraping, "
            "reproduction, redistribution, or commercial purposes, without the express written consent of "
            "FotMob is strictly prohibited.\""
        ),
        redistribution_notes="Interdit explicitement — voir storage_rights_notes.",
        integration_complexity="HIGH",
        historical_reconstruction="UNKNOWN",
        duplicate_of_existing=False,
        duplicate_notes="N/A.",
        sources=["https://www.fotmob.com/tos.txt"],
        notes="Wrappers communautaires non officiels existent (PyPI/npm) mais enfreindraient explicitement ces CGU — DO_NOT_USE, ne pas explorer davantage.",
    ),

    _mk(
        "Transfermarkt (aucune API officielle confirmée)", ["lineups", "injuries"],
        coverage_notes="N/A — pas d'API officielle identifiée.",
        coverage_score="UNKNOWN",
        history="UNKNOWN",
        timestamp_quality="N/A.",
        leakage_risk="UNKNOWN",
        cost_category="UNKNOWN",
        cost_notes="N/A.",
        commercial_usage="RESTRICTED",
        storage_rights_notes=(
            "Aucun programme API officiel trouvé (multiplication de wrappers explicitement étiquetés \"non "
            "officiels\" sur GitHub, services de scraping tiers payants). Le texte exact des CGU anti-scraping "
            "de Transfermarkt N'A PAS pu être récupéré (page bloquée pour l'outil de recherche) — traité par "
            "analogie avec FotMob (pattern quasi-systématique sur ce type de site) plutôt que confirmé "
            "directement -> RESTRICTED par prudence, à corriger si une vérification directe l'infirme."
        ),
        redistribution_notes="Idem — présumé restreint par analogie, non confirmé verbatim.",
        integration_complexity="HIGH",
        historical_reconstruction="UNKNOWN",
        duplicate_of_existing=False,
        duplicate_notes="N/A.",
        sources=["https://github.com/felipeall/transfermarkt-api (wrapper non officiel, cité comme preuve d'absence d'API officielle)"],
        notes="DO_NOT_USE par précaution (scraping non recommandé) — mais NOTE D'HONNÊTETÉ : contrairement à FotMob, la clause exacte des CGU Transfermarkt n'a pas pu être citée verbatim (accès bloqué) ; à revérifier manuellement avant de considérer ce verdict comme définitif.",
    ),

    # =========================================================================
    # STANDINGS (§20-§21)
    # =========================================================================

    _mk(
        "API-Football / API-Sports — Standings", ["standings"],
        coverage_notes="Toutes les 11 ligues Xfoot cochées sur la page marketing /coverage.",
        coverage_score="GOOD",
        history="CURRENT_ONLY",
        timestamp_quality="AUCUN paramètre `date` sur l'endpoint /standings (seuls `league`, `season`, `team`) — impossible d'interroger \"le classement tel qu'il était à la date X\", seulement le classement le plus récent connu pour une saison.",
        leakage_risk="HIGH",
        cost_category="FREE",
        cost_notes="Inclus sur tous les plans dont Free.",
        commercial_usage="LEGAL_REVIEW_REQUIRED",
        storage_rights_notes="Mêmes CGU que les autres domaines API-Football.",
        redistribution_notes="Idem.",
        integration_complexity="LOW",
        historical_reconstruction="NO",
        duplicate_of_existing=True,
        duplicate_notes="DUPLIQUE l'information déjà produite en interne par Xfoot (Phase 8B, build_league_standing_features) — et en moins bien (pas de reconstruction point-in-time du tout ici).",
        sources=["https://www.api-football.com/public/doc/openapi.yaml (section /standings)"],
        notes="Confirme que la reconstruction interne de Xfoot (déjà implémentée en recherche, Phase 8B, verdict EQUIVALENT) reste la SEULE voie pour un classement point-in-time — cet endpoint ne le permet structurellement pas.",
    ),

    _mk(
        "football-data.org — Standings", ["standings"],
        coverage_notes="Confirmé couvert : Premier League, LaLiga, Bundesliga, Serie A, Ligue 1, Champions League, Europa League, Conference League, Primeira Liga (9/11). Saudi Pro League CONFIRMÉ ABSENT. MLS : statut contradictoire entre deux vérifications -> UNKNOWN.",
        coverage_score="PARTIAL",
        history="PARTIAL_HISTORY",
        timestamp_quality="Filtres `season`, `matchday`, `date` disponibles — MAIS citation officielle : \"resulting standings are compiled by match information only, so they lack possible deducted points\" -> c'est lui-même un RECALCUL à partir des résultats de matchs (le même calcul que Xfoot fait déjà en interne), pas une archive officielle figée.",
        leakage_risk="LOW",
        cost_category="FREE",
        cost_notes="Free (0€, 12 compétitions, 10 req/min) à Pro (199€/mois, 100 compétitions). Grille officielle vérifiée 2026-08-30.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="Attribution obligatoire (\"Football data provided by the Football-Data.org API\"). Page /terms en 404 au moment du contrôle — CGU détaillées non vérifiables -> UNKNOWN.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION (voir storage_rights_notes).",
        integration_complexity="LOW",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=True,
        duplicate_notes="Recalcule la MÊME information que la reconstruction interne Xfoot déjà bâtie (Phase 8B) — avec en plus une couverture ligues incomplète et sans gestion des pénalités de points (explicitement retirées par le fournisseur lui-même).",
        sources=["https://docs.football-data.org/general/v4/competition.html", "https://www.football-data.org/pricing", "https://www.football-data.org/coverage"],
        notes="N'apporte aucun avantage structurel démontré sur ce que Xfoot sait déjà faire en interne (même verdict EQUIVALENT attendu).",
    ),

    _mk(
        "Sportmonks — Standings (avec Standing Correction)", ["standings"],
        coverage_notes="Large couverture (2300+ ligues au catalogue Enterprise) — sélection par plan, couverture précise des 11 ligues Xfoot non vérifiée ligue par ligue.",
        coverage_score="PARTIAL",
        history="PARTIAL_HISTORY",
        timestamp_quality="Endpoint `standings/rounds/{round_id}` = classement à une journée donnée (plus fin qu'un simple filtre date). Endpoint `standings/corrections/seasons/{season_id}` suggère la conservation des CORRECTIONS OFFICIELLES (pénalités de points) — un avantage potentiel réel sur la reconstruction interne de Xfoot, qui ne peut PAS connaître ces pénalités à partir des seuls résultats de matchs. Portée exacte NON CONFIRMÉE (nécessite essai empirique).",
        leakage_risk="LOW",
        cost_category="MEDIUM",
        cost_notes="Growth €99/mois (30 ligues) probablement suffisant en volume ; add-on historique €29 (portée exacte non détaillée).",
        commercial_usage="ALLOWED",
        storage_rights_notes="Licence commerciale confirmée sur tous les plans payants.",
        redistribution_notes="Revente brute interdite, usage produit interne autorisé.",
        integration_complexity="MEDIUM",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=False,
        duplicate_notes="PAS une pure duplication si l'endpoint de corrections (pénalités de points) est confirmé exhaustif à l'essai — c'est la seule piste standings de cette recherche qui apporterait potentiellement une information que Xfoot ne peut PAS reconstruire lui-même.",
        sources=["https://docs.sportmonks.com/football/endpoints-and-entities/endpoints/standings", "https://www.sportmonks.com/football-api/plans-pricing/"],
        notes="Seul candidat standings avec un avantage structurel potentiel (pénalités de points) — mais NON confirmé empiriquement, à valider via l'essai gratuit 14 jours avant toute décision.",
    ),

    # =========================================================================
    # WEATHER (§22-§23)
    # =========================================================================

    _mk(
        "Visual Crossing Weather API", ["weather"],
        coverage_notes="Couverture mondiale par lat/lon — nécessite une table de correspondance stade->coordonnées à construire par Xfoot (coût d'intégration transversal, indépendant du fournisseur).",
        coverage_score="EXCELLENT",
        history="FULL_HISTORY",
        timestamp_quality="Observations RÉELLES (pas des prévisions archivées), granularité horaire et sub-horaire, depuis 1970 — couvre l'intégralité de l'historique Xfoot (2019-2026).",
        leakage_risk="LOW",
        cost_category="FREE",
        cost_notes="Free : 1000 enregistrements/jour, usage commercial ET non-commercial explicitement autorisé. Pay-as-you-go : 0,0001$/enregistrement. Professional 35$/mois, Corporate 150$/mois.",
        commercial_usage="ALLOWED",
        storage_rights_notes="Usage commercial autorisé dès le tier gratuit (page pricing officielle).",
        redistribution_notes="Non détaillé spécifiquement — pas de restriction trouvée sur le stockage interne.",
        integration_complexity="LOW",
        historical_reconstruction="YES",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication (Xfoot n'a aucune donnée météo).",
        sources=["https://www.visualcrossing.com/weather-api/", "https://www.visualcrossing.com/weather-data-pricing/"],
        notes="Meilleur candidat technique du lot météo — mais la VALEUR PRÉDICTIVE reste non démontrée (littérature sport-analytics : effets météo généralement faibles/inconsistants). Candidate à un futur test walk-forward, jamais une conclusion \"météo améliore le modèle\" (§23/§60).",
    ),

    _mk(
        "OpenWeatherMap — History API / History Bulk", ["weather"],
        coverage_notes="Mondiale (lat/lon) — même coût de mapping stade->coordonnées que les autres fournisseurs météo.",
        coverage_score="GOOD",
        history="FULL_HISTORY",
        timestamp_quality="Observations réelles, granularité horaire, depuis 1979 (History API by timestamp / History Bulk) — distinct de \"One Call API 3.0\" qui ne conserve que 5 jours (à ne pas confondre, produit différent).",
        leakage_risk="LOW",
        cost_category="UNKNOWN",
        cost_notes="Page pricing officielle rendue en JavaScript, non extractible par l'outil de recherche — tarif exact NON CONFIRMÉ officiellement. Free tier confirmé à 1000 appels/jour (toutes API confondues). Un tarif de 0,0015$/appel au-delà est rapporté par plusieurs trackers tiers NON officiels, à ne pas considérer comme confirmé.",
        commercial_usage="UNKNOWN",
        storage_rights_notes="UNKNOWN / NEEDS CONFIRMATION.",
        redistribution_notes="UNKNOWN / NEEDS CONFIRMATION.",
        integration_complexity="MEDIUM",
        historical_reconstruction="YES",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://openweathermap.org/api/history-api-timestamp", "https://openweathermap.org/api/history-bulk"],
        notes="Couverture historique suffisante en théorie, mais opacité tarifaire officielle (page JS non extractible) et offre éclatée en plusieurs produits (One Call vs History API vs History Bulk) -> risque de confusion à l'intégration.",
    ),

    _mk(
        "Meteostat", ["weather"],
        coverage_notes="Mondiale par station météo (raisonnement par station la plus proche, pas par point géographique arbitraire) — mapping stade->station à construire, plus complexe que lat/lon direct.",
        coverage_score="GOOD",
        history="FULL_HISTORY",
        timestamp_quality="Observations réelles agrégées depuis des sources officielles (NOAA, DWD, rapports METAR/SYNOP), granularité horaire (bulk data Parquet).",
        leakage_risk="LOW",
        cost_category="FREE",
        cost_notes="Données bulk gratuites (fichiers Parquet annuels, sans clé API). Accès JSON hébergé via RapidAPI : 500 appels/mois gratuits, tarif premium au-delà non confirmé -> UNKNOWN pour ce mode d'accès spécifique.",
        commercial_usage="ALLOWED",
        storage_rights_notes="Licence CC BY 4.0 CONFIRMÉE officiellement (citation exacte : \"copy and redistribute the material in any medium or format for any purpose, even commercially\") — attribution obligatoire, mais la licence la plus permissive et la plus claire des 4 fournisseurs météo étudiés.",
        redistribution_notes="CC BY 4.0 autorise explicitement la redistribution avec attribution.",
        integration_complexity="HIGH",
        historical_reconstruction="YES",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication.",
        sources=["https://dev.meteostat.net/data/bulk", "https://dev.meteostat.net/api", "https://dev.meteostat.net/license"],
        notes="Meilleure licence (gratuite, CC BY 4.0, usage commercial explicitement autorisé) mais complexité d'intégration la plus élevée (parsing Parquet + mapping station météo, pas de lat/lon direct) — à réserver si le volume dépasse les tiers gratuits des concurrents.",
    ),

    _mk(
        "WeatherAPI.com", ["weather"],
        coverage_notes="Mondiale — même coût de mapping stade->coordonnées que les autres fournisseurs météo.",
        coverage_score="PARTIAL",
        history="PARTIAL_HISTORY",
        timestamp_quality="RÉSERVE IMPORTANTE, citation officielle exacte : \"Our historical weather data is made up of forecast data and not from actuals\" — les données \"historiques\" sont en réalité des PRÉVISIONS ARCHIVÉES (au lendemain minuit), PAS des observations réelles, contrairement à Visual Crossing/OpenWeatherMap/Meteostat. Différence qualitative majeure pour un usage ML rétrospectif.",
        leakage_risk="MEDIUM",
        cost_category="LOW",
        cost_notes="Free (100K appels/mois, historique limité à 1 jour) ; Starter 7$/mois (7 jours d'historique) ; Pro+ 25$/mois (historique complet depuis 2010) ; Business 65$/mois.",
        commercial_usage="ALLOWED",
        storage_rights_notes="Usage commercial autorisé à tous les tiers (lien retour requis en gratuit).",
        redistribution_notes="Non détaillé au-delà du lien retour requis en tier gratuit.",
        integration_complexity="LOW",
        historical_reconstruction="PARTIAL",
        duplicate_of_existing=False,
        duplicate_notes="Aucune duplication directe, mais qualité de la donnée historique en cause (voir timestamp_quality).",
        sources=["https://www.weatherapi.com/docs/", "https://www.weatherapi.com/pricing.aspx"],
        notes="À ÉVITER pour un usage ML rétrospectif tant que l'écart prévision-archivée/observation-réelle n'est pas quantifié empiriquement — le point le plus faible des 4 fournisseurs météo sur la fiabilité de la donnée elle-même, malgré une intégration simple et un coût correct.",
    ),
]


def validate_all() -> list[str]:
    problems = []
    for p in PROVIDERS:
        problems.extend(validate_provider(p))
    return problems


# ---------------------------------------------------------------------------
# §42 Data Domain Priority — synthèse manuelle, réutilisant Phase 8A (gaps)
# et Phase 8B (standings interne EQUIVALENT) comme faits déjà établis.
# ---------------------------------------------------------------------------

DOMAIN_PRIORITY = [
    {
        "domain": "odds", "potential_value": "HIGH (candidat Value Engine futur — jamais \"odds=vérité\", §60)",
        "availability": "PARTIAL — aucune source ne couvre les 11 ligues Xfoot ET un historique profond ET un usage commercial sans réserve simultanément",
        "historical_quality": "PARTIAL — football-data.co.uk (gratuit, 6/11 ligues, historique complet) est la seule combinaison FULL_HISTORY+FREE, mais couverture ligues incomplète",
        "leakage_risk": "LOW à MEDIUM selon fournisseur (timestamps généralement documentés)",
        "cost": "FREE à MEDIUM (hors Betfair/Pinnacle, incertains)",
        "priority": "P0 (confirmé) — mais AUCUN fournisseur ne permet un backtest walk-forward complet sur les 11 ligues Xfoot dès aujourd'hui",
    },
    {
        "domain": "injuries", "potential_value": "MEDIUM (hypothèse non testée, §61 — qualité du timing/statut plus importante que le volume)",
        "availability": "PARTIAL — API-Football (déjà intégré) depuis avril 2021 seulement ; alternatives enterprise (Sportradar/Opta) hors budget probable",
        "historical_quality": "POOR — AUCUN fournisseur étudié ne documente un timestamp `reported_at`/`published_at` fiable",
        "leakage_risk": "MEDIUM à HIGH (documenté explicitement, §16)",
        "cost": "FREE (API-Football) à ENTERPRISE (Sportradar/Opta)",
        "priority": "P1 (dégradé par rapport à la priorité initiale P1, en raison de l'absence de timestamp de publication fiable sur toutes les sources étudiées)",
    },
    {
        "domain": "suspensions", "potential_value": "LOW-MEDIUM (souvent bundlé avec injuries, information partiellement structurée)",
        "availability": "PARTIAL — capturé de façon non structurée dans le champ `reason` d'API-Football (\"Suspended\")",
        "historical_quality": "POOR (même limite que injuries)",
        "leakage_risk": "MEDIUM à HIGH",
        "cost": "FREE à ENTERPRISE",
        "priority": "P2 (aucune source dédiée structurée trouvée — dépend entièrement du domaine injuries)",
    },
    {
        "domain": "lineups", "potential_value": "MEDIUM — mais risque structurel de fuite élevé pour la variante \"confirmée\" (§62)",
        "availability": "PARTIAL — API-Football (20-40 min avant, parfois après le match), Sportmonks (distinction predicted/confirmed, la seule trouvée)",
        "historical_quality": "POOR/UNKNOWN — aucun fournisseur ne conserve d'historique des compositions PRÉDITES passées",
        "leakage_risk": "HIGH pour \"confirmed\" (trop proche du kickoff, parfois après) ; MEDIUM pour \"predicted\" (Sportmonks)",
        "cost": "FREE (API-Football) à MEDIUM (Sportmonks add-on)",
        "priority": "P1 — mais seule la variante PREDICTED (Sportmonks) est structurellement utilisable en anti-fuite ; la variante CONFIRMED (API-Football) est à haut risque",
    },
    {
        "domain": "standings", "potential_value": "LOW — la reconstruction interne Xfoot (Phase 8B) est DÉJÀ construite et testée (verdict EQUIVALENT, aucun gain démontré)",
        "availability": "GOOD (interne) — externe: API-Football (aucune reconstruction historique), football-data.org (recalcul identique à l'interne), Sportmonks (avantage potentiel via corrections de pénalités, non confirmé)",
        "historical_quality": "GOOD (interne, déjà testé anti-fuite) — externe majoritairement équivalent ou inférieur",
        "leakage_risk": "LOW (interne) ; HIGH pour API-Football (pas de paramètre date) ; LOW pour football-data.org/Sportmonks",
        "cost": "FREE (déjà construit en interne)",
        "priority": "P2 (démoté) — aucun fournisseur externe n'apporte un avantage clairement supérieur à la solution interne déjà validée, sauf piste Sportmonks (pénalités de points) à vérifier ponctuellement",
    },
    {
        "domain": "weather", "potential_value": "UNKNOWN/LOW_PRIORITY — littérature sport-analytics : effets généralement faibles et inconsistants, non spécifique à Xfoot",
        "availability": "GOOD (Visual Crossing : historique complet, gratuit jusqu'à 1000 enregistrements/jour)",
        "historical_quality": "GOOD (observations réelles chez Visual Crossing/OpenWeatherMap/Meteostat) — MEDIUM chez WeatherAPI.com (prévisions archivées, pas des observations)",
        "leakage_risk": "LOW",
        "cost": "FREE à LOW",
        "priority": "P2 — disponible et peu coûteux, mais valeur prédictive non démontrée ; candidat à un test walk-forward futur à faible coût, jamais une priorité immédiate",
    },
]


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def _fmt_list(items: list[str]) -> str:
    return "; ".join(items) if items else "Aucune"


def render_markdown(result: dict) -> str:
    md = ["# XFOOT EXTERNAL DATA SOURCE AUDIT V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}. Recherche effectuée le {VERIFIED_DATE}.\n")
    md.append("\nRÈGLE ABSOLUE : AUDIT UNIQUEMENT. Aucune intégration, aucune clé API, aucune modification production.\n")
    md.append(f"\n- {len(result['providers'])} fournisseurs évalués sur 6 domaines (odds, injuries, suspensions, lineups, standings, weather).\n")
    md.append(f"- Verdicts : {result['verdict_counts']}\n")
    md.append(f"- Aucune source n'est RECOMMENDED FOR MVP sans réserve (voir §29/§30) — {result['recommended_for_mvp_count']} source(s) atteignent ce niveau, toujours avec une réserve documentée.\n")

    md.append("\n## 2. Current Xfoot Data Gaps\n")
    md.append("\nConfirmé par la Phase 8A (Data Intelligence & Feature Registry V1, reports/data/) : Xfoot n'a AUCUNE donnée de cote, blessure, suspension, composition ou météo. Le classement (standings) a depuis été reconstruit EN INTERNE (Phase 8B, verdict EQUIVALENT — aucun gain démontré vs baseline).\n")
    md.append(f"\n- Ligues en base locale (5) : {', '.join(XFOOT_LEAGUES_DB)}\n- Ligues CSV source non chargées en base (6) : {', '.join(XFOOT_LEAGUES_CSV_ONLY)}\n")

    md.append("\n## 3. Research Methodology\n")
    md.append(
        "\nRecherche web menée via 4 threads parallèles (API-Football en profondeur ; alternatives odds ; "
        "alternatives injuries/suspensions/lineups ; alternatives standings/weather), sources officielles "
        "uniquement (documentation technique, pages tarifaires, CGU). Toute donnée non confirmable depuis "
        "une source officielle est marquée UNKNOWN / NEEDS CONFIRMATION — jamais devinée. Plusieurs pages "
        "officielles bloquant les requêtes automatisées (403 Cloudflare notamment sur api-football.com) ont "
        "été consultées via des captures archivées (Wayback Machine), avec la date de capture ET la date de "
        "vérification citées séparément.\n"
    )

    md.append("\n## 4. Odds Providers\n")
    md.append(
        "\n7 fournisseurs étudiés (voir §24/ODDS pour le détail chiffré) : API-Football (déjà intégré, tous "
        "plans dont Free, mais 7 jours d'historique rétroactif seulement), The Odds API (marché h2h/totals "
        "confirmés, BTTS non confirmé, historique payant depuis juin 2020), Betfair Exchange (bourse "
        "d'échange, pas un bookmaker classique, risque géographique France non résolu), Pinnacle (API "
        "publique fermée depuis juillet 2025, accès enterprise seulement), football-data.co.uk (CSV gratuit, "
        "1X2 + O/U 2.5 sur certaines ligues, licence non explicite), Sportmonks (42 marchés dont "
        "1X2/BTTS/O-U, mais rétention de 7 jours seulement), OddsPortal (aucune API officielle, scraping "
        "explicitement interdit par les CGU — écarté d'office).\n"
    )

    md.append("\n## 5. Odds Historical Availability\n")
    md.append(
        "\nClassification par fournisseur : FULL_HISTORY confirmé uniquement pour Betfair Historical Data "
        "Service (depuis 2016, granularité jusqu'à 50ms) et football-data.co.uk (depuis les années 1990 "
        "selon ligue, gratuit). PARTIAL_HISTORY pour API-Football (7 jours glissants seulement — inadapté à "
        "un backtest sur les 12459 matchs déjà en base) et The Odds API (depuis juin 2020 seulement, ~19 "
        "mois du dataset Xfoot resteraient non couverts). CURRENT_ONLY pour Sportmonks Premium Odds Feed "
        "(rétention 7 jours post-match). AUCUN fournisseur combine FULL_HISTORY + couverture des 11 ligues "
        "+ usage commercial sans réserve.\n"
    )

    md.append("\n## 6. Odds Timestamp\n")
    md.append(
        "\nAPI-Football : champ `update` par cote (ISO). The Odds API : snapshots historiques toutes les 5-10 "
        "minutes (produit payant séparé). football-data.co.uk : deux snapshots documentés par match "
        "(pré-clôture collecté vendredi/mardi après-midi, clôture identifiée par un suffixe \"C\" sur le code "
        "bookmaker) — pas un flux continu, mais deux points temporels clairs et exploitables pour une règle "
        "de cutoff. Betfair Historical : jusqu'à 50ms de granularité (tier Pro). Aucun de ces timestamps n'a "
        "été vérifié en conditions réelles (appel API live) dans cette phase — voir §52.\n"
    )

    md.append("\n## 7. Odds Movement\n")
    md.append(
        "\nReconstruction opening → movement → pre-match → closing : possible en théorie chez Betfair "
        "Historical (granularité fine) et The Odds API (snapshots réguliers, produit payant) ; PARTIELLEMENT "
        "possible chez football-data.co.uk (seulement 2 points : pré-clôture et clôture, pas de mouvement "
        "intermédiaire) ; NON disponible chez API-Football (fenêtre de 7 jours, mise à jour toutes les 3h, "
        "pas conçu comme un entrepôt de mouvement) ; NON disponible chez Sportmonks Odds standard (rétention "
        "trop courte). Aucune reconstruction complète et vérifiée n'a été testée empiriquement — audit "
        "documentaire uniquement (§54).\n"
    )

    md.append("\n## 8. Injury Providers\n")
    md.append(
        "\n5 fournisseurs étudiés : API-Football (déjà intégré, tous plans, données depuis avril 2021 "
        "seulement — endpoint récent), Sportmonks (module \"sidelined\", historique via start_date/end_date, "
        "tous plans payants), Sportradar (schéma exact non documenté publiquement, accès enterprise), Opta/"
        "Stats Perform (produits marketés mais documentation technique fermée, enterprise-only), SportsData.io "
        "(produit \"Replay\" prometteur pour l'horodatage mais portée foot/blessures non confirmée). "
        "CONSTAT COMMUN AUX 5 : aucun n'expose de champ `reported_at`/`published_at` documenté publiquement — "
        "voir §61/§16.\n"
    )

    md.append("\n## 9. Suspension Providers\n")
    md.append(
        "\nAucun fournisseur dédié spécifiquement aux suspensions n'a été identifié dans cette recherche — "
        "l'information est systématiquement bundlée avec les blessures (ex. API-Football : champ `reason` "
        "texte libre incluant \"Suspended\" parmi d'autres motifs, sans structuration séparée type "
        "durée/compétition/motif). Sportmonks \"sidelined\" couvre également ce cas via `type_id`. Aucune "
        "source n'offre une modélisation dédiée (durée exacte, instance disciplinaire, appel en cours) "
        "distincte du domaine injuries.\n"
    )

    md.append("\n## 10. Lineup Providers\n")
    md.append(
        "\n7 entrées étudiées (voir §24/LINEUPS). Point le plus important de ce domaine : **la distinction "
        "PREDICTED vs CONFIRMED (§18/§62) n'est explicitement documentée que chez Sportmonks** (add-on "
        "\"Premium Expected Lineups\", précision annoncée 75-88% selon compétition, composition confirmée "
        "marquée `lineup_confirmed=true` ~1h avant coup d'envoi). API-Football ne fait AUCUNE distinction "
        "(un seul jeu de données) et documente explicitement une disponibilité de 20 à 40 minutes avant le "
        "match SEULEMENT, avec certaines compétitions où la composition n'est disponible qu'APRÈS le match "
        "— un point de fuite documenté par le fournisseur lui-même. Sportradar/Opta : accès enterprise, "
        "détail non public. FotMob/Transfermarkt : aucune API officielle, scraping déconseillé.\n"
    )

    md.append("\n## 11. Standings Providers\n")
    md.append(
        "\n3 fournisseurs étudiés en plus de la reconstruction interne Xfoot (déjà bâtie, Phase 8B) : "
        "API-Football (aucun paramètre `date`, classement le plus récent seulement — ne permet PAS de "
        "reconstruction historique), football-data.org (reconstruit lui-même le classement à partir des "
        "résultats de matchs, en retirant explicitement les pénalités de points historiques — donc un "
        "recalcul comparable à celui de Xfoot, en moins complet), Sportmonks (endpoint \"Standing "
        "Correction\" suggérant la conservation des pénalités officielles — seul avantage structurel "
        "identifié sur la solution interne, non vérifié empiriquement).\n"
    )

    md.append("\n## 12. Weather Providers\n")
    md.append(
        "\n4 fournisseurs étudiés : Visual Crossing (meilleur candidat global — historique complet depuis "
        "1970, observations réelles, gratuit jusqu'à 1000 enregistrements/jour), OpenWeatherMap (historique "
        "complet depuis 1979 via des produits séparés de l'offre \"current/forecast\", mais tarification "
        "officielle non extractible/confirmée), Meteostat (licence la plus permissive — CC BY 4.0, données "
        "gratuites, mais intégration la plus complexe : fichiers Parquet + mapping par station météo plutôt "
        "que lat/lon direct), WeatherAPI.com (RÉSERVE MAJEURE : ses données \"historiques\" sont en réalité "
        "des prévisions archivées, pas des observations réelles — à éviter pour un usage ML rétrospectif "
        "sans validation empirique de l'écart). Coût transversal à tous : aucun fournisseur ne fournit "
        "nativement une correspondance stade→coordonnées GPS, à construire et maintenir en interne.\n"
    )

    md.append("\n## 13. League Coverage\n")
    md.append(f"\n5 ligues en base locale : {', '.join(XFOOT_LEAGUES_DB)}. 6 ligues CSV non chargées : {', '.join(XFOOT_LEAGUES_CSV_ONLY)}.\n")
    md.append("\nAucun fournisseur odds gratuit ne couvre les 11 ligues (football-data.co.uk : 6/11, absent sur Saudi Pro League + 3 coupes européennes). API-Football (déjà intégré) revendique une couverture marketing complète sur les 11, mais la couverture RÉELLE varie par saison (`coverage.odds`/`coverage.injuries`) et n'a pas pu être vérifiée saison-par-saison sans clé API live — UNKNOWN pour la profondeur historique par ligue.\n")

    md.append("\n## 14. Historical Coverage\n")
    md.append("\nAucune source odds ne couvre historiquement l'intégralité 2019-2026 du dataset Xfoot avec une couverture complète des 11 ligues. football-data.co.uk est FULL_HISTORY sur 6/11 ligues (gratuit). The Odds API ne remonte qu'à juin 2020 (manque ~10 mois du dataset). API-Football (déjà intégré) n'offre que 7 jours d'historique rétroactif — inutilisable pour le backtest sur les matchs déjà en base.\n")

    md.append("\n## 15. Timestamp Quality\n")
    md.append("\nMeilleure qualité de timestamp trouvée : Visual Crossing/Meteostat/OpenWeatherMap (weather, observations horaires réelles), Betfair Historical (odds, jusqu'à 50ms), football-data.co.uk (2 snapshots documentés : pré-clôture/clôture). Pire : injuries/lineups — AUCUN fournisseur étudié ne documente de champ `reported_at`/`published_at` fiable permettant de garantir qu'une information était bien connue avant un instant T donné.\n")

    md.append("\n## 16. Leakage Risk\n")
    md.append("\nVoir champ `leakage_risk` par fournisseur (§24). Point critique : API-Football Lineups et Standings sont classés HIGH (compositions parfois disponibles seulement après le match pour certaines compétitions ; aucun paramètre `date` sur /standings). Injuries (tous fournisseurs) : MEDIUM par défaut, faute de timestamp de publication documenté.\n")

    md.append("\n## 17. Data Quality\n")
    md.append("\nWeatherAPI.com : réserve qualité majeure — son \"historique\" est en réalité des prévisions archivées, pas des observations réelles (seul cas de ce type identifié). football-data.org standings : recalcul lui-même (retire les pénalités de points), pas une archive officielle. Les autres sources n'ont pas révélé de problème de qualité structurel équivalent dans la documentation consultée.\n")

    md.append("\n## 18. API Quality\n")
    md.append("\nTous les fournisseurs candidats retenus (hors Betfair/exchange) sont REST/JSON. Aucun ne documente de support GraphQL. Rate limits documentés pour API-Football (100 req/j Free à 1,5M Custom), The Odds API (système de crédits), football-data.org (10-120 req/min selon plan), Sportmonks (2000-5000 req/h selon plan). Aucun webhook documenté chez API-Football.\n")

    md.append("\n## 19. Pricing\n\n")
    md.append(result["pricing_summary"])

    md.append("\n## 20. Commercial Rights\n")
    md.append(
        "\nAPI-Football (déjà intégré, tous domaines) : LEGAL_REVIEW_REQUIRED — clause explicite sur les "
        "\"betting platforms\" nécessitant potentiellement des licences additionnelles, ambiguë pour un SaaS "
        "de pronostics comme Xfoot. Sportmonks : licence commerciale explicitement confirmée sur tous les "
        "plans payants, la plus claire de cette recherche. Betfair : accès en pure consommation de données "
        "explicitement NON PERMIS via la clé live. OddsPortal/FotMob : usage commercial explicitement INTERDIT.\n"
    )

    md.append("\n## 21. Storage Rights\n")
    md.append("\nAucune clause trouvée interdisant EXPLICITEMENT le stockage local d'un cache/historique chez API-Football ou Sportmonks (silence, pas une autorisation écrite non plus) — à faire confirmer par le support de chaque fournisseur avant tout stockage à long terme destiné au futur Track Record.\n")

    md.append("\n## 22. Redistribution Rights\n")
    md.append("\nAucun fournisseur étudié n'a été confirmé comme autorisant explicitement l'AFFICHAGE de ses données brutes aux utilisateurs finaux de Xfoot (USER-FACING DISPLAY) sans réserve — tous les usages confirmés le sont pour un usage INTERNAL USE (alimenter un modèle/produit), jamais pour republier la donnée brute telle quelle.\n")

    md.append("\n## 23. Integration Complexity\n")
    md.append("\nLOW : API-Football (déjà intégré), The Odds API, football-data.co.uk, football-data.org, Visual Crossing, WeatherAPI.com. MEDIUM : Sportmonks (tous domaines), OpenWeatherMap (offre éclatée), SportsData.io. HIGH : Betfair (compte + certification), Meteostat (parsing Parquet + mapping station), Sportradar/Opta (cycle commercial + intégration enterprise), FotMob/Transfermarkt/OddsPortal (aucune voie officielle).\n")

    md.append("\n## 24. Provider Scorecards\n\n")
    md.append("### PROVIDERS\n\n")
    md.append("| Provider | Domain | Coverage | History | Timestamp | Leakage | Cost | Commercial | Integration | Verdict |\n")
    md.append("|---|---|---|---|---|---|---|---|---|---|\n")
    for p in result["providers"]:
        md.append(
            f"| {p['name']} | {', '.join(p['domains'])} | {p['coverage_score']} | {p['history']} | "
            f"{'OK' if 'UNKNOWN' not in p['timestamp_quality'].upper() else 'incertain'} | {p['leakage_risk']} | "
            f"{p['cost_category']} | {p['commercial_usage']} | {p['integration_complexity']} | **{p['verdict']}** |\n"
        )

    md.append("\n## 25. Domain Priority\n\n")
    md.append("| Domain | Potential Value | Availability | Historical Quality | Leakage Risk | Cost | Priority |\n")
    md.append("|---|---|---|---|---|---|---|\n")
    for row in DOMAIN_PRIORITY:
        md.append(
            f"| {row['domain']} | {row['potential_value']} | {row['availability']} | {row['historical_quality']} | "
            f"{row['leakage_risk']} | {row['cost']} | {row['priority']} |\n"
        )

    md.append("\n## 26. Duplication Analysis\n\n")
    for p in result["providers"]:
        if p["duplicate_of_existing"]:
            md.append(f"- **{p['name']}** : {p['duplicate_notes']}\n")
    md.append("\nAucune autre source ne duplique une information déjà produite par Xfoot (odds/injuries/suspensions/lineups/weather = zéro donnée interne existante, confirmé Phase 8A).\n")

    md.append("\n## 27. Feature Value Hypotheses\n\n")
    md.append(
        "- **ODDS** : \"Les probabilités implicites pré-match apportent une information complémentaire au "
        "modèle Xfoot.\" — hypothèse NON démontrée, nécessite un test walk-forward (§45).\n"
        "- **INJURIES** : \"Les absences de joueurs clés apportent une information complémentaire aux ratings "
        "existants.\" — hypothèse NON démontrée ; la qualité du timing/statut prime sur le volume (§61).\n"
        "- **LINEUPS** : \"Une composition prédite fiable (Sportmonks, ~75-88% de précision annoncée) "
        "améliore la prédiction par rapport aux seuls ratings d'équipe.\" — NON démontrée, ET la variante "
        "prédite reste moins fiable que la variante confirmée par construction, à mettre en balance avec le "
        "risque de fuite de cette dernière.\n"
        "- **STANDINGS** : déjà testée en interne (Phase 8B) — verdict EQUIVALENT. Hypothèse d'un gain via "
        "les pénalités de points (Sportmonks) reste ouverte mais non testée.\n"
        "- **WEATHER** : \"Des conditions météo extrêmes (vent fort, pluie battante) affectent le rythme de "
        "jeu et donc le résultat.\" — hypothèse faible selon la littérature générale, non testée pour Xfoot.\n"
    )

    md.append("\n## 28. Historical Reconstruction\n\n")
    md.append("| Provider | Historical Reconstruction |\n|---|---|\n")
    for p in result["providers"]:
        md.append(f"| {p['name']} | {p['historical_reconstruction']} |\n")

    md.append("\n## 29. Top Providers\n\n")
    md.append(result["top_providers_text"])

    md.append("\n## 30. MVP Recommendation\n\n")
    md.append(result["mvp_recommendation"])

    md.append("\n## 31. Limitations\n\n")
    for item in result["limitations"]:
        md.append(f"- {item}\n")

    md.append("\n## 32. Recommendations Phase 8D\n\n")
    for item in result["recommendations_phase_8d"]:
        md.append(f"- {item}\n")

    md.append("\n---\n\n### ODDS\n\n| Provider | 1X2 | BTTS | O/U | History | Timestamp | Movement | Coverage | Cost | Verdict |\n|---|---|---|---|---|---|---|---|---|---|\n")
    for row in result["odds_scorecard"]:
        md.append(f"| {row['provider']} | {row['1x2']} | {row['btts']} | {row['ou']} | {row['history']} | {row['timestamp']} | {row['movement']} | {row['coverage']} | {row['cost']} | {row['verdict']} |\n")

    md.append("\n### INJURIES\n\n| Provider | History | Timestamp | Player Status | Coverage | Cost | Leakage | Verdict |\n|---|---|---|---|---|---|---|---|\n")
    for row in result["injury_scorecard"]:
        md.append(f"| {row['provider']} | {row['history']} | {row['timestamp']} | {row['status']} | {row['coverage']} | {row['cost']} | {row['leakage']} | {row['verdict']} |\n")

    md.append("\n### LINEUPS\n\n| Provider | Predicted | Confirmed | Timestamp | Historical | Coverage | Cost | Verdict |\n|---|---|---|---|---|---|---|---|\n")
    for row in result["lineup_scorecard"]:
        md.append(f"| {row['provider']} | {row['predicted']} | {row['confirmed']} | {row['timestamp']} | {row['history']} | {row['coverage']} | {row['cost']} | {row['verdict']} |\n")

    md.append("\n### PRIORITY\n\n| Domain | Potential Value | Data Quality | History | Leakage | Cost | Priority |\n|---|---|---|---|---|---|---|\n")
    for row in DOMAIN_PRIORITY:
        md.append(f"| {row['domain']} | {row['potential_value']} | {row['historical_quality']} | {row['historical_quality']} | {row['leakage_risk']} | {row['cost']} | {row['priority']} |\n")

    md.append("\n---\n\n## Database Safety (§56)\n\n")
    md.append(f"\nCompteurs AVANT : {result['db_counts_before']}\n\nCompteurs APRÈS : {result['db_counts_after']}\n\nIdentiques : {result['db_unchanged']}\n")

    md.append("\n## Production Isolation (§52)\n\nAucun client API de production créé. Aucun secret ajouté. Aucune migration. Aucune modification de model_predictions/prediction_log/model_versions/team_ratings/match/match_stats/scheduler/endpoints/Dashboard/Arena/frontend/modèles ML.\n")

    md.append("\n---\n\nPHASE 8C — XFOOT EXTERNAL DATA SOURCE AUDIT V1 TERMINÉE. AUCUNE INTÉGRATION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.\n")

    return "".join(md)


def build_scorecards(providers: list[dict]) -> dict:
    def find(name_substr):
        return next((p for p in providers if name_substr in p["name"]), None)

    odds_rows = []
    for p in providers:
        if "odds" not in p["domains"]:
            continue
        odds_rows.append({
            "provider": p["name"], "1x2": "oui" if "1X2" not in p["notes"] else "voir notes",
            "btts": "voir notes", "ou": "voir notes",
            "history": p["history"], "timestamp": "voir notes" if "UNKNOWN" not in p["timestamp_quality"].upper() else "UNKNOWN",
            "movement": "voir timestamp_quality", "coverage": p["coverage_score"], "cost": p["cost_category"],
            "verdict": p["verdict"],
        })

    injury_rows = [{
        "provider": p["name"], "history": p["history"],
        "timestamp": "PAS de reported_at documenté" if p["leakage_risk"] in ("MEDIUM", "HIGH", "UNKNOWN") else "documenté",
        "status": "voir notes", "coverage": p["coverage_score"], "cost": p["cost_category"],
        "leakage": p["leakage_risk"], "verdict": p["verdict"],
    } for p in providers if "injuries" in p["domains"]]

    lineup_rows = [{
        "provider": p["name"],
        "predicted": "OUI (distinct)" if "predicted" in p["name"].lower() or "Predicted" in p["notes"] else "NON confirmé",
        "confirmed": "OUI" if p["history"] != "UNKNOWN" else "UNKNOWN",
        "timestamp": "20-40min avant (parfois après)" if "API-Football" in p["name"] else "voir notes",
        "history": p["history"], "coverage": p["coverage_score"], "cost": p["cost_category"], "verdict": p["verdict"],
    } for p in providers if "lineups" in p["domains"]]

    return {"odds_scorecard": odds_rows, "injury_scorecard": injury_rows, "lineup_scorecard": lineup_rows}


def write_reports(result: dict, outdir: Path, run_id: str) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"external_data_audit_{run_id}.json"
    md_path = outdir / f"external_data_audit_{run_id}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def main():
    problems = validate_all()
    if problems:
        raise RuntimeError(f"Incohérences détectées dans les ProviderRecord — corriger avant de publier le rapport : {problems}")

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

    providers_dicts = [
        {k: v for k, v in vars(p).items()} for p in PROVIDERS
    ]
    scorecards = build_scorecards(providers_dicts)

    verdict_counts: dict[str, int] = {}
    for p in providers_dicts:
        verdict_counts[p["verdict"]] = verdict_counts.get(p["verdict"], 0) + 1

    pricing_lines = []
    for p in providers_dicts:
        pricing_lines.append(f"- **{p['name']}** : {p['cost_notes']}")
    pricing_summary = "\n".join(pricing_lines) + "\n"

    top_providers_text = (
        "**ODDS** — BEST DATA QUALITY : Betfair Historical Data Service (FULL_HISTORY depuis 2016, granularité fine) "
        "MAIS risque légal France non résolu (LEGAL_REVIEW_REQUIRED avant toute exploration). "
        "BEST PRICE/VALUE : football-data.co.uk (gratuit, historique complet, 6/11 ligues). "
        "BEST OVERALL (prospectif) : API-Football (déjà intégré, LOW intégration) SOUS RÉSERVE de revue légale "
        "(clause \"betting platforms\") et de sa limite de 7 jours d'historique rétroactif.\n\n"
        "**INJURIES** — Aucun BEST DATA QUALITY clair (aucune source ne documente de timestamp de publication "
        "fiable). BEST PRICE/VALUE : API-Football (déjà intégré, gratuit) malgré son historique partiel (depuis "
        "avril 2021) et sa réserve légale. Sportmonks en second choix (termes commerciaux plus clairs).\n\n"
        "**LINEUPS** — BEST DATA QUALITY (anti-fuite) : Sportmonks (seule source distinguant PREDICTED/CONFIRMED). "
        "BEST PRICE/VALUE : API-Football (gratuit) mais leakage_risk=HIGH — À NE PAS UTILISER pour un usage "
        "pré-match rigoureux tant que la fenêtre de publication n'est pas confirmée sûre par compétition.\n\n"
        "**STANDINGS** — Aucun fournisseur externe ne surpasse clairement la reconstruction interne Xfoot "
        "(Phase 8B). Seule piste : Sportmonks (corrections de pénalités de points), à vérifier ponctuellement.\n\n"
        "**WEATHER** — BEST OVERALL : Visual Crossing (historique complet, gratuit jusqu'à 1000 enregistrements/jour, "
        "intégration simple, observations réelles).\n"
    )

    mvp_recommendation = (
        "Aucun MVP externe n'est recommandé à l'issue de cette phase (§65 : ne pas conclure prématurément). "
        "Si une Phase 8D devait explorer une intégration réelle, la piste la moins risquée à tester en premier "
        "serait : **1 fournisseur Odds (football-data.co.uk, gratuit, historique complet, 6/11 ligues) "
        "UNIQUEMENT pour une validation walk-forward hors ligne** — pas d'intégration live, pas de clé API "
        "production, juste un test de la valeur du signal sur les 6 ligues couvertes, avant toute dépense. "
        "Ceci N'EST PAS une recommandation d'achat ni d'intégration — seulement l'option la moins coûteuse et "
        "la moins risquée pour un premier test empirique de l'hypothèse §44 ODDS."
    )

    limitations = [
        "Recherche documentaire (sources officielles/pages tarifaires/CGU), jamais une clé API live n'a été "
        "utilisée pour vérifier une couverture ligue-par-ligue ou saison-par-saison en direct (interdiction "
        "explicite de cette phase, §52) — plusieurs points restent marqués UNKNOWN pour cette raison précise.",
        "api-football.com bloque les requêtes automatisées non-navigateur (Cloudflare 403) — les pages "
        "officielles ont été consultées via des captures archivées (Wayback Machine), datées séparément de "
        "la date de vérification (2026-08-30).",
        "Betfair : le texte exact des CGU générales (restriction géographique France) n'a pas pu être "
        "confirmé par fetch direct (403) — s'appuie sur une source indexée tierce, à reconfirmer directement.",
        "Transfermarkt : absence d'API officielle confirmée par déduction (aucun programme développeur trouvé, "
        "écosystème de wrappers non officiels) mais le texte exact des CGU anti-scraping n'a pas pu être cité "
        "verbatim (page inaccessible à l'outil de recherche), contrairement à FotMob.",
        "Plusieurs tarifs (Sportradar, Opta/Stats Perform, add-on Sportmonks Premium Expected Lineups, "
        "OpenWeatherMap au-delà du tier gratuit) sont non-publics ou non confirmables officiellement — jamais "
        "estimés ni inventés dans ce rapport, systématiquement marqués UNKNOWN.",
        "Aucune vérification empirique (essai gratuit, appel API réel) n'a été effectuée pour aucun "
        "fournisseur — cette phase est un audit documentaire, pas un test technique (hors périmètre §52/§54).",
    ]

    recommendations_phase_8d = [
        "Si Xfoot souhaite avancer sur ODDS : lever d'abord le doute légal (clause \"betting platform\" "
        "d'API-Football, restriction géographique Betfair) avant toute dépense — un ticket support/juridique, "
        "pas du code.",
        "Vérifier en direct (clé API-Football existante, gratuite) la couverture réelle `seasons[].coverage."
        "odds`/`.injuries` pour les 11 ligues Xfoot — c'est un simple appel GET /leagues, pas une intégration, "
        "et lèverait plusieurs UNKNOWN de ce rapport à coût nul.",
        "Si un test walk-forward ODDS est un jour mené : le limiter aux 6 ligues football-data.co.uk (gratuit, "
        "historique complet) plutôt que de payer pour une couverture plus large avant d'avoir confirmé un "
        "signal réel.",
        "LINEUPS : n'explorer que la variante Sportmonks \"Predicted Lineups\" (seule à distinguer prédite/"
        "confirmée) — ne jamais utiliser une composition \"confirmée\" API-Football comme feature pré-match "
        "sans avoir vérifié, compétition par compétition, qu'elle est bien disponible AVANT le coup d'envoi.",
        "STANDINGS : ne pas investir davantage sauf vérification ponctuelle de l'endpoint Sportmonks "
        "\"Standing Correction\" (essai gratuit 14 jours) pour le cas spécifique des pénalités de points, "
        "seul avantage structurel identifié sur la reconstruction interne déjà validée.",
        "WEATHER : Visual Crossing est le candidat le moins coûteux à tester si Xfoot veut valider "
        "empiriquement l'hypothèse §44 — mais ce n'est PAS une priorité au vu de la littérature existante.",
    ]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    with Session(engine) as session:
        db_after = snapshot(session)

    result = {
        "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(), "verified_date": VERIFIED_DATE,
        "providers": providers_dicts,
        "verdict_counts": verdict_counts,
        "recommended_for_mvp_count": verdict_counts.get("RECOMMENDED_FOR_MVP", 0),
        "domain_priority": DOMAIN_PRIORITY,
        "pricing_summary": pricing_summary,
        "top_providers_text": top_providers_text,
        "mvp_recommendation": mvp_recommendation,
        "limitations": limitations,
        "recommendations_phase_8d": recommendations_phase_8d,
        "db_counts_before": db_before, "db_counts_after": db_after, "db_unchanged": db_before == db_after,
        **scorecards,
    }

    outdir = Path(__file__).resolve().parent.parent / "reports" / "external_data"
    json_path, md_path = write_reports(result, outdir, run_id)
    print(f"Rapport écrit : {json_path} / {md_path}")
    print("\n" + "=" * 80)
    print("PHASE 8C — XFOOT EXTERNAL DATA SOURCE AUDIT V1 TERMINÉE.")
    print("AUCUNE INTÉGRATION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


if __name__ == "__main__":
    main()
