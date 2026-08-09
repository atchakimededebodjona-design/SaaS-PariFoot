"""
test_elo_backtest.py — Brier score Elo, garde-fou de non-régression
(Phase 3, tâche 6).

Échoue si le Brier score Elo se dégrade significativement par rapport au
baseline mesuré et documenté dans le rapport Phase 3 (K=32,
home_advantage=60, choisis par la grille — voir scripts/backtest_elo.py).
Hyperparamètres figés ici plutôt que re-recherchés à chaque run : c'est la
QUALITÉ du modèle qui en résulte qu'on veut protéger d'une régression, pas
la recherche en grille elle-même (déjà exécutée et documentée une fois).

Copie api/app.db dans un fichier temporaire isolé (voir
test_dixon_coles_db.py pour la même précaution) — ce test ne persiste
JAMAIS de ModelVersion (contrairement à scripts/backtest_elo.py), pour ne
jamais entrer en conflit avec la contrainte UNIQUE sur ModelVersion.name
lors d'exécutions répétées.

Usage : python test_elo_backtest.py (depuis api/)
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

_SOURCE_DB = Path(__file__).resolve().parent / "app.db"
_TEST_DB = Path(__file__).resolve().parent / "test_elo_backtest.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
if not _SOURCE_DB.exists():
    raise SystemExit(f"{_SOURCE_DB} introuvable — lancer scripts/load_historical_matches.py d'abord.")
shutil.copy2(_SOURCE_DB, _TEST_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

from backtest_elo import LEAGUES, elo_test_brier, dc_test_brier  # noqa: E402
from app.ai.engine.dixon_coles import load_matches_from_db  # noqa: E402

BEST_K = 32.0
BEST_HOME_ADVANTAGE = 60.0
# Baseline mesuré (rapport Phase 3) : Brier moyen Elo = 0.6153. ~5% de
# marge pour absorber le bruit de reproductibilité sans masquer une
# vraie régression.
MAX_MEAN_BRIER = 0.65


def test_elo_brier_not_regressed():
    briers = []
    for league in LEAGUES:
        matches = load_matches_from_db(league).sort_values("date").reset_index(drop=True)
        elo_brier, _ = elo_test_brier(matches, BEST_K, BEST_HOME_ADVANTAGE)
        briers.append(elo_brier)

    mean_brier = sum(briers) / len(briers)
    assert mean_brier <= MAX_MEAN_BRIER, (
        f"Brier moyen Elo {mean_brier:.4f} dépasse le seuil de non-régression {MAX_MEAN_BRIER} "
        f"(baseline documenté : 0.6153)"
    )
    print(f"  [OK] Brier moyen Elo (K={BEST_K}, home_advantage={BEST_HOME_ADVANTAGE}) = {mean_brier:.4f} "
          f"(seuil {MAX_MEAN_BRIER})")


def test_elo_vs_dixon_coles_comparable():
    """Ne bloque PAS sur qui gagne (Elo n'a pas battu Dixon-Coles au moment
    de ce ticket — voir rapport Phase 3, verdict honnête, ModelVersion
    xfoot-elo-v1 inactive) — vérifie seulement que la comparaison reste
    calculable et dans des bornes valides (Brier dans [0, 2] pour 3
    classes)."""
    for league in LEAGUES:
        matches = load_matches_from_db(league).sort_values("date").reset_index(drop=True)
        elo_brier, _ = elo_test_brier(matches, BEST_K, BEST_HOME_ADVANTAGE)
        dc_brier, n = dc_test_brier(league, matches)
        assert n > 0, f"[{league}] aucune prédiction Dixon-Coles calculable sur le test set"
        assert 0.0 <= elo_brier <= 2.0, f"[{league}] Brier Elo hors bornes : {elo_brier}"
        assert 0.0 <= dc_brier <= 2.0, f"[{league}] Brier Dixon-Coles hors bornes : {dc_brier}"
    print(f"  [OK] Comparaison calculable sur les {len(LEAGUES)} ligues")


if __name__ == "__main__":
    tests = [test_elo_brier_not_regressed, test_elo_vs_dixon_coles_comparable]
    passed = 0
    for test in tests:
        print(f"\n=== {test.__name__} ===")
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  [ÉCHEC] {e}")

    print("\n" + "=" * 60)
    print(f"{passed}/{len(tests)} tests reussis")
    print("=" * 60)
    try:
        _TEST_DB.unlink(missing_ok=True)
    except PermissionError:
        pass  # verrou Windows bref sur le fichier SQLite — sans conséquence (cf. _test_support.cleanup_db)
    sys.exit(0 if passed == len(tests) else 1)
