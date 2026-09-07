# PHASE 15.17-R1 — AUDIT COMPLET DES 180 CHANGEMENTS AVANT COMMIT/PUSH

Généré : 2026-09-04 00:06:27 UTC — **audit uniquement, aucune action Git effectuée.**

## Correction du rapport précédent

Le rapport Phase 15.17 affirmait « rien à committer » — vrai **uniquement** pour le code du retrait manuel/recherche promoteur (déjà dans `a0dcbc7`/`dcd70e6`), mais 178 fichiers non suivis + 2 fichiers modifiés (**180 au total**) existaient dans l'arbre de travail sans avoir été énumérés individuellement. Cette phase corrige cette omission.

## PARTIE A — Inventaire complet

`git status --short` n'affiche que **102 lignes** car il **collapse** un répertoire entièrement non suivi en une seule ligne (`?? reports/phase14_5/`). `git ls-files --others --exclude-standard` **expanse** chaque répertoire en fichiers individuels : **178 fichiers non suivis** + **2 fichiers modifiés déjà suivis** (`gates.py`, `evidence_snapshots.json`) = **180** — exactement le chiffre observé dans VS Code (qui affiche toujours les fichiers individuels).

## PARTIE B — Classification (16 groupes, tous les 180 fichiers)

| # | Groupe | Fichiers | Catégorie | Vérification |
|---|---|---|---|---|
| 1 | `gates.py` | 1 | AI_FOREIGN | Connu, hors périmètre depuis 15.9 |
| 2 | `evidence_snapshots.json` | 1 | AI_FOREIGN | Chemin `reports/shadow/watch/` = monitoring IA |
| 3 | `test_phase13.py` | 1 | AI_FOREIGN | Contenu lu : shadow evidence maturation IA |
| 4 | `phase13_evidence_maturation.py` | 1 | AI_FOREIGN | Contenu lu : même Phase 13 IA |
| 5 | `test_phase15_7_referral_attribution_repro.py` | 1 | TEST_XFOOT (referral) | Activement utilisé en régression depuis 15.9 |
| 6 | `reports/phase9_5/*` | 24 | AI_FOREIGN | Mots-clés shadow/gate/model_version confirmés, 0% promoter/referral |
| 7 | `reports/phase11/*` | 24 | AI_FOREIGN | Idem |
| 8 | `reports/phase12/*` | 8 | AI_FOREIGN | Idem |
| 9 | `reports/phase13/*` | 4 | AI_FOREIGN | Idem |
| 10 | `reports/phase14*/*` (22 répertoires) | 47 | PROMOTION_REFERRAL | Mots-clés promoter/referral/commission confirmés ; `deployment_manifest.json` confirme explicitement l'origine |
| 11 | `reports/phase15_1` à `15_5` | 10 | PROMOTION_REFERRAL / BILLING_PAYMENT | Mots-clés chariow/checkout/commission/referral confirmés |
| 12 | `reports/phase15_6/*` | 14 | PROMOTION_REFERRAL | Titres confirmés (audit attribution referral) |
| 13 | `reports/phase15_7/*` | 18 | PROMOTION_REFERRAL | Titres confirmés (correctif referral + déploiement) |
| 14 | `reports/phase15_8/*`, `15_9/*` | 4 | BILLING_PAYMENT | Connu de l'historique direct de cette conversation |
| 15 | `reports/phase15_10` à `15_13` | 12 | BILLING_PAYMENT | Idem |
| 16 | `reports/phase15_14` à `15_17` | 10 | PROMOTION_WITHDRAWAL | Rapports de cette série même |

**Totaux** : AI_FOREIGN = 64 · PROMOTION_REFERRAL (reports) = 89 · BILLING_PAYMENT (reports) = 16 · PROMOTION_WITHDRAWAL (reports) = 10 · TEST_XFOOT = 1 · **UNKNOWN = 0** (aucun fichier non classifiable avec certitude). Total = **180**.

*(La liste exhaustive des 178 chemins individuels figure dans le JSON accompagnant ce rapport, groupe par groupe.)*

## PARTIE C — `gates.py`

**`AI_FOREIGN` / `EXCLUDED_FOREIGN_AI_CHANGE`.** Aucune action : non modifié, non stagé, non committé, non supprimé, non restauré. Laissé strictement dans son état actuel.

## PARTIE D — Rapports Phase 9.5/11/12/13 (et tout le lot AI_FOREIGN)

- **Origine** : confirmée par le champ `"phase"` interne de chaque JSON + scan de mots-clés (shadow/gate_/model_version/readiness/prediction), aucune ambiguïté.
- **Déjà suivis par Git ?** Non, sauf `gates.py`/`evidence_snapshots.json` (suivis mais modifiés).
- **Secrets ?** Scan explicite (patterns `sk_live`, clé API littérale, mot de passe littéral, secret littéral, Bearer token en dur, license_key littérale) sur les 178 fichiers non suivis → **9 correspondances, toutes vérifiées manuellement et confirmées fausses positives** : le mot de passe de test fixe `correct-horse-battery-staple` (déjà utilisé partout dans ce dépôt), des mentions `"CHARIOW_PULSE_SECRET": "PRESENT (valeur jamais affichée)"`, et des listes de *patterns recherchés* (jamais de valeurs réelles). **Aucun secret réel trouvé dans aucun des 180 fichiers.**
- **Définitifs ou temporaires ?** Définitifs — rapports de phase complets et signés (verdict + horodatage), pas des fichiers cache/temporaires.

