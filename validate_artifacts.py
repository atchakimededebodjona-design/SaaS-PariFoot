"""
validate_artifacts.py — Vérifications automatiques minimales avant de
remplacer un model_artifacts/<league>.json existant par une version
fraîchement ré-entraînée.
=============================================================================

Objectif : attraper les régressions grossières (bug de parsing dans la
mise à jour des données, saison mal fusionnée, optimiseur qui diverge)
AVANT qu'un artefact cassé n'atteigne l'API — pas un audit statistique
complet (cf. audit_features_multileague.py pour ça), juste un garde-fou
rapide et bon marché à faire tourner à chaque cycle de ré-entraînement.

Seuils par défaut choisis à partir des valeurs RÉELLEMENT observées sur les
5 ligues au moment d'écrire ce module (cf. model_artifacts/*.json) :

    Bundesliga      home_advantage=+0.166  rho=-0.128
    LaLiga          home_advantage=+0.241  rho=+0.018
    Ligue1          home_advantage=+0.154  rho=-0.068
    PremierLeague   home_advantage=+0.136  rho=-0.004
    SerieA          home_advantage=+0.133  rho=-0.073

Toutes confortablement dans [0.05, 0.4] pour home_advantage et
[-0.3, 0.1] pour rho — ces bornes larges laissent de la marge pour la
variation saison après saison sans les élargir à l'aveugle.

Chaque vérification est PAR LIGUE : une ligue qui échoue ne bloque pas les
4 autres (cf. refresh_and_retrain.py — l'ancien artefact de cette ligue
précise est conservé, les autres sont mis à jour normalement).
"""

from dataclasses import dataclass, field


HOME_ADVANTAGE_RANGE = (0.05, 0.4)
RHO_RANGE = (-0.3, 0.1)
MAX_TEAM_DROP_PCT = 0.30  # perte de >30% des équipes connues = suspect


@dataclass
class ValidationResult:
    league: str
    ok: bool
    reasons: list = field(default_factory=list)  # raisons d'échec (vide si ok=True)
    warnings: list = field(default_factory=list)  # signalé mais pas bloquant


def validate_artifact(new_artifact: dict, old_artifact: dict | None,
                       home_advantage_range=HOME_ADVANTAGE_RANGE,
                       rho_range=RHO_RANGE,
                       max_team_drop_pct=MAX_TEAM_DROP_PCT) -> ValidationResult:
    """
    Valide un artefact nouvellement entraîné pour UNE ligue.

    new_artifact : dict produit par export_model_artifacts.export_league()
    old_artifact : artefact précédemment en place pour cette ligue (dict
                   chargé depuis le JSON existant), ou None si aucun
                   artefact précédent n'existe encore (première exécution)
    """
    league = new_artifact["league"]
    reasons = []
    warnings = []

    n_teams_new = len(new_artifact["teams"])
    if n_teams_new == 0:
        reasons.append("0 équipe dans l'artefact — entraînement clairement cassé")

    home_adv = new_artifact["home_advantage"]
    lo, hi = home_advantage_range
    if not (lo <= home_adv <= hi):
        reasons.append(f"home_advantage={home_adv:.4f} hors fourchette plausible [{lo}, {hi}]")

    rho = new_artifact["rho"]
    lo, hi = rho_range
    if not (lo <= rho <= hi):
        reasons.append(f"rho={rho:.4f} hors fourchette plausible [{lo}, {hi}]")

    if old_artifact is not None:
        n_teams_old = len(old_artifact["teams"])
        if n_teams_old > 0:
            drop_pct = 1 - (n_teams_new / n_teams_old)
            if drop_pct > max_team_drop_pct:
                reasons.append(
                    f"nombre d'équipes en chute suspecte : {n_teams_old} -> {n_teams_new} "
                    f"(-{drop_pct:.0%}, seuil={max_team_drop_pct:.0%}) — probable bug de parsing, "
                    f"pas une vraie évolution de championnat"
                )

        n_matches_old = old_artifact.get("trained_on_matches", 0)
        n_matches_new = new_artifact.get("trained_on_matches", 0)
        if n_matches_new < n_matches_old:
            reasons.append(
                f"nombre de matchs d'entraînement en baisse : {n_matches_old} -> {n_matches_new} "
                f"— l'historique ne devrait jamais rétrécir (fusion incrémentale), signe probable "
                f"de corruption des données brutes"
            )

        # Écart brutal de home_advantage/rho d'un cycle à l'autre : pas
        # forcément une erreur (une saison peut légitimement déplacer ces
        # paramètres), mais assez inhabituel pour mériter un avertissement
        # non bloquant dans les logs.
        if abs(home_adv - old_artifact["home_advantage"]) > 0.1:
            warnings.append(
                f"home_advantage a bougé de {old_artifact['home_advantage']:.4f} à {home_adv:.4f} "
                f"(> 0.1) d'un cycle à l'autre — à surveiller, pas bloquant"
            )
        if abs(rho - old_artifact["rho"]) > 0.1:
            warnings.append(
                f"rho a bougé de {old_artifact['rho']:.4f} à {rho:.4f} (> 0.1) "
                f"d'un cycle à l'autre — à surveiller, pas bloquant"
            )

    return ValidationResult(league=league, ok=(len(reasons) == 0), reasons=reasons, warnings=warnings)
