"""
Génération et validation du slug promoteur (Phase 14, §6/§7).

Lien public : https://www.xfoot.site/{slug}

§6 : "Avant de réserver un slug, vérifier les routes existantes." — liste
dérivée d'une inspection réelle de ce dépôt (jamais une liste générique
copiée d'ailleurs) :
  - Toutes les pages statiques frontend-design/*.html (sans extension,
    puisque le lien promoteur est une URL "propre" sans .html — voir
    frontend-design/.htaccess) : index, login, register, billing, dashboard,
    history, live, vip, arena, design-system, promoter.
  - Tous les segments de premier niveau des routes API FastAPI réellement
    déclarées (api/main.py, app/auth/router.py, app/billing/router.py) :
    health, leagues, predictions, live-scores, ratings, fixtures, models,
    auth, billing.
  - Les nouvelles routes de CETTE phase elles-mêmes : promoter, promoters,
    admin, referral, r.
  - Mots réservés listés explicitement par le prompt (§6), même s'ils ne
    correspondent à aucune route réelle aujourd'hui — pour ne jamais bloquer
    une future route légitime avec un slug promoteur déjà pris : register,
    settings, support, pricing, subscription, logout, static, assets,
    favicon.ico, robots.txt, sitemap.xml.
"""

from __future__ import annotations

import re
import unicodedata

RESERVED_SLUGS = frozenset({
    # Pages statiques frontend-design/*.html (sans extension).
    "index", "login", "register", "billing", "dashboard", "history", "live",
    "vip", "arena", "design-system", "promoter",
    # Segments de premier niveau des routes API réelles.
    "health", "leagues", "predictions", "live-scores", "ratings", "fixtures",
    "models", "auth", "billing", "referral", "r", "promoters", "admin",
    # Mots réservés explicites du prompt (§6), même sans route réelle actuelle.
    "settings", "support", "pricing", "subscription", "logout", "api",
    "static", "assets", "favicon.ico", "robots.txt", "sitemap.xml", "www",
})

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")  # 1-40 chars, URL-safe, pas de slash/point
MIN_SLUG_LENGTH = 3
MAX_SLUG_LENGTH = 40


def normalize_slug(raw: str) -> str:
    """§6 : normalisé — minuscules (insensible à la casse), accents retirés, tout ce qui n'est pas
    [a-z0-9-] remplacé par '-', tirets répétés/en bordure collapsés. Jamais de slash (§6 : "pas de slash
    supplémentaire") — un '/' devient un '-' comme n'importe quel autre caractère non autorisé."""
    nfkd = unicodedata.normalize("NFKD", raw)
    without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = without_accents.lower()
    dashed = re.sub(r"[^a-z0-9]+", "-", lowered)
    return dashed.strip("-")[:MAX_SLUG_LENGTH].strip("-")


def is_valid_slug_format(slug: str) -> bool:
    """§6 : URL-safe, pas de caractère dangereux — format déjà garanti par normalize_slug() pour un slug
    généré par ce module, mais revalidé explicitement pour tout slug fourni par un promoteur (demande de
    changement, §7) — jamais une confiance implicite sur une entrée utilisateur."""
    if not (MIN_SLUG_LENGTH <= len(slug) <= MAX_SLUG_LENGTH):
        return False
    return bool(_SLUG_RE.match(slug))


def is_reserved_slug(slug: str) -> bool:
    return slug.lower() in RESERVED_SLUGS


def generate_base_slug(display_name: str) -> str:
    """§7 : "Jean Dupont" -> "jean-dupont". Repli sur "promoteur" si le nom ne produit aucun caractère
    exploitable (ex. nom composé uniquement d'emojis/symboles) — jamais un slug vide."""
    base = normalize_slug(display_name)
    if len(base) < MIN_SLUG_LENGTH:
        base = "promoteur"
    return base


def next_slug_candidate(base_slug: str, attempt: int) -> str:
    """§7 : "jean-dupont" -> "jean-dupont-2" -> "jean-dupont-3"... — mécanisme déterministe, jamais
    aléatoire (deux appels avec le même attempt donnent toujours le même résultat)."""
    if attempt <= 1:
        return base_slug
    suffix = f"-{attempt}"
    truncated_base = base_slug[: MAX_SLUG_LENGTH - len(suffix)]
    return f"{truncated_base}{suffix}"
