"""
test_phase16_1_b_retry.py - Tests unitaires Phase 16.1-B.
"""
import io, json, logging, shutil, sys, tempfile
from pathlib import Path
from unittest.mock import patch
import pandas as pd

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import update_raw_data
from update_raw_data import (
    download_direct_season, download_league_season,
    COLUMN_MAP, MAX_DOWNLOAD_ATTEMPTS, RETRY_DELAYS_S, _is_temporary_error,
)
import refresh_and_retrain

NOOP_SLEEP = lambda _: None

def _valid_raw_df():
    return pd.DataFrame({col: [] for col in COLUMN_MAP.keys()})

def _http_exc(code, reason=""):
    return Exception(f"HTTP Error {code}: {reason or str(code)}")

def _timeout_exc():
    return Exception("urlopen error timed out")

def _non_temp_exc():
    return Exception("SSL: CERTIFICATE_VERIFY_FAILED")

def _make_minimal_raw_csv(path):
    df = pd.DataFrame({
        "date": pd.Series([], dtype="datetime64[ns]"),
        "home_team": pd.Series([], dtype=str),
        "away_team": pd.Series([], dtype=str),
        "home_goals": pd.Series([], dtype=float),
        "away_goals": pd.Series([], dtype=float),
        "home_shots": pd.Series([], dtype=float),
        "away_shots": pd.Series([], dtype=float),
        "home_shots_target": pd.Series([], dtype=float),
        "away_shots_target": pd.Series([], dtype=float),
        "home_corners": pd.Series([], dtype=float),
        "away_corners": pd.Series([], dtype=float),
        "league": pd.Series([], dtype=str),
    })
    df.to_csv(path, index=False)

def _make_selective_503(local_csv_path):
    _real = pd.read_csv
    def _mock(p, **kw):
        if str(p).startswith("http"):
            raise Exception("HTTP Error 503: Service Temporarily Unavailable")
        return _real(p, **kw)
    return _mock

# ---- Tests helpers ----------------------------------------------------------
def test_constants():
    print("=== Constantes retry ===")
    assert MAX_DOWNLOAD_ATTEMPTS == 3
    assert len(RETRY_DELAYS_S) == MAX_DOWNLOAD_ATTEMPTS - 1
    assert all(d > 0 for d in RETRY_DELAYS_S)
    print(f"  OK MAX_DOWNLOAD_ATTEMPTS={MAX_DOWNLOAD_ATTEMPTS} RETRY_DELAYS_S={RETRY_DELAYS_S}")

def test_is_temporary_error_coverage():
    print("=== _is_temporary_error() ===")
    assert _is_temporary_error("HTTP Error 503: Service Unavailable")
    assert _is_temporary_error("HTTP Error 502: Bad Gateway")
    assert _is_temporary_error("HTTP Error 504: Gateway Timeout")
    assert _is_temporary_error("HTTP Error 429: Too Many Requests")
    assert _is_temporary_error("urlopen error timed out")
    assert _is_temporary_error("Read timed out.")
    assert _is_temporary_error("Connection reset by peer")
    assert not _is_temporary_error("HTTP Error 404: Not Found")
    assert not _is_temporary_error("HTTP Error 400: Bad Request")
    assert not _is_temporary_error("SSL: CERTIFICATE_VERIFY_FAILED")
    print("  OK")

# ---- T1 : 503 -> succes au 2e essai ----------------------------------------
def test_01_503_then_success():
    print("=== T1 : 503 -> succes 2e tentative ===")
    valid = _valid_raw_df()
    sleep_calls = []
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(503), valid]) as m:
        result = download_direct_season("P1", "2526", _sleep_fn=lambda s: sleep_calls.append(s))
    assert result is not None
    assert m.call_count == 2
    assert sleep_calls == [RETRY_DELAYS_S[0]]
    print(f"  OK appels={m.call_count} sleeps={sleep_calls}")

# ---- T2 : 503 -> 503 -> succes au 3e essai ---------------------------------
def test_02_503_503_then_success():
    print("=== T2 : 503->503->succes 3e tentative ===")
    valid = _valid_raw_df()
    sleep_calls = []
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(503), _http_exc(503), valid]) as m:
        result = download_direct_season("P1", "2526", _sleep_fn=lambda s: sleep_calls.append(s))
    assert result is not None
    assert m.call_count == 3
    assert sleep_calls == RETRY_DELAYS_S
    print(f"  OK appels={m.call_count} sleeps={sleep_calls}")

