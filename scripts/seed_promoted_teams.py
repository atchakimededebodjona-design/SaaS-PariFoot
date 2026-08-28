"""
scripts/seed_promoted_teams.py — amorce une note Dixon-Coles pour des
équipes qui viennent d'être promues dans une compétition suivie par Xfoot
et qui n'ont donc AUCUN match dans data/all_leagues_raw_with_stats.csv (ni,
par conséquent, dans l'artefact de production correspondant) — sans cette
amorce, resolve_team_name() (api/main.py) les rejette systématiquement
("Équipe non reconnue"), pour Dixon-Coles ET pour Elo/XGBoost/LightGBM (tous
gatés par le même roster, voir docstring de resolve_team_name).

Trouvé le 2026-08-28 : Malaga et Deportivo La Coruna (LaLiga), et en
auditant les 11 ligues suivies contre le vrai roster actuel (API-Football,
/teams), 6 autres cas identiques — Hull City/Coventry (Premier League),
Racing Santander (LaLiga), SV Elversberg (Bundesliga), Le Mans (Ligue 1).

=== MÉTHODE (documentée, reproductible, jamais un chiffre inventé) ===

1. Récupère l'historique RÉEL 2025-26 de l'équipe dans sa division
   d'origine (Championship anglais/E1, Segunda División/SP2,
   2. Bundesliga/D2, Ligue 2/F2 — football-data.co.uk, en réutilisant TEL
   QUEL update_raw_data.py::download_direct_season, même mécanisme déjà en
   place pour la Primeira Liga, aucune nouvelle logique de téléchargement).
2. Entraîne un Dixon-Coles (_FastDixonColesL2, réutilisé tel quel depuis
   export_model_artifacts.py, aucune réimplémentation) SUR CETTE SEULE
   division — jamais mélangé aux données de la division cible : les
   adversaires n'ont pas le même niveau, un entraînement conjoint
   fausserait les notes des deux côtés.
3. Calcule le rang percentile RÉEL de l'équipe (attaque et défense
   séparément) parmi ses pairs de division inférieure.
4. Reporte ce percentile sur la MOITIÉ INFÉRIEURE [min, médiane] de la
   distribution attaque/défense déjà entraînée pour la division cible —
   jamais au-dessus de la médiane. Choix conservateur documenté : une
   équipe promue n'a aucune preuve de niveau dans sa nouvelle division ;
   la régularité empirique bien établie en football est qu'une équipe
   promue termine très majoritairement dans la moitié basse de son nouveau
   championnat la saison suivante.
5. Patch CIBLÉ de l'artefact JSON de production correspondant — ajoute
   l'équipe à teams/attack/defense, ne touche à AUCUNE valeur existante
   des autres équipes. Ajoute une entrée dans "seeded_teams" (métadonnée
   additive, ignorée par LeagueModel.__init__ qui ne lit que les clés
   qu'il connaît) pour tracer QUELLES équipes sont des amorces et
   QUAND/COMMENT, plutôt que de les laisser se confondre silencieusement
   avec des notes réellement entraînées.

=== LIMITE IMPORTANTE, documentée honnêtement ===

Ce patch sera EFFACÉ par le prochain passage du cron hebdomadaire
(refresh_and_retrain.py, lundi 04:00 UTC) tant que l'équipe promue n'a pas
encore de vrais matchs de première division dans
data/all_leagues_raw_with_stats.csv — à ce moment-là,
export_model_artifacts.py régénère l'artefact ENTIER depuis le CSV, qui ne
contient toujours pas l'équipe (elle disparaît du fichier tant qu'aucun
vrai match n'existe). Ce script est donc à RE-EXÉCUTER chaque semaine
jusqu'à ce que de vrais matchs de première division existent pour chaque
équipe (à ce moment-là, le retrain normal prend le relais avec une vraie
note, bien meilleure que cette amorce) — ou à intégrer dans
refresh_and_retrain.py pour devenir automatique (non fait dans cette
phase, voir rapport).

Usage : python scripts/seed_promoted_teams.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from update_raw_data import download_direct_season  # noqa: E402
from export_model_artifacts import _FastDixonColesL2  # noqa: E402

ARTIFACTS_DIR = ROOT / "api" / "model_artifacts"
SOURCE_SEASON_CODE = "2526"  # saison 2025-26 : dernière saison complète des équipes promues dans leur ancienne division

# league cible -> (code division source football-data.co.uk, {nom exact dans le CSV source: nom canonique à insérer})
# Le nom canonique choisi est EXACTEMENT celui renvoyé par API-Football pour
# ces fixtures (voir audit du 2026-08-28) — pas besoin d'alias supplémentaire
# dans TEAM_ALIASES, la correspondance exacte suffira directement.
PROMOTED_TEAMS = {
    "PremierLeague": {
        "division_code": "E1",
        "teams": {"Hull": "Hull City", "Coventry": "Coventry"},
    },
    "LaLiga": {
        "division_code": "SP2",
        "teams": {"Malaga": "Malaga", "La Coruna": "Deportivo La Coruna", "Santander": "Racing Santander"},
    },
    "Bundesliga": {
        "division_code": "D2",
        "teams": {"Elversberg": "SV Elversberg"},
    },
    "Ligue1": {
        "division_code": "F2",
        "teams": {"Le Mans": "Le Mans"},
    },
}


def _percentile_rank(value: float, population: list[float]) -> float:
    """Rang percentile de `value` dans `population` (0 = le plus faible, 1 = le plus fort)."""
    arr = np.array(population)
    return float((arr < value).sum()) / max(len(arr) - 1, 1)


def _map_to_lower_half(percentile: float, target_values: list[float]) -> float:
    """Reporte un percentile [0,1] sur [min, médiane] de la distribution cible
    — jamais au-dessus de la médiane (voir §4 du docstring module)."""
    arr = np.array(target_values)
    target_min = float(arr.min())
    target_median = float(np.median(arr))
    return target_min + percentile * (target_median - target_min)


class _GoalDifferenceProxyModel:
    """
    Repli robuste pour fit_source_division() quand Dixon-Coles ne converge
    pas sur les données de la division SOURCE (observé sur Ligue 2 2025-26
    — SLSQP zéro-init, SLSQP point de départ perturbé, SLSQP régularisation
    renforcée, ET trust-constr [NaN] ont TOUS échoué, voir git blame de ce
    fichier pour le détail des tentatives).

    Même interface (.attack_/.defense_, dicts équipe -> float) que
    _FastDixonColesL2, pour que le reste du script (percentile, mapping)
    n'ait besoin d'aucun traitement spécial. Proxy transparent et 100%
    dérivé des vraies données de la saison — attaque = buts marqués par
    match, défense = -(buts encaissés par match), même convention de signe
    que Dixon-Coles (plus haut = plus fort des deux côtés). Moins raffiné
    qu'un vrai Dixon-Coles (ignore la force des adversaires affrontés),
    mais suffisant pour un simple RANG PERCENTILE au sein d'une seule
    division — jamais la note finale injectée dans un artefact de
    production (celle-ci reste calculée par le vrai Dixon-Coles de la
    division CIBLE, inchangé, voir seed_league()).
    """

    def __init__(self, matches):
        teams = sorted(set(matches["home_team"]) | set(matches["away_team"]))
        self.attack_, self.defense_ = {}, {}
        for t in teams:
            home = matches[matches["home_team"] == t]
            away = matches[matches["away_team"] == t]
            played = len(home) + len(away)
            goals_for = home["home_goals"].sum() + away["away_goals"].sum()
            goals_against = home["away_goals"].sum() + away["home_goals"].sum()
            self.attack_[t] = goals_for / played
            self.defense_[t] = -(goals_against / played)


def fit_source_division(division_code: str):
    df = download_direct_season(division_code, SOURCE_SEASON_CODE)
    if df is None:
        raise RuntimeError(f"{division_code}/{SOURCE_SEASON_CODE} introuvable sur football-data.co.uk")

    try:
        model = _FastDixonColesL2()
        model.fit(df)
        return model
    except RuntimeError as e1:
        print(f"    (SLSQP non convergé sur {division_code} avec l2_reg=0.05 ({e1}), "
              f"nouvelle tentative avec point de départ perturbé)")

    n_teams = len(sorted(set(df["home_team"]) | set(df["away_team"])))
    rng = np.random.default_rng(42)
    jitter = np.concatenate([
        rng.normal(0, 0.05, n_teams), rng.normal(0, 0.05, n_teams), [0.2], [-0.05],
    ])
    try:
        model = _FastDixonColesL2()
        model.fit(df, init_params=jitter)
        return model
    except RuntimeError as e2:
        print(f"    (toujours non convergé avec un point de départ perturbé ({e2}), "
              f"nouvelle tentative avec une régularisation plus forte)")

    try:
        model = _FastDixonColesL2(l2_reg=0.15)
        model.fit(df)
        return model
    except RuntimeError as e3:
        print(f"    (toujours non convergé avec l2_reg=0.15 ({e3}), "
              f"repli sur un proxy buts marqués/encaissés par match)")

    # Dernier recours, jamais silencieux : Dixon-Coles a échoué sur les 3
    # configurations ci-dessus (dont trust-constr, testé séparément, a
    # produit des NaN) — le proxy but marqués/encaissés reste 100% dérivé
    # des vraies données de la division source, juste moins raffiné (voir
    # docstring _GoalDifferenceProxyModel). N'affecte jamais
    # export_model_artifacts.py (production, toujours Dixon-Coles pur).
    print(f"    [ATTENTION] {division_code} : Dixon-Coles non convergé après 3 tentatives, "
          f"utilisation du proxy buts marqués/encaissés par match")
    return _GoalDifferenceProxyModel(df)


def seed_league(league: str, dry_run: bool) -> dict:
    cfg = PROMOTED_TEAMS[league]
    artifact_path = ARTIFACTS_DIR / f"{league}.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    target_attack_values = list(artifact["attack"].values())
    target_defense_values = list(artifact["defense"].values())

    source_model = fit_source_division(cfg["division_code"])
    source_attack_values = list(source_model.attack_.values())
    source_defense_values = list(source_model.defense_.values())

    seeded = artifact.setdefault("seeded_teams", {})
    added = []
    for source_name, canonical_name in cfg["teams"].items():
        if canonical_name in artifact["teams"]:
            print(f"  [SKIP] {canonical_name} déjà présent dans {league}.json (retrain automatique a pris le relais)")
            continue
        if source_name not in source_model.attack_:
            print(f"  [ATTENTION] '{source_name}' introuvable dans le fit {cfg['division_code']} — ignoré")
            continue

        atk_pct = _percentile_rank(source_model.attack_[source_name], source_attack_values)
        def_pct = _percentile_rank(source_model.defense_[source_name], source_defense_values)
        new_attack = _map_to_lower_half(atk_pct, target_attack_values)
        new_defense = _map_to_lower_half(def_pct, target_defense_values)

        artifact["teams"].append(canonical_name)
        artifact["attack"][canonical_name] = new_attack
        artifact["defense"][canonical_name] = new_defense
        seeded[canonical_name] = {
            "method": "lower_division_percentile_calibration",
            "source_division": cfg["division_code"],
            "source_season": SOURCE_SEASON_CODE,
            "source_team_name": source_name,
            "source_attack_percentile": atk_pct,
            "source_defense_percentile": def_pct,
            "seeded_at": datetime.now(timezone.utc).isoformat(),
            "note": "Amorce conservatrice (moitié basse de la distribution cible) — sera remplacée par une "
                    "vraie note dès que des matchs réels existeront dans data/all_leagues_raw_with_stats.csv "
                    "(voir refresh_and_retrain.py, cron hebdomadaire).",
        }
        added.append(canonical_name)
        print(f"  [SEED] {canonical_name} <- {cfg['division_code']}:{source_name} "
              f"(percentile attaque={atk_pct:.2f}, défense={def_pct:.2f}) "
              f"-> attack={new_attack:.4f}, defense={new_defense:.4f}")

    if added and not dry_run:
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> {artifact_path} mis à jour ({len(added)} équipe(s) amorcée(s))")
    elif added and dry_run:
        print(f"  -> [DRY-RUN] {artifact_path} non modifié")

    return {"league": league, "added": added}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="calcule et affiche sans écrire les artefacts")
    args = parser.parse_args()

    summary = []
    for league in PROMOTED_TEAMS:
        print(f"=== {league} ===")
        summary.append(seed_league(league, args.dry_run))

    print("\nRésumé :")
    for s in summary:
        print(f"  {s['league']}: {s['added'] or '(rien à amorcer)'}")


if __name__ == "__main__":
    main()
