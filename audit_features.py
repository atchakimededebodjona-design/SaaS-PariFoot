"""
audit_features.py — Audit indépendant de data/ligue1_features_xgboost.csv
============================================================================

Objectif : valider AVANT tout entraînement XGBoost que les features générées
par build_features.py n'ont pas de fuite de données (data leakage) et sont
cohérentes. Ce script est délibérément indépendant de build_features.py —
il ne réutilise AUCUNE de ses fonctions, il relit data/ligue1_raw_with_stats.csv
et recalcule tout à la main pour le point 1 (fuite de données), afin qu'un
bug identique dans les deux fichiers ne puisse pas se masquer lui-même.

5 vérifications, dans l'ordre demandé :
  1. Fuite de données sur home_form_points_avg (le point le plus critique)
  2. Cohérence des probabilités Dixon-Coles (somme = 1.0)
  3. Distribution des NaN (rodage attendu vs NaN inexpliqué)
  4. Cohérence de h2h_matches_found
  5. Plage de home/away_days_since_last_match

Usage : python audit_features.py
"""

import numpy as np
import pandas as pd

RAW_FILE = "data/ligue1_raw_with_stats.csv"
FEATURES_FILE = "data/ligue1_features_xgboost.csv"

MIN_TRAIN_MATCHES = 100  # doit rester synchronisé avec build_features.py — voir point 3


# ---------------------------------------------------------------------------
# 1. FUITE DE DONNÉES — recalcul manuel indépendant
# ---------------------------------------------------------------------------

def manual_home_form_points_avg(raw: pd.DataFrame, team: str, match_date, window: int = 5) -> float:
    """
    Réimplémentation volontairement indépendante de build_features.py :
    relit le CSV brut et recalcule à la main la moyenne de points sur les
    5 derniers matchs de `team` (domicile ou extérieur) à une date
    STRICTEMENT antérieure à match_date.
    """
    prior = raw[(raw["date"] < match_date) &
                ((raw["home_team"] == team) | (raw["away_team"] == team))]
    prior = prior.sort_values("date").tail(window)
    if prior.empty:
        return np.nan

    points = []
    for _, m in prior.iterrows():
        if m["home_team"] == team:
            gf, ga = m["home_goals"], m["away_goals"]
        else:
            gf, ga = m["away_goals"], m["home_goals"]
        points.append(3 if gf > ga else (1 if gf == ga else 0))
    return float(np.mean(points))


def check_1_leakage(raw: pd.DataFrame, features: pd.DataFrame, n_samples: int = 5) -> bool:
    print("=" * 80)
    print("1. FUITE DE DONNÉES — home_form_points_avg (recalcul manuel indépendant)")
    print("=" * 80)

    candidates = features.dropna(subset=["home_form_points_avg"])
    sample = candidates.sample(n=n_samples)

    ok = True
    for _, row in sample.iterrows():
        expected = manual_home_form_points_avg(raw, row["home_team"], row["date"])
        actual = row["home_form_points_avg"]
        diff = abs(expected - actual)
        status = "OK" if diff < 1e-9 else "ÉCART DÉTECTÉ"
        ok = ok and diff < 1e-9
        print(f"  {row['date'].date()}  {row['home_team']:<12} - {row['away_team']:<12} "
              f"| fichier={actual:.4f}  recalcul_manuel={expected:.4f}  diff={diff:.2e}  [{status}]")

    verdict = "OK — aucun écart détecté sur l'échantillon." if ok else "PROBLÈME — voir écarts ci-dessus."
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 2. COHÉRENCE DES PROBABILITÉS DIXON-COLES
# ---------------------------------------------------------------------------

def check_2_probability_consistency(features: pd.DataFrame, tol: float = 1e-6) -> bool:
    print("=" * 80)
    print("2. COHÉRENCE DES PROBABILITÉS DIXON-COLES")
    print("=" * 80)

    valid_1x2 = features.dropna(subset=["dc_home_win", "dc_draw", "dc_away_win"])
    sum_1x2 = valid_1x2["dc_home_win"] + valid_1x2["dc_draw"] + valid_1x2["dc_away_win"]
    bad_1x2 = valid_1x2[(sum_1x2 - 1.0).abs() > tol]

    valid_ou = features.dropna(subset=["dc_over_2_5", "dc_under_2_5"])
    sum_ou = valid_ou["dc_over_2_5"] + valid_ou["dc_under_2_5"]
    bad_ou = valid_ou[(sum_ou - 1.0).abs() > tol]

    print(f"  1X2   : {len(valid_1x2)} lignes vérifiées, {len(bad_1x2)} violation(s) (> {tol})")
    print(f"  O/U 2.5 : {len(valid_ou)} lignes vérifiées, {len(bad_ou)} violation(s) (> {tol})")

    if len(bad_1x2):
        print("\n  Violations 1X2 :")
        print(bad_1x2[["date", "home_team", "away_team", "dc_home_win", "dc_draw", "dc_away_win"]]
              .to_string(index=False))
    if len(bad_ou):
        print("\n  Violations O/U 2.5 :")
        print(bad_ou[["date", "home_team", "away_team", "dc_over_2_5", "dc_under_2_5"]].to_string(index=False))

    ok = len(bad_1x2) == 0 and len(bad_ou) == 0
    verdict = "OK — toutes les probabilités somment à 1.0 (± 1e-6)." if ok else "PROBLÈME — violations listées ci-dessus."
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 3. DISTRIBUTION DES NaN — rodage attendu vs NaN inexpliqué
# ---------------------------------------------------------------------------

