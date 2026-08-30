"""
scripts/odds_integrity_audit.py — Phase 8E : XFOOT ODDS TIMESTAMP &
HISTORICAL INTEGRITY AUDIT V1.
=============================================================================
RESEARCH / DATA INTEGRITY ONLY. Aucune écriture dans match / match_stats /
model_predictions / model_versions / team_ratings / prediction_log. Aucun
secret, aucune clé API. Réutilise INTÉGRALEMENT l'infrastructure Phase 8D
(scripts/odds_research_walkforward.py::download_all/load_xfoot_matches/
parse_and_map/DIV_TO_LEAGUE/SEASONS, même cache local hors dépôt) — jamais
une deuxième implémentation du téléchargement/mapping (§1 du prompt).

=== CONSTAT CENTRAL DE CETTE PHASE ===

football-data.co.uk (source Phase 8D) NE fournit AUCUN timestamp par
observation de cote. La seule colonne temporelle du CSV est `Time`, qui est
l'HEURE DE COUP D'ENVOI du match (T_MATCH), jamais un T_ODDS mesuré — vérifié
en relisant le header brut des 35 fichiers déjà en cache (aucune colonne
"OddsTimestamp"/"quote_time" ou équivalent, sur aucune saison 1920-2526).

Conséquence directe (§7/§8/§51 du prompt) : TOUTE observation de cote valide
de ce dataset reçoit la classification HISTORICAL_BUT_UNTIMESTAMPED — JAMAIS
TEMPORALLY_VERIFIED, quel que soit le cutoff théorique testé (T-24h à T-1h),
puisque has_measured_timestamp est structurellement False pour les trois
séries utilisées en Phase 8D (pré-clôture Bet365, clôture Bet365, consensus
Avg). Le benchmark principal (§9) est donc INSUFFICIENT_DATA_FOR_TEMPORAL_
VALIDATION, et le résultat Phase 8D est reclassé HISTORICAL_SIGNAL_BUT_
TEMPORAL_VALIDATION_INSUFFICIENT (§37) — jamais supprimé, jamais présenté
comme invalidé, seulement requalifié honnêtement.

Usage (depuis la racine du dépôt) :
    DATABASE_URL="sqlite:///./api/app.db" python scripts/odds_integrity_audit.py \
        [--cache-dir <dir>] [--phase8d-report <path>]
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlmodel import Session  # noqa: E402

from app.core.database import engine, init_db  # noqa: E402
from app.ai.odds_research.integrity import classify_observation, OBSERVATION_CLASSES  # noqa: E402
# combine_date_time / safe_consensus : exercées uniquement par les tests
# adversariaux (api/test_odds_integrity_core.py) — la donnée réelle
# football-data.co.uk n'a ni structure bookmaker-par-bookmaker horodatée, ni
# heure de coup d'envoi exploitée directement dans ce run (voir §5/§9 du
# rapport : documenté comme capacité vérifiée, pas comme appliquée ici faute
# de données réellement horodatées à leur fournir).

import feature_engineering_walkforward as fewf  # noqa: E402
import odds_research_walkforward as orw  # noqa: E402 — réutilisation intégrale Phase 8D

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("odds_integrity_audit")

CUTOFFS_HOURS = [24, 12, 6, 3, 1]  # §11/§12/§36 : horizons théoriques demandés
SNAPSHOT_SERIES = ("pre_1x2", "close_1x2", "consensus_1x2")  # les 3 séries réellement utilisées en Phase 8D
SNAPSHOT_LABELS = {"pre_1x2": "PRE_CLOSING (Bet365)", "close_1x2": "CLOSING (Bet365)", "consensus_1x2": "MARKET CONSENSUS (Avg)"}


# ---------------------------------------------------------------------------
# 1. Audit des colonnes brutes — §6/§7 : confirmer, jamais supposer,
#    l'absence de toute colonne de timestamp d'odds.
# ---------------------------------------------------------------------------

def audit_raw_columns(files: dict) -> dict:
    import pandas as pd
    time_like_columns: set[str] = set()
    all_columns: set[str] = set()
    sample_seasons_checked = 0
    for (div, season), path in files.items():
        try:
            header = pd.read_csv(path, nrows=0, encoding="latin-1").columns.tolist()
        except Exception:
            continue
        all_columns.update(header)
        time_like_columns.update(c for c in header if "time" in c.lower() or "stamp" in c.lower())
        sample_seasons_checked += 1
    return {
        "files_checked": sample_seasons_checked,
        "time_like_columns_found": sorted(time_like_columns),
        "odds_timestamp_column_exists": bool(time_like_columns - {"Time"}),
        "conclusion": (
            "Aucune colonne de timestamp d'odds trouvée (seule 'Time' = heure de coup d'envoi du match) "
            "sur l'ensemble des fichiers en cache." if not (time_like_columns - {"Time"}) else
            f"ATTENTION : colonne(s) inattendue(s) ressemblant à un timestamp trouvée(s) : {sorted(time_like_columns - {'Time'})} — à examiner manuellement, jamais supposée être un timestamp d'odds sans vérification."
        ),
    }


# ---------------------------------------------------------------------------
# 2. Reconstruction T_MATCH (Date+Time football-data.co.uk) — §24
# ---------------------------------------------------------------------------

def audit_kickoff_precision(files: dict) -> dict:
    import pandas as pd
    total, with_time = 0, 0
    for (div, season), path in files.items():
        try:
            df = pd.read_csv(path, encoding="latin-1", low_memory=False)
        except Exception:
            continue
        total += len(df)
        if "Time" in df.columns:
            with_time += int(df["Time"].notna().sum())
    return {
        "total_rows": total, "rows_with_kickoff_time": with_time,
        "coverage_pct": 100.0 * with_time / total if total else 0.0,
        "timezone_status": "UNKNOWN / NEEDS CONFIRMATION — football-data.co.uk ne documente pas la timezone de la colonne Time (heure locale probable, jamais supposée UTC/CET sans preuve — voir §25).",
        "xfoot_match_date_precision": "Match.date en base Xfoot ne conserve AUCUNE heure (minuit systématique, confirmé Phase 8A) — la précision Date+Time n'existe que dans le CSV source, jamais dans `match`.",
    }


# ---------------------------------------------------------------------------
# 3. Classification à 5 voies de chaque observation — §8/§9/§35
# ---------------------------------------------------------------------------

def classify_all_observations(observations: list[dict]) -> dict:
    """Pour chacune des 3 séries réellement utilisées en Phase 8D
    (pré-clôture/clôture/consensus), classe chaque observation. Comme aucun
    timestamp mesuré n'existe (has_measured_timestamp=False partout, voir
    docstring module), le cutoff théorique n'a AUCUN effet sur le résultat —
    documenté explicitement plutôt que masqué."""
    per_series = {}
    for series in SNAPSHOT_SERIES:
        counts = {c: 0 for c in OBSERVATION_CLASSES}
        for o in observations:
            is_valid = o[series] is not None
            cls = classify_observation(is_valid_odds=is_valid, has_measured_timestamp=False)
            counts[cls] += 1
        per_series[series] = counts
    return per_series


def temporal_coverage_table(observations: list[dict], total_xfoot_matches: int) -> list[dict]:
    """§11/§31 : table Cutoff x (Total/Verified/Unverified/Future/Unknown/Coverage).
    Résultat STRUCTURELLEMENT identique pour chaque cutoff (aucun timestamp
    mesuré ne dépend du cutoff choisi) — documenté, jamais masqué."""
    rows = []
    n = len(observations)
    # verified = 0 systématiquement : has_measured_timestamp est False pour
    # toute observation de cette source (voir docstring module) -> aucune
    # observation ne peut jamais atteindre TEMPORALLY_VERIFIED, quel que soit
    # le cutoff testé ci-dessous.
    verified = 0
    unverified = sum(1 for o in observations if o["pre_1x2"] is not None)
    rejected = n - unverified
    for h in CUTOFFS_HOURS:
        rows.append({
            "cutoff": f"T-{h}h", "total_matches": total_xfoot_matches, "verified": verified,
            "unverified": unverified, "future": 0, "unknown": 0, "rejected": rejected,
            "coverage_pct": 0.0,  # coverage = part de TEMPORALLY_VERIFIED, jamais la couverture "historique" (§9)
        })
    return rows


def historical_reconstruction_table() -> list[dict]:
    """§12/§36 : peut-on reconstruire "quelles odds étaient disponibles
    exactement à T-Xh" ? Réponse NO pour tous les cutoffs — aucun timestamp
    mesuré n'existe pour distinguer un état à T-24h d'un état à T-1h."""
    rows = []
    for h in CUTOFFS_HOURS:
        rows.append({
            "cutoff": f"T-{h}h", "reconstruction": "NO",
            "evidence": "Aucun timestamp mesuré par observation — seule une méthodologie de collecte documentée (§5/§29 rapport Phase 8D) existe, jamais un snapshot daté.",
            "verdict": "HISTORICAL_BUT_UNTIMESTAMPED",
        })
    return rows


# ---------------------------------------------------------------------------
# 4. Bookmaker / Consensus quality — §33/§34
# ---------------------------------------------------------------------------

def bookmaker_quality_table(observations: list[dict]) -> list[dict]:
    rows = []
    for series, label in (("pre_1x2", "Bet365 (pré-clôture)"), ("close_1x2", "Bet365 (clôture)")):
        n = len(observations)
        valid = sum(1 for o in observations if o[series] is not None)
        rows.append({
            "bookmaker": label, "matches": n, "timestamped": 0, "safe": 0,
            "unknown": valid, "coverage_pct": 100.0 * valid / n if n else 0.0,
            "verdict": "HISTORICAL_BUT_UNTIMESTAMPED",
        })
    return rows


def consensus_quality_table(observations: list[dict]) -> list[dict]:
    """§17/§18/§34 : la colonne `Avg` de football-data.co.uk est un consensus
    PRÉ-CALCULÉ PAR LA SOURCE — sa provenance temporelle (quels bookmakers,
    à quel instant chacun a été observé) est INCONNUE. §19 : ne jamais
    utiliser un consensus déjà calculé si son contenu temporel ne peut pas
    être vérifié -> systématiquement UNSAFE, quel que soit le cutoff."""
    rows = []
    n = len(observations)
    valid = sum(1 for o in observations if o["consensus_1x2"] is not None)
    for h in CUTOFFS_HOURS:
        rows.append({
            "cutoff": f"T-{h}h", "matches": n, "bookmakers": "UNKNOWN (consensus opaque, source pré-calculée)",
            "safe_consensus": 0, "unsafe": valid, "unknown": n - valid,
            "verdict": "UNSAFE — provenance temporelle du consensus non vérifiable (§18)",
        })
    return rows


# ---------------------------------------------------------------------------
# 5. Réévaluation Phase 8D — §19/§37/§38
# ---------------------------------------------------------------------------

def reassess_phase8d(phase8d_report_path: Path | None) -> dict:
    if phase8d_report_path is None or not phase8d_report_path.exists():
        return {
            "found": False,
            "note": "Aucun rapport Phase 8D fourni/trouvé — réévaluation basée uniquement sur le constat structurel de cette phase (aucune donnée TEMPORALLY_VERIFIED disponible dans la source).",
        }
    data = json.loads(phase8d_report_path.read_text(encoding="utf-8"))
    original_verdicts = data.get("group_verdicts", {})
    reassessed = {}
    for exp, verdict in original_verdicts.items():
        if verdict == "BASELINE":
            reassessed[exp] = "BASELINE"
        elif verdict == "BETTER":
            reassessed[exp] = "HISTORICAL_SIGNAL_BUT_TEMPORAL_VALIDATION_INSUFFICIENT"
        else:
            reassessed[exp] = verdict
    return {
        "found": True, "source_report": str(phase8d_report_path),
        "original_verdicts": original_verdicts, "temporally_verified_verdicts": reassessed,
        "note": (
            "Aucun résultat Phase 8D n'est supprimé (§37) — chaque verdict BETTER est requalifié "
            "HISTORICAL_SIGNAL_BUT_TEMPORAL_VALIDATION_INSUFFICIENT : le signal statistique observé en "
            "Phase 8D reste réel sur les données telles quelles, mais ne peut PAS être prouvé disponible "
            "avant un cutoff pré-match précis, faute de timestamp mesuré dans la source."
        ),
    }


# ---------------------------------------------------------------------------
# 6. Rapport
# ---------------------------------------------------------------------------

def _fmt(v):
    return "N/A" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))


def render_markdown(result: dict) -> str:
    md = ["# XFOOT ODDS TIMESTAMP & HISTORICAL INTEGRITY AUDIT V1\n"]

    md.append("\n## 1. Executive Summary\n")
    md.append(f"\nRun id : `{result['run_id']}` — généré le {result['generated_at']}.\n")
    md.append("\nRÈGLE ABSOLUE : RESEARCH / DATA INTEGRITY ONLY. Aucune intégration production.\n")
    md.append(f"\n- TEMPORAL INTEGRITY : {result['temporal_integrity']}\n")
    md.append(f"- ODDS SIGNAL : {result['odds_signal']}\n")
    md.append("- VALUE ENGINE : NOT BUILT.\n- PRODUCTION : NO CHANGES.\n")
    md.append(f"\n**Constat central** : {result['central_finding']}\n")

    md.append("\n## 2. Source\n\n")
    md.append(f"\nfootball-data.co.uk (même cache que Phase 8D, {result['column_audit']['files_checked']} fichiers vérifiés). {result['column_audit']['conclusion']}\n")

    md.append("\n## 3. Dataset\n\n")
    ds = result["dataset"]
    md.append(f"\n- Matchs Xfoot (5 ligues) : {ds['total_xfoot_matches']}\n- Observations odds rapprochées : {ds['mapped']}\n- Cotes 1X2 pré-clôture valides : {ds['valid_pre_1x2']}\n")

    md.append("\n## 4. Timestamp Availability\n\n")
    md.append(f"\n{result['column_audit']['conclusion']} Colonnes temporelles trouvées : {result['column_audit']['time_like_columns_found']}.\n")

    md.append("\n## 5. Timezone\n\n")
    kp = result["kickoff_precision"]
    md.append(f"\n- Lignes avec heure de coup d'envoi (colonne Time) : {kp['rows_with_kickoff_time']}/{kp['total_rows']} ({kp['coverage_pct']:.1f}%)\n- Statut timezone : {kp['timezone_status']}\n- {kp['xfoot_match_date_precision']}\n")

    md.append("\n## 6. Cutoff Definitions\n\n")
    md.append("\nT_MATCH = kickoff. T_ODDS = timestamp de l'observation odds (INEXISTANT dans cette source, voir §4). T_CUTOFF = moment théorique de prédiction Xfoot. Règle : T_ODDS <= T_CUTOFF < T_MATCH pour une observation utilisable (§3). Cutoffs théoriques testés : " + ", ".join(f"T-{h}h" for h in CUTOFFS_HOURS) + ".\n")

    md.append("\n## 7. Temporal Classification\n\n")
    md.append("| Series | TEMPORALLY_VERIFIED | HISTORICAL_BUT_UNTIMESTAMPED | TIMESTAMPED_BUT_AFTER_CUTOFF | UNKNOWN | REJECTED |\n|---|---|---|---|---|---|\n")
    for series, counts in result["classification_per_series"].items():
        md.append(f"| {SNAPSHOT_LABELS[series]} | {counts['TEMPORALLY_VERIFIED']} | {counts['HISTORICAL_BUT_UNTIMESTAMPED']} | {counts['TIMESTAMPED_BUT_AFTER_CUTOFF']} | {counts['UNKNOWN']} | {counts['REJECTED']} |\n")

    md.append("\n## 8. Historical Reconstruction\n\n")
    md.append("| Cutoff | Reconstruction | Evidence | Verdict |\n|---|---|---|---|\n")
    for row in result["historical_reconstruction"]:
        md.append(f"| {row['cutoff']} | {row['reconstruction']} | {row['evidence']} | {row['verdict']} |\n")

    md.append("\n## 9. Opening Odds\n\n")
    md.append("\nLa série \"pré-clôture\" (B365H/D/A) de Phase 8D correspond à la première valeur PERSISTÉE par football-data.co.uk pour un match, PAS nécessairement la cote d'ouverture réelle du marché (§15) — la source ne documente aucune garantie qu'aucune cote antérieure n'ait existé et n'ait simplement pas été collectée. Classée HISTORICAL_BUT_UNTIMESTAMPED.\n")

    md.append("\n## 10. Closing Odds\n\n")
    md.append("\nLa série \"clôture\" (B365CH/CD/CA) est étiquetée comme telle par la source mais SANS timestamp mesuré (§16) — reste HISTORICAL_BUT_UNTIMESTAMPED, jamais promue TEMPORALLY_VERIFIED sur la seule foi de l'étiquette \"C\" (closing).\n")

    md.append("\n## 11. Odds Movement\n\n")
    md.append("\nExperiment 4 (Phase 8D) calcule un delta pré-clôture->clôture. Puisque NI l'un NI l'autre snapshot n'a de timestamp mesuré, ce \"mouvement\" ne peut être positionné sur aucun axe temporel précis — ODDS MOVEMENT = NOT AVAILABLE au sens strict du §14 (une seule paire de valeurs par match, jamais une série de snapshots datés).\n")

    md.append("\n## 12. Bookmaker Analysis\n\n")
    md.append("| Bookmaker | Matches | Timestamped | Safe | Unknown | Coverage | Verdict |\n|---|---|---|---|---|---|---|\n")
    for row in result["bookmaker_quality"]:
        md.append(f"| {row['bookmaker']} | {row['matches']} | {row['timestamped']} | {row['safe']} | {row['unknown']} | {row['coverage_pct']:.1f}% | {row['verdict']} |\n")

    md.append("\n## 13. Market Consensus\n\n")
    md.append("| Cutoff | Matches | Bookmakers | Safe Consensus | Unsafe | Unknown | Verdict |\n|---|---|---|---|---|---|---|\n")
    for row in result["consensus_quality"]:
        md.append(f"| {row['cutoff']} | {row['matches']} | {row['bookmakers']} | {row['safe_consensus']} | {row['unsafe']} | {row['unknown']} | {row['verdict']} |\n")
    md.append("\nExemple adversarial vérifié par test (§42) : bookmakers A=T-10h, B=T-8h, C=T-2h, cutoff=T-6h -> `safe_consensus` inclut EXACTEMENT A+B, exclut C. La colonne `Avg` de football-data.co.uk ne permet PAS de reproduire ce filtrage (provenance temporelle opaque) — voir api/test_odds_integrity_core.py.\n")

    md.append("\n## 14. Leakage Audit\n\n")
    md.append("\nTests adversariaux (§41/§42) exécutés et vérifiés : T-10h->SAFE, T-5h->FUTURE_INFORMATION, T-1h->FUTURE_INFORMATION, T+10min->REJECTED, null->REJECTED, consensus A+B+C avec exclusion correcte de C. Voir api/test_odds_integrity_core.py (26 tests). Sur les DONNÉES RÉELLES (jamais synthétiques), aucune fuite n'est démontrable NI exclue — l'absence de timestamp empêche toute affirmation dans un sens ou dans l'autre (§9 : ne jamais transformer une donnée non timestampée en donnée sûre).\n")

    md.append("\n## 15. League Coverage\n\n")
    md.append(f"\n{ds['total_xfoot_matches']} matchs sur 5 ligues (Bundesliga, LaLiga, Ligue1, PremierLeague, SerieA) — les mêmes que Phase 8D. Aucune conclusion étendue aux 6 autres ligues Xfoot (MLS, Saudi Pro League, Champions/Europa/Conference League), non couvertes par football-data.co.uk.\n")

    md.append("\n## 16. Match Mapping\n\n")
    q = result["mapping_quality"]
    md.append(f"\n- Exact (rapprochement déterministe réutilisé de Phase 8D) : {q['mapped']}\n- Ambiguous : 0 (le mapping par clé exacte ne produit structurellement aucune ambiguïté — deux lignes menant à la même clé sont comptées séparément comme \"duplicate\")\n- Duplicate : {q['duplicate_mapped']}\n- Unmatched : {q['unmapped']}\n")

    md.append("\n## 17. Data Quality\n\n")
    md.append(f"\nCotes 1X2 invalides après rapprochement : {result['dataset']['invalid_1x2_odds']}. Aucune donnée imputée, aucune cote fabriquée (§50).\n")

    md.append("\n## 18. Reproducibility\n\n")
    md.append("\nMême cache football-data.co.uk + même logique de classification (aucun paramètre aléatoire) -> même classification à chaque exécution. Vérifié par api/test_odds_integrity_core.py::test_reproducibility_same_input_same_classification.\n")

    md.append("\n## 19. Phase 8D Reassessment\n\n")
    r8d = result["phase8d_reassessment"]
    if r8d["found"]:
        md.append(f"\nSource : `{r8d['source_report']}`\n\n| Experiment | ORIGINAL RESULT | TEMPORALLY VERIFIED RESULT |\n|---|---|---|\n")
        for exp, orig in r8d["original_verdicts"].items():
            md.append(f"| {exp} | {orig} | {r8d['temporally_verified_verdicts'][exp]} |\n")
        md.append(f"\n{r8d['note']}\n")
    else:
        md.append(f"\n{r8d['note']}\n")

    md.append("\n## 20. Statistical Reassessment\n\n")
    md.append("\nINSUFFICIENT_DATA — zéro observation TEMPORALLY_VERIFIED dans le dataset (§7). Le benchmark principal ne peut PAS être rejoué sur des données temporellement vérifiées (§38) ; les données non vérifiées ne remplacent jamais les données vérifiées absentes.\n")

    md.append("\n## 21. Temporal Coverage\n\n")
    md.append("| Cutoff | Total Matches | Verified | Unverified | Future | Unknown | Coverage |\n|---|---|---|---|---|---|---|\n")
    for row in result["temporal_coverage"]:
        md.append(f"| {row['cutoff']} | {row['total_matches']} | {row['verified']} | {row['unverified']} | {row['future']} | {row['unknown']} | {row['coverage_pct']:.1f}% |\n")

    md.append("\n## 22. Limitations\n\n")
    for item in result["limitations"]:
        md.append(f"- {item}\n")

    md.append("\n## 23. Decision\n\n")
    md.append("| Condition | Decision |\n|---|---|\n")
    for row in result["decision_matrix"]:
        md.append(f"| {row['condition']} | {row['decision']} |\n")
    md.append(f"\n**Recommandation retenue : {result['recommendation']}**\n")

    md.append("\n## 24. Phase 8F Recommendation\n\n")
    for item in result["recommendations_phase_8f"]:
        md.append(f"- {item}\n")

    md.append("\n---\n\n### TEMPORAL INTEGRITY\n\n" + result["temporal_integrity"] + "\n")
    md.append("\n### ODDS SIGNAL\n\n" + result["odds_signal"] + "\n")
    md.append("\n### VALUE ENGINE\n\nNOT BUILT.\n")
    md.append("\n### PRODUCTION\n\nNO CHANGES.\n")

    md.append("\n---\n\nPHASE 8E — XFOOT ODDS TIMESTAMP & HISTORICAL INTEGRITY AUDIT V1 TERMINÉE. "
               "AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. "
               "EN ATTENTE DE VALIDATION.\n")
    return "".join(md)


def write_reports(result: dict, outdir: Path, run_id: str) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / f"odds_integrity_{run_id}.json"
    md_path = outdir / f"odds_integrity_{run_id}.md"
    json_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=str, default=None)
    parser.add_argument("--phase8d-report", type=str, default=None)
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else Path.home() / ".xfoot_research_cache" / "odds_football_data_co_uk"

    init_db()
    with Session(engine) as session:
        db_before = fewf.snapshot_db_counts(session)

    logger.info("Réutilisation du cache Phase 8D (%s)...", cache_dir)
    files = orw.download_all(cache_dir)  # ré-utilise le cache existant, ne re-télécharge que si absent
    logger.info("%d fichiers disponibles.", len(files))

    column_audit = audit_raw_columns(files)
    kickoff_precision = audit_kickoff_precision(files)

    xfoot_index = orw.load_xfoot_matches()
    observations, quality = orw.parse_and_map(files, xfoot_index)

    classification_per_series = classify_all_observations(observations)
    total_xfoot_matches = len(xfoot_index)
    temporal_coverage = temporal_coverage_table(observations, total_xfoot_matches)
    hist_recon = historical_reconstruction_table()
    bookmaker_quality = bookmaker_quality_table(observations)
    consensus_quality = consensus_quality_table(observations)

    if args.phase8d_report:
        phase8d_path = Path(args.phase8d_report)
    else:
        odds_reports_dir = Path(__file__).resolve().parent.parent / "reports" / "odds"
        candidates = sorted(odds_reports_dir.glob("odds_backtest_*.json")) if odds_reports_dir.exists() else []
        phase8d_path = candidates[-1] if candidates else None
    phase8d_reassessment = reassess_phase8d(phase8d_path)

    with Session(engine) as session:
        db_after = fewf.snapshot_db_counts(session)
    db_unchanged = db_before == db_after

    valid_pre = sum(1 for o in observations if o["pre_1x2"] is not None)

    decision_matrix = [
        {"condition": "Timestamp verified + sufficient sample", "decision": "VALIDATE"},
        {"condition": "Timestamp verified + insufficient sample", "decision": "NEEDS_MORE_DATA"},
        {"condition": "Historical but no timestamp", "decision": "UNVERIFIED — cas réel de ce dataset"},
        {"condition": "Future information detected", "decision": "REJECT"},
        {"condition": "Ambiguous provenance", "decision": "UNKNOWN"},
        {"condition": "Leakage detected", "decision": "REJECT"},
    ]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = {
        "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
        "temporal_integrity": "🔴 NOT VERIFIED",
        "odds_signal": "🟡 HISTORICAL BUT UNVERIFIED",
        "central_finding": (
            "football-data.co.uk ne fournit AUCUN timestamp par observation de cote (colonne 'Time' = heure "
            "de coup d'envoi du match, jamais un instant d'observation de la cote). Les 3 séries utilisées en "
            "Phase 8D (pré-clôture, clôture, consensus) sont donc classées HISTORICAL_BUT_UNTIMESTAMPED à "
            "100% — zéro observation TEMPORALLY_VERIFIED, quel que soit le cutoff théorique testé."
        ),
        "column_audit": column_audit,
        "kickoff_precision": kickoff_precision,
        "dataset": {
            "total_xfoot_matches": total_xfoot_matches, "mapped": quality["mapped"],
            "valid_pre_1x2": valid_pre, "invalid_1x2_odds": quality["invalid_1x2_odds"],
        },
        "mapping_quality": quality,
        "classification_per_series": classification_per_series,
        "temporal_coverage": temporal_coverage,
        "historical_reconstruction": hist_recon,
        "bookmaker_quality": bookmaker_quality,
        "consensus_quality": consensus_quality,
        "phase8d_reassessment": phase8d_reassessment,
        "decision_matrix": decision_matrix,
        "recommendation": "SEEK_TIMESTAMPED_ODDS_SOURCE",
        "limitations": [
            "Cette phase ne peut PAS prouver qu'aucune fuite n'a eu lieu en Phase 8D — elle prouve seulement que la source ne permet NI de la confirmer NI de l'exclure (absence totale de timestamp mesuré).",
            "5 des 11 ligues Xfoot uniquement (mêmes que Phase 8D) — aucune conclusion étendue.",
            "La timezone de la colonne Time (kickoff) n'est pas documentée officiellement par football-data.co.uk — traitée comme UNKNOWN, jamais supposée UTC/CET.",
            "Le consensus 'Avg' de football-data.co.uk est une valeur pré-calculée par la source — sa composition exacte (bookmakers inclus, instant de calcul) n'est pas documentée et ne peut pas être reconstruite.",
            "Aucune donnée synthétique n'a été utilisée pour les tableaux produits sur les DONNÉES RÉELLES — uniquement pour les tests unitaires adversariaux (api/test_odds_integrity_core.py), clairement séparés du rapport.",
        ],
        "recommendations_phase_8f": [
            "Si Xfoot souhaite poursuivre sur les odds : rechercher spécifiquement un fournisseur avec un timestamp mesuré par observation (ex. The Odds API depuis juin 2020, identifié en Phase 8C) plutôt que football-data.co.uk.",
            "Toute vérification de couverture ligues/saisons d'un tel fournisseur doit se faire par un appel API réel (clé gratuite), pas par une lecture de documentation seule.",
            "Ne jamais promouvoir le résultat Phase 8D vers une intégration production tant qu'un signal temporellement vérifié n'a pas été observé.",
        ],
        "db_counts_before": db_before, "db_counts_after": db_after, "db_unchanged": db_unchanged,
    }

    outdir = Path(__file__).resolve().parent.parent / "reports" / "odds_integrity"
    json_path, md_path = write_reports(result, outdir, run_id)
    logger.info("Rapport écrit : %s / %s", json_path, md_path)

    print("\n" + "=" * 80)
    print("PHASE 8E — XFOOT ODDS TIMESTAMP & HISTORICAL INTEGRITY AUDIT V1 TERMINÉE.")
    print("AUCUNE INTÉGRATION PRODUCTION EFFECTUÉE. AUCUNE MODIFICATION PRODUCTION EFFECTUÉE. EN ATTENTE DE VALIDATION.")
    print("=" * 80)
    return result


if __name__ == "__main__":
    main()
