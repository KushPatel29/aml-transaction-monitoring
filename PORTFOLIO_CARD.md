# Portfolio card

Blurb and ready-to-paste markup for [kushpatel29.github.io](https://kushpatel29.github.io),
matching the existing `card-grid` component in `portfolio-site/index.html`.

**Live:** https://aml-transaction-monitoring.streamlit.app
**Repo:** https://github.com/KushPatel29/aml-transaction-monitoring

---

## The two-line blurb

> **Transaction Monitoring & Anomaly Detection** — Finds all 60 planted cases in
> 100k synthetic payments, then converts 366 alerted entities into a governed
> investigation register with review clocks, 8,972 network evidence links and a
> fail-closed release docket. Every SQL feature has a Python twin proven equal
> on all 100,299 rows; scores prioritize evidence but never authorize a decision.

## Alternative, leading with the honest finding

> **Transaction Monitoring & Anomaly Detection** — A layered AML-style detector
> over 100k synthetic payments: PR-AUC 0.774 (95% CI 0.689–0.850) against a
> 0.37% base rate. The anomaly model lifts ranking 49% over rules alone and
> saves 1.8% at the operating point — so the README says the model earns its
> place on triage order, not on the decision.

---

## Card markup

```html
<article class="project card" id="p-aml-monitoring" data-tags="finance ai">
  <button type="button" class="card-zoom" data-zoom="assets/aml-triage.jpg"
          aria-label="Enlarge: transaction monitoring triage console">
    <img src="assets/aml-triage.jpg" width="1280" height="983" loading="lazy" decoding="async"
         alt="Transaction monitoring triage console — case queue with masked entity identifiers, risk scores and reason codes">
    <span class="zoom-hint" aria-hidden="true">⤢ VIEW FULL</span>
  </button>
  <div class="card-body">
    <div class="card-head">
      <p class="kicker">ANOMALY DETECTION · FINANCIAL CRIME</p>
      <p class="card-tests">182 tests ✓</p>
    </div>
    <h3>Transaction Monitoring</h3>
    <p class="outcome">Every planted case found, then governed as one investigation per alerted entity with a fail-closed release docket.</p>
    <p class="card-text">Five explainable rules and an unsupervised model monitor 100,299 synthetic transactions. AML-INV-01 then builds 366 investigations, assigns P0/P1/P2 review clocks, connects 8,972 entity–counterparty evidence links and tests eight release gates. The result remains REVIEW REQUIRED because human disposition and filing approval are deliberately outside the public demo; removing one case in memory changes the release to BLOCKED.</p>
    <div class="card-stats">
      <div><div class="stat-v">60/60</div><div class="stat-k">CASES FOUND</div></div>
      <div><div class="stat-v">0.774</div><div class="stat-k">PR-AUC</div></div>
      <div><div class="stat-v">6 / 2 / 0</div><div class="stat-k">PASS · REVIEW · BLOCK</div></div>
    </div>
    <p class="proof"><span class="proof-tick" aria-hidden="true">✓</span> Ground-truth quarantine, one-case-per-entity completeness, release gates and a fail-closed re-verification drill are executable contracts.</p>
    <a class="card-open" href="https://aml-transaction-monitoring.streamlit.app" target="_blank" rel="noopener"><span>OPEN LIVE APP</span><span aria-hidden="true">↗</span></a>
    <a class="card-open" href="https://github.com/KushPatel29/aml-transaction-monitoring" target="_blank" rel="noopener"><span>OPEN REPO</span><span aria-hidden="true">↗</span></a>
  </div>
</article>
```

**Screenshot:** `docs/dashboard_case_queue.png` (1500×1150). The other cards use
1280-wide JPEGs at ~q82 in `portfolio-site/assets/` — this one is already
canvas-only, so it needs resizing but no cropping.

**Command palette entry** (`main.js` uses repo name + test count):

```js
{ label: 'Transaction Monitoring', hint: '182 tests', url: 'https://github.com/KushPatel29/aml-transaction-monitoring' },
```

---

## Where it fits

This is the anomaly-detection and financial-crime entry in the set. It is the
only project where the headline claim is a *cost* comparison against a named
baseline, which makes it the natural one to lead with for FINTRAC/CBSA/CRA-
adjacent analytics roles and for any BI role where "does this alert earn its
investigation" is the actual question.

Every number above is in [`results/metrics.json`](results/metrics.json),
produced by `python run_pipeline.py`.

**One caveat to keep in the pitch, not hide from it:** the dollar figures rest
on invented cost constants. The repo says so, ships the sensitivity sweep that
shows how far the recommendation moves when they change, and the honest line is
"the shape of the cost curve is the finding; the dollar figure is an
illustration."
