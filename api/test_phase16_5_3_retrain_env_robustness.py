"""
test_phase16_5_3_retrain_env_robustness.py — Phase 16.5.3 : non-régression du
correctif de robustesse de RETRAIN_MIN_LEAGUES et RETRAIN_MIN_PERIOD_DAYS
(app/ai/arena/retraining.py::_resolve_min_leagues / _resolve_min_period_days),
même correctif que Phase 16.5.1 (RETRAIN_MIN_MATCHES), appliqué ici aux deux
dernières variables identifiées à risque de startup crash par l'audit
Phase 16.5.2 (RETRAIN_ENV_AUDIT_ADDITIONAL_STARTUP_RISK_FOUND).

Comme ces deux constantes sont résolues UNE SEULE FOIS au chargement du
module, chaque valeur testée nécessite un sous-processus Python isolé et
jetable — jamais un rechargement manuel dans le processus de test courant
(même technique que test_phase16_5_retrain_min_matches_robustness.py).

N'utilise jamais la vraie DATABASE_URL/production, ne modifie jamais Railway
ni .env.

Usage : python api/test_phase16_5_3_retrain_env_robustness.py
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


def _run_import_subprocess(var_name: str, value: str | None) -> subprocess.CompletedProcess:
    """Sous-processus Python isolé et jetable : importe app.ai.arena.retraining
    et affiche la valeur résolue de `var_name` sur stdout. `value=None` retire
    complètement la variable (cas "absente")."""
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)  # jamais la vraie DB — retombe sur le défaut SQLite local, jamais ouvert
    env.pop("JWT_SECRET_KEY", None)
    if value is None:
        env.pop(var_name, None)
    else:
        env[var_name] = value
    code = f"import app.ai.arena.retraining as r\nprint(getattr(r, {var_name!r}))\n"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(API_DIR), env=env, capture_output=True, text=True, timeout=30,
    )


def _check_value(label: str, var_name: str, env_value, expected_output: str) -> None:
    result = _run_import_subprocess(var_name, env_value)
    check(f"{label} : import réussi (exit code 0)", result.returncode == 0)
    check(f"{label} : {var_name} == {expected_output}", result.stdout.strip() == expected_output)
    if result.returncode != 0:
        print(f"    stderr: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '(vide)'}")


# ---------------------------------------------------------------------------
# RETRAIN_MIN_LEAGUES — A à G
# ---------------------------------------------------------------------------

def test_A_leagues_absent():
    _check_value("A. RETRAIN_MIN_LEAGUES absente", "RETRAIN_MIN_LEAGUES", None, "1")


def test_B_leagues_empty():
    _check_value("B. RETRAIN_MIN_LEAGUES=\"\"", "RETRAIN_MIN_LEAGUES", "", "1")


def test_C_leagues_whitespace():
    _check_value("C. RETRAIN_MIN_LEAGUES=\" \"", "RETRAIN_MIN_LEAGUES", " ", "1")


def test_D_leagues_abc():
    _check_value("D. RETRAIN_MIN_LEAGUES=\"abc\"", "RETRAIN_MIN_LEAGUES", "abc", "1")


def test_E_leagues_decimal():
    _check_value("E. RETRAIN_MIN_LEAGUES=\"12.5\"", "RETRAIN_MIN_LEAGUES", "12.5", "1")


def test_F_leagues_valid_10():
    _check_value("F. RETRAIN_MIN_LEAGUES=\"10\"", "RETRAIN_MIN_LEAGUES", "10", "10")


def test_G_leagues_valid_10_spaces():
    _check_value("G. RETRAIN_MIN_LEAGUES=\" 10 \"", "RETRAIN_MIN_LEAGUES", " 10 ", "10")


# ---------------------------------------------------------------------------
# RETRAIN_MIN_PERIOD_DAYS — H à N
# ---------------------------------------------------------------------------

def test_H_period_absent():
    _check_value("H. RETRAIN_MIN_PERIOD_DAYS absente", "RETRAIN_MIN_PERIOD_DAYS", None, "90")


def test_I_period_empty():
    _check_value("I. RETRAIN_MIN_PERIOD_DAYS=\"\"", "RETRAIN_MIN_PERIOD_DAYS", "", "90")


def test_J_period_whitespace():
    _check_value("J. RETRAIN_MIN_PERIOD_DAYS=\" \"", "RETRAIN_MIN_PERIOD_DAYS", " ", "90")


def test_K_period_abc():
    _check_value("K. RETRAIN_MIN_PERIOD_DAYS=\"abc\"", "RETRAIN_MIN_PERIOD_DAYS", "abc", "90")


def test_L_period_decimal():
    _check_value("L. RETRAIN_MIN_PERIOD_DAYS=\"12.5\"", "RETRAIN_MIN_PERIOD_DAYS", "12.5", "90")


def test_M_period_valid_10():
    _check_value("M. RETRAIN_MIN_PERIOD_DAYS=\"10\"", "RETRAIN_MIN_PERIOD_DAYS", "10", "10")


def test_N_period_valid_10_spaces():
    _check_value("N. RETRAIN_MIN_PERIOD_DAYS=\" 10 \"", "RETRAIN_MIN_PERIOD_DAYS", " 10 ", "10")


# ---------------------------------------------------------------------------
# O — 0 et -1 conservés tels quels (aucune validation de plage introduite)
# ---------------------------------------------------------------------------

def test_O_zero_and_negative_preserved_as_valid_integers():
    for var_name, default in (("RETRAIN_MIN_LEAGUES", "1"), ("RETRAIN_MIN_PERIOD_DAYS", "90")):
        _check_value(f"O. {var_name}=\"0\" -> conservé tel quel (pas de fallback)", var_name, "0", "0")
        _check_value(f"O. {var_name}=\"-1\" -> conservé tel quel (pas de fallback)", var_name, "-1", "-1")
    # 999999999 — également requis explicitement par le prompt (grande valeur valide)
    _check_value("O. RETRAIN_MIN_LEAGUES=\"999999999\"", "RETRAIN_MIN_LEAGUES", "999999999", "999999999")
    _check_value("O. RETRAIN_MIN_PERIOD_DAYS=\"999999999\"", "RETRAIN_MIN_PERIOD_DAYS", "999999999", "999999999")


# ---------------------------------------------------------------------------
# P/Q — présence/absence du warning
# ---------------------------------------------------------------------------

def test_P_warning_present_for_invalid_values():
    for var_name in ("RETRAIN_MIN_LEAGUES", "RETRAIN_MIN_PERIOD_DAYS"):
        result = _run_import_subprocess(var_name, "abc")
        check(f"P. {var_name} invalide -> warning présent (stderr non vide)", result.stderr.strip() != "")
        check(f"P. warning mentionne {var_name}", var_name in result.stderr)


def test_Q_no_warning_when_absent():
    for var_name in ("RETRAIN_MIN_LEAGUES", "RETRAIN_MIN_PERIOD_DAYS"):
        result = _run_import_subprocess(var_name, None)
        check(f"Q. {var_name} absente -> aucun warning", result.stderr.strip() == "")


# ---------------------------------------------------------------------------
# R — import réussi (déjà couvert par le exit code 0 de chaque _check_value
# ci-dessus, ex. B/C/D/E/I/J/K/L) ; résumé explicite ici pour lisibilité.
# ---------------------------------------------------------------------------

def test_R_import_never_crashes_on_any_invalid_value():
    for var_name in ("RETRAIN_MIN_LEAGUES", "RETRAIN_MIN_PERIOD_DAYS"):
        for bad_value in ("", " ", "abc", "12.5"):
            result = _run_import_subprocess(var_name, bad_value)
            check(f"R. import réussi pour {var_name}={bad_value!r}", result.returncode == 0)


# ---------------------------------------------------------------------------
# S — check_training_data() fonctionne toujours avec min_leagues/min_period_days explicites
# ---------------------------------------------------------------------------

def test_S_check_training_data_unchanged_with_explicit_values():
    sys.path.insert(0, str(API_DIR))
    from _test_support import configure_test_env, cleanup_db
    db_path = configure_test_env("test_phase16_5_3_retrain_env_robustness.db")

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

        readiness_not_enough_leagues = retraining.check_training_data(
            session, min_matches=1, min_leagues=2, min_period_days=1)
        check("S. min_leagues=2 explicite, 1 ligue seulement -> ready=False (inchangé)",
              readiness_not_enough_leagues.ready is False)

        readiness_ok = retraining.check_training_data(
            session, min_matches=1, min_leagues=1, min_period_days=10)
        check("S. min_leagues=1, min_period_days=10 explicites -> ready=True (inchangé)",
              readiness_ok.ready is True)

    cleanup_db(db_path)


if __name__ == "__main__":
    steps = [
        test_A_leagues_absent, test_B_leagues_empty, test_C_leagues_whitespace,
        test_D_leagues_abc, test_E_leagues_decimal, test_F_leagues_valid_10, test_G_leagues_valid_10_spaces,
        test_H_period_absent, test_I_period_empty, test_J_period_whitespace,
        test_K_period_abc, test_L_period_decimal, test_M_period_valid_10, test_N_period_valid_10_spaces,
        test_O_zero_and_negative_preserved_as_valid_integers,
        test_P_warning_present_for_invalid_values, test_Q_no_warning_when_absent,
        test_R_import_never_crashes_on_any_invalid_value,
        test_S_check_training_data_unchanged_with_explicit_values,
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
