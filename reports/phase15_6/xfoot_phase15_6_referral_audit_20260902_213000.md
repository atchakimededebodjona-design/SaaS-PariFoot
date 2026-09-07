# PHASE 15.6 — PRE-PAYMENT REFERRAL ATTRIBUTION AUDIT V1

Généré : 2026-09-02 21:30:00 UTC

## Question centrale

> Si `payment@test.com` paie maintenant 1500 FCFA, le paiement sera-t-il réellement attribué à `promoter-2` et la commission attendue sera-t-elle 600 FCFA ?

**Réponse : ne peut pas être confirmée par OUI.** Voir verdict.

## Étape 1 — Modèle d'attribution (code)

| | |
|---|---|
| Table | `referral_attribution` (`api/app/models/promoter.py:76-114`) |
| Colonnes clés | `promoter_id` (FK), `converted_user_id` (FK, **UNIQUE**), `visitor_id`, `attributed_at`, `captured_at` |
| Contrainte | `UniqueConstraint('converted_user_id')` — un utilisateur n'a qu'UNE attribution, jamais réécrite |
| Création | `POST /referral/attribute` (authentifié) — rejette self-referral, promoteur inactif, hors fenêtre 30j |
| Lecture au paiement | `SELECT ReferralAttribution WHERE converted_user_id = <user_id du webhook>` — requête **fraîche**, jamais une valeur transportée depuis le checkout |

## Étape 2 — Acheteur

**NOT_VERIFIABLE.** Aucun accès DB/admin cette session pour re-confirmer existence/rôle/attribution de `payment@test.com` au-delà de ce qui a été relayé précédemment.

## Étape 3 — Promoteur

| | |
|---|---|
| Slug existe | **PASS** (déjà vérifié Phase 15.6 Étape 1, `POST /referral/resolve/promoter-2` sans `visitor_id` → `{valid:true}`) |
| Status ACTIVE | **PASS** (même preuve) |
| user_id = 206 | **NOT_VERIFIABLE** (endpoint public ne l'expose jamais, par design de confidentialité) |

## Étape 4 — Recherche de l'attribution

**Non exécutée** — nécessiterait un accès direct à la base de production (Postgres Railway), dont cet assistant ne dispose pas.

## Étape 5 — Persistance

Aucune des catégories A/B/C/D ne peut être choisie avec preuve. B (`ATTRIBUTION_NOT_PERSISTED`) et A (`ATTRIBUTION_PERSISTED`) sont toutes deux des affirmations positives nécessitant une observation réelle — ni l'une ni l'autre n'est prouvée. Le statut honnête est un **blocage d'accès**, pas une catégorie forcée.

## Étape 6 — Utilisation au checkout (code) — analyse complète

- **Le checkout n'envoie et ne lit JAMAIS de `promoter_id`.** Il envoie uniquement `custom_metadata={user_id, plan}` à Chariow.
- **L'attribution est entièrement découplée du checkout.** Au moment du webhook `successful.sale`, `create_commission_for_confirmed_payment` fait une requête SQL **fraîche** : `WHERE converted_user_id = <user_id extrait du webhook>`.
- Idempotence à deux niveaux (`ProcessedPulseDelivery` + `UniqueConstraint` sur `source_event_id`).
- Commission = `floor(gross_paid_amount × commission_rate_bp / 10000)`, montant toujours issu du webhook, jamais du catalogue.

**Conclusion architecturale** : *si* une ligne `ReferralAttribution` existe déjà pour `converted_user_id=209` pointant vers le `Promoter` dont `user_id=206`, *alors* la commission sera automatiquement et correctement calculée à la confirmation — indépendamment de ce qui s'est passé pendant le checkout. **C'est une garantie de CODE, pas une observation de DONNÉES réelles.**

## Étape 7 — Absence de paiement préalable

**NOT_VERIFIABLE** techniquement par cet assistant. (Relayé précédemment par l'utilisateur : 0 ventes, 0 FCFA — non re-vérifié ici faute d'accès.)

## Étape 8 — Tests

**65/65, 0 échec réel.** Suites ciblées : `test_referral_promoter_platform.py` (99/99), `test_phase14_1_production_integration.py` (36/36). Aucune modification de test, aucune régression.

## Étape 9 — Isolation IA

6 tables strictement identiques avant/après : `match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`.

## Étape 10 — Git

`HEAD = eeb01ec`, aucun changement lié à cette phase, aucun commit, aucun push.

## Limitations exactes

- Aucun accès DB production, aucun token pour aucun des 3 comptes, aucun outil de navigateur.
- Seul point vérifié avec des données réelles : `promoter-2` existe et est `ACTIVE`.
- L'analyse du mécanisme (Étapes 1 et 6) est complète et fondée sur le code réel — elle prouve que le système **fonctionnerait correctement SI** une attribution existe, jamais **qu'**une attribution existe réellement pour ce compte.

## Verdict

**`AUDIT_BLOCKED`**

Les accès nécessaires (DB production, ou tokens authentifiés) ne sont pas disponibles cette session, empêchant une vérification fiable et positive de l'attribution serveur pour `user_id=209 → promoter_id (user_id=206)`. Le mécanisme applicatif est audité et jugé correct — signal positif indirect, mais insuffisant pour `REFERRAL_ATTRIBUTION_CONFIRMED`, qui exige une preuve positive sur les données réelles, jamais une déduction.

---

**Pour lever ce blocage** : soit un accès admin/DB (relais humain via le dashboard admin — vérifier si `payment@test.com` apparaît déjà dans une liste d'attributions/visiteurs de `promoter-2`), soit accepter que la confirmation définitive ne pourra se faire qu'**a posteriori**, immédiatement après le paiement réel, en vérifiant que la commission de 600 FCFA apparaît bien pour `promoter-2` — ce qui n'est plus une garantie a priori mais une observation directe du résultat.
