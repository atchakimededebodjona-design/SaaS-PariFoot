"""
Modèles du programme de promotion/affiliation (Phase 14).

Trois tables, strictement additives — aucune table existante n'est modifiée
(User, ProviderSubscription, Entitlement, et a fortiori les tables IA
match/match_stats/model_predictions/model_versions/prediction_log/
team_ratings, jamais touchées ici).

  - Promoter : un compte utilisateur réel devient promoteur (1-1 avec User).
  - ReferralAttribution : créée UNIQUEMENT au moment où un visiteur référé
    devient un compte réel (inscription) — jamais pour un simple clic
    anonyme, pour ne stocker aucune donnée personnelle inutile (§8 du
    prompt Phase 14 : "NE PAS stocker inutilement des données
    personnelles"). Le clic/la visite eux-mêmes restent purement côté
    client (localStorage, voir frontend-design/api.js::captureReferralFromUrl),
    jamais persistés côté serveur avant conversion.
  - ReferralCommission : le GRAND LIVRE (ledger) — SEULE source de vérité
    financière de ce module. Une ligne est créée UNIQUEMENT en réaction à un
    paiement RÉELLEMENT confirmé (voir app/referral/commission_service.py,
    appelé depuis app/billing/router.py::_handle_successful_sale) — jamais
    au clic, à l'inscription, à la création du checkout ni au choix d'un
    plan (§11/§52 du prompt).

Argent : XOF (FCFA) est une devise SANS sous-unité (pas de centimes) — les
montants sont donc des entiers dans leur unité courante directement (pas de
"minor units" façon centimes EUR/USD). Le calcul de commission utilise
UNIQUEMENT de l'arithmétique entière (jamais de float) — voir
app/referral/money.py::compute_commission_amount pour la règle d'arrondi
documentée (floor, déterministe, testée).
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Index, UniqueConstraint, text
from sqlmodel import SQLModel, Field


# ---------------------------------------------------------------------------
# §5 : statuts promoteur.
# ---------------------------------------------------------------------------

PROMOTER_STATUSES = ("ACTIVE", "INACTIVE", "SUSPENDED")

# §3 : taux V1 — représenté explicitement par ligne de ledger (jamais un
# taux global relu a posteriori), voir ReferralCommission.commission_rate_bp
# ci-dessous. Exprimé en points de base (1/100 de %) pour rester un entier
# exact : 4000 = 40.00%. Permet un futur taux différent par promoteur sans
# jamais réécrire l'historique (chaque ligne de ledger garde SON taux réel).
DEFAULT_COMMISSION_RATE_BP = 4000  # 40%


class Promoter(SQLModel, table=True):
    __tablename__ = "promoter"

    id: Optional[int] = Field(default=None, primary_key=True)
    # unique : un seul compte Promoter par utilisateur (§2 : "un promoteur
    # doit être associé à un compte utilisateur réel").
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)

    # §6 : normalisé en minuscules à l'écriture (voir app/referral/slug.py) —
    # unique et indexé, c'est la clé de résolution publique https://www.xfoot.site/{slug}.
    slug: str = Field(unique=True, index=True)

    status: str = Field(default="ACTIVE")  # PROMOTER_STATUSES

    # §3 : taux par défaut pour les NOUVELLES commissions de ce promoteur —
    # ne change JAMAIS une ligne de ledger déjà écrite (voir ReferralCommission.commission_rate_bp,
    # copié à la création, jamais relu depuis Promoter après coup).
    commission_rate_bp: int = Field(default=DEFAULT_COMMISSION_RATE_BP)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReferralAttribution(SQLModel, table=True):
    """
    §8/§9 : UNE ligne = UNE conversion (visiteur -> compte réel) réellement
    attribuée à un promoteur. Jamais créée pour un simple clic non converti.

    Règle d'attribution V1 (§9) : LAST VALID REFERRER — si un utilisateur
    n'a encore AUCUNE ReferralAttribution, la première conversion valide en
    crée une ; en cas de nouvelle tentative avec un slug DIFFÉRENT après
    coup, l'attribution existante n'est PAS écrasée (un utilisateur n'a
    qu'un seul compte, donc qu'une seule vraie conversion possible — "last
    valid referrer" s'applique côté CAPTURE client, en écrasant le référent
    stocké en localStorage avant inscription ; une fois l'inscription faite,
    l'attribution serveur est définitive, voir app/referral/router.py::attribute_referral).
    """
    __tablename__ = "referral_attribution"
    __table_args__ = (
        # Un utilisateur ne peut être attribué qu'à UN SEUL promoteur, une
        # seule fois (§9 : "dernier référent valide" se résout côté capture
        # client AVANT la création de cette ligne, jamais après).
        UniqueConstraint("converted_user_id", name="uq_referral_attribution_converted_user"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    promoter_id: int = Field(foreign_key="promoter.id", index=True)

    # Identifiant de visiteur généré côté client (UUID localStorage, voir
    # api.js) — PUREMENT informatif/best-effort, jamais une donnée
    # personnelle (§8), jamais utilisé pour une décision de sécurité.
    visitor_id: Optional[str] = Field(default=None, index=True)

    converted_user_id: int = Field(foreign_key="user.id", index=True)

    source: str = Field(default="link")  # "link" — seule source V1 (lien /{slug})
    attributed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Horodatage de la capture cliente originale (avant inscription) — utilisé
    # pour vérifier la fenêtre d'attribution (§9) au moment de la création de
    # cette ligne ; jamais recalculé après coup.
    captured_at: Optional[datetime] = None


# §14 : statuts du ledger — ACCRUED = commission créée sur paiement confirmé
# (jamais avant) ; REVERSED = correction suite à remboursement intégral (§35).
# Aucun statut "PENDING" : une commission n'existe QUE si le paiement est
# déjà confirmé (§11 — jamais créée avant), donc jamais "en attente" par nature.
COMMISSION_STATUSES = ("ACCRUED", "REVERSED")


class ReferralCommission(SQLModel, table=True):
    """
    §14 : le LEDGER — source de vérité financière. Chaque ligne répond à
    "quel promoteur/client/paiement/abonnement/plan/montant/taux/commission/
    quand/statut" (§14). Jamais de UPDATE du montant après création — seul
    `status` évolue (ACCRUED -> REVERSED, §35), jamais une suppression
    (§5/§22 : "ne jamais supprimer l'historique financier").
    """
    __tablename__ = "referral_commission"
    __table_args__ = (
        # §12 : idempotence — un même événement de paiement (delivery Chariow)
        # ne peut créer qu'UNE seule commission, même si le Pulse est reçu
        # plusieurs fois. Défense en profondeur : le routeur billing
        # (ProcessedPulseDelivery, déjà existant) déduplique déjà AVANT
        # d'appeler le handler — cette contrainte protège en plus contre tout
        # futur appel direct au service de commission hors de ce chemin.
        UniqueConstraint("source_event_id", name="uq_referral_commission_source_event"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    promoter_id: int = Field(foreign_key="promoter.id", index=True)
    referred_user_id: int = Field(foreign_key="user.id", index=True)

    # Référence du paiement — la ProviderSubscription du client référé
    # (app/models/provider_subscription.py, déjà la source de vérité
    # abonnement/paiement du projet, jamais dupliquée ici).
    provider_subscription_id: int = Field(foreign_key="provider_subscription.id", index=True)
    plan: Optional[str] = None  # "monthly" | "yearly" — copié depuis ProviderSubscription.plan au moment T

    # §12 : clé d'idempotence — x-pulse-delivery-id du Pulse Chariow qui a
    # confirmé ce paiement précis. Nullable seulement en théorie (toujours
    # fourni en pratique par Chariow) — jamais réutilisée pour un autre événement.
    source_event_id: Optional[str] = Field(default=None, index=True)

    # §4/§33 : montant RÉELLEMENT payé (jamais le prix catalogue), entier,
    # dans l'unité courante de `currency` (XOF = pas de sous-unité). Voir
    # app/referral/money.py pour la provenance (webhook si disponible, sinon
    # prix fixe configuré — jamais un prix "deviné").
    gross_paid_amount: int
    currency: str = Field(default="XOF")

    commission_rate_bp: int  # copié depuis Promoter.commission_rate_bp AU MOMENT de la création — jamais relu après coup (§3)
    commission_amount: int  # floor(gross_paid_amount * commission_rate_bp / 10000) — voir money.py

    status: str = Field(default="ACCRUED")  # COMMISSION_STATUSES
    reversed_at: Optional[datetime] = None
    reversed_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReferralVisit(SQLModel, table=True):
    """
    §15 : "nombre de visiteurs attribués" — la SEULE donnée collectée avant
    conversion (contrairement à ReferralAttribution, créée seulement à
    l'inscription). Volontairement minimal pour respecter §8 ("ne pas
    stocker inutilement des données personnelles") : aucune IP, aucun
    user-agent, aucun identifiant personnel — uniquement un UUID anonyme
    généré côté client (même mécanisme que localStorage.xfoot_visitor_id,
    voir frontend-design/api.js) et un horodatage. Comptée par
    COUNT(DISTINCT visitor_id) — jamais un compteur arbitraire incrémenté
    (§46 : les totaux doivent rester recomputables par agrégation).
    """
    __tablename__ = "referral_visit"

    id: Optional[int] = Field(default=None, primary_key=True)
    promoter_id: int = Field(foreign_key="promoter.id", index=True)
    visitor_id: str = Field(index=True)
    visited_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# §37 : audit trail — réutilise le principe déjà établi par EntitlementEvent
# (app/models/entitlement.py, Phase 1) : append-only, jamais UPDATE/DELETE.
# Un second système d'audit distinct existe déjà pour le Kill Switch IA
# (app/ai/safety/kill_switch.py) mais vit dans un fichier JSON séparé, hors
# de portée de ce module produit — celui-ci reste en base, cohérent avec
# EntitlementEvent qui couvre déjà le même type d'événement (facturation).
# ---------------------------------------------------------------------------

REFERRAL_AUDIT_EVENT_TYPES = (
    "PROMOTER_CREATED", "PROMOTER_ACTIVATED", "PROMOTER_DEACTIVATED", "PROMOTER_SUSPENDED",
    "COMMISSION_CREATED", "COMMISSION_REVERSED", "SELF_REFERRAL_REJECTED",
    "WITHDRAWAL_REQUESTED", "WITHDRAWAL_PAID", "WITHDRAWAL_REJECTED",
)


class ReferralAuditEvent(SQLModel, table=True):
    __tablename__ = "referral_audit_event"

    id: Optional[int] = Field(default=None, primary_key=True)
    event_type: str  # REFERRAL_AUDIT_EVENT_TYPES
    promoter_id: Optional[int] = Field(default=None, foreign_key="promoter.id", index=True)
    actor_user_id: Optional[int] = Field(default=None, foreign_key="user.id")  # admin ayant agi, si applicable
    detail: Optional[str] = None  # texte libre court, jamais de secret/donnée bancaire (§38)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Phase 15.14 : retrait MANUEL des commissions promoteurs — Xfoot n'effectue
# JAMAIS de paiement automatique (aucun fournisseur externe intégré). Cette
# table est un JOURNAL DE DEMANDES, pas une seconde source de vérité
# financière : le montant réellement dû reste TOUJOURS dérivable de
# ReferralCommission (le grand livre existant) moins les retraits déjà
# PAYÉS/EN ATTENTE — voir app/referral/withdrawal_service.py::
# compute_promoter_available_amount, jamais un champ `promoter.balance`
# mutable (Partie M du prompt Phase 15.14).
#
# "PAID" signifie exclusivement : un administrateur a envoyé l'argent
# MANUELLEMENT, HORS Xfoot, puis est revenu confirmer explicitement ce
# paiement déjà effectué (Partie C/I du prompt) — jamais un paiement
# déclenché par ce bouton.
# ---------------------------------------------------------------------------

WITHDRAWAL_STATUSES = ("PENDING", "PAID", "REJECTED")
# §Partie C du prompt : "CANCELLED" est explicitement omis en V1 — aucune
# action promoteur d'auto-annulation n'est demandée nulle part dans cette
# phase (seul le refus ADMIN, "REJECTED", est spécifié avec un comportement
# précis, Partie P) ; ajouter CANCELLED sans cas d'usage réel serait une
# règle métier inventée, interdite (§ "en cas de règle métier inconnue :
# NOT_DEFINED"). Documenté ici plutôt qu'ajouté silencieusement.


class PromoterWithdrawal(SQLModel, table=True):
    """
    §Partie B : une ligne = UNE demande de retrait. §Partie F/M : le montant
    "disponible" n'est JAMAIS lu depuis cette table seule — toujours recalculé
    (ReferralCommission ACCRUED - retraits PENDING - retraits PAID), voir
    withdrawal_service.py. Une fois PAID, une ligne est un historique
    immuable (§Partie Q) : jamais UPDATE du montant, jamais suppression,
    jamais une seconde confirmation possible (transition atomique côté DB,
    voir withdrawal_service.py::confirm_withdrawal_paid).
    """
    __tablename__ = "promoter_withdrawal"
    __table_args__ = (
        # §Partie G/N/O : au plus UNE demande PENDING à la fois par promoteur —
        # empêche un double-clic ou deux requêtes concurrentes de créer deux
        # demandes, au niveau DB (pas seulement applicatif), sur SQLite ET
        # PostgreSQL (les deux moteurs réellement utilisés par ce projet, voir
        # app/core/database.py) : un index UNIQUE PARTIEL (WHERE status =
        # 'PENDING') plutôt qu'une contrainte globale, pour qu'un promoteur
        # puisse à nouveau demander un retrait après qu'une précédente demande
        # ait été PAID ou REJECTED.
        Index(
            "uq_promoter_withdrawal_one_pending", "promoter_id", unique=True,
            sqlite_where=text("status = 'PENDING'"),
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    promoter_id: int = Field(foreign_key="promoter.id", index=True)

    # §Partie E : TOUJOURS le montant calculé côté serveur au moment de la
    # création (jamais une valeur transmise par le client sans revérification,
    # voir withdrawal_service.py::create_withdrawal_request) — entier, XOF
    # sans sous-unité, même convention que ReferralCommission.gross_paid_amount.
    amount: int
    currency: str = Field(default="XOF")

    status: str = Field(default="PENDING", index=True)  # WITHDRAWAL_STATUSES

    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processed_at: Optional[datetime] = None
    processed_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")

    # §Partie J : référence externe TEXTUELLE, jamais un format imposé, jamais
    # une vérification automatique prétendue (l'admin l'enregistre après avoir
    # réellement payé hors Xfoot) — nullable : une confirmation reste possible
    # sans référence, mais admin + date sont TOUJOURS conservés (voir champs
    # ci-dessus), garantissant la traçabilité minimale même sans référence.
    external_reference: Optional[str] = None
    admin_note: Optional[str] = None  # §Partie J : commentaire libre facultatif, jamais de donnée bancaire (§38)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
