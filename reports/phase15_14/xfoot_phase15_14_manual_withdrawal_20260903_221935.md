# PHASE 15.14 — SYSTÈME DE RETRAIT MANUEL DES COMMISSIONS PROMOTEURS V1

Généré : 2026-09-03 22:19:35 UTC — **implémentation + validation locale, aucun commit/push/déploiement.**

## PARTIE A — Audit avant modification

Recherche exhaustive confirmée : aucun modèle/route/migration de payout n'existait avant cette phase — seul `app/referral/stats.py::compute_promoter_stats` exposait déjà le flag `PAYOUT_SYSTEM_NOT_YET_IMPLEMENTED` et un `commission_available` qui était en réalité le total accru brut (sans déduction). Rien n'a été dupliqué : `ReferralCommission` reste l'unique grand livre, `require_admin` (Phase 10) et `get_current_promoter` (Phase 14) sont réutilisés tels quels, et le pattern d'idempotence (check-then-insert + `IntegrityError`) déjà établi par `commission_service.py` a été répliqué à l'identique dans le nouveau `withdrawal_service.py`.

## PARTIE B — Modèle `PromoterWithdrawal`

`api/app/models/promoter.py` : `id, promoter_id, amount, currency, status, requested_at, processed_at, processed_by_admin_id, external_reference, admin_note, created_at, updated_at` — exactement les champs demandés, rien de plus.

## PARTIE C — Statuts

`PENDING → PAID` ou `PENDING → REJECTED`. `CANCELLED` a été **explicitement omis** en V1 : aucun cas d'usage d'auto-annulation promoteur n'est demandé nulle part dans le prompt — décision documentée dans le code plutôt qu'ajoutée sans justification. `PAID` ne peut être atteint que via `confirm=true` explicite côté admin — jamais un simple clic.

## PARTIE D/E — Demande côté promoteur + montant côté serveur

