"""
Know your customer: customer risk rating, enhanced due diligence triggers and
beneficial ownership, on the synthetic customer book.

    python -m src.kyc

Monitoring asks whether a transaction pattern is suspicious. KYC asks a prior
question: who is this customer, who stands behind them, and how much scrutiny
do they warrant? Under the PCMLTFA a reporting entity assesses each client's
risk, applies enhanced measures to high-risk clients, and for a corporation or
other entity obtains the names of everyone who directly or indirectly owns or
controls 25% or more. This module does those three things on the 1,500
synthetic entities the monitoring pipeline already uses.

The book's KYC files do not exist in the generator, so they are made here,
seeded and labelled synthetic: segment-based declared activity, a small share
of politically exposed persons, onboarding channel, adverse-media hits, and
for each business an ownership tree that can run through holding companies,
an opaque jurisdiction or, rarely, a loop. Persons are identifiers, never
names.

Two ratings per customer:

    onboarding  what the KYC file alone supports, before any activity
    refresh     the periodic review a year later: the file plus twelve months
                of activity (declared against actual, cash share, wires) and
                whether monitoring alerted

The scoring is points on stated factors, so every rating carries its reasons.
The rating policy (weights, bands, review cycle) is this project's choice, set
before looking at which customers carry planted cases; the regulator sets the
obligations, not the numbers.

Outputs: results/kyc_customer_risk.csv, results/kyc_beneficial_owners.csv and
results/kyc_summary.json. Inputs it generates: data/generated/kyc_profiles.csv.gz
and data/generated/kyc_ownership.csv.gz.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config as cfg

from . import _ROOT  # noqa: F401

PROFILES_PATH = cfg.DATA_DIR / "kyc_profiles.csv.gz"
OWNERSHIP_PATH = cfg.DATA_DIR / "kyc_ownership.csv.gz"
RISK_PATH = cfg.RESULTS_DIR / "kyc_customer_risk.csv"
UBO_PATH = cfg.RESULTS_DIR / "kyc_beneficial_owners.csv"
SUMMARY_PATH = cfg.RESULTS_DIR / "kyc_summary.json"

SEED = 4417
UBO_THRESHOLD = 0.25  # directly or indirectly owns or controls 25% or more
AS_OF = pd.Timestamp(cfg.SIM_END)

# Inherent risk of the customer's line of business (policy).
SEGMENT_RISK = {
    "salaried": 0,
    "retired": 0,
    "gig": 1,
    "self_employed": 1,
    "ecommerce": 1,
    "wholesale": 1,
    "professional_services": 2,
    "retail_cash_intensive": 3,
}
# Points per factor (policy). A foreign PEP is high risk whatever the points.
POINTS = {
    "business": 1,
    "segment": None,
    "domestic_pep_or_hio": 2,
    "non_face_to_face": 1,
    "adverse_media": 3,
    "layered_ownership": 2,
    "opaque_jurisdiction": 2,
    "no_25pct_owner": 1,
    "activity_over_3x_declared": 2,
    "activity_over_1_5x_declared": 1,
    "cash_over_half_of_inflows": 1,
    "wires_over_fifth_of_outflows": 1,
    "monitoring_alert": 2,
}
BANDS = ((6, "High"), (3, "Medium"), (0, "Low"))
REVIEW_MONTHS = {"High": 12, "Medium": 24, "Low": 36}


# ---------------------------------------------------------------- synthetic files
def make_profiles(entities: pd.DataFrame, seed: int = SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(entities)
    individual = entities["entity_type"].eq("individual").to_numpy()
    pep_draw = rng.random(n)
    pep = np.where(
        individual & (pep_draw < 0.004),
        "foreign_pep",
        np.where(individual & (pep_draw < 0.012), "domestic_pep_or_hio", "none"),
    )
    # What the customer said to expect each month, at onboarding: a payroll-led
    # figure for people, a typical-ticket figure for businesses, with the
    # optimism and vagueness real declarations carry.
    base = np.where(
        individual,
        entities["payroll_base_cad"].fillna(0) * 2.0,
        entities["typical_amount_cad"].fillna(0) * 40.0,
    )
    declared = np.round(np.maximum(base, 500.0) * rng.lognormal(0.0, 0.35, n), -2)
    return pd.DataFrame(
        {
            "entity_id": entities["entity_id"],
            "pep_status": pep,
            "onboarding_channel": np.where(rng.random(n) < 0.3, "non_face_to_face", "in_person"),
            "adverse_media": rng.random(n) < 0.01,
            "declared_monthly_inflow_cad": declared,
        }
    )


def make_ownership(entities: pd.DataFrame, seed: int = SEED) -> pd.DataFrame:
    """Edges owner -> owned, with the share owned. Businesses are owned by
    persons or by holding companies; holdings are owned in turn, up to three
    layers; a few holdings sit in an opaque jurisdiction; two loops are built
    on purpose, because real registries contain them."""
    rng = np.random.default_rng(seed + 1)
    individuals = entities.loc[entities.entity_type.eq("individual"), "entity_id"].to_numpy()
    edges: list[dict] = []
    holdings: dict[str, bool] = {}
    counter = {"person": 0, "holding": 0}

    def person() -> str:
        if rng.random() < 0.3:  # an owner who is also a customer of the bank
            return str(rng.choice(individuals))
        counter["person"] += 1
        return f"PER-{counter['person']:05d}"

    def holding() -> str:
        counter["holding"] += 1
        hid = f"HLD-{counter['holding']:04d}"
        holdings[hid] = rng.random() < 0.15
        return hid

    def owners_of(target: str, depth: int) -> None:
        k = int(rng.choice([1, 2, 3, 4, 6], p=[0.35, 0.3, 0.15, 0.12, 0.08]))
        shares = np.round(rng.dirichlet(np.ones(k) * 1.5), 4)
        shares[-1] = round(1.0 - shares[:-1].sum(), 4)
        for s in shares:
            if depth < 3 and rng.random() < 0.22:
                h = holding()
                edges.append({"owner": h, "owned": target, "share": float(s)})
                owners_of(h, depth + 1)
            else:
                edges.append({"owner": person(), "owned": target, "share": float(s)})

    for bid in entities.loc[entities.entity_type.eq("business"), "entity_id"]:
        owners_of(bid, 1)

    # Two loops: a holding that owns part of one of its own owners.
    loop_holdings = sorted(h for h in holdings if any(e["owner"] == h for e in edges))[:2]
    for h in loop_holdings:
        owned_by_h = [e["owned"] for e in edges if e["owner"] == h]
        edges.append({"owner": owned_by_h[0], "owned": h, "share": 0.1})

    frame = pd.DataFrame(edges)
    frame["owner_type"] = np.where(
        frame.owner.str.startswith("HLD-"),
        "holding",
        np.where(
            frame.owner.str.startswith("PER-") | frame.owner.isin(individuals), "person", "business"
        ),
    )
    frame["owner_opaque_jurisdiction"] = frame.owner.map(lambda o: holdings.get(o, False)).astype(
        bool
    )
    return frame


# ---------------------------------------------------------------- beneficial ownership
@dataclass
class OwnershipResult:
    owners: dict[str, float]  # person -> effective share
    paths: dict[str, list[str]]  # person -> chains through which they own
    max_depth: int
    opaque: bool
    circular: bool


def effective_ownership(target: str, edges: pd.DataFrame) -> OwnershipResult:
    """Sum, over every chain of holdings, the product of the shares. A chain
    that comes back to a company already on it is a loop: it is flagged and
    not followed, so the walk always ends."""
    by_owned: dict[str, list[tuple[str, float, str, bool]]] = defaultdict(list)
    for e in edges.itertuples(index=False):
        by_owned[e.owned].append((e.owner, e.share, e.owner_type, e.owner_opaque_jurisdiction))
    owners: dict[str, float] = defaultdict(float)
    paths: dict[str, list[str]] = defaultdict(list)
    state = {"depth": 0, "opaque": False, "circular": False}

    def walk(node: str, product: float, chain: tuple[str, ...]) -> None:
        for owner, share, kind, opaque in by_owned.get(node, []):
            if owner in chain:
                state["circular"] = True
                continue
            state["opaque"] |= bool(opaque)
            p = product * share
            if kind == "person":
                owners[owner] += p
                paths[owner].append(" <- ".join((*chain, owner)))
                state["depth"] = max(state["depth"], len(chain))
            else:
                walk(owner, p, (*chain, owner))

    walk(target, 1.0, (target,))
    return OwnershipResult(
        dict(owners), dict(paths), state["depth"], state["opaque"], state["circular"]
    )


def beneficial_owners(
    entities: pd.DataFrame, edges: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, flags = [], []
    for bid in entities.loc[entities.entity_type.eq("business"), "entity_id"]:
        r = effective_ownership(bid, edges)
        ubos = {p: s for p, s in r.owners.items() if s >= UBO_THRESHOLD - 1e-12}
        for p, s in sorted(r.owners.items(), key=lambda kv: -kv[1]):
            rows.append(
                {
                    "entity_id": bid,
                    "person_id": p,
                    "effective_share": round(s, 6),
                    "is_beneficial_owner": p in ubos,
                    "chains": len(r.paths[p]),
                    "example_chain": r.paths[p][0],
                }
            )
        flags.append(
            {
                "entity_id": bid,
                "beneficial_owners": len(ubos),
                "layers": r.max_depth,
                "layered_ownership": r.max_depth >= 2,
                "opaque_jurisdiction": r.opaque,
                "circular_ownership": r.circular,
                "no_25pct_owner": not ubos,
                "accounted_share": round(sum(r.owners.values()), 6),
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(flags)


# ---------------------------------------------------------------- activity
def activity(transactions: pd.DataFrame) -> pd.DataFrame:
    """Twelve months of behaviour per entity, for the periodic review."""
    t = transactions
    inbound = t[t.direction.eq("inbound")]
    outbound = t[t.direction.eq("outbound")]
    months = max(1.0, (t.txn_epoch.max() - t.txn_epoch.min()) / (86400 * 30.4375))
    a = pd.DataFrame(
        {
            "actual_monthly_inflow_cad": inbound.groupby("entity_id").amount_cad.sum() / months,
            "cash_share_of_inflows": (
                inbound[inbound.is_cash.eq(1)].groupby("entity_id").amount_cad.sum()
                / inbound.groupby("entity_id").amount_cad.sum()
            ),
            "wire_share_of_outflows": (
                outbound[outbound.channel.eq("wire")].groupby("entity_id").amount_cad.sum()
                / outbound.groupby("entity_id").amount_cad.sum()
            ),
        }
    )
    return a.fillna(0.0)


# ---------------------------------------------------------------- rating
def _band(points: int) -> str:
    return next(label for floor, label in BANDS if points >= floor)


def _verdict(reasons: list[tuple[str, int]], foreign_pep: bool) -> tuple[int, str]:
    pts = sum(p for _, p in reasons)
    return pts, "High" if foreign_pep else _band(pts)


def rate(entities, profiles, own_flags, act, alerted: set[str]) -> pd.DataFrame:
    df = (
        entities[["entity_id", "entity_type", "segment"]]
        .merge(profiles, on="entity_id")
        .merge(own_flags, on="entity_id", how="left")
        .merge(act, left_on="entity_id", right_index=True, how="left")
    )
    out = []
    for r in df.itertuples(index=False):
        reasons: list[tuple[str, int]] = []
        if r.entity_type == "business":
            reasons.append(("business", POINTS["business"]))
        seg = SEGMENT_RISK[r.segment]
        if seg:
            reasons.append((f"segment:{r.segment}", seg))
        if r.pep_status == "domestic_pep_or_hio":
            reasons.append(("domestic_pep_or_hio", POINTS["domestic_pep_or_hio"]))
        if r.onboarding_channel == "non_face_to_face":
            reasons.append(("non_face_to_face", POINTS["non_face_to_face"]))
        if r.adverse_media:
            reasons.append(("adverse_media", POINTS["adverse_media"]))
        for flag in ("layered_ownership", "opaque_jurisdiction", "no_25pct_owner"):
            if r.entity_type == "business" and bool(getattr(r, flag)):
                reasons.append((flag, POINTS[flag]))
        onboarding = list(reasons)

        ratio = (r.actual_monthly_inflow_cad or 0.0) / r.declared_monthly_inflow_cad
        if ratio > 3:
            reasons.append(("activity_over_3x_declared", POINTS["activity_over_3x_declared"]))
        elif ratio > 1.5:
            reasons.append(("activity_over_1_5x_declared", POINTS["activity_over_1_5x_declared"]))
        if (r.cash_share_of_inflows or 0) > 0.5:
            reasons.append(("cash_over_half_of_inflows", POINTS["cash_over_half_of_inflows"]))
        if (r.wire_share_of_outflows or 0) > 0.2:
            reasons.append(("wires_over_fifth_of_outflows", POINTS["wires_over_fifth_of_outflows"]))
        if r.entity_id in alerted:
            reasons.append(("monitoring_alert", POINTS["monitoring_alert"]))

        foreign_pep = r.pep_status == "foreign_pep"
        on_pts, on_rating = _verdict(onboarding, foreign_pep)
        re_pts, re_rating = _verdict(reasons, foreign_pep)
        edd = []
        if r.pep_status == "foreign_pep":
            edd.append(
                "foreign PEP: source of funds, senior management approval, "
                "enhanced ongoing monitoring"
            )
        if re_rating == "High":
            edd.append("high risk: enhanced measures and enhanced ongoing monitoring")
        if r.entity_type == "business" and bool(r.no_25pct_owner):
            edd.append(
                "no individual owns or controls 25%: verify the most senior managing officer"
            )
        if r.entity_type == "business" and bool(r.circular_ownership):
            edd.append("circular ownership: obtain the structure chart and confirm control")
        out.append(
            {
                "entity_id": r.entity_id,
                "entity_type": r.entity_type,
                "segment": r.segment,
                "pep_status": r.pep_status,
                "onboarding_points": on_pts,
                "onboarding_rating": on_rating,
                "refresh_points": re_pts,
                "refresh_rating": re_rating,
                "declared_monthly_inflow_cad": r.declared_monthly_inflow_cad,
                "actual_monthly_inflow_cad": round(float(r.actual_monthly_inflow_cad or 0.0), 2),
                "factors": "; ".join(f"{n} +{p}" if p else n for n, p in reasons),
                "edd_required": bool(edd),
                "edd_reasons": " | ".join(edd),
                "next_review": (AS_OF + pd.DateOffset(months=REVIEW_MONTHS[re_rating]))
                .date()
                .isoformat(),
            }
        )
    return pd.DataFrame(out)


# ---------------------------------------------------------------- summary
def summarise(
    risk: pd.DataFrame,
    flags: pd.DataFrame,
    ubo: pd.DataFrame,
    cases: pd.DataFrame,
    alerted: set[str],
) -> dict:
    order = ["Low", "Medium", "High"]
    migration = pd.crosstab(risk.onboarding_rating, risk.refresh_rating).reindex(
        index=order, columns=order, fill_value=0
    )
    planted = set(cases.entity_id)
    by = risk.set_index("entity_id")
    edd_reasons = defaultdict(int)
    for reasons in risk.edd_reasons[risk.edd_required]:
        for reason in reasons.split(" | "):
            edd_reasons[reason.split(":")[0]] += 1
    multi = ubo[ubo.is_beneficial_owner].groupby("person_id").entity_id.nunique()
    return {
        "as_of": AS_OF.date().isoformat(),
        "customers": len(risk),
        "onboarding": risk.onboarding_rating.value_counts().reindex(order, fill_value=0).to_dict(),
        "refresh": risk.refresh_rating.value_counts().reindex(order, fill_value=0).to_dict(),
        "migration_onboarding_to_refresh": {
            k: v for k, v in migration.to_dict(orient="index").items()
        },
        "edd_required": int(risk.edd_required.sum()),
        "edd_by_reason": dict(sorted(edd_reasons.items())),
        "pep": risk.pep_status.value_counts().to_dict(),
        "planted_case_customers": len(planted),
        "planted_high_at_onboarding": int(
            (by.loc[sorted(planted), "onboarding_rating"] == "High").sum()
        ),
        "planted_high_at_refresh": int((by.loc[sorted(planted), "refresh_rating"] == "High").sum()),
        "alerted_customers": len(alerted),
        "businesses": len(flags),
        "businesses_layered": int(flags.layered_ownership.sum()),
        "businesses_opaque_jurisdiction": int(flags.opaque_jurisdiction.sum()),
        "businesses_no_25pct_owner": int(flags.no_25pct_owner.sum()),
        "businesses_circular": int(flags.circular_ownership.sum()),
        "beneficial_owner_links": int(ubo.is_beneficial_owner.sum()),
        "persons_owning_two_or_more_businesses": int((multi >= 2).sum()),
        "policy": {
            "points": POINTS,
            "segment_risk": SEGMENT_RISK,
            "bands": {b: f for f, b in BANDS},
            "review_months": REVIEW_MONTHS,
            "ubo_threshold": UBO_THRESHOLD,
        },
    }


def run() -> dict:
    entities = pd.read_csv(cfg.ENTITIES_PATH, keep_default_na=False)
    entities["payroll_base_cad"] = pd.to_numeric(entities.payroll_base_cad, errors="coerce")
    entities["typical_amount_cad"] = pd.to_numeric(entities.typical_amount_cad, errors="coerce")
    profiles = make_profiles(entities)
    edges = make_ownership(entities)
    profiles.to_csv(PROFILES_PATH, **cfg.CSV_WRITE_OPTIONS)
    edges.to_csv(OWNERSHIP_PATH, **cfg.CSV_WRITE_OPTIONS)

    tx = pd.read_csv(
        cfg.TRANSACTIONS_PATH,
        usecols=["entity_id", "txn_epoch", "amount_cad", "direction", "channel", "is_cash"],
    )
    scored = pd.read_csv(cfg.SCORED_PATH, usecols=["entity_id", "n_rules_fired"], low_memory=False)
    alerted = set(scored.loc[scored.n_rules_fired > 0, "entity_id"])
    cases = pd.read_csv(cfg.CASES_PATH, usecols=["entity_id"])

    ubo, flags = beneficial_owners(entities, edges)
    risk = rate(entities, profiles, flags, activity(tx), alerted)
    risk.to_csv(RISK_PATH, index=False, lineterminator="\n")
    ubo.to_csv(UBO_PATH, index=False, lineterminator="\n")
    summary = summarise(risk, flags, ubo, cases, alerted)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    s = run()
    print(f"onboarding {s['onboarding']}  refresh {s['refresh']}  EDD {s['edd_required']}")
    print(
        f"planted-case customers rated High: {s['planted_high_at_onboarding']} at onboarding, "
        f"{s['planted_high_at_refresh']} at refresh, of {s['planted_case_customers']}"
    )
    print(
        f"businesses {s['businesses']}: layered {s['businesses_layered']}, opaque "
        f"{s['businesses_opaque_jurisdiction']}, no 25% owner {s['businesses_no_25pct_owner']}, "
        f"circular {s['businesses_circular']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
