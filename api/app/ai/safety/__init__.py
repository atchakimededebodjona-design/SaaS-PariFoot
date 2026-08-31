"""
api/app/ai/safety — Phase 9.1 : XFOOT PRODUCTION SAFETY CONTROLS & KILL
SWITCH V1.

SAFETY HARDENING ONLY (§Règle absolue) — ce package ne promeut, n'entraîne,
ne recalibre, ne modifie aucune prédiction historique et n'active aucune
production. Il fournit le MÉCANISME (Kill Switch réel, fail-closed,
rollback empiriquement testable) que la Phase 9 (api/app/ai/readiness/)
avait constaté absent (gate OBSERVABILITY, "aucun mécanisme de kill switch
n'existe dans le code actuel").

Un seul mécanisme central (§2 : "NE PAS créer plusieurs mécanismes
concurrents") — jamais un deuxième Kill Switch ailleurs dans le dépôt.
"""
