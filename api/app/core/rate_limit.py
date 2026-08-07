"""
Rate limiting partagé (slowapi), pour les endpoints sensibles au brute
force / spam : /auth/login, /auth/register, /billing/pulse.

Limité par adresse IP (get_remote_address) — protection de premier niveau,
pas infaillible derrière un proxy/CDN partagé sans configuration
X-Forwarded-For dédiée (à revisiter si l'API est déployée derrière un tel
proxy — la plupart exposent l'IP réelle du client dans ce header, que
get_remote_address ne lit pas par défaut).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
