"""
api/app/ai/readiness — Phase 9 : XFOOT PRODUCTION READINESS & CONTROLLED
ACTIVATION V1.

ÉVALUATION uniquement (§47 du prompt) — ce package ne décide, n'active, ne
promeut et ne modifie AUCUN modèle/table de production. Il lit
exclusivement (DB read-only + Shadow Store read-only + rapports JSON déjà
générés par les phases 8G-8N) et produit un verdict GO/CONDITIONAL/NO-GO.
"""