def explain_dc_nan(raw: pd.DataFrame, home: str, away: str, date, min_train_matches: int) -> str | None:
    """Ré-vérifie SANS dépendre de build_features.py si un NaN dc_* à cette
    ligne est justifié : historique global insuffisant (rodage) OU l'une des
    deux équipes n'est encore jamais apparue avant cette date. Retourne None
    si aucune de ces deux raisons ne s'applique (NaN potentiellement un bug)."""
    prior = raw[raw["date"] < date]
    if len(prior) < min_train_matches:
        return f"rodage (seulement {len(prior)} matchs d'historique < {min_train_matches})"
    seen = set(prior["home_team"]) | set(prior["away_team"])
    missing = [t for t in (home, away) if t not in seen]
    if missing:
        return f"équipe(s) jamais vue(s) avant cette date : {missing}"
    return None


def explain_form_nan(raw: pd.DataFrame, team: str, date) -> str | None:
    """Idem pour une feature home_form_*/away_form_* : justifiée seulement
    si l'équipe n'a strictement aucun match antérieur dans l'historique."""
    prior = raw[(raw["date"] < date) & ((raw["home_team"] == team) | (raw["away_team"] == team))]
    if prior.empty:
        return "aucun match antérieur pour cette équipe (première apparition)"
    return None


def check_3_nan_distribution(raw: pd.DataFrame, features: pd.DataFrame,
                              min_train_matches: int = MIN_TRAIN_MATCHES) -> bool:
    print("=" * 80)
    print("3. DISTRIBUTION DES NaN")
    print("=" * 80)

    dc_cols = ["dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5"]
    form_cols = [c for c in features.columns if c.startswith("home_form_") or c.startswith("away_form_")]

    dc_nan_mask = features[dc_cols].isna().any(axis=1)
    last_dc_nan_idx = features.index[dc_nan_mask].max() if dc_nan_mask.any() else -1
    print(f"  Dernier index avec un NaN dc_* : {last_dc_nan_idx} (sur {len(features)} lignes, "
          f"{dc_nan_mask.sum()} lignes NaN au total)")

    unexplained = []
    for i, row in features[dc_nan_mask].iterrows():
        reason = explain_dc_nan(raw, row["home_team"], row["away_team"], row["date"], min_train_matches)
        if reason is None:
            unexplained.append((i, row, "dc_*"))

    form_nan_mask = features[form_cols].isna().any(axis=1)
    for i, row in features[form_nan_mask].iterrows():
        bad_form_cols = [c for c in form_cols if pd.isna(row[c])]
        # une feature home_form_* est justifiée seulement par l'historique de HOME,
        # une away_form_* par l'historique de AWAY
        for c in bad_form_cols:
            team = row["home_team"] if c.startswith("home_form_") else row["away_team"]
            reason = explain_form_nan(raw, team, row["date"])
            if reason is None:
                unexplained.append((i, row, c))

    # dédoublonne par index de ligne
    seen_idx = set()
    unexplained_unique = []
    for i, row, col in unexplained:
        if i not in seen_idx:
            unexplained_unique.append((i, row, col))
            seen_idx.add(i)

    rodage_cutoff = features.loc[
        [i for i, row in features[dc_nan_mask].iterrows()
         if explain_dc_nan(raw, row["home_team"], row["away_team"], row["date"], min_train_matches)
         and explain_dc_nan(raw, row["home_team"], row["away_team"], row["date"], min_train_matches).startswith("rodage")]
    ].index.max() if dc_nan_mask.any() else -1

    print(f"  Dernier index en 'rodage' pur (historique global < {min_train_matches} matchs) : {rodage_cutoff}")
    print(f"  NaN dc_*/form_* AU-DELÀ de ce rodage mais expliqués par une équipe jamais vue : "
          f"{sum(1 for i, r, c in [(i, row, 'dc_*') for i, row in features[dc_nan_mask].iterrows()] if i > rodage_cutoff)}")

    print(f"\n  Lignes avec un NaN INEXPLIQUÉ (ni rodage, ni équipe absente de l'historique) : "
          f"{len(unexplained_unique)}")
    if unexplained_unique:
        for i, row, col in unexplained_unique:
            print(f"    index={i} {row['date'].date()} {row['home_team']} vs {row['away_team']} -> colonne {col}")

    ok = len(unexplained_unique) == 0
    verdict = ("OK — tous les NaN sont justifiés (rodage ou équipe jamais rencontrée avant cette date)."
               if ok else "PROBLÈME — NaN inexpliqués listés ci-dessus.")
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 4. COHÉRENCE DE h2h_matches_found
# ---------------------------------------------------------------------------

