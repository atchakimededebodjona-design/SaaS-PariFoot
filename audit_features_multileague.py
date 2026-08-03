"""
audit_features_multileague.py — Audit indépendant de data/all_leagues_features_xgboost.csv
=============================================================================================

Adaptation multi-ligues de audit_features.py (qui reste la référence mono-ligue,
inchangée, pour data/ligue1_features_xgboost.csv). Mêmes 5 vérifications, mais
chacune est refaite PAR LIGUE plutôt que sur l'ensemble mélangé : puisque
build_features.py calcule Dixon-Coles/forme/repos/h2h isolément par ligue, la
période de rodage, les probabilités et le head-to-head n'ont de sens qu'au
sein d'une même ligue — les vérifier sur le pool global masquerait ou
inventerait des problèmes qui n'existent pas.

Ce script reste délibérément indépendant de build_features.py : il ne réutilise
aucune de ses fonctions, il relit data/all_leagues_raw_with_stats.csv et
recalcule tout à la main pour le point 1 (fuite de données).

5 vérifications, par ligue :
  1. Fuite de données — 2 matchs pris dans 2 ligues différentes, recalcul manuel
  2. dc_home_win + dc_draw + dc_away_win == 1.0, par ligue
  3. Distribution des NaN — rodage attendu PAR LIGUE (une zone de rodage par ligue)
  4. Cohérence de h2h_matches_found, par ligue
  5. Plage de days_since_last_match, par ligue (négatifs/zéros/aberrants)

Rapporte aussi, pour finir : nombre total de lignes du CSV final et nombre de
lignes exploitables (dc_* non-NaN) par ligue.

Usage : python audit_features_multileague.py
"""

import numpy as np
import pandas as pd

RAW_FILE = "data/all_leagues_raw_with_stats.csv"
FEATURES_FILE = "data/all_leagues_features_xgboost.csv"

MIN_TRAIN_MATCHES = 100  # doit rester synchronisé avec build_features.py


# ---------------------------------------------------------------------------
# 1. FUITE DE DONNÉES — recalcul manuel indépendant, 2 matchs, 2 ligues
# ---------------------------------------------------------------------------

def manual_home_form_points_avg(raw_league: pd.DataFrame, team: str, match_date, window: int = 5) -> float:
    """
    Réimplémentation volontairement indépendante de build_features.py :
    relit le sous-ensemble brut D'UNE SEULE LIGUE et recalcule à la main la
    moyenne de points sur les 5 derniers matchs de `team` (domicile ou
    extérieur) à une date STRICTEMENT antérieure à match_date.
    """
    prior = raw_league[(raw_league["date"] < match_date) &
                        ((raw_league["home_team"] == team) | (raw_league["away_team"] == team))]
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


def check_1_leakage(raw: pd.DataFrame, features: pd.DataFrame, seed: int = 0) -> bool:
    print("=" * 80)
    print("1. FUITE DE DONNÉES — home_form_points_avg, 2 matchs pris dans 2 ligues différentes")
    print("=" * 80)

    leagues = sorted(features["league"].unique())
    chosen_leagues = leagues[:2] if len(leagues) >= 2 else leagues

    ok = True
    for league in chosen_leagues:
        raw_league = raw[raw["league"] == league]
        feat_league = features[features["league"] == league]
        candidates = feat_league.dropna(subset=["home_form_points_avg"])
        sample = candidates.sample(n=1, random_state=seed)

        for _, row in sample.iterrows():
            expected = manual_home_form_points_avg(raw_league, row["home_team"], row["date"])
            actual = row["home_form_points_avg"]
            diff = abs(expected - actual)
            status = "OK" if diff < 1e-9 else "ÉCART DÉTECTÉ"
            ok = ok and diff < 1e-9
            print(f"  [{league}] {row['date'].date()}  {row['home_team']:<15} - {row['away_team']:<15} "
                  f"| fichier={actual:.4f}  recalcul_manuel={expected:.4f}  diff={diff:.2e}  [{status}]")

    verdict = "OK — aucun écart détecté sur les 2 ligues échantillonnées." if ok else "PROBLÈME — voir écarts ci-dessus."
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 2. COHÉRENCE DES PROBABILITÉS DIXON-COLES, PAR LIGUE
# ---------------------------------------------------------------------------