# ---- T3 : 503 x3 -> RuntimeError -------------------------------------------
def test_03_503_503_503_exhausted():
    print("=== T3 : 503 x3 -> epuisement ===")
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(503)]*3) as m:
        try:
            download_direct_season("P1", "2526", _sleep_fn=NOOP_SLEEP)
            assert False, "RuntimeError attendue"
        except RuntimeError as exc:
            assert m.call_count == MAX_DOWNLOAD_ATTEMPTS
            print(f"  OK RuntimeError apres {m.call_count} tentatives: {exc}")

# ---- T4 : 404 -> None, aucun retry ----------------------------------------
def test_04_404_no_retry():
    print("=== T4 : 404 -> None, pas de retry ===")
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(404, "Not Found")]) as m:
        result = download_direct_season("P1", "2526", _sleep_fn=NOOP_SLEEP)
    assert result is None
    assert m.call_count == 1, f"404 ne doit provoquer aucun retry, obtenu {m.call_count} appel(s)"
    print(f"  OK None retourne, 1 seul appel")

# ---- T4b : 404 GitHub -> None, aucun retry ---------------------------------
def test_04b_404_github_no_retry():
    print("=== T4b : 404 miroir GitHub -> None ===")
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(404, "Not Found")]) as m:
        result = download_league_season("ligue-1", "2627", _sleep_fn=NOOP_SLEEP)
    assert result is None
    assert m.call_count == 1
    print(f"  OK")

# ---- T5 : 429 -> retry -----------------------------------------------------
def test_05_429_retried():
    print("=== T5 : 429 -> retry ===")
    valid = _valid_raw_df()
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(429, "Too Many Requests"), valid]) as m:
        result = download_direct_season("P1", "2526", _sleep_fn=NOOP_SLEEP)
    assert result is not None
    assert m.call_count == 2
    print(f"  OK 429 retrye")

# ---- T6 : 502 -> retry -----------------------------------------------------
def test_06_502_retried():
    print("=== T6 : 502 -> retry ===")
    valid = _valid_raw_df()
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(502, "Bad Gateway"), valid]) as m:
        result = download_league_season("ligue-1", "2526", _sleep_fn=NOOP_SLEEP)
    assert result is not None
    assert m.call_count == 2
    print(f"  OK 502 retrye")

# ---- T7 : 504 -> retry -----------------------------------------------------
def test_07_504_retried():
    print("=== T7 : 504 -> retry ===")
    valid = _valid_raw_df()
    with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(504, "Gateway Timeout"), valid]) as m:
        result = download_league_season("ligue-1", "2526", _sleep_fn=NOOP_SLEEP)
    assert result is not None
    assert m.call_count == 2
    print(f"  OK 504 retrye")

# ---- T8 : timeout -> retry -------------------------------------------------
def test_08_timeout_retried():
    print("=== T8 : timeout reseau -> retry ===")
    valid = _valid_raw_df()
    with patch("update_raw_data.pd.read_csv", side_effect=[_timeout_exc(), valid]) as m:
        result = download_direct_season("P1", "2526", _sleep_fn=NOOP_SLEEP)
    assert result is not None
    assert m.call_count == 2
    print(f"  OK timeout retrye")

# ---- T9 : erreur non temporaire -> pas de retry ----------------------------
def test_09_non_temporary_no_retry():
    print("=== T9 : erreur non temporaire -> 1 seul appel ===")
    with patch("update_raw_data.pd.read_csv", side_effect=[_non_temp_exc()]) as m:
        try:
            download_direct_season("P1", "2526", _sleep_fn=NOOP_SLEEP)
            assert False, "RuntimeError attendue"
        except RuntimeError:
            pass
    assert m.call_count == 1, f"Attendu 1 appel, obtenu {m.call_count}"
    print(f"  OK RuntimeError immediate, 1 seul appel")

