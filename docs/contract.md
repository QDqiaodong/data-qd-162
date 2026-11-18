# Dimensional parcel rating

The package has three entry points sharing the same strict JSON reader, source
SHA256 provenance and atomic report publication:

1. **Per-piece rating (schema_version 1)** — `analyze(data)` /
   `parcel-rating --source FILE --output FILE`. Unchanged from v1.
2. **Batch dispatch planning (schema_version 2)** — `plan(data)` /
   `parcel-rating --plan --source FILE --output FILE`. Described below.
3. **Authoritative revision timelines (schema_version 3)** — `temporal(data)` /
   `parcel_rating.model.temporal(data)` /
   `parcel-rating --temporal --source FILE --output FILE`. Described in its
   own section below. `--temporal` and `--plan` are mutually exclusive.

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

## v3 authoritative revision timelines and point-in-time rating

### Input contract

The top level contains exactly `revisions`, `shipments`, `as_of_seq`,
`window_start`, `window_end` (no other fields, none missing). Every time
coordinate is an integer in `0..10^15` and booleans are rejected as
integers; `window_start < window_end`. `as_of_seq` shares the `0..10^15`
range. No external time library or carrier service is involved.

`revisions` has at most 500 entries. Each entry has exactly
`revision_id`, `known_seq`, `start`, `end`, `tariff`, `supersedes`:

- `revision_id` follows the v1 identifier rules and is globally unique.
- `known_seq` is an integer in `1..10^15`; incomparable revisions may share
  a `known_seq`.
- `start < end`; the effective interval is half-open `[start, end)` and
  need not overlap, or even intersect, the request window.
- `tariff` contains exactly the v1 `TARIFF_FIELDS` with the v1 numeric and
  identifier bounds. Every revision carries a complete independent tariff;
  the same `(service, zone)` may have many revisions.
- `supersedes` is an array of distinct revision ids without self
  references. Across the whole request there are at most 20000 edges. Each
  parent must exist, must share the child's service and zone, and must have
  a strictly smaller `known_seq`. Parent/child effective intervals need not
  overlap or nest. The whole graph is validated on every request, including
  revisions that are not visible at the current `as_of_seq`; an illegal
  edge on a future record still fails the request.

`shipments` has at most 10000 entries. Each has exactly the v1 per-piece
fields `id`, `service`, `zone`, `weight_grams`, `length_mm`, `width_mm`,
`height_mm`, `declared_cents`, `remote` plus `shipped_at`; v1 validation
bounds are reused, ids are unique, and `shipped_at` must lie in
`[window_start, window_end)`. A shipment whose `(service, zone)` has no
revision at all is legal and rates as `missing` (this differs from the v1
missing-tariff input error). Empty `revisions` and empty `shipments` are
both legal. Any extra/missing field, illegal number, unknown parent id,
cross-key parent or equal-`known_seq` parent edge fails the whole request
(CLI exit code 2) without overwriting an existing report.

### Visibility, ancestry and the shared frontier

Visibility is defined solely by knowledge cutoff: a revision is visible iff
`known_seq <= as_of_seq`. Interval order and input array order never decide
visibility.

Ancestry is the transitive closure of `supersedes` over the visible
revisions, and it is computed over the full visible graph **before** any
interval or window filtering. An intermediate node whose own interval never
covers the evaluation point (including a node effective only outside the
window) therefore still connects its descendants to their ancestors;
clipping records first would change authority and is explicitly wrong.
Because every edge goes from a larger `known_seq` to a strictly smaller
one, the closure is computed in one ascending-seq pass with bitmask sets.

For one `(service, zone)` at one integer point, the covering set is the
visible revisions whose half-open interval contains that point
(`start <= t < end`). A covering revision is dominated, and removed, when
another covering revision can reach it through one or more `supersedes`
edges. The remaining records are the **frontier**:

- empty frontier → `missing`;
- one revision → `resolved`, and `revision_id` is that id;
- two or more revisions → `ambiguous`, even when their tariff payloads are
  byte-for-byte identical. Authority identity is never collapsed by equal
  amounts, cheapest price, earliest/latest interval or id ordering.