def check_2_probability_consistency(features: pd.DataFrame, tol: float = 1e-6) -> bool:
    print("=" * 80)
    print("2. COHÉRENCE DES PROBABILITÉS DIXON-COLES, PAR LIGUE")
    print("=" * 80)

    ok = True
    for league, sub in features.groupby("league", sort=False):
        valid_1x2 = sub.dropna(subset=["dc_home_win", "dc_draw", "dc_away_win"])
        sum_1x2 = valid_1x2["dc_home_win"] + valid_1x2["dc_draw"] + valid_1x2["dc_away_win"]
        bad_1x2 = valid_1x2[(sum_1x2 - 1.0).abs() > tol]

        valid_ou = sub.dropna(subset=["dc_over_2_5", "dc_under_2_5"])
        sum_ou = valid_ou["dc_over_2_5"] + valid_ou["dc_under_2_5"]
        bad_ou = valid_ou[(sum_ou - 1.0).abs() > tol]

        league_ok = len(bad_1x2) == 0 and len(bad_ou) == 0
        ok = ok and league_ok
        print(f"  [{league:15s}] 1X2 : {len(valid_1x2):5d} lignes, {len(bad_1x2)} violation(s) | "
              f"O/U 2.5 : {len(valid_ou):5d} lignes, {len(bad_ou)} violation(s)  [{'OK' if league_ok else 'PROBLÈME'}]")

        if len(bad_1x2):
            print(bad_1x2[["date", "home_team", "away_team", "dc_home_win", "dc_draw", "dc_away_win"]].to_string(index=False))
        if len(bad_ou):
            print(bad_ou[["date", "home_team", "away_team", "dc_over_2_5", "dc_under_2_5"]].to_string(index=False))

    verdict = "OK — toutes les probabilités somment à 1.0 (± 1e-6) dans chaque ligue." if ok else "PROBLÈME — violations listées ci-dessus."
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 3. DISTRIBUTION DES NaN, PAR LIGUE — une zone de rodage attendue par ligue
# ---------------------------------------------------------------------------

def explain_dc_nan(raw_league: pd.DataFrame, home: str, away: str, date, min_train_matches: int) -> str | None:
    """Ré-vérifie SANS dépendre de build_features.py si un NaN dc_* à cette
    ligne est justifié : historique de LA LIGUE insuffisant (rodage) OU l'une
    des deux équipes n'est encore jamais apparue avant cette date DANS CETTE
    LIGUE. Retourne None si aucune de ces deux raisons ne s'applique."""
    prior = raw_league[raw_league["date"] < date]
    if len(prior) < min_train_matches:
        return f"rodage (seulement {len(prior)} matchs d'historique dans la ligue < {min_train_matches})"
    seen = set(prior["home_team"]) | set(prior["away_team"])
    missing = [t for t in (home, away) if t not in seen]
    if missing:
        return f"équipe(s) jamais vue(s) avant cette date dans la ligue : {missing}"
    return None


def explain_form_nan(raw_league: pd.DataFrame, team: str, date) -> str | None:
    """Idem pour une feature home_form_*/away_form_* : justifiée seulement
    si l'équipe n'a strictement aucun match antérieur dans l'historique de sa ligue."""
    prior = raw_league[(raw_league["date"] < date) & ((raw_league["home_team"] == team) | (raw_league["away_team"] == team))]
    if prior.empty:
        return "aucun match antérieur pour cette équipe dans la ligue (première apparition)"
    return None


