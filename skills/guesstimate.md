# Playbook: guesstimate (estimate a quantity with little data)

## Clarify first
Scope precisely: geography, time unit (per day/month/year), what counts (e.g. which
product types, which customers), stock vs flow. Agree scope before any number.

## Structure: pick the side with better anchors
- Demand side: population → segment (urban/rural, income, age, occupation) → share that
  uses/owns → usage rate or replacement cycle → quantity.
- Supply side: capacity → utilisation → throughput (e.g. runways x slots, flights x
  seats x load factor, outlets x sales per outlet).
- Cross-check with the other side or a known benchmark, and sanity-check the order of
  magnitude.

## Common patterns (frequency in 6 training cases)
1. Population segmented by income and age, then ownership/usage per segment (3/6).
2. Supply-side capacity estimation when demand data is weak (2/6).
3. Re-scoping what actually consumes the item (e.g. packaging, not the product) (1/6).

## Analyses
`top_down` / `bottom_up` in the sandbox, with every factor recorded as an assumption
(value, unit, rationale) or a cited fact; `sensitivity` on the two most uncertain factors.

## Traps
- Assumptions without a stated basis.
- Including segments outside scope (e.g. international when only domestic counts).
- Ignoring peak vs off-peak or seasonality where it matters.
- Jumping to arithmetic before the approach is agreed.

## Definition of done
A number with its formula, every factor labelled (fact id or assumption id), a
cross-check or sanity check, and the range implied by the most uncertain factors.
