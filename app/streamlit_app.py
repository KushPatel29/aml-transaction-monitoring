"""
Transaction monitoring triage console.

    streamlit run app/streamlit_app.py

Reads the committed pipeline artefacts, so it starts cold without running
anything (C5) and shows exactly the numbers the README quotes.

Two structural rules this file keeps to, both enforced by
tests/test_dashboard_masks_identifiers.py:

  * Every table on screen goes through `show_table`, which masks party
    identifiers first. There is no other route to st.dataframe, so a new panel
    cannot accidentally render raw identifiers.

  * The data-shaping functions are pure -- they take frames and return frames,
    touching no Streamlit API -- so the tests can call them directly and check
    what would have been rendered.
"""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import matplotlib
import numpy as np
import pandas as pd
import streamlit as st

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import networkx as nx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
from src import cost_model, masking, risk_scorer  # noqa: E402

QUEUE_COLUMNS = [
    "entity_id", "display_name", "entity_type", "segment", "risk_score",
    "typology_guess", "rules_fired", "transactions_flagged", "amount_involved_cad",
    "reason_codes",
]

RULE_TO_TYPOLOGY = {
    "R1_STRUCTURING": "Structuring",
    "R2_LAYERING": "Rapid layering",
    "R3_SMURFING": "Smurfing",
    "R4_ROUND_DOLLAR": "Round-dollar anomaly",
    "R5_DORMANT": "Dormant reactivation",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading scored transactions...")
def load_scored() -> pd.DataFrame:
    return pd.read_csv(cfg.SCORED_PATH, keep_default_na=False)


@st.cache_data
def load_entities() -> pd.DataFrame:
    return pd.read_csv(cfg.ENTITIES_PATH, keep_default_na=False)


@st.cache_data
def load_metrics() -> dict:
    import json

    return json.loads(cfg.METRICS_PATH.read_text(encoding="utf-8"))


@st.cache_data
def load_sweep(_scored: pd.DataFrame) -> pd.DataFrame:
    return risk_scorer.sweep(
        _scored["risk_score"].to_numpy(),
        _scored["is_suspicious"].to_numpy(),
        _scored["entity_id"].to_numpy(),
    )


# ---------------------------------------------------------------------------
# Pure data shaping -- no Streamlit calls below this line until render()
# ---------------------------------------------------------------------------
def infer_typology(rule_flags: dict[str, bool]) -> str:
    """
    What the alert looks like, from the rules that fired.

    An alert with no rule behind it is labelled honestly rather than being
    assigned the nearest typology: the model found it unusual and cannot say
    which pattern it resembles.
    """
    hits = [RULE_TO_TYPOLOGY[code] for code, fired in rule_flags.items() if fired]
    if not hits:
        return "Unclassified (model only)"
    return " + ".join(hits)


def build_case_queue(scored: pd.DataFrame, entities: pd.DataFrame,
                     threshold: float) -> pd.DataFrame:
    """
    One row per alerted entity -- the unit an analyst actually works.

    Consolidating here rather than listing flagged transactions is the same
    correction the cost model makes: an entity with six flagged rows is one
    investigation, not six.
    """
    flagged = scored[scored["risk_score"] >= threshold]
    if flagged.empty:
        return pd.DataFrame(columns=QUEUE_COLUMNS)

    rows = []
    for entity_id, group in flagged.groupby("entity_id", sort=False):
        rule_flags = {code: bool(group[code].any()) for code in cfg.RULE_CODES}
        reasons = [r for r in group["reason_codes"].unique() if r]
        rows.append(
            {
                "entity_id": entity_id,
                "risk_score": round(float(group["risk_score"].max()), 1),
                "typology_guess": infer_typology(rule_flags),
                "rules_fired": sum(rule_flags.values()),
                "transactions_flagged": int(len(group)),
                "amount_involved_cad": round(float(group["amount_cad"].sum()), 2),
                "reason_codes": " | ".join(reasons)[:400],
            }
        )

    queue = pd.DataFrame(rows).merge(
        entities[["entity_id", "display_name", "entity_type", "segment"]],
        on="entity_id", how="left",
    )
    return queue.sort_values("risk_score", ascending=False)[QUEUE_COLUMNS]


def filter_queue(queue: pd.DataFrame, entity_types: list[str], typologies: list[str],
                 min_amount: float) -> pd.DataFrame:
    filtered = queue
    if entity_types:
        filtered = filtered[filtered["entity_type"].isin(entity_types)]
    if typologies:
        filtered = filtered[
            filtered["typology_guess"].apply(lambda g: any(t in g for t in typologies))
        ]
    return filtered[filtered["amount_involved_cad"] >= min_amount]


def build_entity_timeline(scored: pd.DataFrame, entity_id: str) -> pd.DataFrame:
    columns = [
        "txn_datetime", "counterparty_id", "direction", "channel", "amount_cad",
        "risk_score", "n_rules_fired", "reason_codes",
    ]
    rows = scored[scored["entity_id"] == entity_id].sort_values("txn_epoch")
    return rows[columns].reset_index(drop=True)


def build_feature_profile(scored: pd.DataFrame, entity_id: str) -> pd.DataFrame:
    """
    The selected entity's peak-risk transaction against the population, so the
    drill-down answers "why is this here" with numbers rather than a score.
    """
    rows = scored[scored["entity_id"] == entity_id]
    if rows.empty:
        return pd.DataFrame(columns=["feature", "this_transaction", "population_mean",
                                     "deviation_sigma"])
    peak = rows.loc[rows["risk_score"].idxmax()]

    population = scored[cfg.FEATURE_COLUMNS]
    means = population.mean()
    stds = population.std(ddof=0).replace(0, np.nan)
    # A row sliced out of a mixed-dtype frame comes back as object dtype, and
    # arithmetic on it silently downcasts. Coerce first.
    values = peak[cfg.FEATURE_COLUMNS].astype(float)
    deviation = ((values - means) / stds).fillna(0.0)

    profile = pd.DataFrame(
        {
            "feature": cfg.FEATURE_COLUMNS,
            "this_transaction": values.to_numpy(),
            "population_mean": means.to_numpy().round(3),
            "deviation_sigma": deviation.to_numpy().round(2),
        }
    )
    return profile.reindex(
        profile["deviation_sigma"].abs().sort_values(ascending=False).index
    ).reset_index(drop=True)


def build_counterparty_network(scored: pd.DataFrame, entity_id: str,
                               max_nodes: int = 45) -> nx.Graph:
    """
    The selected entity, its counterparties, and any other monitored entity
    sharing them.

    Shared counterparties are how these networks actually show themselves. The
    caveat, stated on the page as well as here: employers and utilities are
    legitimately shared by hundreds of entities, so a dense picture is not by
    itself evidence of anything.
    """
    own = scored[(scored["entity_id"] == entity_id) & (scored["counterparty_id"] != "")]
    counterparties = set(own["counterparty_id"].unique())
    if not counterparties:
        return nx.Graph()

    neighbourhood = scored[scored["counterparty_id"].isin(counterparties)]
    degree = neighbourhood.groupby("counterparty_id")["entity_id"].nunique()
    # Keep the counterparties that connect this entity to a few others, not the
    # hubs that connect it to everyone.
    interesting = degree[degree.between(2, 12)].index.tolist()
    keep = (interesting or list(counterparties))[: max_nodes // 3]

    graph = nx.Graph()
    graph.add_node(entity_id, kind="subject")
    for counterparty in keep:
        graph.add_node(counterparty, kind="counterparty")
        graph.add_edge(entity_id, counterparty)
        others = neighbourhood.loc[
            neighbourhood["counterparty_id"] == counterparty, "entity_id"
        ].unique()
        for other in others:
            if other == entity_id or graph.number_of_nodes() >= max_nodes:
                continue
            graph.add_node(other, kind="peer")
            graph.add_edge(other, counterparty)
    return graph


def baseline_comparison_frame(scored: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """C8, side by side, at whatever threshold the slider is on."""
    y_true = scored["is_suspicious"].to_numpy()
    entity_ids = scored["entity_id"].to_numpy()

    layered = cost_model.evaluate(
        y_true, scored["risk_score"].to_numpy() >= threshold, entity_ids
    )["entity_level"]
    naive = cost_model.evaluate(
        y_true,
        cost_model.naive_baseline_flags(scored["amount_cad"].to_numpy()),
        entity_ids,
    )["entity_level"]

    return pd.DataFrame(
        {
            "measure": ["Entities alerted", "Cases caught", "Cases missed",
                        "Precision", "Recall", "Expected cost (CAD)",
                        "Alerts per week"],
            "Layered system": [
                layered["alerts"], layered["true_positives"], layered["false_negatives"],
                f"{layered['precision']:.3f}", f"{layered['recall']:.3f}",
                f"${layered['expected_cost_cad']:,.0f}",
                f"{layered['alerts_per_week']:.1f}",
            ],
            f"Naive >${cfg.NAIVE_BASELINE_THRESHOLD_CAD:,.0f} rule": [
                naive["alerts"], naive["true_positives"], naive["false_negatives"],
                f"{naive['precision']:.3f}", f"{naive['recall']:.3f}",
                f"${naive['expected_cost_cad']:,.0f}",
                f"{naive['alerts_per_week']:.1f}",
            ],
        }
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def escape_markdown(text: object) -> str:
    """
    Neutralise dollar signs before they reach st.markdown or st.caption.

    Streamlit treats `$...$` as inline LaTeX, so a reason code carrying two
    currency amounts loses both dollar signs and renders the text between them
    as maths: "11 senders paid in 10,995.55 ... (avg 999.60)". The reason
    strings themselves stay clean -- they go into CSV and JSON as well -- so
    the escaping belongs here at the render boundary.
    """
    return str(text).replace("$", r"\$")


def show_table(frame: pd.DataFrame, **kwargs) -> None:
    """
    The ONLY route to a table on screen.

    Everything is masked on the way through, so no panel can render a party
    identifier in full by forgetting to. tests/test_dashboard_masks_identifiers
    asserts this function is the sole caller of st.dataframe in the file.
    """
    st.dataframe(masking.mask_frame(frame), use_container_width=True, **kwargs)


def render_disclaimer() -> None:
    """C1 -- present on every run, above everything else."""
    st.warning(cfg.DISCLAIMER, icon=":material/science:")


def render_network(graph: nx.Graph, entity_id: str):
    figure, axis = plt.subplots(figsize=(7, 4.5))
    if graph.number_of_nodes() == 0:
        axis.text(0.5, 0.5, "No counterparty activity", ha="center", va="center")
        axis.axis("off")
        return figure

    layout = nx.spring_layout(graph, seed=cfg.RANDOM_SEED, k=0.6)
    styles = {
        "subject": ("#b3261e", 420),
        "counterparty": ("#6750a4", 130),
        "peer": ("#7d8590", 160),
    }
    for kind, (colour, size) in styles.items():
        nodes = [n for n, d in graph.nodes(data=True) if d.get("kind") == kind]
        nx.draw_networkx_nodes(graph, layout, nodelist=nodes, node_color=colour,
                               node_size=size, ax=axis, alpha=0.9)
    nx.draw_networkx_edges(graph, layout, ax=axis, alpha=0.3, width=0.9)
    nx.draw_networkx_labels(
        graph, layout,
        labels={n: masking.mask_identifier(n) for n in graph.nodes()},
        font_size=6, ax=axis,
    )
    axis.set_title(
        f"{masking.mask_identifier(entity_id)} and entities sharing its counterparties",
        fontsize=9,
    )
    axis.axis("off")
    figure.tight_layout()
    return figure


def main() -> None:
    st.set_page_config(page_title="Transaction Monitoring Triage",
                       page_icon=":material/policy:", layout="wide")

    if not cfg.SCORED_PATH.exists():
        render_disclaimer()
        st.error(
            "No scored data found. Run `python run_pipeline.py` to build "
            "`results/scored_transactions.csv.gz`."
        )
        return

    scored = load_scored()
    entities = load_entities()
    metrics = load_metrics()
    sweep = load_sweep(scored)
    default_threshold = float(metrics["operating_point"]["threshold"])

    st.title("Transaction Monitoring - Triage Console")
    render_disclaimer()

    # ---- sidebar ----------------------------------------------------------
    with st.sidebar:
        st.header("Alerting")
        threshold = st.slider(
            "Risk score threshold", 0.0, 100.0, default_threshold, 0.1,
            help="Cost-minimising threshold from the last pipeline run is "
                 f"{default_threshold}.",
        )
        st.caption(escape_markdown(
            f"Cost model is illustrative: ${cfg.COST_PER_INVESTIGATION_CAD:,.2f} per "
            f"investigation, ${cfg.PROXY_COST_PER_MISSED_CASE_CAD:,.0f} per missed "
            "case. Both invented -- see config.py."
        ))

        st.header("Queue filters")
        entity_types = st.multiselect("Entity type", sorted(entities["entity_type"].unique()))
        typologies = st.multiselect("Typology", sorted(set(RULE_TO_TYPOLOGY.values())))
        min_amount = st.number_input("Minimum amount involved (CAD)", 0.0, value=0.0, step=1_000.0)

        st.header("Capacity")
        headcount = st.slider("Analysts", 1, 20, cfg.ANALYST_HEADCOUNT)
        capacity = headcount * cfg.ANALYST_HOURS_PER_WEEK / cfg.INVESTIGATION_HOURS_PER_ALERT
        st.caption(f"{capacity:.0f} investigations per week at "
                   f"{cfg.INVESTIGATION_HOURS_PER_ALERT}h each.")

        show_baseline = st.toggle("Compare against the naive $10k rule", value=True)

    # ---- headline ---------------------------------------------------------
    at_threshold = sweep.iloc[(sweep["threshold"] - threshold).abs().idxmin()]
    columns = st.columns(5)
    columns[0].metric("Entities alerted", f"{int(at_threshold['entity_alerts']):,}")
    columns[1].metric("Precision", f"{at_threshold['entity_precision']:.3f}")
    columns[2].metric("Recall", f"{at_threshold['entity_recall']:.3f}")
    columns[3].metric("Expected cost",
                      f"${at_threshold['entity_expected_cost_cad']:,.0f}")
    alerts_per_week = at_threshold["entity_alerts"] / 52
    columns[4].metric(
        "Alerts / week", f"{alerts_per_week:.1f}",
        delta=f"{capacity - alerts_per_week:+.0f} vs capacity",
        delta_color="normal" if alerts_per_week <= capacity else "inverse",
    )

    queue_tab, performance_tab, entity_tab, method_tab = st.tabs(
        ["Case queue", "Performance", "Entity drill-down", "Method"]
    )

    # ---- case queue -------------------------------------------------------
    with queue_tab:
        queue = filter_queue(
            build_case_queue(scored, entities, threshold),
            entity_types, typologies, min_amount,
        )
        st.subheader(f"{len(queue):,} entities above a risk score of {threshold:g}")
        if queue.empty:
            st.info("No entities match the current threshold and filters.")
        else:
            show_table(queue, height=460, hide_index=True)
            st.caption(
                "Identifiers are masked to the last four characters. Case and "
                "transaction references are shown in full -- they identify an "
                "event, not a person."
            )

        if show_baseline:
            st.subheader("Against the naive baseline")
            show_table(baseline_comparison_frame(scored, threshold), hide_index=True)

    # ---- performance ------------------------------------------------------
    with performance_tab:
        left, right = st.columns(2)

        with left:
            st.subheader("Precision-recall")
            curve = sweep[["entity_recall", "entity_precision", "threshold"]]
            chart = (
                alt.Chart(curve)
                .mark_line(color="#6750a4")
                .encode(
                    x=alt.X("entity_recall:Q", title="Recall (cases caught)"),
                    y=alt.Y("entity_precision:Q", title="Precision"),
                    tooltip=["threshold:Q", "entity_precision:Q", "entity_recall:Q"],
                )
                .properties(height=320)
            )
            st.altair_chart(chart, use_container_width=True)
            st.caption(
                f"Entity level. PR-AUC at transaction level is "
                f"{metrics['ranking']['pr_auc']:.3f} "
                f"(95% CI {metrics['confidence_intervals_95']['pr_auc']['ci_low']:.3f}"
                f"-{metrics['confidence_intervals_95']['pr_auc']['ci_high']:.3f})."
            )

        with right:
            st.subheader("Expected cost by threshold")
            cost_chart = (
                alt.Chart(sweep[["threshold", "entity_expected_cost_cad"]])
                .mark_line(color="#b3261e")
                .encode(
                    x=alt.X("threshold:Q", title="Risk score threshold"),
                    y=alt.Y("entity_expected_cost_cad:Q", title="Expected cost (CAD)"),
                    tooltip=["threshold:Q", "entity_expected_cost_cad:Q"],
                )
                .properties(height=320)
            )
            marker = (
                alt.Chart(pd.DataFrame({"threshold": [threshold]}))
                .mark_rule(color="#1f6f43", strokeDash=[5, 4])
                .encode(x="threshold:Q")
            )
            st.altair_chart(cost_chart + marker, use_container_width=True)
            st.caption(
                "The curve steps wherever a case crosses the threshold, which is "
                "why the sweep runs at 0.1 resolution -- an integer grid reported "
                "an optimum 19% more expensive than the real one."
            )

        st.subheader("Does each component earn its weight?")
        show_table(pd.DataFrame(metrics["ablation"]), hide_index=True)
        st.caption(
            "Rules alone rank at PR-AUC "
            f"{metrics['ablation'][0]['pr_auc']:.3f}, the model alone "
            f"{metrics['ablation'][1]['pr_auc']:.3f}, the blend "
            f"{metrics['ablation'][2]['pr_auc']:.3f}. The model earns its weight "
            "on ranking order rather than on the accept/reject decision."
        )

        st.subheader("Model bake-off (5-fold entity-disjoint CV)")
        show_table(pd.DataFrame(metrics["model_bakeoff"]), hide_index=True)

    # ---- drill-down -------------------------------------------------------
    with entity_tab:
        queue = build_case_queue(scored, entities, threshold)
        if queue.empty:
            st.info("No entities above the current threshold.")
        else:
            options = queue["entity_id"].tolist()
            selected = st.selectbox(
                "Entity", options,
                format_func=lambda e: (
                    f"{masking.mask_identifier(e)} - risk "
                    f"{queue.loc[queue['entity_id'] == e, 'risk_score'].iloc[0]:.1f}"
                ),
            )
            row = queue[queue["entity_id"] == selected].iloc[0]

            detail = st.columns(4)
            detail[0].metric("Risk score", f"{row['risk_score']:.1f}")
            detail[1].metric("Rules fired", int(row["rules_fired"]))
            detail[2].metric("Flagged transactions", int(row["transactions_flagged"]))
            detail[3].metric("Amount involved", f"${row['amount_involved_cad']:,.0f}")

            if row["reason_codes"]:
                for reason in row["reason_codes"].split(" | "):
                    st.markdown(f"- {escape_markdown(reason)}")
            else:
                st.markdown(
                    "- No rule fired. This entity is here on the anomaly model's "
                    "score alone, which means there is no typology to name yet."
                )

            st.subheader("Transaction timeline")
            timeline = build_entity_timeline(scored, selected)
            spark = (
                alt.Chart(timeline)
                .mark_circle()
                .encode(
                    x=alt.X("txn_datetime:T", title=None),
                    y=alt.Y("amount_cad:Q", title="Amount (CAD)", scale=alt.Scale(type="symlog")),
                    color=alt.Color("risk_score:Q", scale=alt.Scale(scheme="orangered")),
                    size=alt.Size("n_rules_fired:Q", legend=None),
                    tooltip=["txn_datetime:T", "amount_cad:Q", "direction:N",
                             "channel:N", "risk_score:Q"],
                )
                .properties(height=240)
            )
            st.altair_chart(spark, use_container_width=True)
            show_table(timeline, height=280, hide_index=True)

            left, right = st.columns([3, 2])
            with left:
                st.subheader("Counterparty network")
                st.pyplot(render_network(build_counterparty_network(scored, selected), selected))
                st.caption(
                    "Shows counterparties shared with 2-12 other monitored "
                    "entities. Employers and utilities are legitimately shared "
                    "by hundreds, so a dense picture is not evidence on its own."
                )
            with right:
                st.subheader("Feature profile")
                st.caption("Peak-risk transaction against the whole population.")
                show_table(build_feature_profile(scored, selected).head(8), hide_index=True)

    # ---- method -----------------------------------------------------------
    with method_tab:
        st.markdown(
            f"""
### What this is

A portfolio simulation of transaction-monitoring analytics on
**{metrics['dataset']['transactions']:,} synthetic transactions** across
**{metrics['dataset']['entities']:,} entities** over twelve months, with
**{metrics['dataset']['planted_cases']} suspicious cases** planted into
{metrics['dataset']['entities_with_a_case']} of them
({metrics['dataset']['prevalence']:.2%} of transactions).

Every figure on this page comes from `results/metrics.json`, written by
`python run_pipeline.py`. Nothing is typed in by hand.

### How the score is built

`risk = 100 x ({cfg.RULE_WEIGHT} x rule_component + {cfg.MODEL_WEIGHT} x model_component)`

The rule component saturates at {cfg.RULE_SATURATION} simultaneous hits. The
model component is an IsolationForest anomaly percentile, produced under
{cfg.N_CV_FOLDS}-fold entity-disjoint cross-validation so no entity is ever
scored by a model that trained on its own history.

### What it misses

At a conservative threshold the system misses
{len(metrics['failure_analysis']['operating_points']['conservative']['missed_cases'])}
of {metrics['dataset']['planted_cases']} cases, all of them slow structuring.
R1 needs {cfg.R1_MIN_DEPOSITS} deposits inside a seven-day window, and those
cases spread three deposits across ten days, leaving at most two in any window.
The anomaly model ranked all of them in its top 1% -- the detector saw them and
the scoring architecture discarded them, because a transaction no rule fires on
is capped at {cfg.MODEL_WEIGHT * 100:.0f} points.

### The cost numbers are invented

`${cfg.COST_PER_INVESTIGATION_CAD:,.2f}` per investigation and
`${cfg.PROXY_COST_PER_MISSED_CASE_CAD:,.0f}` per missed case are illustrative
constants in `config.py`. They are not benchmarks. The optimal threshold moves
from {metrics['cost_sensitivity'][0]['optimal_threshold']} to
{metrics['cost_sensitivity'][-1]['optimal_threshold']} across the sensitivity
range, so treat the shape of the cost curve as the finding and the dollar
figure as an illustration.
            """
        )
        show_table(pd.DataFrame(metrics["cost_sensitivity"]), hide_index=True)


if __name__ == "__main__":
    main()