`WITHDRAWAL_AMOUNT_POLICY = FULL_AVAILABLE_ONLY`, documenté explicitement dans `withdrawal_service.py` (aucune règle de retrait partiel n'existait avant cette phase — la préférence recommandée par le prompt a été retenue). Le montant écrit en base est **toujours** celui recalculé côté serveur, jamais celui envoyé par le client :

| Disponible | Demande | Résultat |
|---|---|---|
| 600 | 600 | ✅ ACCEPTÉE |
| 600 | 601 | ❌ REFUSÉE (400) |
| 600 | 1000 | ❌ REFUSÉE (400) |
| 600 | 0 | ❌ REFUSÉE (400) |
| 600 | -100 | ❌ REFUSÉE (400) |
| 600 | 100000 (falsifié) | ❌ REFUSÉE (400) |

## PARTIE F/G — Réservation + double demande

`commission_available` est **toujours dérivé** : `SUM(ReferralCommission ACCRUED) − SUM(PromoterWithdrawal PAID) − SUM(PromoterWithdrawal PENDING)`, jamais un champ `promoter.balance` mutable (Partie M respectée). Double-clic / retry réseau : une demande `PENDING` déjà existante est retournée telle quelle (no-op) ; protection DB en défense en profondeur via un **index UNIQUE PARTIEL** (`promoter_id` WHERE `status='PENDING'`), fonctionnel sous SQLite **et** PostgreSQL (les deux moteurs réels de ce projet) — testé : 3 soumissions rapides → 1 seule ligne en base.

## PARTIE H/I/J — Admin : liste + traitement manuel + référence

Nouvelle page `admin-withdrawals.html` (+ lien "Retraits" ajouté aux 3 pages admin existantes) : liste filtrable PENDING/PAID/REJECTED. Pour une demande `PENDING`, panneau inline avec référence externe (texte libre, optionnelle) + note + **case à cocher obligatoire** ("Je confirme avoir réellement envoyé ce montant au promoteur") avant que le bouton "Confirmer le versement effectué" ne soit activable. Côté API, `confirm=true` est une valeur obligatoire du corps de requête — sans elle, refus explicite (400), jamais une confirmation implicite.

## PARTIE K/L — Visibilité promoteur + calcul des gains

`promoter.html` : section "Gains" étendue (Acquise / Reversée / Disponible / **En attente de retrait** / Déjà versée) + bouton "Demander un retrait" (désactivé si rien n'est disponible) + nouvelle section "Mes demandes de retrait" listant uniquement les demandes du promoteur courant. Séquence testée et conforme à l'exemple du prompt : Acquis=600, pending=600 → Disponible=0, En attente=600 ; après confirmation → Disponible=0, En attente=0, Déjà versé=600.

## PARTIE M — Source de vérité

Aucun `promoter.balance`. Tout est recalculé depuis `ReferralCommission` + `PromoterWithdrawal` à chaque appel (`compute_promoter_available_amount`), jamais un compteur mis en cache.

## PARTIE N/O — Idempotence et concurrence

- Double demande → 1 seule ligne (testé, index UNIQUE partiel).
- Double confirmation admin → `UPDATE ... WHERE status='PENDING'` atomique ; la 2e tentative trouve `rowcount=0` → **409**, jamais une seconde transition (testé : référence externe de la 1ʳᵉ confirmation préservée, pas écrasée).
- **Limite documentée** (même limite déjà actée en Phase 15.10/15.12) : le harness de test est synchrone, aucun vrai test multi-thread n'a été exécuté — la protection elle-même (UPDATE atomique conditionnel + contrainte DB) est correcte par construction indépendamment de cette limite de test.

## PARTIE P — Refus / annulation

Un refus admin (`REJECTED`) libère automatiquement le montant (recalcul, aucune perte) — testé : disponible revient à 600 après refus d'une demande de 600. Une nouvelle demande reste possible après un refus (l'index UNIQUE partiel ne bloque que les `PENDING`) — testé.

## PARTIE Q — Après PAID

Un retrait `PAID` est historique : jamais supprimé, jamais modifié, jamais re-confirmable (testé : 2e confirmation → 409, données inchangées).

## PARTIE R — Nouvelles commissions après payout

Testé exactement selon l'exemple du prompt : 600 déjà versés, nouvelle vente réelle (Pulse signé, 1000 FCFA → commission 400) → Acquis total=1000, Versé=600, **Disponible=400** (jamais remis à 0).

## PARTIE S — Remboursement

Trois cas audités, comportement documenté dans `withdrawal_service.py::REFUND_AFTER_PAYOUT_LIMITATION` :
1. Commission `ACCRUED` jamais retirée → un remboursement (mécanisme `reverse_commissions_for_subscription` déjà existant, inchangé) la passe en `REVERSED` → disponible baisse correctement. **Testé.**
2. Une demande `PENDING` existe au moment du remboursement → son montant n'est pas automatiquement annulé ; un administrateur doit trancher manuellement.
3. Une demande est déjà `PAID` au moment du remboursement → **aucun mécanisme de récupération d'argent** n'est implémenté (interdit explicitement par le prompt) — limite connue et acceptée pour la V1, jamais masquée.

## PARTIE T — Sécurité des routes

Testé : non authentifié → 401 ; utilisateur normal (sans compte promoteur) → 403 sur les routes promoteur ; promoteur non-admin → 403 sur les routes admin ; promoteur A ne voit ni n'affecte les demandes de promoteur B (liste + actions confirmées isolées).

## PARTIE U/V — Messages honnêtes, aucun fournisseur de paiement

Le bandeau `PAYOUT_SYSTEM_NOT_YET_IMPLEMENTED` a été remplacé par un message honnête expliquant le traitement manuel. Aucun fournisseur externe (TMoney/Flooz/MoMo/PayPal/Stripe/banque) n'a été intégré — les endpoints tracent, permettent la confirmation d'un paiement déjà effectué, et informent, sans jamais transférer d'argent eux-mêmes.

## PARTIE W — Tests

`api/test_phase15_14_manual_withdrawal.py` — **56/56 assertions réussies**, couvrant les 30 scénarios numérotés du prompt (montant/sécurité/idempotence/concurrence/refus/remboursement/historique) + la matrice de sécurité complète. Toutes les `ReferralCommission` de test sont créées via un **vrai** Pulse `successful.sale` signé (même helper que `test_referral_promoter_platform.py`), jamais une ligne fabriquée à la main.

## PARTIE X — Migration

`api/alembic/versions/99fd1d8ad121_phase15_14_promoter_withdrawal.py` — auto-générée, strictement additive (1 seule nouvelle table). Testée sur une **copie scratch** de `app.db` (jamais la base de dev/prod réelle) : `upgrade` → schéma vérifié (table + 2 index + 1 index UNIQUE partiel avec la clause `WHERE` correcte) → `downgrade` (table supprimée) → `re-upgrade` (idempotent), tout confirmé fonctionnel.

## PARTIE Y — Cas réel promoter-2

**Aucune action de production effectuée.** État inchangé : acquis 600 FCFA, disponible 600 FCFA, déjà versé 0 FCFA. Aucune demande de retrait créée pour promoter-2 pendant cette phase.

## PARTIE Z — Isolation IA

6 tables strictement identiques (`match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`). `api/app/ai/readiness/gates.py` reste la modification étrangère pré-existante déjà présente avant cette phase — non touchée.

## PARTIE AA — Régression complète

| Suite | Résultat |
|---|---|
| test_referral_promoter_platform.py | 99/99 |
| test_phase14_1_production_integration.py | 36/36 |
| test_phase15_1_new_offers.py | 17/17 |
| test_phase15_7_referral_attribution_repro.py | 24/24 |
| test_phase15_9_webhook_hardening.py | 31/31 |
| test_phase15_10_activate_license_commission.py | 33/33 |
| test_phase15_12_amount_reconciliation.py | 36/36 |
| test_chariow_billing.py | 30/30 |
| test_entitlement.py | 8/8 |
| test_provider_subscription_backfill.py | 4/4 |
| test_google_play_billing.py | 44/44 |
| test_auth.py | 8/8 |
| test_main.py | 9/9 |

**0 échec. Aucun test historique cassé.**

## PARTIE AB — Aucun commit/push/déploiement

Respecté intégralement. Tous les changements restent dans l'arbre de travail local.

## Fichiers modifiés/créés

**Backend modifié** : `app/models/promoter.py`, `app/referral/admin_router.py`, `app/referral/router.py`, `app/referral/stats.py`.
**Backend nouveau** : `app/referral/withdrawal_service.py`, `alembic/versions/99fd1d8ad121_phase15_14_promoter_withdrawal.py`, `test_phase15_14_manual_withdrawal.py`.
**Frontend modifié** : `promoter.html`, `admin-earnings.html`, `admin-promoters.html`, `admin-subscribers.html`.
**Frontend nouveau** : `admin-withdrawals.html`.

## Limitations

- Frontend implémenté selon les conventions existantes (mêmes classes CSS, mêmes helpers `api.js`) mais **non testé visuellement** dans un navigateur réel — aucun accès navigateur/computer-use dans cet environnement (limitation déjà documentée dans les phases précédentes). Syntaxe JS inline vérifiée mécaniquement (aucune erreur).
- Aucun test de concurrence réelle multi-thread — même limite déjà actée en Phase 15.10/15.12.

## Verdict

**`MANUAL_WITHDRAWAL_IMPLEMENTED_TESTS_PASS`**

Workflow manuel complet et testé de bout en bout (demande → réservation → confirmation admin explicite → statut versé), source de vérité unique jamais dupliquée, idempotence et isolation prouvées, aucun paiement automatique, aucun fournisseur externe, aucune action de production, cas réel promoter-2 strictement inchangé.

---

**PHASE 15.14 — SYSTÈME DE RETRAIT MANUEL IMPLÉMENTÉ ET TESTÉ : 56/56 TESTS DÉDIÉS (30 SCÉNARIOS DU PROMPT + MATRICE DE SÉCURITÉ), 435 ASSERTIONS AU TOTAL EN RÉGRESSION COMPLÈTE SUR 13 SUITES EXISTANTES, 0 ÉCHEC, AUCUN TEST HISTORIQUE CASSÉ, 6 TABLES IA INCHANGÉES, 0 FICHIER app/ai/* DANS LE DIFF. MODÈLE PromoterWithdrawal STRICTEMENT ADDITIF (MIGRATION ALEMBIC TESTÉE UPGRADE/DOWNGRADE/RE-UPGRADE SUR COPIE SCRATCH, JAMAIS LA DB RÉELLE). AUCUNE SECONDE SOURCE DE VÉRITÉ FINANCIÈRE : LE DISPONIBLE RESTE TOUJOURS DÉRIVÉ DE ReferralCommission + PromoterWithdrawal. AUCUN PAIEMENT AUTOMATIQUE, AUCUN FOURNISSEUR EXTERNE, AUCUNE CONFIRMATION IMPLICITE (confirm=true OBLIGATOIRE). LE CAS RÉEL promoter-2 RESTE STRICTEMENT INCHANGÉ (600 FCFA DISPONIBLES, 0 FCFA VERSÉS, AUCUNE DEMANDE CRÉÉE). AUCUN COMMIT, AUCUN PUSH, AUCUN DÉPLOIEMENT. VERDICT : MANUAL_WITHDRAWAL_IMPLEMENTED_TESTS_PASS.**