def check_3_nan_distribution(raw: pd.DataFrame, features: pd.DataFrame,
                              min_train_matches: int = MIN_TRAIN_MATCHES) -> bool:
    print("=" * 80)
    print("3. DISTRIBUTION DES NaN, PAR LIGUE (une zone de rodage attendue par ligue)")
    print("=" * 80)

    dc_cols = ["dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5"]
    form_cols = [c for c in features.columns if c.startswith("home_form_") or c.startswith("away_form_")]

    ok = True
    for league, sub in features.groupby("league", sort=False):
        raw_league = raw[raw["league"] == league]
        sub = sub.sort_values("date")

        dc_nan_mask = sub[dc_cols].isna().any(axis=1)
        n_dc_nan = dc_nan_mask.sum()
        last_dc_nan_idx = sub.index[dc_nan_mask].max() if dc_nan_mask.any() else -1

        unexplained = []
        for i, row in sub[dc_nan_mask].iterrows():
            reason = explain_dc_nan(raw_league, row["home_team"], row["away_team"], row["date"], min_train_matches)
            if reason is None:
                unexplained.append((i, row, "dc_*"))

        form_nan_mask = sub[form_cols].isna().any(axis=1)
        for i, row in sub[form_nan_mask].iterrows():
            bad_form_cols = [c for c in form_cols if pd.isna(row[c])]
            for c in bad_form_cols:
                team = row["home_team"] if c.startswith("home_form_") else row["away_team"]
                reason = explain_form_nan(raw_league, team, row["date"])
                if reason is None:
                    unexplained.append((i, row, c))

        seen_idx = set()
        unexplained_unique = []
        for i, row, col in unexplained:
            if i not in seen_idx:
                unexplained_unique.append((i, row, col))
                seen_idx.add(i)

        league_ok = len(unexplained_unique) == 0
        ok = ok and league_ok
        print(f"  [{league:15s}] total={len(sub):5d}  NaN dc_*={n_dc_nan:4d}  dernier index NaN={last_dc_nan_idx:6d}  "
              f"NaN inexpliqués={len(unexplained_unique)}  [{'OK' if league_ok else 'PROBLÈME'}]")
        if unexplained_unique:
            for i, row, col in unexplained_unique:
                print(f"    index={i} {row['date'].date()} {row['home_team']} vs {row['away_team']} -> colonne {col}")

    verdict = ("OK — dans chaque ligue, tous les NaN sont justifiés (rodage propre à la ligue ou équipe jamais rencontrée avant cette date dans la ligue)."
               if ok else "PROBLÈME — NaN inexpliqués listés ci-dessus.")
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 4. COHÉRENCE DE h2h_matches_found, PAR LIGUE
# ---------------------------------------------------------------------------

def check_4_h2h_consistency(features: pd.DataFrame, top_n: int = 10, h2h_window: int = 5) -> bool:
    print("=" * 80)
    print("4. COHÉRENCE DE h2h_matches_found, PAR LIGUE")
    print("=" * 80)

    ok = True
    for league, sub in features.groupby("league", sort=False):
        sub = sub.sort_values("date").reset_index(drop=True)
        pair_key = sub.apply(lambda r: frozenset((r["home_team"], r["away_team"])), axis=1)
        freq = pair_key.value_counts()
        top_pairs = freq.head(top_n)

        print(f"\n  --- [{league}] Top {min(top_n, len(top_pairs))} affiches les plus fréquentes ---")
        league_ok_recent = True
        for pair, count in top_pairs.items():
            a, b = sorted(pair)
            pair_sub = sub[pair_key == pair]
            last = pair_sub.iloc[-1]
            expected = min(count - 1, h2h_window)
            actual = last["h2h_matches_found"]
            status = "OK" if actual == expected else "ÉCART"
            league_ok_recent = league_ok_recent and (actual == expected)
            print(f"    {a} vs {b:<15} : {count} confrontations | dernier match "
                  f"{last['date'].date()} -> h2h_matches_found={actual} (attendu={expected}) [{status}]")

        seen_pairs = set()
        first_meeting_rows = []
        for _, row in sub.iterrows():
            p = frozenset((row["home_team"], row["away_team"]))
            if p not in seen_pairs:
                first_meeting_rows.append(row)
                seen_pairs.add(p)
        first_df = pd.DataFrame(first_meeting_rows)
        bad_first = first_df[first_df["h2h_matches_found"] != 0]
        print(f"  {len(first_df)} premières confrontations distinctes, {len(bad_first)} violation(s) (h2h_matches_found != 0)")
        if len(bad_first):
            print(bad_first[["date", "home_team", "away_team", "h2h_matches_found"]].to_string(index=False))

        league_ok = league_ok_recent and len(bad_first) == 0
        ok = ok and league_ok
        print(f"  [{league}] verdict local : {'OK' if league_ok else 'PROBLÈME'}")

    verdict = ("OK — h2h_matches_found cohérent dans chaque ligue (affiches fréquentes et premières confrontations)."
               if ok else "PROBLÈME — écarts listés ci-dessus.")
    print(f"\n>>> VERDICT : {verdict}\n")
    return ok


