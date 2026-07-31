"""
End-to-end pipeline (C4).

    pip install -r requirements.txt && python run_pipeline.py

Regenerates the synthetic data, rebuilds the SQLite warehouse, executes the SQL
feature layer verbatim, runs the rules engine, cross-validates the anomaly
models, blends everything into a 0-100 risk score, sweeps the threshold, costs
it out against the naive baseline, and writes results/metrics.json.

Every headline number in README.md comes out of that file (C3). If a number is
in the README and not in metrics.json, it is not a number, it is a claim.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
from sklearn.metrics import average_precision_score, roc_auc_score  # noqa: E402
from src import anomaly_model, cost_model, db, risk_scorer, rules_engine  # noqa: E402


def _step(message: str) -> None:
    print(f"\n=== {message}")


def _typology_breakdown(scored: pd.DataFrame, threshold: float) -> dict:
    """Recall per typology, at transaction and at case level."""
    flagged = scored["risk_score"] >= threshold
    suspicious = scored[scored["is_suspicious"] == 1].copy()
    suspicious["flagged"] = flagged[scored["is_suspicious"] == 1].to_numpy()

    out = {}
    for typology, group in suspicious.groupby("typology"):
        by_case = group.groupby("case_id")["flagged"].any()
        out[typology] = {
            "transactions": int(len(group)),
            "transactions_flagged": int(group["flagged"].sum()),
            "transaction_recall": float(group["flagged"].mean()),
            "cases": int(len(by_case)),
            "cases_caught": int(by_case.sum()),
            "case_recall": float(by_case.mean()),
        }
    return out


def _structuring_variants(scored: pd.DataFrame, threshold: float) -> dict:
    """
    The planted stress case: does the seven-day rule window actually cost us
    the slow variant? This is the measured basis for the README's "what this
    system misses" section, not an assertion about it.
    """
    flagged = scored["risk_score"] >= threshold
    rows = scored[scored["typology"] == "structuring"].copy()
    rows["flagged"] = flagged[scored["typology"] == "structuring"].to_numpy()

    out = {}
    for variant, group in rows.groupby("typology_variant"):
        by_case = group.groupby("case_id")["flagged"].any()
        r1_by_case = group.groupby("case_id")["R1_STRUCTURING"].any()
        out[variant] = {
            "cases": int(len(by_case)),
            "transactions": int(len(group)),
            "transaction_recall": float(group["flagged"].mean()),
            "cases_caught": int(by_case.sum()),
            "case_recall": float(by_case.mean()),
            "r1_cases_caught": int(r1_by_case.sum()),
            "r1_case_recall": float(r1_by_case.mean()),
        }
    return out


def _structuring_window_analysis(scored: pd.DataFrame) -> list[dict]:
    """
    Why R1 catches some structuring cases and not others.

    For each case, the densest seven-day window is computed directly from the
    deposit timestamps and compared against R1's minimum. This turns "the rule
    window is too narrow" from an assertion into an arithmetic fact about
    specific cases, and it is where the README's failure section comes from.
    """
    rows = []
    model_percentile = scored["model_component"].rank(pct=True)
    window = cfg.R1_WINDOW_DAYS * 86_400

    for case_id, group in scored[scored["typology"] == "structuring"].groupby("case_id"):
        epochs = np.sort(group["txn_epoch"].to_numpy())
        densest = max(
            int(((epochs >= epochs[i]) & (epochs <= epochs[i] + window)).sum())
            for i in range(len(epochs))
        )
        rows.append(
            {
                "case_id": case_id,
                "variant": group["typology_variant"].iloc[0],
                "deposits": int(len(epochs)),
                "span_days": round(float((epochs[-1] - epochs[0]) / 86_400), 2),
                "densest_7day_window": densest,
                "r1_minimum_deposits": cfg.R1_MIN_DEPOSITS,
                "r1_can_fire": densest >= cfg.R1_MIN_DEPOSITS,
                "r1_did_fire": bool(group["R1_STRUCTURING"].any()),
                "best_model_percentile": round(
                    float(model_percentile.loc[group.index].max()), 4
                ),
                "max_risk_score": round(float(group["risk_score"].max()), 2),
                "any_deposit_over_naive_threshold": bool(
                    (group["amount_cad"] > cfg.NAIVE_BASELINE_THRESHOLD_CAD).any()
                ),
            }
        )
    return sorted(rows, key=lambda r: (r["variant"], r["case_id"]))


def _missed_at(scored: pd.DataFrame, threshold: float) -> list[dict]:
    caught = (
        scored[scored["is_suspicious"] == 1]
        .assign(flagged=lambda f: f["risk_score"] >= threshold)
        .groupby(["case_id", "typology", "typology_variant"])["flagged"]
        .any()
        .reset_index()
    )
    return caught[~caught["flagged"]].drop(columns="flagged").to_dict(orient="records")


def main(skip_generate: bool = False) -> dict:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    cfg.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- generate
    if not skip_generate:
        _step("Generating synthetic data")
        mark = time.perf_counter()
        sys.path.insert(0, str(cfg.ROOT / "data"))
        import generate_transactions

        generate_transactions.main()
        timings["generate_seconds"] = time.perf_counter() - mark

    transactions = pd.read_csv(cfg.TRANSACTIONS_PATH, keep_default_na=False)
    entities = pd.read_csv(cfg.ENTITIES_PATH, keep_default_na=False)
    counterparties = pd.read_csv(cfg.COUNTERPARTIES_PATH, keep_default_na=False)
    cases = pd.read_csv(cfg.CASES_PATH, keep_default_na=False)

    # ---------------------------------------------------------------- features
    _step("Building SQLite warehouse and running sql/02_features.sql verbatim")
    features, sql_timings = db.build_all()
    timings.update(sql_timings)

    # ------------------------------------------------------------------- rules
    _step("Applying rules R1-R5")
    mark = time.perf_counter()
    rule_hits = rules_engine.apply_rules(transactions, features)
    timings["rules_seconds"] = time.perf_counter() - mark
    rule_summary = rules_engine.summarise(rule_hits, transactions)
    print(rule_summary.to_string(index=False))

    # ------------------------------------------------------------------ models
    _step(f"Cross-validating models over {cfg.N_CV_FOLDS} entity-disjoint folds")
    mark = time.perf_counter()
    model_run = anomaly_model.run_models(features, transactions)
    timings["models_seconds"] = time.perf_counter() - mark

    labels_by_txn = transactions.set_index("transaction_id")["is_suspicious"]
    y_for_scores = labels_by_txn.reindex(model_run.scores["transaction_id"]).to_numpy()
    bakeoff = []
    for name in anomaly_model.ALL_MODELS:
        column = model_run.scores[f"{name}_score"].to_numpy()
        bakeoff.append(
            {
                "model": name,
                "supervised": name in anomaly_model.SUPERVISED_MODELS,
                "pr_auc": float(average_precision_score(y_for_scores, column)),
                "roc_auc": float(roc_auc_score(y_for_scores, column)),
                "fit_score_seconds": round(model_run.timings[name], 2),
            }
        )
    print(pd.DataFrame(bakeoff).to_string(index=False))

    # ------------------------------------------------------------- risk scores
    _step("Blending rules and model into a 0-100 risk score")
    blended = risk_scorer.compute_risk_scores(rule_hits, model_run.scores)

    scored = (
        transactions.merge(blended, on="transaction_id", how="inner", validate="1:1")
        .merge(
            features.drop(columns=["entity_id", "counterparty_id", "txn_epoch",
                                   "amount_cents", "direction", "is_cash"]),
            on="transaction_id", how="inner", validate="1:1",
        )
    )
    assert len(scored) == len(transactions), "scoring join changed the row count"

    risk = scored["risk_score"].to_numpy()
    y_true = scored["is_suspicious"].to_numpy()
    entity_ids = scored["entity_id"].to_numpy()
    folds = scored["fold"].to_numpy()

    # ------------------------------------------------------------------- sweep
    _step("Sweeping the alert threshold from 0 to 100")
    sweep_frame = risk_scorer.sweep(risk, y_true, entity_ids)
    sweep_frame.to_csv(cfg.SWEEP_PATH, index=False)

    entity_threshold = risk_scorer.cost_minimising_threshold(sweep_frame, level="entity")
    txn_threshold = risk_scorer.cost_minimising_threshold(sweep_frame, level="txn")
    operating = cost_model.evaluate(y_true, risk >= entity_threshold, entity_ids)
    print(
        f"  cost-minimising threshold: {entity_threshold} (entity level), "
        f"{txn_threshold} (transaction level)"
    )
    print(
        f"  at {entity_threshold}: entity precision "
        f"{operating['entity_level']['precision']:.3f}, recall "
        f"{operating['entity_level']['recall']:.3f}, "
        f"{operating['entity_level']['alerts']} alerts"
    )

    # --------------------------------------------------------------- ablation
    _step("Ablation: do the rules and the model each earn their weight?")
    ablation = []
    for variant, component in (
        ("rules_only", 100.0 * scored["rule_component"].to_numpy()),
        ("model_only", 100.0 * scored["model_component"].to_numpy()),
        ("blended", risk),
    ):
        frame = risk_scorer.sweep(component, y_true, entity_ids)
        chosen = risk_scorer.cost_minimising_threshold(frame, level="entity")
        evaluated = cost_model.evaluate(y_true, component >= chosen, entity_ids)["entity_level"]
        ablation.append(
            {
                "variant": variant,
                "pr_auc": float(average_precision_score(y_true, component)),
                "optimal_threshold": chosen,
                "entity_precision": evaluated["precision"],
                "entity_recall": evaluated["recall"],
                "entity_alerts": evaluated["alerts"],
                "entity_expected_cost_cad": evaluated["expected_cost_cad"],
            }
        )
    print(pd.DataFrame(ablation).to_string(index=False))

    # ------------------------------------------------------- honesty machinery
    _step("Nested threshold selection, bootstrap intervals, cost sensitivity")
    nested = risk_scorer.nested_threshold_check(risk, y_true, entity_ids, folds)
    intervals = risk_scorer.bootstrap_metrics(risk, y_true, entity_ids, entity_threshold)
    sensitivity = risk_scorer.cost_sensitivity(risk, y_true, entity_ids)
    ranking = risk_scorer.ranking_metrics(risk, y_true)

    # --------------------------------------------------------- naive baseline
    _step("Naive baseline: flag every transaction over $10,000 CAD")
    naive_flags = cost_model.naive_baseline_flags(scored["amount_cad"].to_numpy())
    naive = cost_model.evaluate(y_true, naive_flags, entity_ids)
    print(
        f"  baseline flags {naive['transaction_level']['alerts']:,} transactions "
        f"({naive['entity_level']['alerts']} entities), entity precision "
        f"{naive['entity_level']['precision']:.4f}, recall "
        f"{naive['entity_level']['recall']:.3f}"
    )

    # ------------------------------------------------- which cases got away
    # Two operating points, because the choice between them is entirely a
    # function of an invented constant. The cheapest-under-default-assumptions
    # point, and the conservative one you would pick if a missed case cost the
    # low end of the sensitivity range.
    conservative_threshold = min(
        sensitivity, key=lambda s: s["cost_per_missed_case_cad"]
    )["optimal_threshold"]

    failure_analysis = {
        "operating_points": {
            "cost_optimal": {
                "threshold": entity_threshold,
                "missed_cases": _missed_at(scored, entity_threshold),
            },
            "conservative": {
                "threshold": conservative_threshold,
                "chosen_when_a_missed_case_costs_cad": min(cfg.COST_SENSITIVITY_MISS_VALUES),
                "missed_cases": _missed_at(scored, conservative_threshold),
            },
        },
        "structuring_windows": _structuring_window_analysis(scored),
    }
    for label, block in failure_analysis["operating_points"].items():
        print(
            f"  cases missed at threshold {block['threshold']} ({label}): "
            f"{len(block['missed_cases'])}"
        )
        for case in block["missed_cases"]:
            print(f"    {case['case_id']}  {case['typology']} "
                  f"({case['typology_variant'] or '-'})")

    # ------------------------------------------------------------------- write
    # Six decimal places is well past anything the dashboard or the README
    # reads, and it takes several megabytes off the committed artefact.
    float_columns = scored.select_dtypes("float").columns
    scored[float_columns] = scored[float_columns].round(6)
    scored.to_csv(cfg.SCORED_PATH, index=False, compression=cfg.GZIP)

    metrics = {
        "_disclaimer": (
            "All figures below are computed from SYNTHETIC data generated by "
            "data/generate_transactions.py. This is a portfolio simulation of "
            "transaction-monitoring analytics, not a compliance product, and no "
            "real institution's data is involved. The cost constants are "
            "illustrative -- see config.py."
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "python run_pipeline.py",
        "python_version": platform.python_version(),
        "dataset": {
            "transactions": int(len(transactions)),
            "entities": int(len(entities)),
            "counterparties": int(len(counterparties)),
            "planted_cases": int(len(cases)),
            "suspicious_transactions": int(transactions["is_suspicious"].sum()),
            "prevalence": float(transactions["is_suspicious"].mean()),
            "entities_with_a_case": int(cases["entity_id"].nunique()),
            "date_range": [
                str(transactions["txn_datetime"].min()),
                str(transactions["txn_datetime"].max()),
            ],
        },
        "configuration": {
            "random_seed": cfg.RANDOM_SEED,
            "cv_folds": cfg.N_CV_FOLDS,
            "typology_entity_rate": cfg.TYPOLOGY_ENTITY_RATE,
            "structuring_slow_share": cfg.STRUCTURING_SLOW_SHARE,
            "rule_weight": cfg.RULE_WEIGHT,
            "model_weight": cfg.MODEL_WEIGHT,
            "cost_per_investigation_cad": cfg.COST_PER_INVESTIGATION_CAD,
            "proxy_cost_per_missed_case_cad": cfg.PROXY_COST_PER_MISSED_CASE_CAD,
            "cost_constants_are_illustrative": True,
        },
        "runtime_seconds": {k: round(v, 2) for k, v in timings.items()},
        "rules": rule_summary.to_dict(orient="records"),
        "model_bakeoff": bakeoff,
        "ablation": ablation,
        "failure_analysis": failure_analysis,
        "ranking": ranking,
        "operating_point": {
            "threshold": entity_threshold,
            "selected_by": "minimum expected cost at entity level",
            "transaction_level_threshold_would_be": txn_threshold,
            **operating,
        },
        "confidence_intervals_95": intervals,
        "nested_threshold_check": nested,
        "cost_sensitivity": sensitivity,
        "naive_baseline": {
            "rule": f"flag every transaction over ${cfg.NAIVE_BASELINE_THRESHOLD_CAD:,.0f} CAD",
            **naive,
        },
        "typology_recall": _typology_breakdown(scored, entity_threshold),
        "structuring_variants": _structuring_variants(scored, entity_threshold),
    }

    layered = operating["entity_level"]
    baseline = naive["entity_level"]
    metrics["baseline_comparison"] = {
        "level": "entity (one investigation per alerted entity)",
        "layered_precision": layered["precision"],
        "baseline_precision": baseline["precision"],
        "layered_recall": layered["recall"],
        "baseline_recall": baseline["recall"],
        "layered_alerts": layered["alerts"],
        "baseline_alerts": baseline["alerts"],
        "layered_expected_cost_cad": layered["expected_cost_cad"],
        "baseline_expected_cost_cad": baseline["expected_cost_cad"],
        "cost_delta_cad": baseline["expected_cost_cad"] - layered["expected_cost_cad"],
        "cost_reduction_pct": (
            (baseline["expected_cost_cad"] - layered["expected_cost_cad"])
            / baseline["expected_cost_cad"] * 100
            if baseline["expected_cost_cad"] else 0.0
        ),
    }

    timings["total_seconds"] = time.perf_counter() - started
    metrics["runtime_seconds"]["total"] = round(timings["total_seconds"], 2)

    cfg.METRICS_PATH.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    _step("Done")
    print(f"  results/metrics.json           ({cfg.METRICS_PATH.stat().st_size / 1024:.0f} KB)")
    print(f"  results/scored_transactions.csv.gz ({cfg.SCORED_PATH.stat().st_size / 1e6:.1f} MB)")
    print(f"  results/threshold_sweep.csv")
    print(f"  total {timings['total_seconds']:.1f}s")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="reuse the committed data instead of regenerating it",
    )
    args = parser.parse_args()
    main(skip_generate=args.skip_generate)
