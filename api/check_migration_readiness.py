"""
check_migration_readiness.py — Diagnostic EN LECTURE SEULE à lancer avant
d'appliquer la migration Alembic Provider/Entitlement (Phase 1, Chariow).

N'écrit jamais rien en base, ne corrige jamais rien automatiquement : liste
les anomalies trouvées et laisse la décision à un humain, conformément à la
procédure validée pour cette phase.

Vérifie, sur la table `subscription` existante :
  1. Lignes avec un user_id qui ne correspond à aucun User (FK cassée).
  2. Doublons de chariow_license_key entre deux user_id différents — ferait
     échouer la contrainte UNIQUE(provider, external_ref) de la nouvelle
     table ProviderSubscription.
  3. Doublons de user_id dans `subscription` elle-même (ne devrait jamais
     arriver vu la contrainte UNIQUE existante, vérifié par prudence).
  4. Lignes status='active' sans chariow_license_key ni current_period_end
     (cas légitime documenté dans le code — accès débloqué dès
     successful.sale, en attendant license.activated — mais signalé pour
     information, pas une anomalie bloquante).

Usage :
    python api/check_migration_readiness.py

Sortie : code 0 si aucune anomalie BLOQUANTE (doublons/orphelins), 1 sinon.
Les points seulement informatifs (point 4) n'affectent pas le code de sortie.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sqlmodel import Session, select

from app.core.database import engine, DATABASE_URL
from app.models.user import User
from app.models.subscription import Subscription


def check_orphan_user_ids(session: Session) -> list[str]:
    problems = []
    subs = session.exec(select(Subscription)).all()
    user_ids = {u.id for u in session.exec(select(User)).all()}
    for sub in subs:
        if sub.user_id not in user_ids:
            problems.append(f"Subscription id={sub.id} : user_id={sub.user_id} introuvable dans `user`.")
    return problems


def check_duplicate_license_keys(session: Session) -> list[str]:
    problems = []
    subs = session.exec(
        select(Subscription).where(Subscription.chariow_license_key.is_not(None))
    ).all()
    by_key: dict[str, list[int]] = {}
    for sub in subs:
        by_key.setdefault(sub.chariow_license_key, []).append(sub.user_id)
    for key, user_ids in by_key.items():
        if len(set(user_ids)) > 1:
            problems.append(
                f"chariow_license_key='{key}' partagée par plusieurs utilisateurs : user_ids={sorted(set(user_ids))}."
            )
    return problems


def check_duplicate_user_ids(session: Session) -> list[str]:
    problems = []
    subs = session.exec(select(Subscription)).all()
    seen: dict[int, int] = {}
    for sub in subs:
        seen[sub.user_id] = seen.get(sub.user_id, 0) + 1
    for user_id, count in seen.items():
        if count > 1:
            problems.append(f"user_id={user_id} apparaît {count} fois dans `subscription` (devrait être unique).")
    return problems


def list_active_without_license(session: Session) -> list[str]:
    notes = []
    subs = session.exec(
        select(Subscription).where(
            Subscription.status == "active",
            Subscription.chariow_license_key.is_(None),
        )
    ).all()
    for sub in subs:
        notes.append(
            f"Subscription id={sub.id} user_id={sub.user_id} : status='active' sans chariow_license_key "
            f"(cas légitime si successful.sale reçu mais pas encore license.activated — pour information)."
        )
    return notes


def main() -> int:
    print(f"Base analysée : {DATABASE_URL}")
    print("Lecture seule — aucune écriture, aucune correction automatique.\n")

    with Session(engine) as session:
        blocking: list[str] = []
        blocking += check_orphan_user_ids(session)
        blocking += check_duplicate_license_keys(session)
        blocking += check_duplicate_user_ids(session)
        informational = list_active_without_license(session)

        n_subs = len(session.exec(select(Subscription)).all())
        n_users = len(session.exec(select(User)).all())

    print(f"Utilisateurs : {n_users}")
    print(f"Lignes Subscription : {n_subs}\n")

    if informational:
        print(f"Notes informatives ({len(informational)}) — non bloquantes :")
        for note in informational:
            print(f"  - {note}")
        print()

    if blocking:
        print(f"ANOMALIES BLOQUANTES ({len(blocking)}) — migration à NE PAS lancer avant résolution :")
        for problem in blocking:
            print(f"  - {problem}")
        return 1

    print("Aucune anomalie bloquante détectée. Migration considérée prête côté données.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