def check_4_h2h_consistency(features: pd.DataFrame, top_n: int = 10, h2h_window: int = 5) -> bool:
    print("=" * 80)
    print("4. COHÉRENCE DE h2h_matches_found")
    print("=" * 80)

    features = features.sort_values("date").reset_index(drop=True)
    pair_key = features.apply(lambda r: frozenset((r["home_team"], r["away_team"])), axis=1)
    freq = pair_key.value_counts()
    top_pairs = freq.head(top_n)

    print(f"  Top {top_n} affiches les plus fréquentes du dataset :")
    ok_recent = True
    for pair, count in top_pairs.items():
        a, b = sorted(pair)
        sub = features[pair_key == pair]
        last = sub.iloc[-1]
        expected = min(count - 1, h2h_window)  # count-1 = confrontations strictement avant ce dernier match
        actual = last["h2h_matches_found"]
        status = "OK" if actual == expected else "ÉCART"
        ok_recent = ok_recent and (actual == expected)
        print(f"    {a} vs {b:<12} : {count} confrontations au total | dernier match "
              f"{last['date'].date()} -> h2h_matches_found={actual} (attendu={expected}) [{status}]")

    print(f"\n  Premières confrontations jamais enregistrées (doivent avoir h2h_matches_found = 0) :")
    seen_pairs = set()
    first_meeting_rows = []
    for _, row in features.iterrows():
        p = frozenset((row["home_team"], row["away_team"]))
        if p not in seen_pairs:
            first_meeting_rows.append(row)
            seen_pairs.add(p)
    first_df = pd.DataFrame(first_meeting_rows)
    bad_first = first_df[first_df["h2h_matches_found"] != 0]
    print(f"    {len(first_df)} premières confrontations distinctes trouvées, "
          f"{len(bad_first)} violation(s) (h2h_matches_found != 0)")
    if len(bad_first):
        print(bad_first[["date", "home_team", "away_team", "h2h_matches_found"]].to_string(index=False))

    ok = ok_recent and len(bad_first) == 0
    verdict = ("OK — h2h_matches_found cohérent sur les affiches fréquentes et les premières confrontations."
               if ok else "PROBLÈME — écarts listés ci-dessus.")
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 5. PLAGE DE days_since_last_match
# ---------------------------------------------------------------------------

def check_5_days_since_last_match(features: pd.DataFrame, high_threshold: int = 45) -> bool:
    print("=" * 80)
    print("5. PLAGE DE days_since_last_match")
    print("=" * 80)

    ok = True
    for col in ["home_days_since_last_match", "away_days_since_last_match"]:
        s = features[col].dropna()
        print(f"\n  --- {col} ---")
        print(f"  n={len(s)}  min={s.min():.0f}  max={s.max():.0f}  médiane={s.median():.1f}  "
              f"q1%={s.quantile(0.01):.1f}  q99%={s.quantile(0.99):.1f}")

        neg = features[features[col] < 0]
        zero = features[features[col] == 0]
        high = features[features[col] > high_threshold]

        if len(neg):
            ok = False
            print(f"  NÉGATIFS ({len(neg)}) — impossible, à corriger :")
            print(neg[["date", "home_team", "away_team", col]].to_string(index=False))
        if len(zero):
            ok = False
            print(f"  ZÉROS ({len(zero)}) — deux matchs le même jour pour la même équipe, à vérifier :")
            print(zero[["date", "home_team", "away_team", col]].to_string(index=False))
        if len(high):
            print(f"  > {high_threshold} jours ({len(high)}) :")
            print(high[["date", "home_team", "away_team", col]].head(10).to_string(index=False))
            if len(high) > 10:
                print(f"    ... et {len(high) - 10} de plus")

    print(f"\n>>> VERDICT : {'OK — pas de valeur négative ni nulle.' if ok else 'PROBLÈME — valeurs négatives ou nulles listées ci-dessus.'}")
    print("    (Les valeurs > 45 jours sont attendues sur ce dataset : voir note dans le rapport —")
    print("     échantillon d'environ un match par mois, pas le calendrier complet Ligue 1.)\n")
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raw = pd.read_csv(RAW_FILE, parse_dates=["date"])
    features = pd.read_csv(FEATURES_FILE, parse_dates=["date"])

    results = {
        "1_fuite_donnees": check_1_leakage(raw, features),
        "2_probabilites_dc": check_2_probability_consistency(features),
        "3_distribution_nan": check_3_nan_distribution(raw, features),
        "4_h2h_matches_found": check_4_h2h_consistency(features),
        "5_days_since_last_match": check_5_days_since_last_match(features),
    }

    print("=" * 80)
    print("RÉSUMÉ")
    print("=" * 80)
    for name, ok in results.items():
        print(f"  {name:<28} : {'OK' if ok else 'PROBLÈME DÉTECTÉ'}")
