"""
README figures, rendered from the pipeline's own output.

    python docs/make_figures.py

Reads results/metrics.json, results/threshold_sweep.csv and the scored
artefact. Nothing here is drawn by hand or adjusted for presentation -- if a
chart looks bad, the pipeline produced something bad, which is the point.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402

INK = "#12243b"
ACCENT = "#3f5f8f"
WARN = "#b3261e"
GOOD = "#1f6f43"
MUTED = "#8a8f98"

plt.rcParams.update(
    {
        "figure.dpi": 130,
        "savefig.dpi": 130,
        "font.size": 9,
        "axes.edgecolor": MUTED,
        "axes.labelcolor": INK,
        "text.color": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.18,
        "grid.linewidth": 0.6,
    }
)


def _load():
    metrics = json.loads(cfg.METRICS_PATH.read_text(encoding="utf-8"))
    sweep = pd.read_csv(cfg.SWEEP_PATH)
    scored = pd.read_csv(cfg.SCORED_PATH, keep_default_na=False)
    return metrics, sweep, scored


def precision_recall(metrics, sweep, path):
    figure, axis = plt.subplots(figsize=(6.2, 4.0))

    axis.plot(sweep["entity_recall"], sweep["entity_precision"],
              color=ACCENT, lw=2, label="Layered system (entity level)")
    axis.plot(sweep["txn_recall"], sweep["txn_precision"],
              color=ACCENT, lw=1.2, ls="--", alpha=0.65,
              label="Layered system (transaction level)")

    operating = metrics["operating_point"]["entity_level"]
    axis.scatter([operating["recall"]], [operating["precision"]], s=70, zorder=5,
                 color=GOOD, edgecolor="white", lw=1.2,
                 label=f"Operating point (threshold {metrics['operating_point']['threshold']})")

    baseline = metrics["naive_baseline"]["entity_level"]
    axis.scatter([baseline["recall"]], [baseline["precision"]], s=70, zorder=5,
                 marker="X", color=WARN, edgecolor="white", lw=1.2,
                 label="Naive >$10,000 rule")

    axis.axhline(metrics["ranking"]["prevalence"], color=MUTED, ls=":", lw=1)
    axis.text(0.02, metrics["ranking"]["prevalence"] + 0.015,
              f"random guessing ({metrics['ranking']['prevalence']:.2%})",
              fontsize=7, color=MUTED)

    axis.set_xlabel("Recall")
    axis.set_ylabel("Precision")
    axis.set_xlim(-0.02, 1.02)
    axis.set_ylim(-0.02, 1.02)
    axis.set_title(
        f"Precision-recall  |  PR-AUC {metrics['ranking']['pr_auc']:.3f} "
        f"(95% CI {metrics['confidence_intervals_95']['pr_auc']['ci_low']:.3f}"
        f"-{metrics['confidence_intervals_95']['pr_auc']['ci_high']:.3f})",
        fontsize=10, loc="left",
    )
    axis.legend(frameon=False, fontsize=7.5, loc="upper right")
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)


def cost_curve(metrics, sweep, path):
    figure, axis = plt.subplots(figsize=(6.2, 4.0))

    axis.plot(sweep["threshold"], sweep["entity_expected_cost_cad"],
              color=ACCENT, lw=1.8, label="Layered system")

    baseline_cost = metrics["naive_baseline"]["entity_level"]["expected_cost_cad"]
    axis.axhline(baseline_cost, color=WARN, ls="--", lw=1.4,
                 label=f"Naive >$10,000 rule (${baseline_cost:,.0f})")

    threshold = metrics["operating_point"]["threshold"]
    operating_cost = metrics["operating_point"]["entity_level"]["expected_cost_cad"]
    axis.scatter([threshold], [operating_cost], s=70, zorder=5, color=GOOD,
                 edgecolor="white", lw=1.2)
    axis.annotate(
        f"threshold {threshold}\n${operating_cost:,.0f}",
        xy=(threshold, operating_cost), xytext=(threshold + 6, operating_cost * 2.4),
        fontsize=7.5, color=GOOD,
        arrowprops=dict(arrowstyle="-", color=GOOD, lw=0.8),
    )

    axis.set_yscale("log")
    axis.set_xlabel("Risk score threshold")
    axis.set_ylabel("Expected cost, CAD (log scale)")
    axis.set_title(
        "Expected cost by threshold  |  cost constants are illustrative, see config.py",
        fontsize=10, loc="left",
    )
    axis.legend(frameon=False, fontsize=7.5)
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)


def model_bakeoff(metrics, path):
    figure, axis = plt.subplots(figsize=(6.2, 3.4))
    rows = sorted(metrics["model_bakeoff"], key=lambda r: r["pr_auc"])

    labels = [r["model"].replace("_", " ") for r in rows]
    values = [r["pr_auc"] for r in rows]
    colours = [MUTED if r["supervised"] else ACCENT for r in rows]

    bars = axis.barh(labels, values, color=colours, height=0.62)
    for bar, row in zip(bars, rows, strict=True):
        axis.text(bar.get_width() + 0.008, bar.get_y() + bar.get_height() / 2,
                  f"{row['pr_auc']:.3f}  ({row['fit_score_seconds']:.1f}s)",
                  va="center", fontsize=7.5, color=INK)

    axis.axvline(metrics["ranking"]["prevalence"], color=WARN, ls=":", lw=1)
    axis.set_xlim(0, max(values) * 1.35)
    axis.set_xlabel("PR-AUC (pooled out-of-fold)")
    axis.set_title(
        "Model bake-off  |  blue = unsupervised (shippable), grey = supervised ceiling",
        fontsize=10, loc="left",
    )
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)


def typology_recall(metrics, path):
    figure, axis = plt.subplots(figsize=(6.2, 3.4))
    data = metrics["typology_recall"]
    names = sorted(data)
    positions = np.arange(len(names))

    case_recall = [data[n]["case_recall"] for n in names]
    txn_recall = [data[n]["transaction_recall"] for n in names]

    axis.barh(positions + 0.19, case_recall, height=0.34, color=ACCENT, label="Case level")
    axis.barh(positions - 0.19, txn_recall, height=0.34, color=MUTED, label="Transaction level")

    for index, name in enumerate(names):
        axis.text(case_recall[index] + 0.012, index + 0.19,
                  f"{data[name]['cases_caught']}/{data[name]['cases']}",
                  va="center", fontsize=7.5, color=INK)

    axis.set_yticks(positions)
    axis.set_yticklabels([n.replace("_", " ") for n in names])
    axis.set_xlim(0, 1.15)
    axis.set_xlabel("Recall")
    axis.set_title(
        f"Recall by typology at threshold {metrics['operating_point']['threshold']}",
        fontsize=10, loc="left",
    )
    axis.legend(frameon=False, fontsize=7.5, loc="lower right")
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)


def structuring_windows(metrics, scored, path):
    """
    The failure, drawn.

    Each row is one structuring case, plotted as deposits against days from the
    first. The shaded band is R1's seven-day window. A case is caught when three
    deposits fall inside some seven-day span, and the three that escape are the
    ones that spread AND kept the count low.
    """
    windows = {w["case_id"]: w for w in metrics["failure_analysis"]["structuring_windows"]}
    rows = scored[scored["typology"] == "structuring"]

    ordered = sorted(
        windows.values(), key=lambda w: (w["variant"], not w["r1_did_fire"], w["span_days"])
    )
    figure, axis = plt.subplots(figsize=(6.6, 4.2))

    for index, window in enumerate(ordered):
        case = rows[rows["case_id"] == window["case_id"]]
        days = (case["txn_epoch"] - case["txn_epoch"].min()) / 86_400.0
        caught = window["r1_did_fire"]
        axis.scatter(days, [index] * len(days), s=42, zorder=4,
                     color=GOOD if caught else WARN, edgecolor="white", lw=0.8)
        axis.hlines(index, 0, max(days.max(), cfg.R1_WINDOW_DAYS),
                    color=MUTED, lw=0.6, alpha=0.5, zorder=1)

    axis.axvspan(0, cfg.R1_WINDOW_DAYS, color=ACCENT, alpha=0.10, zorder=0)
    # Label below the rows rather than above them; at the top it collides with
    # the title.
    axis.set_ylim(-1.15, len(ordered) - 0.35)
    axis.text(cfg.R1_WINDOW_DAYS / 2, -0.85, "R1's 7-day window",
              ha="center", fontsize=7.5, color=ACCENT)

    axis.set_yticks(range(len(ordered)))
    axis.set_yticklabels(
        [f"{w['case_id']}  ({w['variant']}, {w['deposits']}x)" for w in ordered],
        fontsize=7,
    )
    axis.set_xlabel("Days from the first deposit")
    axis.set_title(
        "Why R1 misses three cases  |  green = caught, red = missed",
        fontsize=10, loc="left",
    )
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)


def main() -> None:
    cfg.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    metrics, sweep, scored = _load()

    figures = {
        "pr_curve.png": lambda p: precision_recall(metrics, sweep, p),
        "cost_curve.png": lambda p: cost_curve(metrics, sweep, p),
        "model_bakeoff.png": lambda p: model_bakeoff(metrics, p),
        "typology_recall.png": lambda p: typology_recall(metrics, p),
        "structuring_windows.png": lambda p: structuring_windows(metrics, scored, p),
    }
    for name, draw in figures.items():
        path = cfg.DOCS_DIR / name
        draw(path)
        print(f"  docs/{name}  ({path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
