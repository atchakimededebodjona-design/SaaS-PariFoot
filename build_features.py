"""
build_features.py — Construction des features pour l'entraînement XGBoost (multi-ligues)
==========================================================================================

Source : data/all_leagues_raw_with_stats.csv (10 707 matchs, 2019-2025,
         5 championnats : Ligue1, PremierLeague, LaLiga, Bundesliga, SerieA,
         colonne `league`).
Sortie : data/all_leagues_features_xgboost.csv

ISOLATION PAR LIGUE : Dixon-Coles, la forme récente, les jours de repos et le
head-to-head sont des statistiques dérivées de l'historique d'UNE équipe (ou
d'une paire d'équipes). Or une même équipe ne joue jamais dans deux
championnats différents la même saison (vérifié — aucune collision équipe/
ligue trouvée sur ce dataset), donc mélanger les ligues n'apporterait aucune
information utile mais risquerait au contraire de faire fuiter des
statistiques d'un championnat vers un autre (calendriers différents,
niveaux différents). Toutes les fonctions de features ci-dessous sont donc
appliquées séparément à chaque sous-ensemble `df[df.league == L]`, trié par
date au sein de cette ligue uniquement, puis les résultats sont recollés sur
l'index d'origine. `MIN_TRAIN_MATCHES` (le seuil de rodage Dixon-Coles)
s'applique donc à l'historique DE LA LIGUE, pas à l'historique global.

La colonne `league` est conservée telle quelle dans la sortie, pour un
encodage catégoriel ultérieur côté XGBoost.

Les colonnes brutes post-match (home_goals/away_goals, et maintenant aussi
home_shots/away_shots/home_shots_target/away_shots_target/home_corners/
away_corners) sont conservées dans la sortie — comme c'était déjà le cas
pour home_goals/away_goals dans la version mono-ligue — mais ne sont PAS
utilisées pour calculer une quelconque feature pré-match : ce sont des
données connues seulement APRÈS le coup de sifflet final, donc à exclure
des features d'entrée côté entraînement XGBoost (au même titre que les buts).

Construit, pour chaque match, des features calculées EXCLUSIVEMENT à partir
de matchs à une date strictement antérieure, DANS LA MÊME LIGUE (aucune
fuite d'information du futur vers le passé, ni d'une ligue vers une autre) :

  - home_form_points_avg / away_form_points_avg : moyenne de points (3/1/0)
    sur les 5 derniers matchs de l'équipe dans sa ligue, tous contextes
    confondus (domicile ou extérieur).
  - home_form_goals_scored_avg / _conceded_avg (et away_*) : moyenne de buts
    marqués/encaissés sur la même fenêtre de 5 matchs.
  - home_days_since_last_match / away_days_since_last_match : jours écoulés
    depuis le match précédent de chaque équipe DANS SA LIGUE (NaN si
    l'équipe n'a encore aucun match antérieur dans cette ligue — sa toute
    première apparition), PLAFONNÉS à BREAK_THRESHOLD_DAYS (90).
  - home_returning_from_break / away_returning_from_break : flag booléen
    (1 si le nombre de jours AVANT plafonnement dépasse BREAK_THRESHOLD_DAYS,
    sinon 0) calculé AVANT le clip, pour ne pas perdre le signal "vraie
    rupture de rythme" qu'un plafonnement effacerait sinon. 0 si l'équipe
    n'a aucun match antérieur dans la ligue (première apparition, déjà
    signalée par le NaN sur days_since_last_match).
  - h2h_matches_found : nombre de confrontations directes trouvées dans une
    fenêtre des 5 dernières rencontres entre les deux équipes DANS CETTE
    LIGUE, strictement antérieures à la date du match courant.
  - h2h_home_win_rate : taux de victoire (0.5 pour un nul) de l'équipe qui
    reçoit AUJOURD'HUI, calculé sur ces confrontations passées. NaN si
    h2h_matches_found == 0.
  - dc_home_win / dc_draw / dc_away_win / dc_over_2_5 / dc_under_2_5 :
    probabilités issues d'un modèle Dixon-Coles ré-entraîné en walk-forward
    SUR LA LIGUE CONCERNÉE UNIQUEMENT — un seul fit par DATE de match (au
    sein de cette ligue), sur tous les matchs de cette ligue strictement
    antérieurs à cette date. NaN pendant le rodage (< MIN_TRAIN_MATCHES
    matchs d'historique dans la ligue) ou si l'une des deux équipes n'est
    encore jamais apparue dans l'historique d'entraînement de sa ligue.

Performance : le fit Dixon-Coles walk-forward demande un ré-entraînement par
date de match et par ligue (≈3450 fits au total sur ce dataset : 594 dates
Bundesliga + 812 LaLiga + 590 Ligue1 + 719 PremierLeague + 738 SerieA). Avec
la classe officielle DixonColesModel (~100s/fit, boucle Python pure) ce
serait injouable (~100h). On utilise donc fast_fit (dixon_coles_fast.py), la
réimplémentation vectorisée validée équivalente à DixonColesModel à 1e-5
près (cf. validate_l2_regularization.py sanity_check()) — jamais la classe
officielle lente.
"""

