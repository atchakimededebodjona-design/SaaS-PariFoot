"""
test_dixon_coles_db.py — Non-régression Dixon-Coles : base vs production
(Phase 3, tâche 6).

Copie api/app.db dans un fichier temporaire ISOLÉ (jamais écrit dans le
dev DB réel par ce test — même précaution que _test_support.configure_test_env,
mais ici on a besoin d'un historique RÉEL déjà chargé, pas d'une base
vide) puis compare les paramètres entraînés depuis match/match_stats aux
paramètres actuellement en production (api/model_artifacts/*.json).

Nécessite que l'historique ait été chargé au préalable (Phase 2,
scripts/load_historical_matches.py) — échoue explicitement si match est
vide plutôt que de "réussir" silencieusement sur 0 ligne.

Usage : python test_dixon_coles_db.py (depuis api/)
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

_SOURCE_DB = Path(__file__).resolve().parent / "app.db"
_TEST_DB = Path(__file__).resolve().parent / "test_dixon_coles_db.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
if not _SOURCE_DB.exists():
    raise SystemExit(f"{_SOURCE_DB} introuvable — lancer scripts/load_historical_matches.py d'abord.")
shutil.copy2(_SOURCE_DB, _TEST_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

from train_dixon_coles_from_db import compare_to_production, TOLERANCE  # noqa: E402
from app.ai.engine.dixon_coles import train_all_leagues_from_db  # noqa: E402


def test_non_regression_vs_production():
    artifacts = train_all_leagues_from_db()
    assert artifacts, "Aucune ligue entraînée — la table match est-elle vide ?"

    result = compare_to_production(artifacts)
    for league, r in result["per_league"].items():
        assert "reason" not in r, f"[{league}] {r.get('reason')}"
        assert not r["missing_teams"] and not r["extra_teams"], f"[{league}] écart d'équipes : {r}"
        assert r["trained_on_matches_db"] == r["trained_on_matches_prod"], (
            f"[{league}] nombre de matchs différent : base={r['trained_on_matches_db']} "
            f"prod={r['trained_on_matches_prod']}"
        )
        assert r["max_diff"] <= TOLERANCE, (
            f"[{league}] écart de paramètre {r['max_diff']:.2e} > tolérance {TOLERANCE:.0e}"
        )
    print(f"  [OK] {len(result['per_league'])} ligues, écart max toutes ligues : {result['max_diff_overall']:.2e} "
          f"(tolérance {TOLERANCE:.0e})")


if __name__ == "__main__":
    tests = [test_non_regression_vs_production]
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