## PARTIE E — Tests

| Fichier | Rôle | Utile ? | Déjà committé ? | Recommandation |
|---|---|---|---|---|
| `test_phase13.py` | Tests IA shadow evidence (Phase 13) | Oui, pour la suite IA | Non | Hors périmètre Promotion/Retraits — candidat à un futur checkpoint AI séparé |
| `test_phase15_7_referral_attribution_repro.py` | Reproduction attribution referral | **Oui, activement** — exécuté en régression par 5 phases depuis 15.9 | Non | **Candidat prioritaire** — son absence du dépôt versionné est un risque réel pour la reproductibilité |

Aucun test supprimé.

## PARTIE F — Historique Git

`a0dcbc7` et `dcd70e6` contiennent exactement le **code source** (`app/`, `frontend-design/`) des Phases 15.14/15.15 et 15.16.1/15.16.2 — reconfirmé ici. **Aucun** des 178 fichiers non suivis ne correspond à du code de ces phases : ce sont exclusivement des **rapports** (JSON/MD) et 3 fichiers de tests/scripts jamais committés à travers **toute** l'histoire du dépôt — convention constante depuis le tout premier commit (`00dd971`), pas une omission propre à cette série récente.

## PARTIE G — Promotion / Retraits

Le code des Phases 15.14 à 15.16.2 est **100% déjà commité** et poussé (reconfirmé : `git diff --stat -- api frontend-design` ne montre que `gates.py`, hors périmètre). Les seuls fichiers non commités appartenant réellement à ces phases sont les **rapports** (groupe 16, 10 fichiers) — aucune logique n'a été ni ne sera modifiée pour créer artificiellement un commit.

## PARTIE H — IA

64 fichiers `AI_FOREIGN` identifiés et exclus. 6 tables protégées vérifiées inchangées (`match=12459, match_stats=12459, model_predictions=3610, model_versions=15, team_ratings=568, prediction_log=9`). **Aucun fichier AI recommandé pour ce checkpoint.**

## PARTIE I — Aucune suppression

Aucun `rm`, `git clean`, `git reset --hard`, `git checkout -- .` ni `git restore .` exécuté. 180 fichiers avant, 180 après.

## PARTIE J — Aucun commit

`git add`, `git commit`, `git push` : **aucun exécuté** dans cette sous-phase.

## Recommandation de périmètre (proposition, aucune action prise)

| Option | Portée | Implication |
|---|---|---|
| **1 — Minimale (recommandée)** | `test_phase15_7_referral_attribution_repro.py` seul | Corrige un risque réel de reproductibilité de régression, sans ouvrir le débat des rapports historiques |
| **2 — Rapports de cette série** | + les 10 rapports du groupe 16 (Phase 15.14-15.17) | Nécessite une décision explicite : commencer à versionner les rapports de *cette* série |
| **3 — Tous les rapports Xfoot non-IA** | + les 115 rapports des groupes 10 à 16 | Changerait une convention constante depuis le début du projet — décision majeure, jamais assumée par cet agent |

**Jamais recommandé** : les 64 fichiers `AI_FOREIGN` — strictement hors périmètre d'un checkpoint Promotion/Retraits.

## Verdict

**`AUDIT_REQUIRES_HUMAN_DECISION`**

La classification des 180 fichiers est **certaine** (vérifiée par lecture de contenu, 0 fichier en catégorie UNKNOWN). Ce qui reste en suspens n'est pas un problème de classification mais une **décision de convention** : faut-il commencer à versionner des rapports jamais commités jusqu'ici, et à quelle échelle (option 1, 2 ou 3) ? Cette décision appartient explicitement à l'utilisateur.

---

**PHASE 15.17-R1 — INVENTAIRE EXHAUSTIF TERMINÉ : 180 CHANGEMENTS IDENTIFIÉS AVEC CERTITUDE (2 MODIFIÉS + 178 NON SUIVIS), CLASSÉS EN 16 GROUPES, 0 FICHIER UNKNOWN. 64 FICHIERS AI_FOREIGN CONFIRMÉS PAR LECTURE DE CONTENU (JAMAIS PROMOTER/REFERRAL), STRICTEMENT EXCLUS. AUCUN SECRET TROUVÉ DANS AUCUN DES 180 FICHIERS (9 FAUX POSITIFS VÉRIFIÉS UN PAR UN). LE CODE PROMOTION/RETRAITS EST DÉJÀ 100% COMMITÉ (a0dcbc7, dcd70e6) — SEULS DES RAPPORTS HISTORIQUES ET 1 FICHIER DE TEST ACTIVEMENT UTILISÉ MAIS JAMAIS VERSIONNÉ RESTENT EN SUSPENS. AUCUN FICHIER SUPPRIMÉ, AUCUN STAGING, AUCUN COMMIT, AUCUN PUSH. VERDICT : AUDIT_REQUIRES_HUMAN_DECISION — LA DÉCISION DE PÉRIMÈTRE (OPTION 1/2/3) APPARTIENT À L'UTILISATEUR.**
