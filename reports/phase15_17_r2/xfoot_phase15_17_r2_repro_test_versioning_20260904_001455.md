# PHASE 15.17-R2 — DÉCISION ET VERSIONNAGE DU TEST DE REPRODUCTIBILITÉ

Généré : 2026-09-04 00:14:55 UTC

## PARTIE A — Convention Git

`.gitignore` : `__pycache__/`, `*.pyc`, `.claude/`, `logs/`, `*.tmp`, `.env`, `*.db*`. **Aucune règle ne concerne `reports/` ni les fichiers de test** — l'absence de versionnage de ces 178 fichiers était une **convention d'usage** (jamais staged), jamais une exclusion technique. Confirmé : `git ls-files` vide pour ce fichier, `git check-ignore` renvoie code 1 (non ignoré), `git log --all` vide (jamais committé, dans aucun commit, aucune branche).

## PARTIE B — Audit du test

`api/test_phase15_7_referral_attribution_repro.py` lu intégralement.

| Critère | Résultat |
|---|---|
| Test Xfoot, attribution referral | ✅ |
| Secrets | ✅ Aucun — emails fictifs `@repro157.example.com`, mot de passe de test fixe déjà utilisé partout dans ce dépôt |
| Identifiants sensibles réels | ✅ Aucun — jamais USER 245/oktest@test.com, jamais de vraie licence |
| Dépendance à un fichier local personnel | ✅ Aucune — `_test_support.py`, `main.py`, `models/promoter.py`, `promoter_service.py` tous déjà suivis par Git |
| Base isolée | ✅ SQLite dédiée, jamais `app.db`, jamais la production |
| Reproductible | ✅ |
| Compatible avec la suite actuelle | ✅ Déjà exécuté 24/24 dans 5 phases précédentes |
| Aucun paiement réel | ✅ Checkout mocké, jamais de `successful.sale` simulé |
| Aucune modification de production | ✅ |

**Aucune réécriture effectuée** — le fichier était déjà correct et fonctionnel.

## PARTIE C — Rapports historiques

**Non ajoutés.** Les 115 rapports referral/billing/retraits et les rapports phase14.x/15.x restent hors staging, hors commit, non supprimés.

## PARTIE D — AI_FOREIGN

**Rien ajouté, rien modifié, rien restauré, rien supprimé** : `gates.py`, `evidence_snapshots.json`, `test_phase13.py`, `phase13_evidence_maturation.py`, rapports phases 9.5/11/12/13 — tous strictement hors de ce commit.

## PARTIE E — Tests

Standalone : **24/24**. Régression pertinente : `test_referral_promoter_platform.py` 99/99, `test_phase14_1_production_integration.py` 36/36, `test_phase15_9_webhook_hardening.py` 31/31, `test_phase15_10_activate_license_commission.py` 33/33, `test_phase15_12_amount_reconciliation.py` 36/36, `test_phase15_14_manual_withdrawal.py` 56/56, `test_phase15_16_1_admin_promoter_search.py` 41/41, `test_auth.py` 8/8. **0 échec.** 6 tables IA vérifiées inchangées.

## PARTIE F — Staging

`git add api/test_phase15_7_referral_attribution_repro.py` — chemin explicite, jamais `git add .`/`-A`. `git diff --cached --name-status` : **exactement 1 fichier** (`A api/test_phase15_7_referral_attribution_repro.py`), 224 insertions. Aucun fichier inattendu.

## PARTIE G — Commit

**SHA : `b0a680974666776dce8708c675e3328534d0d91d`** — 1 fichier, 224 insertions. Aucun mélange avec rapports/AI/gates.py/retraits/pricing/billing.

## PARTIE H — Push

Autorisation générale déjà accordée pour les checkpoints Xfoot. Poussé vers `origin/main`. **3 vérifications indépendantes convergentes** sur `b0a6809`.

## PARTIE I — Post-commit

- Test désormais suivi par Git : ✅
- Commit ne contient que le test : ✅
- Aucun rapport ajouté : ✅
- Aucun fichier AI_FOREIGN ajouté : ✅
- `gates.py` hors commit, dernier commit le touchant toujours `00dd971` (inchangé) : ✅
- Aucun retrait réel modifié, aucune donnée de production modifiée : ✅

**Note sur `git status`** : 179 fichiers non suivis subsistent après ce commit (178 − 1 test committé + 2 nouveaux fichiers du rapport Phase 15.17-R1, écrits entre-temps dans cette même conversation — écart vérifié par diff explicite, aucune anomalie). Le working tree n'a **pas** été artificiellement nettoyé, conformément à la consigne.

## Verdict

**`REPRO_TEST_VERSIONED_AND_PUSHED`**

Le test de reproductibilité de l'attribution referral est désormais versionné, testé (24/24 + régression complète 0 échec), commité seul (`b0a6809`) et poussé, vérifié par 3 méthodes indépendantes convergentes. Les 115 rapports historiques et les fichiers `AI_FOREIGN` restent strictement hors Git, `gates.py` intouché, aucun changement financier, aucun changement IA.

---

**PHASE 15.17-R2 — TEST DE REPRODUCTIBILITÉ VERSIONNÉ AVEC SUCCÈS : COMMIT b0a6809 (1 SEUL FICHIER, 224 INSERTIONS) POUSSÉ ET VÉRIFIÉ PAR 3 MÉTHODES INDÉPENDANTES CONVERGENTES. AUDIT COMPLET DU TEST CONFIRME AUCUN SECRET, AUCUNE DONNÉE SENSIBLE RÉELLE, AUCUNE DÉPENDANCE NON VERSIONNÉE, AUCUN PAIEMENT RÉEL. 24/24 STANDALONE + RÉGRESSION COMPLÈTE (8 SUITES) À 0 ÉCHEC, 6 TABLES IA INCHANGÉES. LES 115 RAPPORTS HISTORIQUES ET LES 64 FICHIERS AI_FOREIGN RESTENT STRICTEMENT HORS GIT, AUCUN N'A ÉTÉ MODIFIÉ NI SUPPRIMÉ. gates.py INTOUCHÉ (DERNIER COMMIT LE CONCERNANT TOUJOURS 00dd971). AUCUN RETRAIT RÉEL MODIFIÉ, AUCUN PAIEMENT EFFECTUÉ, AUCUNE DONNÉE IA MODIFIÉE. VERDICT : REPRO_TEST_VERSIONED_AND_PUSHED.**
