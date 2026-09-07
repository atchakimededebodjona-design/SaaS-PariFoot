# PHASE 15.10 — EXTENSION SÉCURISÉE DE activate-license

Généré : 2026-09-03 03:00:00 UTC

## PARTIE A — Code

### Audit préalable (A1-A2)

`POST /billing/activate-license` : authentification JWT, le client ne fournit **que** `license_key` (aucun email/promoter_id/montant). Appelle `GET {CHARIOW_API_BASE_URL}/licenses/{license_key}` — **lecture seule**, endpoint officiel documenté.

**Gap découvert** : aucun champ montant n'est documenté dans l'objet licence Chariow. J'ai réutilisé `extract_actual_paid_amount()` (déjà existant) tel quel — s'il ne trouve pas de montant dans `license_data`, il retombe sur son repli déjà en place (prix fixe configuré) ou sur « indisponible », **jamais un montant halluciné**.

### Extension implémentée — `api/app/billing/router.py` (seul fichier backend touché)

- Après activation de l'abonnement, appel à `create_commission_for_confirmed_payment(session, provider_subscription=sub, sale_body=license_data, delivery_id=f"activate-license:{license_key}")` — **réutilisation stricte**, aucune seconde logique de commission écrite.
- **Idempotence** : `delivery_id` dérivé de la clé de licence elle-même → un second appel avec la même licence retombe sur la contrainte `UNIQUE` déjà en place (`ReferralCommission.source_event_id`), y compris pour des appels concurrents (`IntegrityError` déjà gérée).
- **Sécurité confirmée sans aucune modification** : `promoter_id` et `email` sont **absents du contrat client** — impossibles à falsifier. Le promoteur provient exclusivement de la `ReferralAttribution` déjà en base.

### Correction annulée pendant le développement — transparence

J'ai d'abord ajouté un rejet explicite (422) quand le plan restait indéterminable, pour satisfaire le « Test 5 — produit incorrect ». **Ce changement cassait un test préexistant et délibéré** (`test_activate_license_email_fallback_when_no_metadata`) : une licence Chariow legacy (achat antérieur à la correction historique du bug metadata/custom_metadata) est **intentionnellement activée même sans plan résolvable** — refuser l'accès à un client ayant réellement payé, sur la seule absence d'un libellé, serait une régression fonctionnelle réelle. **J'ai retiré ce rejet.** `plan=None` reste possible, sans risque financier puisque la commission ne dépend jamais de `plan`, uniquement du montant réel.

Aucune modification du webhook (`/billing/pulse`) cette phase.

## PARTIE B — Tests

| Suite | Résultat |
|---|---|
| `test_phase15_10_activate_license_commission.py` (33 assertions) | **33/33** |
| Régression complète | **68/68** (67 préexistants incluant les 31 tests Phase 15.9 + le nouveau) |

Correspondance avec les 14 tests demandés : tous PASS, sauf le **Test 5** réinterprété (voir ci-dessus — testé comme « activation préservée malgré plan non résolvable », pas comme un rejet) et le **Test 8** (concurrence) partiellement couvert par rejeu séquentiel s'appuyant sur la même contrainte `UNIQUE` en base — pas de vrai test multi-thread (limite du harness synchrone existant, cohérente avec le reste du dépôt).

**Partie F (non-régression webhook)** : confirmée — `test_phase15_9_webhook_hardening.py` (31/31) re-exécuté sans modification dans la régression complète.

## PARTIE C — Paiement réel `pi_ih0s0eixhgm1`

**Non exécuté cette phase**, conformément à l'instruction explicite. Deux raisons :
1. L'agent n'a et ne doit jamais avoir la clé de licence de USER 245 — action strictement humaine.
2. **Blocage structurel supplémentaire à signaler** : le code de cette phase est encore **entièrement local** (non committé, non déployé). Si USER 245 utilisait `activate-license` maintenant en production, il obtiendrait l'**ancien** comportement : abonnement activé, mais **aucune commission créée** (le gap de la Phase 15.9 existe toujours en production). Le test réel n'a de sens qu'**après** un déploiement autorisé séparément.

Abonnement/vente/commission : **non modifiés, aucune fabrication.**

## PARTIE D — Tables IA

6 tables strictement identiques : `match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`.

## PARTIE E — Déploiement

**Aucun** — pas de commit, pas de push, pas d'upload Hostinger, pas de modification Railway. Aucune autorisation donnée pour cette phase.

## Observabilité

`logger.warning` si la création de commission via `activate-license` échoue (non bloquant) — même pattern que le webhook (Phase 15.9). Aucune donnée sensible journalisée (jamais la licence elle-même).

## Verdict

**`ACTIVATE_LICENSE_COMMISSION_IMPLEMENTED_TESTS_PASS`**

Extension minimale et sûre validée localement : 33/33 + 68/68, 6 tables IA inchangées, webhook Phase 15.9 reconfirmé intact, sécurité vérifiée (promoter_id/email infalsifiables), idempotence garantie. Le test réel n'a pas été tenté — ni l'agent ne doit détenir la licence, ni ce code n'est déployé.

---

**PHASE 15.10 — activate-license ÉTENDU AVEC SUCCÈS POUR COUVRIR LE GAP DE COMMISSION DÉCOUVERT EN PHASE 15.9 : RÉUTILISATION STRICTE DE create_commission_for_confirmed_payment (AUCUNE SECONDE LOGIQUE), IDEMPOTENCE VIA CLÉ DE LICENCE, SÉCURITÉ CONFIRMÉE (promoter_id ET email IMPOSSIBLES À FALSIFIER, ABSENTS DU CONTRAT CLIENT). UNE TENTATIVE INITIALE DE REJET SUR 'PLAN NON RÉSOLVABLE' A ÉTÉ ANNULÉE APRÈS AVOIR CASSÉ UN TEST PRÉEXISTANT LÉGITIME (LICENCES LEGACY) — COMPORTEMENT HISTORIQUE PRÉSERVÉ. 33/33 TESTS DÉDIÉS, 68/68 RÉGRESSION COMPLÈTE, 6 TABLES IA INCHANGÉES, WEBHOOK PHASE 15.9 RECONFIRMÉ INTACT. AUCUNE MODIFICATION DU WEBHOOK, AUCUNE DONNÉE FABRIQUÉE, AUCUNE COMMISSION CRÉÉE MANUELLEMENT, AUCUN DEUXIÈME PAIEMENT. LE TEST RÉEL SUR USER 245/pi_ih0s0eixhgm1 N'A PAS ÉTÉ TENTÉ. AUCUN COMMIT, AUCUN PUSH, AUCUN UPLOAD HOSTINGER, AUCUNE MODIFICATION RAILWAY. VERDICT : ACTIVATE_LICENSE_COMMISSION_IMPLEMENTED_TESTS_PASS — EN ATTENTE D'UNE DÉCISION HUMAINE SÉPARÉE SUR LE DÉPLOIEMENT AVANT TOUTE TENTATIVE RÉELLE DE RÉCUPÉRATION DU PAIEMENT.**
