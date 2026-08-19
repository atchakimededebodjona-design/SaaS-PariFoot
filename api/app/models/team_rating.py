"""
Ratings d'équipes calculés par un modèle (Dixon-Coles, Elo, ...) et
métadonnées de version de modèle — Phase 3 du plan Xfoot AI.

Noms de tables au pluriel (team_ratings, model_versions), tels que
spécifiés dans le ticket Phase 3 et dans le schéma cible de l'audit
Phase 1 (§9) — contrairement à `match`/`match_stats` (Phase 2), qui
suivent la convention singulière déjà en place pour user/subscription.
Incohérence mineure entre les deux phases, gardée telle quelle plutôt que
de renommer `match`/`match_stats` sans qu'on me l'ait demandé.
"""

from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field, Session, select


class ModelVersion(SQLModel, table=True):
    __tablename__ = "model_versions"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)  # ex. "xfoot-dixon-coles-v1"
    model_type: str = Field(index=True)  # "dixon_coles" | "elo"
    trained_at: datetime
    is_active: bool = Field(default=False)
    notes: Optional[str] = None
    # JSON structuré (Phase 7), distinct de `notes` (texte libre jamais
    # reparsé — voir app/ai/arena/service.py) : paramètres nécessaires pour
    # SERVIR ce modèle en direct, quand ses ratings seuls ne suffisent pas.
    # Utilisé aujourd'hui par Elo (voir scripts/backtest_elo.py) pour la
    # calibration (c, scale) et home_advantage PAR LIGUE, choisis une seule
    # fois à l'entraînement/backtest et jamais recalculés en ligne — jamais
    # un nouveau système de stockage parallèle, juste un champ optionnel sur
    # la table de versionnage déjà existante, générique pour tout futur
    # modèle qui en aurait besoin. None pour les modèles qui n'en ont pas
    # besoin (dixon_coles, dont les paramètres complets vivent déjà dans
    # model_artifact) ou pour une ModelVersion créée avant ce champ.
    config: Optional[str] = None
    # Modèle entraîné SÉRIALISÉ (Phase 8, §5/§10) — mécanisme natif de chaque
    # librairie, jamais un format inventé : LightGBM (booster.model_to_string())
    # produit déjà une chaîne texte ; XGBoost (booster.save_raw(raw_format="json")
    # décodé en texte) aussi — les deux tiennent donc dans ce même champ TEXT,
    # comme `config`/`notes`. Stocké en base (jamais sur disque local) pour la
    # même raison que model_artifact.payload : le service web et le job
    # d'entraînement ne partagent pas de système de fichiers (voir
    # app/models/model_artifact.py). None pour les modèles qui n'en ont pas
    # besoin (dixon_coles : model_artifact ; elo : ratings + config suffisent).
    artifact: Optional[str] = None

    # --- Phase 9 : versioning/cycle de vie, en plus de `is_active` --------
    # `is_active` (ci-dessus) reste INCHANGÉ : le flag opérationnel "servi en
    # direct maintenant" déjà utilisé par tout le code Phase 5-8
    # (get_or_create_active_model_version, deactivate_other_versions,
    # availability.py, default_models). `status` est une couche supplémentaire
    # de cycle de vie ("active" | "shadow" | "candidate" | "retired"),
    # orthogonale à `is_active` — une version "shadow" ou "candidate" a
    # TOUJOURS is_active=False, donc n'entre jamais dans un chemin Phase 5-8
    # existant sans modification de ces fonctions. Défaut "active" (via
    # server_default côté migration) pour que toute ligne créée par du code
    # qui ignore ce champ (tous les scripts Phase 3-8 non modifiés) reste
    # cohérente : une version insérée par l'ancien code, sans state explicite,
    # est bien "active" par défaut — la migration corrige ensuite les lignes
    # historiques dont is_active=False vers status="retired" (backfill, voir
    # la migration Alembic).
    status: str = Field(default="active", index=True)
    activated_at: Optional[datetime] = None
    deactivated_at: Optional[datetime] = None

    # Fenêtres temporelles de l'entraînement/validation/test — distinctes
    # pour que la décision de promotion (Phase 9 §22-23) ne lise jamais le
    # test set (audit uniquement, voir app/ai/arena/promotion.py).
    training_period_start: Optional[date] = None
    training_period_end: Optional[date] = None
    validation_period_start: Optional[date] = None
    validation_period_end: Optional[date] = None
    test_period_start: Optional[date] = None
    test_period_end: Optional[date] = None

    sample_size: Optional[int] = None  # taille de l'échantillon de VALIDATION (gate de promotion)
    # JSON structuré {"validation": {...}, "test": {...}} — même convention
    # texte libre que `config`/`notes` ci-dessus, jamais reparsé ailleurs
    # qu'à l'endroit qui le lit explicitement.
    metrics: Optional[str] = None
    # Miroir en première classe de la valeur déjà présente dans `config` JSON
    # pour XGBoost/LightGBM (ex. "phase8-v1") — permet de filtrer/comparer
    # sans reparser `config`.
    feature_version: Optional[str] = None
    # Version utilisée comme référence de comparaison lors de l'évaluation de
    # promotion de CETTE version (voir promotion.py::evaluate_promotion) —
    # None si aucune baseline n'existait encore (bootstrap du model_type).
    baseline_version_id: Optional[int] = Field(default=None, foreign_key="model_versions.id")


