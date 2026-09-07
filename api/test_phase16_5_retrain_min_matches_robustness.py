"""
test_phase16_5_retrain_min_matches_robustness.py — Phase 16.5.1 : non-régression
du correctif de robustesse de RETRAIN_MIN_MATCHES (app/ai/arena/retraining.py::
_resolve_min_matches). Preuve principale : `import app.ai.arena.retraining`
(donc `import main`, donc le démarrage du service FastAPI, Phase 16.5 —
RETRAIN_MIN_MATCHES_API_STARTUP_RISK_CONFIRMED) ne lève plus jamais
d'exception, quelle que soit la valeur de RETRAIN_MIN_MATCHES.

Comme RETRAIN_MIN_MATCHES est résolue UNE SEULE FOIS, au chargement du module
(constante de niveau module), la seule façon fiable de tester plusieurs
valeurs sans polluer le processus de test courant (qui a déjà importé/
importera ce module une fois pour toutes via sys.modules) est un
sous-processus Python isolé et jetable par valeur testée — jamais de rechargement
manuel de module dans le processus principal.

N'utilise jamais la vraie DATABASE_URL/production, ne modifie jamais Railway
ni .env — chaque sous-processus reçoit un environnement copié avec seulement
RETRAIN_MIN_MATCHES modifiée (DATABASE_URL retirée : retombe sur le défaut
SQLite local `sqlite:///./app.db`, jamais ouvert — l'import seul ne se
connecte jamais à une base, SQLAlchemy est paresseux sur create_engine()).

Usage : python api/test_phase16_5_retrain_min_matches_robustness.py
"""

import os
import subprocess
import sys
from pathlib import Path

API_DIR = Path(__file__).parent

_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  [ECHEC] {name}")


def _run_import_subprocess(value: str | None) -> subprocess.CompletedProcess:
    """Sous-processus Python isolé et jetable : importe app.ai.arena.retraining
    et affiche uniquement RETRAIN_MIN_MATCHES sur stdout. `value=None` retire
    complètement la variable (cas "absente"), toute autre valeur (y compris
    "") la positionne telle quelle."""
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)  # jamais la vraie DB — retombe sur le défaut SQLite local, jamais ouvert
    env.pop("JWT_SECRET_KEY", None)
    if value is None:
        env.pop("RETRAIN_MIN_MATCHES", None)
    else:
        env["RETRAIN_MIN_MATCHES"] = value
    code = "import app.ai.arena.retraining as r\nprint(r.RETRAIN_MIN_MATCHES)\n"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(API_DIR), env=env, capture_output=True, text=True, timeout=30,
    )


def _check_value(label: str, env_value, expected_output: str) -> None:
    result = _run_import_subprocess(env_value)
    check(f"{label} : import réussi (exit code 0)", result.returncode == 0)
    check(f"{label} : RETRAIN_MIN_MATCHES == {expected_output}",
          result.stdout.strip() == expected_output)
    if result.returncode != 0:
        print(f"    stderr: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '(vide)'}")


def test_A_absent():
    _check_value("A. variable absente", None, "500")


def test_B_empty():
    _check_value("B. variable vide", "", "500")


def test_C_whitespace_only():
    _check_value("C. variable espaces uniquement", "   ", "500")


def test_D_abc():
    _check_value("D. \"abc\"", "abc", "500")


def test_E_decimal():
    _check_value("E. \"12.5\"", "12.5", "500")


def test_F_valid_10():
    _check_value("F. \"10\"", "10", "10")


def test_G_valid_10_with_spaces():
    _check_value("G. \" 10 \"", " 10 ", "10")


def test_H_very_large():
    _check_value("H. \"999999999\"", "999999999", "999999999")


def test_I_invalid_value_warning_logged_not_swallowed_silently():
    """Le fallback ne doit jamais être totalement silencieux : un warning
    doit apparaître sur stderr (logging par défaut écrit sur stderr) pour
    une valeur invalide — jamais pour une valeur absente (comportement
    normal documenté, pas une erreur de configuration)."""
    result_invalid = _run_import_subprocess("abc")
    check("I. valeur invalide -> un warning apparaît (stderr non vide)", result_invalid.stderr.strip() != "")
    check("I. warning mentionne bien RETRAIN_MIN_MATCHES", "RETRAIN_MIN_MATCHES" in result_invalid.stderr)

    result_absent = _run_import_subprocess(None)
    check("I. variable absente -> aucun warning (comportement normal, pas une erreur)", result_absent.stderr.strip() == "")


def test_J_check_training_data_behavior_unchanged_with_explicit_value():
    """Comportement fonctionnel de check_training_data() inchangé quand
    min_matches est fourni explicitement (jamais affecté par le correctif,
    qui ne touche que la résolution de la CONSTANTE par défaut) — réutilise
    exactement le même schéma que test_retraining.py, DB isolée jetable."""
    sys.path.insert(0, str(API_DIR))
    from _test_support import configure_test_env, cleanup_db
    db_path = configure_test_env("test_phase16_5_retrain_min_matches.db")

    from sqlmodel import Session, select
    from app.core.database import engine, init_db
    from app.models.match import Match
    from app.ai.arena import retraining
    from datetime import date, timedelta

    init_db()
    with Session(engine) as session:
        for row in session.exec(select(Match)).all():
            session.delete(row)
        session.commit()
        for i in range(50):
            session.add(Match(league="Ligue1", date=date(2020, 1, 1) + timedelta(days=i),
                               home_team=f"Home{i}", away_team=f"Away{i}",
                               home_goals=1, away_goals=0))
        session.commit()
        readiness_insufficient = retraining.check_training_data(session, min_matches=500)
        check("J. min_matches=500 explicite, 50 matchs -> ready=False (inchangé)", readiness_insufficient.ready is False)

        readiness_sufficient = retraining.check_training_data(session, min_matches=10, min_period_days=10)
        check("J. min_matches=10 explicite, 50 matchs -> ready=True (inchangé)", readiness_sufficient.ready is True)

    cleanup_db(db_path)


if __name__ == "__main__":
    steps = [
        test_A_absent, test_B_empty, test_C_whitespace_only, test_D_abc, test_E_decimal,
        test_F_valid_10, test_G_valid_10_with_spaces, test_H_very_large,
        test_I_invalid_value_warning_logged_not_swallowed_silently,
        test_J_check_training_data_behavior_unchanged_with_explicit_value,
    ]
    for fn in steps:
        print(f"\n=== {fn.__name__} ===")
        try:
            fn()
        except Exception as e:
            _failed += 1
            print(f"  [ERREUR INATTENDUE] {type(e).__name__}: {e}")

    total = _passed + _failed
    print(f"\n{'='*60}\n{_passed}/{total} assertions reussies\n{'='*60}")
    sys.exit(1 if _failed else 0)
