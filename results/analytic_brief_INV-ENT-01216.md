# Analytic brief: INV-ENT-01216

**Possible structuring of cash deposits below the large cash reporting threshold**

> **SYNTHETIC: PORTFOLIO EXERCISE.** The entity, its name and every transaction
> were generated with Faker by `data/generate_transactions.py`. No real person,
> business, institution or report is involved. Written from the evidence alone,
> in the form an analyst would hand to a second-level reviewer; the answer key
> is at the end, kept separate on purpose.

| | |
|---|---|
| Subject | ENT-01216, "Patton, Sanders and Carpenter 0166", business, retail (cash-intensive) |
| Case state | P0 · second-level review · due in 4 hours |
| Triggered by | `R1_STRUCTURING`, risk score 69.8 |
| Period examined | 1 Aug 2025 – 31 Jul 2026 (137 transactions) |
| Focus window | 23–28 Nov 2025 |

## Bottom line

Between 23 and 28 November 2025 the subject made six cash deposits on six
consecutive days, each between $8,018 and $9,988, totalling **$53,374.51 in
4.9 days**. That is more than nine times its usual weekly cash before November
($5,708), and every deposit sits in the band just under the $10,000 large cash
transaction threshold. **I assess it
is likely the deposits were sized to stay under that threshold (moderate
confidence).** The money does not appear to have been moved on quickly. The
case should go to a reporting decision once three facts the data cannot supply
are obtained (see *Gaps*).

## Key judgments

1. **The November deposits are likely structured (moderate confidence).**
   - Before 23 November the subject deposited cash about 1.3 times a week, with
     a median of $4,101; only 3 of 20 deposits fell between $7,500 and $10,000,
     and none reached $8,800.
   - In the window, all six did. Two came within $222 of the threshold
     ($9,988.23 and $9,778.44).
   - Deposits switched to exactly one per calendar day. On a midnight-to-midnight
     24-hour window, no window held $10,000, so no large cash transaction report
     would arise.
   - The anomaly model ranks the six deposits between the 92nd and 99.5th
     percentile of all 100,299 transactions, independently of the rule.

2. **There is no sign of rapid onward movement (high confidence).** The only
   outbound payment in the window, $5,881.76 by EFT to CPT-02984 about 49
   minutes after the last deposit, matches a pattern seen all year: 24 payments
   to that counterparty on or near the 15th and the last day of each month,
   $326,891 in total. It reads as a supplier run, not a pass-through.

3. **A legitimate explanation cannot yet be ruled out (moderate confidence).**
   See the alternatives below. Moderate rather than high confidence in
   judgment 1 rests on this, and on the absence of conductor and location data.

## Alternatives considered

| Hypothesis | For | Against |
|---|---|---|
| **Structuring to avoid an LCTR** | Every deposit in the $8,000–$10,000 band; one per day; two within $222 of the line; abrupt start and stop | No other indicators: no layering, no new counterparties, no third-party payers. On 22 December the subject deposited $11,727.89 in one go, which would itself require an LCTR; someone set on avoiding reports would not |
| **Seasonal takings** (a late-November sales peak in a cash-heavy retailer) | Retail cash-intensive segment; activity returns to baseline in December | A peak raises the *number* of deposits, not their size into one narrow band; none of December's six deposits falls in the band |
| **A cash-handling rule** (for example an insurer's or carrier's cap on cash in transit) | Consistent amounts under a round figure are what a cap produces | The pattern lasts five days and then stops; a standing policy would persist |

## Indicators matched

FINTRAC, *ML/TF indicators — Financial entities*:

- **Structured below the reporting threshold:** several cash deposits just
  under $10,000 in a short period.
- **Change in account activity:** a sudden change in the pattern and size of
  deposits.
- **Relative to the client's profile:** amounts out of line with the subject's
  own history.

## Reporting position

- **Large cash transaction report.** Not triggered on these facts if the
  institution's documented 24-hour window runs midnight to midnight: no single
  deposit reached $10,000, and no calendar day held two. Under a window
  starting at noon, the first two deposits ($16,915.10, 17 hours apart) would
  share a window, and an LCTR would be required if the institution knows the
  same person conducted both. **Check the documented window before closing
  this point.**
- **Suspicious transaction report.** There is no dollar threshold; the test is
  reasonable grounds to suspect. On the evidence so far the pattern is
  consistent with structuring, but the gaps below are the measures that would
  establish or dispel that. **Recommendation:** complete them within the
  four-hour review, then decide. If the business has no credible explanation,
  the facts support reasonable grounds to suspect, and the report should be
  filed as soon as practicable. This is a recommendation to the reviewer, not
  a filing; the case stays at `HUMAN DECISION REQUIRED`.

## Gaps

1. **Who made each deposit, and where.** The data has no conductor or branch
   field. One person at one branch strengthens judgment 1; several staff at
   several branches weakens it.
2. **The expected cash volume declared at onboarding**, to compare with
   $53,374.51 in five days.
3. **The business's explanation and records** for late November: sales, a
   change in cash handling, or a one-off event.

## How this was produced

`python run_pipeline.py` builds the scored transactions; this brief reads
`results/scored_transactions.csv.gz` and `results/investigation_case_register.csv`.
Figures were computed from those files on the committed data. The rule
thresholds and the 24-hour window discussion are explained in
[`docs/CANADIAN_REGULATORY_MAPPING.md`](../docs/CANADIAN_REGULATORY_MAPPING.md).

---

### Answer key

The generator planted this entity as `CASE-0016`, typology `T01-STRUCTURING`.
The brief's lead judgment is correct. Its confidence stays at moderate because,
without the generator's labels, the alternatives above could not be excluded
from the transaction data alone, which is also how it would be in practice.