`supersedes` expresses authority precedence only: it never deletes a
parent's interval, so a parent can remain the authority wherever no
descendant covers the point. Multi-parent merges and independent forks are
both first-class. Frontier ids are listed in Unicode code-point order.

### Continuous canonical timelines

The timeline key set is the union of every `(service, zone)` appearing in
`revisions` or `shipments`, including keys whose only revisions are still
invisible. Each key gets one timeline covering the entire
`[window_start, window_end)` window with non-empty, gap-free,
non-overlapping segments; a key without any visible coverage is one
`missing` segment.

Segments are built from exact integer boundary events only — no time unit
is ever enumerated. All visible revision boundaries inside the window,
plus the window endpoints, are breakpoints; each raw interval between
consecutive breakpoints has one constant covering set and one constant
frontier. Adjacent raw intervals are merged **iff** both `status` and the
frontier id tuple are identical. Consequently:

- a dominated record entering or leaving coverage changes the covering set
  but not the frontier, and the segments merge;
- equal money with a different frontier identity never merges;
- the half-open rule makes a record ending at `t` and another starting at
  `t` meet exactly at `t` with no overlap and no gap; window endpoints are
  treated identically to any other boundary, including equality at
  `window_start`/`window_end`.

Timelines are sorted by `(service, zone)` and segments by `start`. Each
segment is exactly `{start, end, status, frontier, revision_id}` with
`revision_id` set only for `resolved` segments.

### Point-in-time shipment accounting

Each shipment has a fixed `(service, zone)` and `shipped_at`. It must use
the same authority as the timeline segment containing that point: the
shipment's `status`, `frontier` and `revision_id` are copied from that
segment, never recomputed independently, and no shipment ever traverses
the revision graph. Segment lookup is a binary search over segment ends.

Only a `resolved` frontier is billable. The unique tariff is applied with
the unchanged v1 per-piece rules — girth, dimensional weight, step ceiling,
remote fee, per-piece fuel and half-up insurance — with no group minimum or
group fuel from plan mode. Girth above the limit is `oversize`: settled,
unpaid, carrying `girth_mm` only. `missing` and `ambiguous` shipments keep
their evidence (`id`, `service`, `zone`, `shipped_at`, `status`,
`frontier`, `revision_id`) and are not billed; the implementation never
falls back to the earliest, latest or cheapest rate.

### Result, counts and bill completeness

`parcel_rating.model.temporal(data)` returns `result` directly and never
mutates its argument. The CLI envelope is
`{"schema_version": 3, "source_sha256": ..., "result": ...}`. `result` has
exactly:

- `timelines`: sorted `{service, zone, segments}` entries as above;
- `shipments`: per-shipment results sorted by id; resolved shipments add the
  v1 rated evidence fields (`actual_weight_grams`, the same Fraction string
  in `dimensional_weight_grams`, `chargeable_weight_grams`, `weight_steps`,
  `girth_mm`, `base_cents`, `fuel_cents`, `insurance_cents`,
  `total_cents`), oversize shipments add only `girth_mm`;
- `rated_count`, `oversize_count`, `missing_count`, `ambiguous_count` —
  the four counts sum to the shipment count;
- `settled_total_cents`: the sum of rated shipments' `total_cents`
  (oversize is settled but unpaid and contributes nothing);
- `total_cents`: `null` whenever `missing_count + ambiguous_count > 0`,
  otherwise equal to `settled_total_cents`. An empty shipment list is a
  zero bill. Ambiguity that exists only in the timeline, with no shipment
  inside it, does not block bill completeness. A legal-but-unresolved
  request still exits 0.

### Indexing, scale and determinism

Supported scale: 500 revisions, 20000 edges, 10000 shipments and
coordinates spanning `0..10^15`. The implementation keeps per-key interval
boundaries sorted, represents ancestry as integer bitmasks computed once
per request (one pass in ascending `known_seq`), evaluates each raw
window interval with bitmask cover/frontier operations, and caches one
timeline per key that every shipment reuses. There is no per-time-unit
enumeration and no per-shipment graph traversal.

All outputs are canonically sorted, so reordering any input arrays (or the
ids inside a `supersedes` array) leaves `result` unchanged while
`source_sha256` still tracks the exact raw bytes of that invocation.