# ---------------------------------------------------------------------------
# 5. PLAGE DE days_since_last_match, PAR LIGUE
# ---------------------------------------------------------------------------

def _known_disruption_window(dates: pd.Series) -> pd.Series:
    """
    Fenêtres calendaires connues où un écart > 45 jours est attendu, au-delà
    de la trêve estivale classique (juillet/août/septembre) :
      - suspension COVID-19 : les 5 championnats ont été interrompus mi-mars
        2020 et n'ont repris qu'en mai/juin 2020 (Bundesliga 16/05, Liga/
        Serie A/Ligue 1 juin — la Ligue 1 n'a d'ailleurs jamais repris, sa
        saison 2019-2020 a été arrêtée définitivement, donc le prochain
        match de chaque équipe française tombe directement à la reprise
        2020-2021).
      - trêve hivernale Coupe du Monde Qatar 2022 : compétitions
        nationales interrompues fin novembre, reprise fin décembre 2022 /
        courant janvier 2023 pour la plupart des championnats européens.
    Retourne un masque booléen (True = dans une fenêtre de rupture connue).
    """
    covid = (dates >= "2020-04-01") & (dates <= "2020-07-15")
    world_cup_2022 = (dates >= "2022-11-20") & (dates <= "2023-01-31")
    return covid | world_cup_2022


def check_5_days_since_last_match(features: pd.DataFrame, high_threshold: int = 45) -> bool:
    print("=" * 80)
    print("5. PLAGE DE days_since_last_match, PAR LIGUE")
    print("=" * 80)

    ok = True
    for league, sub in features.groupby("league", sort=False):
        print(f"\n  === [{league}] ===")
        for col in ["home_days_since_last_match", "away_days_since_last_match"]:
            s = sub[col].dropna()
            print(f"  --- {col} ---")
            print(f"  n={len(s)}  min={s.min():.0f}  max={s.max():.0f}  médiane={s.median():.1f}")

            neg = sub[sub[col] < 0]
            zero = sub[sub[col] == 0]
            # Un écart > high_threshold est attendu autour de la trêve
            # estivale (juillet/août/septembre) OU dans l'une des deux
            # fenêtres de rupture calendaire connues (COVID-19 2020,
            # Coupe du Monde Qatar 2022) — cf. _known_disruption_window.
            high = sub[sub[col] > high_threshold]
            expected_mask = high["date"].dt.month.isin([7, 8, 9]) | _known_disruption_window(high["date"])
            high_expected = high[expected_mask]
            high_aberrant = high[~expected_mask]

            if len(neg):
                ok = False
                print(f"  NÉGATIFS ({len(neg)}) — impossible, à corriger :")
                print(neg[["date", "home_team", "away_team", col]].to_string(index=False))
            if len(zero):
                ok = False
                print(f"  ZÉROS ({len(zero)}) — deux matchs le même jour pour la même équipe, à vérifier :")
                print(zero[["date", "home_team", "away_team", col]].to_string(index=False))
            print(f"  > {high_threshold} jours : {len(high)} au total, dont {len(high_expected)} expliqués "
                  f"(trêve estivale, suspension COVID-19 2020, ou trêve Coupe du Monde 2022)")
            if len(high_aberrant):
                ok = False
                print(f"  ABERRANT — > {high_threshold} jours NON expliqué ({len(high_aberrant)}) :")
                print(high_aberrant[["date", "home_team", "away_team", col]].to_string(index=False))

    print(f"\n>>> VERDICT : {'OK — pas de valeur négative/nulle, et tous les écarts > 45 jours sont expliqués (trêve estivale, COVID-19 2020, ou Coupe du Monde 2022).' if ok else 'PROBLÈME — valeurs listées ci-dessus.'}\n")
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

    print("\n" + "=" * 80)
    print("RAPPORT FINAL — VOLUMÉTRIE")
    print("=" * 80)
    print(f"  Nombre total de lignes dans {FEATURES_FILE} : {len(features)}")
    print("  Lignes exploitables (dc_home_win non-NaN) par ligue :")
    for league, sub in features.groupby("league", sort=False):
        print(f"    {league:15s} total={len(sub):5d}  exploitables={sub['dc_home_win'].notna().sum():5d}")