# ---- T10 : fail-closed -> artefacts intacts --------------------------------
def test_10_exhausted_retries_artifacts_untouched():
    print("=== T10 : epuisement retries -> artefacts intacts ===")
    fake_artifact = {
        "league": "PrimeiraLiga", "teams": ["Benfica", "Porto"],
        "home_advantage": 0.18, "rho": -0.05,
        "trained_on_matches": 950, "data_up_to": "2026-05-20",
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        artifacts_dir = tmpdir / "model_artifacts"
        artifacts_dir.mkdir()
        artifact_path = artifacts_dir / "PrimeiraLiga.json"
        artifact_path.write_text(json.dumps(fake_artifact), encoding="utf-8")
        before_bytes = artifact_path.read_bytes()
        raw_csv = tmpdir / "raw.csv"
        _make_minimal_raw_csv(raw_csv)
        with patch("update_raw_data.time.sleep"), \
             patch("update_raw_data.pd.read_csv", side_effect=_make_selective_503(raw_csv)):
            exit_code = refresh_and_retrain.run(
                raw_file=str(raw_csv), artifacts_dir=artifacts_dir, skip_refresh=False,
            )
        assert exit_code == 1, f"Attendu exit_code=1, obtenu {exit_code}"
        assert artifact_path.read_bytes() == before_bytes, "Artefact modifie malgre l'echec!"
        print(f"  OK exit_code={exit_code}, artefact byte-identique")

# ---- T11 : echec -> CSV brut non modifie -----------------------------------
def test_11_no_partial_csv_write():
    print("=== T11 : echec telechargemnt -> CSV brut inchange ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        raw_csv = tmpdir / "raw.csv"
        _make_minimal_raw_csv(raw_csv)
        original_bytes = raw_csv.read_bytes()
        with patch("update_raw_data.time.sleep"), \
             patch("update_raw_data.pd.read_csv", side_effect=_make_selective_503(raw_csv)):
            try:
                update_raw_data.update_raw_data_file(str(raw_csv))
            except Exception:
                pass
        assert raw_csv.read_bytes() == original_bytes, "CSV modifie malgre l'echec!"
        print(f"  OK CSV byte-identique avant/apres l'echec")

# ---- T12 : retry journalise en WARNING -------------------------------------
def test_12_retry_logged():
    print("=== T12 : retry journalise en WARNING ===")
    valid = _valid_raw_df()
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.WARNING)
    target = logging.getLogger("update_raw_data")
    target.addHandler(handler)
    orig_level = target.level
    target.setLevel(logging.DEBUG)
    try:
        with patch("update_raw_data.pd.read_csv", side_effect=[_http_exc(503, "Service Unavailable"), valid]):
            download_direct_season("P1", "2526", _sleep_fn=NOOP_SLEEP)
    finally:
        target.removeHandler(handler)
        target.setLevel(orig_level)
    log_output = log_capture.getvalue()
    assert log_output.strip(), "Aucun WARNING capture"
    assert "Tentative" in log_output or "retry" in log_output.lower(), f"Log manquant: {log_output!r}"
    assert "503" in log_output, f"Code HTTP absent: {log_output!r}"
    print(f"  OK WARNING capture: {log_output.strip()!r}")

# ---- Regression : tests existants ------------------------------------------
def test_regression_existing():
    print("=== Regression : test_refresh_and_retrain.py ===")
    import test_refresh_and_retrain as _t
    _t.test_corrupted_csv_does_not_touch_artifacts()
    _t.test_partial_validation_failure_keeps_only_failing_league()
    print("  OK tests existants toujours verts")

# ---- Runner ----------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        test_constants,
        test_is_temporary_error_coverage,
        test_01_503_then_success,
        test_02_503_503_then_success,
        test_03_503_503_503_exhausted,
        test_04_404_no_retry,
        test_04b_404_github_no_retry,
        test_05_429_retried,
        test_06_502_retried,
        test_07_504_retried,
        test_08_timeout_retried,
        test_09_non_temporary_no_retry,
        test_10_exhausted_retries_artifacts_untouched,
        test_11_no_partial_csv_write,
        test_12_retry_logged,
        test_regression_existing,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            print(f"  X ECHEC {t.__name__}: {exc}")
            import traceback; traceback.print_exc()
            failed += 1
        print()
    print("=" * 70)
    print(f"RESULTATS: {passed} reussi(s) / {failed} echoue(s) / {len(tests)} total")
    print("RETRY_IMPLEMENTED_TESTS_PASS" if failed == 0 else "ATTENTION: echecs detectes")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)