import numpy as np
import pandas as pd
from collections import defaultdict

from dixon_coles_fast import fast_fit, fast_predict_1x2, fast_predict_over_under

RAW_FILE = "data/all_leagues_raw_with_stats.csv"
OUT_FILE = "data/all_leagues_features_xgboost.csv"

FORM_WINDOW = 5
H2H_WINDOW = 5
MIN_TRAIN_MATCHES = 100  # rodage Dixon-Coles, appliqué PAR LIGUE
XI = 0.0018
L2 = 0.05  # même valeur que le défaut de production dans dixon_coles.py
BREAK_THRESHOLD_DAYS = 90  # au-delà : flag "returning_from_break" + plafond des jours de repos


def _points(gf: int, ga: int) -> int:
    if gf > ga:
        return 3
    if gf == ga:
        return 1
    return 0


def check_team_league_collisions(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vérifie qu'aucune équipe n'apparaît sous plus d'une valeur de `league`
    au cours d'une même saison (ce qui invaliderait l'hypothèse d'isolation
    par ligue utilisée pour le calcul des features). Une saison est définie
    ici comme démarrant en juillet (mois >= 7 -> saison de cette année-là,
    sinon saison de l'année précédente), ce qui correspond au calendrier
    des 5 championnats couverts.

    Retourne un DataFrame des cas ambigus (vide si aucun trouvé).
    """
    season = df["date"].dt.year.where(df["date"].dt.month >= 7, df["date"].dt.year - 1)
    home = pd.DataFrame({"season": season, "team": df["home_team"], "league": df["league"]})
    away = pd.DataFrame({"season": season, "team": df["away_team"], "league": df["league"]})
    long = pd.concat([home, away], ignore_index=True)

    league_counts = long.groupby(["team", "season"])["league"].nunique()
    ambiguous_keys = league_counts[league_counts > 1].index
    if len(ambiguous_keys) == 0:
        return pd.DataFrame(columns=["team", "season", "leagues"])

    rows = []
    for team, season_val in ambiguous_keys:
        leagues = sorted(long[(long["team"] == team) & (long["season"] == season_val)]["league"].unique())
        rows.append({"team": team, "season": season_val, "leagues": leagues})
    return pd.DataFrame(rows)


def build_form_and_h2h_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Features de forme, repos et head-to-head pour UNE seule ligue (le
    `df` passé ici doit déjà être filtré sur une seule valeur de `league`
    et trié par date).

    Calculées ligne par ligne dans l'ordre chronologique, en mettant à jour
    l'historique de chaque équipe APRÈS avoir lu ses features pour la ligne
    courante. Une équipe ne pouvant pas jouer deux fois à la même date dans
    ce calendrier (une seule ligne par équipe et par date, au sein d'une
    même ligue), ce séquencement ligne par ligne ne crée aucune fuite
    intra-journée.
    """
    history = defaultdict(list)       # team -> [{"points", "gf", "ga"}, ...]
    last_match_date = {}              # team -> date du dernier match joué
    pair_history = defaultdict(list)  # frozenset({a, b}) -> [{"winner": team_or_None}, ...]

    rows = []
    for row in df.itertuples():
        home, away, date = row.home_team, row.away_team, row.date

        home_hist = history[home][-FORM_WINDOW:]
        away_hist = history[away][-FORM_WINDOW:]

        def avg(hist, key):
            return float(np.mean([h[key] for h in hist])) if hist else np.nan

        pair_key = frozenset((home, away))
        h2h_hist = pair_history[pair_key][-H2H_WINDOW:]
        if h2h_hist:
            # Chaque confrontation passée est ramenée au point de vue de
            # l'équipe qui reçoit AUJOURD'HUI, quel qu'ait été son statut
            # domicile/extérieur lors de cette confrontation passée.
            wins_for_today_home = sum(1 for m in h2h_hist if m["winner"] == home)
            draws = sum(1 for m in h2h_hist if m["winner"] is None)
            h2h_home_win_rate = (wins_for_today_home + 0.5 * draws) / len(h2h_hist)
        else:
            h2h_home_win_rate = np.nan

        home_days_raw = (date - last_match_date[home]).days if home in last_match_date else np.nan
        away_days_raw = (date - last_match_date[away]).days if away in last_match_date else np.nan

        rows.append({
            "home_form_points_avg": avg(home_hist, "points"),
            "home_form_goals_scored_avg": avg(home_hist, "gf"),
            "home_form_goals_conceded_avg": avg(home_hist, "ga"),
            "away_form_points_avg": avg(away_hist, "points"),
            "away_form_goals_scored_avg": avg(away_hist, "gf"),
            "away_form_goals_conceded_avg": avg(away_hist, "ga"),
            # flag calculé sur la valeur BRUTE, avant plafonnement (cf. main())
            "home_returning_from_break": int(home_days_raw > BREAK_THRESHOLD_DAYS) if not np.isnan(home_days_raw) else 0,
            "away_returning_from_break": int(away_days_raw > BREAK_THRESHOLD_DAYS) if not np.isnan(away_days_raw) else 0,
            "home_days_since_last_match": home_days_raw,
            "away_days_since_last_match": away_days_raw,
            "h2h_matches_found": len(h2h_hist),
            "h2h_home_win_rate": h2h_home_win_rate,
        })

        # -- mise à jour de l'historique APRÈS calcul des features de cette ligne --
        history[home].append({"points": _points(row.home_goals, row.away_goals),
                               "gf": row.home_goals, "ga": row.away_goals})
        history[away].append({"points": _points(row.away_goals, row.home_goals),
                               "gf": row.away_goals, "ga": row.home_goals})
        last_match_date[home] = date
        last_match_date[away] = date
        winner = home if row.home_goals > row.away_goals else (away if row.away_goals > row.home_goals else None)
        pair_history[pair_key].append({"winner": winner})

    return pd.DataFrame(rows, index=df.index)


def build_dixon_coles_features(df: pd.DataFrame, refit_every: int = 1) -> pd.DataFrame:
    """
    Probabilités Dixon-Coles en walk-forward, pour UNE seule ligue (le `df`
    passé ici doit déjà être filtré sur une seule valeur de `league` et trié
    par date).

    Un seul fit toutes les `refit_every` dates de match DISTINCTES de cette
    ligue (par défaut 1 : un fit par date, la granularité la plus fine et la
    plus sûre — deux matchs à la même date utilisent le même fit, ce qui est
    correct puisqu'aucun des deux ne peut connaître le résultat de l'autre
    avant le coup d'envoi). Le fit est toujours entraîné sur tous les matchs
    de la ligue strictement antérieurs à la date courante (jamais <=).

    NaN tant que l'historique disponible dans la ligue est trop maigre
    (< MIN_TRAIN_MATCHES matchs) OU si l'une des deux équipes n'est encore
    jamais apparue dans l'historique d'entraînement (typiquement le tout
    premier match d'une équipe promue) — NaN correct et attendu même après
    la période de rodage.
    """
    dc = pd.DataFrame(
        index=df.index,
        columns=["dc_home_win", "dc_draw", "dc_away_win", "dc_over_2_5", "dc_under_2_5"],
        dtype=float,
    )

    dates = sorted(df["date"].unique())
    fit = None
    trained_teams = set()

    for i, date in enumerate(dates):
        idx = df.index[df["date"] == date]

        if i % refit_every == 0 or fit is None:
            train = df[df["date"] < date]
            if len(train) >= MIN_TRAIN_MATCHES:
                trained_teams = set(train["home_team"]) | set(train["away_team"])
                fit = fast_fit(train, xi=XI, l2=L2, reference_date=date)
            else:
                fit = None  # période de rodage : pas assez d'historique dans cette ligue

        if fit is None:
            continue

        for j in idx:
            home, away = df.at[j, "home_team"], df.at[j, "away_team"]
            if home not in trained_teams or away not in trained_teams:
                continue  # équipe jamais vue à l'entraînement (ex: 1er match d'un promu)
            probs = fast_predict_1x2(fit, home, away)
            ou = fast_predict_over_under(fit, home, away, line=2.5)
            dc.at[j, "dc_home_win"] = probs["home_win"]
            dc.at[j, "dc_draw"] = probs["draw"]
            dc.at[j, "dc_away_win"] = probs["away_win"]
            dc.at[j, "dc_over_2_5"] = ou["over"]
            dc.at[j, "dc_under_2_5"] = ou["under"]

    return dc


def main():
    df = pd.read_csv(RAW_FILE, parse_dates=["date"])
    df = df.sort_values("date", kind="stable").reset_index(drop=True)

    collisions = check_team_league_collisions(df)
    if len(collisions):
        print("ATTENTION — équipes ambiguës (>1 ligue sur la même saison) :")
        print(collisions.to_string(index=False))
    else:
        print("Vérification équipe/ligue : aucune collision (chaque équipe reste dans une seule ligue par saison).")

    form_h2h_parts = []
    dc_parts = []
    for league, sub in df.groupby("league", sort=False):
        sub_sorted = sub.sort_values("date", kind="stable")
        form_h2h_parts.append(build_form_and_h2h_features(sub_sorted))
        dc_parts.append(build_dixon_coles_features(sub_sorted))

    form_h2h = pd.concat(form_h2h_parts).loc[df.index]
    dc = pd.concat(dc_parts).loc[df.index]

    out = pd.concat([df, form_h2h, dc], axis=1)

    # Plafonnement APRÈS le calcul du flag returning_from_break ci-dessus —
    # .clip(upper=...) laisse les NaN (première apparition d'une équipe
    # dans sa ligue) intacts, seule la valeur numérique est écrêtée.
    out["home_days_since_last_match"] = out["home_days_since_last_match"].clip(upper=BREAK_THRESHOLD_DAYS)
    out["away_days_since_last_match"] = out["away_days_since_last_match"].clip(upper=BREAK_THRESHOLD_DAYS)

    out.to_csv(OUT_FILE, index=False)

    print(f"\n{len(out)} lignes écrites dans {OUT_FILE}")
    print("\nLignes exploitables (dc_home_win non-NaN) par ligue :")
    for league, sub in out.groupby("league", sort=False):
        n_total = len(sub)
        n_usable = sub["dc_home_win"].notna().sum()
        first_valid = sub["dc_home_win"].first_valid_index()
        print(f"  {league:15s} total={n_total:5d}  exploitables={n_usable:5d}  première ligne valide (index global)={first_valid}")

    return out


if __name__ == "__main__":
    main()