class TeamRating(SQLModel, table=True):
    __tablename__ = "team_ratings"
    __table_args__ = (
        # Même principe que la contrainte UNIQUE sur `match` en Phase 2 : un
        # (team, league) ne doit jamais avoir deux lignes de rating pour la
        # MÊME version de modèle — protège contre une double exécution
        # accidentelle du script de persistance, qui dupliquerait sinon
        # silencieusement les ratings plutôt que de les mettre à jour.
        UniqueConstraint("team", "league", "model_version_id", name="uq_team_ratings_team_league_model_version"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    team: str = Field(index=True)
    league: str = Field(index=True)
    # Dixon-Coles : attack/defense = les deux paramètres appris.
    # Elo : une seule dimension de force -> stockée dans `attack`, `defense`
    # fixé à 0.0 par convention (documentée ici plutôt qu'ajouter une
    # colonne hors du schéma spécifié dans le ticket).
    attack: float
    defense: float
    model_version_id: int = Field(foreign_key="model_versions.id", index=True)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def next_version_name(session: Session, base_name: str) -> str:
    """
    Prochain nom de version disponible pour `base_name` (ex.
    "xfoot-dixon-coles") — "xfoot-dixon-coles-v1" si aucune version
    n'existe encore, "xfoot-dixon-coles-v2" si "v1" existe déjà, etc.

    Corrige un plantage réel : les scripts de persistance (Phase 3)
    utilisaient un nom fixe ("xfoot-dixon-coles-v1") — un run interrompu
    après avoir écrit la ligne model_versions (mais avant team_ratings, ex.
    crash sur un bug non lié) laissait cette version en place, et tout run
    suivant échouait sur la contrainte UNIQUE de `name`. Chaque run crée
    maintenant une NOUVELLE version au lieu d'entrer en conflit — cohérent
    avec l'objet même de model_versions : garder un historique des
    versions successives du modèle, jamais écraser une version existante.
    """
    existing_names = session.exec(
        select(ModelVersion.name).where(ModelVersion.name.like(f"{base_name}-v%"))
    ).all()
    max_n = 0
    prefix_len = len(base_name) + 2  # +2 pour "-v"
    for name in existing_names:
        suffix = name[prefix_len:]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{base_name}-v{max_n + 1}"


def deactivate_other_versions(session: Session, model_type: str) -> None:
    """
    Désactive toutes les ModelVersion existantes du même model_type, avant
    qu'une nouvelle version ne soit potentiellement marquée active — au
    plus UNE version active par type de modèle à la fois, sinon
    `is_active` ne veut plus rien dire dès qu'une deuxième version existe.
    Ne commit pas elle-même (laisse l'appelant grouper dans sa transaction).
    """
    others = session.exec(select(ModelVersion).where(ModelVersion.model_type == model_type)).all()
    for other in others:
        other.is_active = False
        session.add(other)
