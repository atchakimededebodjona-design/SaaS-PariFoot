# PHASE 15.11-DIAG — DIAGNOSTIC POST-ACTIVATION

Généré : 2026-09-03 19:00:00 UTC

## Chaîne causale complète (prouvée, pas supposée)

```
USER 245
  ↓ licence Chariow vérifiée (GET /licenses/{key}, status='active') — PROUVÉ (abonnement ACTIVE)
ProviderSubscription activée (status=active, plan=monthly)         — PROUVÉ (Admin Abonnés)
  ↓
ReferralAttribution retrouvée (245 → promoter_id 206)              — TRÈS FORTEMENT PROUVÉ (indépendant)
promoter-2 status ACTIVE confirmé                                  — TRÈS FORTEMENT PROUVÉ
  ↓
create_commission_for_confirmed_payment(sale_body=license_data,
    delivery_id='activate-license:<license_key>') appelée          — PROUVÉ (code lu)
  ↓
extract_actual_paid_amount(license_data, 'monthly')
  → aucune clé montant dans license_data
  → REFERRAL_PLAN_PRICE_MONTHLY non configuré
  → (None, 'unavailable')                                          — CAUSE RACINE PROUVÉE PAR LE CODE
  ↓
précondition "amount is None" = FAIL → return None (pas d'exception)
  ↓
AUCUNE ReferralCommission créée
  ↓
Admin Abonnés : MONTANT PAYÉ = "—"        (sourcé EXCLUSIVEMENT depuis ReferralCommission, admin_router.py:179)
Admin Gains   : VENTES=0, REVENU=0, COMM=0 (stats.py:72-97, sourcé EXCLUSIVEMENT depuis ReferralCommission)
Promoteur     : CONVERTIS=0, VENTES=0, REVENU=0, COMM=0 (stats.py:26-69, même source exclusive)
```

## PARTIE A — Flux `activate-license` tracé

Toutes les 14 étapes demandées ont été tracées avec citation de code exacte. Points clés : la fonction **appelle bien** `create_commission_for_confirmed_payment` (Phase 15.10, dans un bloc `try/except`), mais **aucun champ montant n'est jamais extrait de `license_data`** — ce n'était déjà pas le cas avant Phase 15.10, la fonction `_fetch_chariow_license` n'a jamais documenté un tel champ. La réponse HTTP finale reste `200` quel que soit le résultat de la commission (best-effort assumé, documenté en Phase 15.10).

## PARTIE B — Payment amount : ROOT CAUSE prouvée

`extract_actual_paid_amount(license_data, 'monthly')` (`api/app/referral/money.py:67-88`) :
1. Cherche `amount`/`amount_paid`/`paid_amount`/`total_amount`/`total`/`price` **à la racine de `license_data`** — aucun de ces champs n'est documenté ni observé nulle part dans ce dépôt pour cet objet (le docstring de `_fetch_chariow_license` énumère explicitement les champs réels : `status`/`expires_at`/`product`/`metadata`/`customer.email` — **jamais un montant**).
2. Replie sur `PLAN_LIST_PRICES['monthly']` (variable `REFERRAL_PLAN_PRICE_MONTHLY`, lue **une seule fois** au chargement du module) — jamais mentionnée comme configurée sur Railway dans tout l'historique de ce projet.
3. Les deux échouent → `(None, "unavailable")`.

**Aucun champ n'a été inventé ou supposé exister** — la conclusion s'appuie sur l'absence documentée, jamais sur une hypothèse positive.

## PARTIE C — Payment/transaction ID

`delivery_id = f"activate-license:{license_key}"` — **différent** de `pi_ih0s0eixhgm1`. Ce n'est **pas** la cause du blocage (la fonction atteint sans problème l'étape d'idempotence) — mais **note architecturale importante pour la Phase 15.12** : si une commission avait pu être créée, elle serait retrouvable par `activate-license:<license_key>`, jamais par la référence Chariow elle-même.

## PARTIE D — Referral

L'attribution `245 → promoter-2` est **fortement confirmée indépendamment** : `admin_router.py:174-175,180` calcule le champ PROMOTEUR d'Admin Abonnés par une **lecture directe de `ReferralAttribution`**, totalement indépendante de l'existence d'une commission. `REFERRAL_LOOKUP_FAIL` est donc **écarté avec de fortes preuves**.

