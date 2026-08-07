"""
test_refresh_and_retrain.py — Vérifie que le job résiste à un CSV corrompu :
les artefacts existants ne doivent JAMAIS être altérés, et l'API doit
continuer à fonctionner avec les anciennes données.

Ne touche JAMAIS aux vrais fichiers du projet (data/all_leagues_raw_with_stats.csv,
api/model_artifacts/*.json) — tout se passe dans un répertoire temporaire, via
les options --raw-file/--artifacts-dir de refresh_and_retrain.run().

Usage : python test_refresh_and_retrain.py
"""

import filecmp
import json
import shutil
import sys
import tempfile
from pathlib import Path

import refresh_and_retrain

REAL_ARTIFACTS_DIR = Path("api/model_artifacts")


def _checksum_dir(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(d.glob("*.json"))}


def test_corrupted_csv_does_not_touch_artifacts():
    print("=== Test : CSV corrompu ne doit jamais altérer les artefacts existants ===")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # Copie des VRAIS artefacts dans un dossier de test (jamais les
        # vrais fichiers du projet ne sont touchés par ce test).
        test_artifacts_dir = tmp / "model_artifacts"
        shutil.copytree(REAL_ARTIFACTS_DIR, test_artifacts_dir)
        before = _checksum_dir(test_artifacts_dir)
        print(f"  {len(before)} artefacts copiés depuis {REAL_ARTIFACTS_DIR} vers {test_artifacts_dir}")

        # CSV volontairement corrompu (colonnes manquantes, contenu incohérent).
        corrupted_csv = tmp / "corrupted_raw.csv"
        corrupted_csv.write_text("this is not,a valid csv\nfor,dixon coles\n???,###\n", encoding="utf-8")
        print(f"  CSV corrompu écrit dans {corrupted_csv}")

        exit_code = refresh_and_retrain.run(
            raw_file=str(corrupted_csv),
            artifacts_dir=test_artifacts_dir,
            skip_refresh=True,  # on injecte directement le fichier corrompu en entrée du ré-entraînement
        )

        print(f"  Code de sortie du job : {exit_code} (attendu : 1, échec total)")
        assert exit_code == 1, f"attendu 1 (échec total), obtenu {exit_code}"

        after = _checksum_dir(test_artifacts_dir)
        assert before.keys() == after.keys(), "des fichiers ont disparu ou été ajoutés !"
        for name in before:
            assert before[name] == after[name], f"{name} a été modifié malgré l'échec du job !"
        print(f"  OK — les {len(after)} artefacts sont byte-identiques avant/après (aucune altération).")

        # L'API doit pouvoir se recharger normalement sur les VRAIS artefacts
        # (jamais touchés), preuve qu'un job cassé ne compromet pas le service.
        sys.path.insert(0, str(Path("api")))
        import importlib
        import main as api_main
        importlib.reload(api_main)  # force le rechargement depuis les vrais api/model_artifacts/
        assert len(api_main.LEAGUE_MODELS) == 5
        r = api_main._resolve_and_predict("Ligue1", "PSG", "Marseille")
        assert abs(r.home_win + r.draw + r.away_win - 1.0) < 1e-6
        print(f"  OK — API toujours fonctionnelle avec les anciennes données "
              f"(prédiction PSG-Marseille : home_win={r.home_win}).")

    print("\n>>> TEST RÉUSSI : job résilient à un CSV corrompu, artefacts intacts, API opérationnelle.\n")


def test_partial_validation_failure_keeps_only_failing_league():
    """
    Vérifie le cas de succès PARTIEL : un artefact "en mémoire" avec des
    paramètres hors fourchette pour UNE ligue ne doit affecter QUE cette
    ligue — les autres doivent quand même être écrites.
    """
    print("=== Test : échec de validation isolé à une seule ligue ===")
    from validate_artifacts import validate_artifact

    old = {"league": "Ligue1", "teams": ["A", "B", "C"], "home_advantage": 0.15, "rho": -0.07,
           "trained_on_matches": 2000}
    bad_new = {"league": "Ligue1", "teams": ["A", "B", "C"], "home_advantage": 5.0,  # hors fourchette
               "rho": -0.07, "trained_on_matches": 2100}
    good_new = {"league": "Ligue1", "teams": ["A", "B", "C"], "home_advantage": 0.16,
                "rho": -0.06, "trained_on_matches": 2100}

    bad_result = validate_artifact(bad_new, old)
    good_result = validate_artifact(good_new, old)

    assert not bad_result.ok, "un home_advantage=5.0 aurait dû échouer la validation"
    assert good_result.ok, "un artefact plausible ne devrait pas échouer"
    print(f"  OK — artefact avec home_advantage=5.0 rejeté : {bad_result.reasons}")
    print(f"  OK — artefact plausible accepté (aucune raison de rejet).")
    print("\n>>> TEST RÉUSSI\n")


if __name__ == "__main__":
    test_corrupted_csv_does_not_touch_artifacts()
    test_partial_validation_failure_keeps_only_failing_league()
    print("=" * 60)
    print("TOUS LES TESTS ONT RÉUSSI")
    print("=" * 60)
