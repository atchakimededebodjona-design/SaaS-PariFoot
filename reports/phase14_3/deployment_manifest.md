# XFOOT — Deployment Manifest (Phase 14.3)

Généré : 2026-09-01 00:24:40 UTC

## LOCAL_COMMIT

- **HEAD** : `00dd971` — "feat: complete AI shadow and production readiness phases"
- Créé par l'utilisateur **en dehors de cette conversation** (2026-08-31 21:59:21 UTC) — n'inclut **pas** le code Phase 14/14.1/14.2/14.3 (affiliation), qui reste **100% non commité**.
- `main...origin/main [ahead 4]`

## TARGET_ENVIRONMENT

| Composant | Hébergeur | Mécanisme |
|---|---|---|
| Backend | Railway (`api.xfoot.site`) | FastAPI + PostgreSQL, déployé via `git push origin main` |
| Frontend | Hostinger (`www.xfoot.site`) | Statique, upload manuel hors git |
| Paiement | Chariow | `PAYMENT_ENVIRONMENT = UNKNOWN` |

## BACKEND_FILES

**Nouveaux (13)** : `app/models/promoter.py`, `app/referral/` (7 fichiers), 1 migration, `test_referral_promoter_platform.py`, `test_phase14_1_production_integration.py`.
**Modifiés (5)** : `app/models/user.py`, `app/auth/router.py`, `app/billing/router.py`, `main.py`, `alembic/env.py`.

## FRONTEND_FILES

**Nouveaux (5)** : `promoter.html`, `admin-promoters.html`, `admin-subscribers.html`, `admin-earnings.html`, `.htaccess`.
**Modifiés (9)** : `api.js`, `login.html`, `index.html`, `live.html`, `arena.html`, `vip.html`, `history.html`, `billing.html`, `dashboard.html`.

## MIGRATIONS

| Revision | Down revision | Tables créées | Nature | Downgrade |
|---|---|---|---|---|
| `5787c4b3b963` | `c4f8a1d75e93` | promoter, referral_attribution, referral_audit_event, referral_commission, referral_visit | Strictement additive (5×`CREATE TABLE`, 0 `ALTER`) | Présent, structurellement vérifié, jamais exécuté en réel |

Appliquée localement ✅ — Appliquée en production ❌ (jamais tentée).

## ENVIRONMENT_VARIABLES

| Name | Required | Secret | Production Status | Verification |
|---|---|---|---|---|
| CHARIOW_API_KEY | ✅ | ✅ | UNKNOWN | présence LOCAL uniquement |
| CHARIOW_PULSE_SECRET | ✅ | ✅ | UNKNOWN | présence LOCAL uniquement |
| CHARIOW_PRODUCT_ID_MONTHLY | ✅ | ❌ | UNKNOWN | présence LOCAL uniquement |
| CHARIOW_PRODUCT_ID_YEARLY | ✅ | ❌ | UNKNOWN | présence LOCAL uniquement |
| JWT_SECRET_KEY | ✅ | ✅ | UNKNOWN | présence LOCAL uniquement |
| DATABASE_URL | ✅ | ✅ | présumée OK (service déjà actif) | non vérifiable |
| **ADMIN_EMAILS** | ✅ | ❌ | **UNKNOWN — CRITIQUE, ABSENTE en local** | **NON VÉRIFIÉ, bloquant potentiel pour /admin/*** |
| ALLOWED_ORIGINS | ✅ | ❌ | présumée OK | non vérifiable, aucune nouvelle origine requise |
| REFERRAL_PLAN_PRICE_MONTHLY | ❌ | ❌ | absente partout (nouvelle) | optionnelle |
| REFERRAL_PLAN_PRICE_YEARLY | ❌ | ❌ | absente partout (nouvelle) | optionnelle |
| ENV | ❌ | ❌ | UNKNOWN | non vérifiable |

## ROUTES (12 nouvelles)

| Method | Path | Auth |
|---|---|---|
| POST | /referral/resolve/{slug} | aucune (public) |
| POST | /referral/attribute | utilisateur authentifié |
| GET | /promoter/me | promoteur |
| GET | /promoter/me/stats | promoteur |
| GET | /promoter/me/sales | promoteur |
| GET/POST | /admin/promoters | admin |
| POST | /admin/promoters/{id}/status | admin |
| GET | /admin/promoters/{id} | admin |
| GET | /admin/subscribers | admin |
| GET | /admin/earnings/totals | admin |
| GET | /admin/earnings/by-promoter | admin |

## WEBHOOKS

Endpoint **réutilisé**, jamais un nouveau webhook : `POST /billing/pulse`, header `x-chariow-signature` + `x-pulse-delivery-id`. Phase 14 ajoute uniquement un appel best-effort à la création/réversion de commission depuis les handlers existants — la vérification de signature/idempotence elle-même n'est pas modifiée.

## CRITICAL_DEPENDENCIES

Aucune nouvelle dépendance Python ou JS — réutilisation exclusive de FastAPI/SQLModel/Alembic/slowapi déjà en place.

## ROLLBACK_REQUIREMENTS

- **Backend** : redéploiement du commit précédent via l'historique Railway.
- **Database** : `alembic downgrade -1` (downgrade() présent et structurellement correct, jamais exécuté en réel).
- **Frontend** : re-upload des fichiers précédents sur Hostinger — **sauvegarde manuelle préalable requise** (pas de versioning natif).
- **Vérifié** : non — méthode documentée, pas une preuve d'exécution.
