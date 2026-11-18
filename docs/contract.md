# Dimensional parcel rating

The package has two entry points sharing the same strict JSON reader, source
SHA256 provenance and atomic report publication:

1. **Per-piece rating (schema_version 1)** — `analyze(data)` /
   `parcel-rating --source FILE --output FILE`. Unchanged from v1.
2. **Batch dispatch planning (schema_version 2)** — `plan(data)` /
   `parcel-rating --plan --source FILE --output FILE`. Described below.

## v1 per-piece rating

Each shipment is rated independently against an explicit service/zone tariff.
Dimensions are positive integer millimetres; actual weight is positive integer
grams. Volume in cubic centimetres is length*width*height / 1,000. Dimensional
weight in grams is volume_cm3 / divisor_cm3_per_kg * 1,000, equivalently
length*width*height / divisor_cm3_per_kg. Chargeable weight is the greater of
actual and dimensional weight, rounded upward to a whole step_grams. Girth is
length+2*(width+height); excess parcels are reported oversize and are not
charged.

Tariff amounts are integer cents: base, per chargeable step, optional
remote-zone fee, fuel basis points on that subtotal, and insurance basis
points on declared value above an included threshold. Basis-point charges use
exact nonnegative half-up rounding. All totals are integer cents. A tariff is
unique by service and zone; every shipment must match one.

No live carrier API, taxes, discounts, currency conversion, dimensional
packaging optimization or delivery promise is included.

## v2 batch dispatch planning

### Input contract

The top level contains exactly `tariffs`, `shipments`, `dispatch_rules`
(no other fields, none missing).

- `tariffs` reuse every v1 tariff field and numeric bound; `(service, zone)`
  pairs are unique.
- `dispatch_rules` has at most four entries. Each entry has exactly
  `service`, `zone`, `max_pieces`, `max_chargeable_grams`,
  `minimum_base_cents`. The `(service, zone)` pair must reference one defined
  tariff, pairs may not repeat, and the integer bounds are
  `max_pieces` 1..12, `max_chargeable_grams` 1..10^12 and
  `minimum_base_cents` 0..10^15.
- `shipments` has at most twelve entries. Parcel fields are the v1 fields
  except the single `service` string is replaced by a nonempty `services`
  array of nonblank identifiers without duplicates. For every candidate
  service, the parcel's `(service, zone)` must have a dispatch rule.
- All identifier, integer and boolean bounds from v1 still apply. Unknown
  fields, duplicate shipment ids, duplicate rules, a missing tariff or a
  missing dispatch rule fail the whole request (CLI exit code 2); such
  failures never publish or overwrite a report.

### Per-candidate evidence

Each parcel/candidate pair is rated independently against its tariff using the
v1 formulas and the same exact rational arithmetic and half-up rounding.
Girth alone decides availability: an over-girth candidate gets
`status: "oversize"` and evidence carrying `girth_mm` only — the parcel is
never discarded early, because another candidate may still fit. Eligible
candidates additionally carry `chargeable_weight_grams`, `base_cents`
(including the remote fee) and `insurance_cents`. Every declared candidate
appears exactly once in `candidates`, sorted by parcel id then service.

### Group billing

An assignment selects exactly one eligible candidate per parcel. Parcels
sharing a `(service, zone)` form at most one billing group, and a group is
feasible only when its piece count and the **sum of the members' chargeable
weights** (not actual weights and not pre-rounding dimensional weights) stay
within the rule's capacities.

For every nonempty group:

- `subtotal_base_cents` S is the sum of the per-piece base amounts.
- `billed_base_cents` B = max(S, minimum_base_cents);
  `minimum_topup_cents` = B − S. The minimum only raises B — it never changes
  chargeable weight or insurance.
- `fuel_cents` = half_up(B · fuel_bp / 10000), rounded **once on the group
  billed base**. Per-piece fuel is never computed and then summed.
- `insurance_cents` is the sum of the per-piece insurance amounts.
- `total_cents` = B + fuel + insurance.

An empty group triggers no minimum, no fuel and no charge at all.

### Exact optimization and tie ordering

Chargeable weights determine whether a group is feasible, while the final
membership determines the minimum top-up and the single group-level fuel
rounding, so costs cannot be decided per piece: the planner solves one joint
problem covering every parcel at the globally minimum total.

The solver enumerates every capacity-feasible parcel subset per dispatch rule
(bitmask over at most twelve parcels), computes the finalized whole-group bill
for each subset once, and runs a memoized search over
`(rule index, covered parcel mask)`. Every legal partition of the parcels
across the at most four rules is examined, which makes the result an exact
optimum rather than a heuristic or local choice. The bound of twelve parcels
and four rules keeps the state space bounded independently of currency or
weight-unit magnitudes, which only affect integer/fraction arithmetic.

Ties use, in order:

1. lower `total_cents`;
2. fewer nonempty groups;
3. the lexicographically smallest service vector built by listing parcel ids
   in ascending Unicode order and reading the assigned service at each
   position (Python string / Unicode code-point order).

### Result and report

`parcel_rating.model.plan(data)` returns the result object directly. The CLI
wraps it as `{"schema_version": 2, "source_sha256": ..., "result": ...}` with
the SHA256 of the exact source bytes of that invocation.

`result` fields:

- `feasible` (boolean);
- `candidates`: one evidence object per declared candidate, sorted by
  `(id, service)` — `{id, service, zone, status, girth_mm}` plus
  `chargeable_weight_grams`, `base_cents`, `insurance_cents` when eligible;
- `assignments`: `{id, service, zone}` per parcel, sorted by id;
- `groups`: sorted by `(service, zone)`, each with `member_ids` (sorted),
  `piece_count`, `chargeable_weight_grams` (group sum),
  `subtotal_base_cents` (S), `billed_base_cents` (B),
  `minimum_topup_cents` (B−S), `fuel_cents`, `insurance_cents`,
  `total_cents`. The group totals sum to `result.total_cents`, and every
  amount can be recomputed from the same report;
- `total_cents`: integer total, or `null` when no complete feasible
  allocation exists.

When capacity is insufficient or every candidate of some parcel is oversize,
`feasible` is `false`, assignments and groups are empty and `total_cents` is
`null`; candidate evidence is still complete and the CLI exits 0. An empty
shipment array is a feasible zero-bill result. An unallocatable parcel is
never silently treated as a zero-cost success.

Reordering any input arrays never changes `result` (all outputs are
canonically sorted); `source_sha256` always reflects the raw bytes of the
current invocation.