## PARTIE E — Préconditions de `create_commission_for_confirmed_payment`

| Précondition | Résultat |
|---|---|
| Attribution existe | PASS (déduit fortement) |
| Promoteur existe | PASS |
| Promoteur ACTIVE | PASS |
| Pas de self-referral | PASS (évident) |
| `delivery_id` pas déjà traité | PASS (première tentative) |
| **Montant non nul** | **FAIL** |

Une seule précondition échoue — pas plusieurs indépendantes.

## PARTIE F — Idempotence

Clé qui aurait dû être utilisée : `activate-license:<license_key>` (pas `pi_ih0s0eixhgm1`). Ledger entry existant : `NOT_VERIFIABLE` directement (pas d'accès DB), mais `COMMISSIONS=0` sur la **totalité** du système (52 abonnements, 0 commission) est une preuve indirecte forte qu'aucune commission n'a jamais pu être créée par ce chemin depuis le déploiement.

## PARTIE G — Écart tests/réalité — identifié

`_fake_license()` (test Phase 15.10) accepte un paramètre `amount` optionnel injecté **directement à la racine** — un champ jamais confirmé comme réellement renvoyé par Chariow (le commentaire du test le disait déjà : *« champ hypothétique, testé tel quel — jamais garanti par Chariow »*). Les TEST 1/2 (chemin « heureux ») l'utilisaient avec `amount=1500`. **Le TEST 6** (montant absent) avait pourtant déjà **correctement prédit** exactement le comportement réel observé aujourd'hui — son signal n'a simplement pas été interprété comme le comportement par défaut le plus probable, faute d'accès à la vraie réponse Chariow au moment de la Phase 15.10.

## PARTIE H — Intention vs comportement

Intention Phase 15.10 : **B) activation + réconciliation financière complète**. Comportement réel : **A) activation seule** — la réconciliation est bien *tentée* mais échoue systématiquement faute de montant exploitable. Le design du code est correct (réutilisation stricte, aucune seconde logique) ; c'est la **donnée source** qui manque.

## Aucune modification effectuée

`router.py` NON · tests NON · DB NON · `ReferralAttribution` NON · ledger commission NON · commit NON · push NON · déploiement NON · licence Chariow jamais demandée/affichée/loggée.

## Verdict

**`MULTIPLE_ROOT_CAUSES`** (cause principale : `AMOUNT_RECONCILIATION_MISSING`)

`AMOUNT_RECONCILIATION_MISSING` suffirait seul à expliquer 100 % des symptômes. `MULTIPLE_ROOT_CAUSES` est retenu car deux problèmes indépendants supplémentaires ont été prouvés : (Partie C) l'identifiant d'idempotence n'est pas la référence Chariow elle-même, et (Partie G) un écart réel entre les tests et la réponse Chariow authentique. `PAYMENT_ID_RECONCILIATION_MISSING` et `REFERRAL_LOOKUP_FAIL` sont explicitement **écartés**, preuves à l'appui.

---

**PHASE 15.11-DIAG — CAUSE RACINE PROUVÉE PAR LE CODE, PAS SUPPOSÉE : extract_actual_paid_amount RETOURNE (None, 'unavailable') CAR L'OBJET GET /licenses/{key} DE CHARIOW NE PORTE AUCUN CHAMP DE MONTANT ET AUCUN PRIX FIXE DE REPLI N'EST CONFIGURÉ. TOUTE LA CHAÎNE EN AVAL EST STRICTEMENT SOURCÉE DEPUIS ReferralCommission (CITATIONS DE CODE EXACTES À L'APPUI). L'ATTRIBUTION REFERRAL EST FORTEMENT CONFIRMÉE FONCTIONNELLE. UN ÉCART TESTS/RÉALITÉ A ÉTÉ IDENTIFIÉ ET DOCUMENTÉ. AUCUNE CORRECTION EFFECTUÉE CETTE PHASE. VERDICT : MULTIPLE_ROOT_CAUSES. LA PHASE 15.12 DE CORRECTION CIBLÉE POURRA S'APPUYER SUR CE DIAGNOSTIC SANS AMBIGUÏTÉ.**
