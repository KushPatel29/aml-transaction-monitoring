# Canadian regulatory mapping

How the detection rules in this repository line up with the reporting regime a
Canadian reporting entity works under: the *Proceeds of Crime (Money
Laundering) and Terrorist Financing Act* (PCMLTFA) and FINTRAC's guidance.

> **Scope.** This is a portfolio simulation on synthetic data. The mapping
> shows how I would frame the rules for a Canadian compliance or intelligence
> team; it is not legal advice and not a compliance program. Thresholds and
> obligations are summarised from FINTRAC's public guidance (linked below),
> which is the authority if anything here differs.

## Two different questions: threshold reporting and suspicion

FINTRAC's regime separates two obligations, and the naive baseline in this
project confuses them on purpose so the difference can be measured.

| Report | When it is required | Deadline |
|---|---|---|
| **Large Cash Transaction Report (LCTR)** | $10,000 or more in cash received in one transaction, or in two or more cash amounts totalling $10,000 or more within a consecutive 24-hour window when the entity knows they share a conductor, a third party they were conducted for, or a beneficiary (the 24-hour rule) | 15 calendar days after the cash is received |
| **Electronic Funds Transfer Report (EFTR)** | International EFTs of $10,000 or more, including amounts aggregated under the 24-hour rule | Set by FINTRAC's EFT guidance |
| **Large Virtual Currency Transaction Report (LVCTR)** | $10,000 or more in virtual currency, single or aggregated under the 24-hour rule | Set by FINTRAC's LVCTR guidance |
| **Suspicious Transaction Report (STR)** | Reasonable grounds to suspect that a completed or attempted transaction is related to a money laundering or terrorist financing offence. **No dollar threshold.** | As soon as practicable after the measures that establish reasonable grounds to suspect are complete |

Threshold reports are filed whether or not anything is suspicious. An STR is
filed because something is, at any amount. The naive `amount > $10,000` rule
in this project is, in effect, the LCTR test used as a suspicion detector. It
finds 38 of 60 planted cases, and it cannot see structuring at all, because
structuring is defined by staying under that line.

## Rule-by-rule mapping

Indicator categories refer to FINTRAC's *Money laundering and terrorist
financing indicators — Financial entities* guidance.

| Rule | What it detects here | FINTRAC indicator category | Threshold report it relates to | Where it stops |
|---|---|---|---|---|
| `R1_STRUCTURING` | Three or more cash deposits of $7,500–$9,999.99 within seven days | Transactions structured below the reporting or identification requirements | LCTR: the pattern exists to avoid one | A depositor who spreads deposits over more than a week *and* keeps the count low escapes the window (see README, "What this system misses") |
| `R2_LAYERING` | $15,000+ inbound, 70%+ passed out to three or more counterparties within 48 hours | Atypical transactional activity (funds in and out quickly); wire transfers | EFTR, if outbound legs are international EFTs of $10,000 or more | 216 of its 219 false positives are wholesalers paying suppliers after a settlement: legitimate pass-through looks identical from one side |
| `R3_SMURFING` | Eight or more distinct senders paying in similar small amounts within 72 hours | Use of other parties; wire transfers (several senders to one recipient) | None directly; each payment is small by design | Payroll-style or marketplace inflows can trip it; precision 0.969 here |
| `R4_ROUND_DOLLAR` | Round amounts two or more standard deviations above the entity's own history | Transactions out of line with the client's profile (rounded sums atypical for the client) | None | Needs ten prior transactions to have a profile at all |
| `R5_DORMANT` | First transaction after 90+ days of silence, at 4× the prior mean or more | Change in account activity (an inactive account that becomes active; sudden change in pattern) | None | Fires on the first transaction only; later ones in the same burst are found at entity level |
| Anomaly model | Isolation-forest rank against all 100,299 transactions | Atypical transactional activity; activity relative to profile | None | Weighted at 40% so it cannot alert alone: an unexplained score is not a reason a reviewer can act on |

## The 24-hour window is a policy choice, and it decides the LCTR

FINTRAC's 24-hour rule uses a static window the reporting entity defines and
documents (for example 09:00 one day to 08:59 the next). The worked example in
[`results/analytic_brief_INV-ENT-01216.md`](../results/analytic_brief_INV-ENT-01216.md)
shows why that matters: six cash deposits of $8,018–$9,988 land one per
calendar day. Under a midnight-to-midnight window, no window holds $10,000 and
no LCTR arises. Under a window starting at noon, the first two deposits
(17 hours apart, $16,915.10 together) share a window and an LCTR would be
required if the entity knows they share a conductor. Structuring that defeats
one institution's window can trip another's, which is one reason the STR
question has to be asked separately.

## What this project does not cover

- **Know-your-client and beneficial ownership.** The synthetic data has no
  conductor identity per deposit, no onboarding profile and no ownership
  records, all of which a real STR decision leans on.
- **Sanctions, politically exposed persons and terrorist property.** No
  screening lists, no Terrorist Property Reports, no ministerial directives.
- **Both legs of a transfer.** Counterparties are external, so the system sees
  one side of every payment.
- **The filing decision.** Every case ends at `HUMAN DECISION REQUIRED`. The
  case packet records the evidence; whether it reaches reasonable grounds to
  suspect is a judgment for a person with the full client file.

## Sources

- FINTRAC, [Reporting large cash transactions to FINTRAC](https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation/lctr-doie/lctr-doie-eng)
- FINTRAC, [Reporting transactions to FINTRAC: the 24-hour rule](https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation/24hour/1-eng)
- FINTRAC, [Money laundering and terrorist financing indicators — Financial entities](https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation/indicators-indicateurs/fin_mltf-eng)
- FINTRAC, [Money laundering and terrorist financing indicators — Money services businesses](https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation/indicators-indicateurs/msb_mltf-eng)
- BLG, [FINTRAC's guidance on filing suspicious transaction reports as soon as practicable](https://www.blg.com/en/insights/2020/05/fintrac-new-guidance-on-filing-suspicious-transaction-reports-as-soon-as-practicable)
