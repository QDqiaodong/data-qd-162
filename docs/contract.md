# Dimensional parcel rating

The package has seven entry points sharing the same strict JSON reader, source
SHA256 provenance and atomic report publication:

1. **Per-piece rating (schema_version 1)** — `analyze(data)` /
   `parcel-rating --source FILE --output FILE`. Unchanged from v1.
2. **Batch dispatch planning (schema_version 2)** — `plan(data)` /
   `parcel-rating --plan --source FILE --output FILE`. Described below.
3. **Authoritative revision timelines (schema_version 3)** — `temporal(data)` /
   `parcel_rating.model.temporal(data)` /
   `parcel-rating --temporal --source FILE --output FILE`. Described in its
   own section below. `--temporal` and `--plan` are mutually exclusive.
4. **External bill closure reconciliation (schema_version 4)** —
   `reconcile(data)` / `parcel_rating.model.reconcile(data)` /
   `parcel-rating --reconcile --source FILE --output FILE`. Described in its
   own section below. `--reconcile` is pairwise mutually exclusive with
   `--plan` and `--temporal`. The three older entry points keep their original
   contracts exactly.
5. **SQLite pre-deposit and manifest register (schema_version 5)** —
   `parcel_rating.register.apply(database, data)` /
   `parcel_rating.register.snapshot(database)` / the independent
   `parcel-register` console script. Described in its own section below. It
   never changes the four older entry points or the `cli.run` signature.
6. **Offline release evaluation (schema_version 6)** —
   `parcel_rating.release.run(source)` /
   `parcel_rating.release.evaluate(min_customers, requests, batches)` / the
   independent `parcel-rate-release` console script. Described in its own
   section below. It reads UTF-8 JSON Lines (not the single-JSON reader) and
   never changes the five older entry points.
7. **Carrier-rate price impact (schema_version 7)** —
   `parcel_rating.impact.evaluate(data)` /
   `parcel_rating.model.impact(data)` /
   `parcel-rating --impact --source FILE --output FILE`. Described in its own
   section below. `--impact` is pairwise mutually exclusive with `--plan`,
   `--temporal` and `--reconcile`; the six older entry points keep their
   original contracts exactly.

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

## v4 external bill closure reconciliation

This entry point only decides whether an external carrier bill (debits and
credits) can be **completely reconciled** against the read-only payable
baseline produced by the existing v1 per-piece rating. It never changes a
service, a rate, a tariff or a shipment decision, and no external bill amount
is ever injected into the rating input.

### Input contract

The top level contains exactly `rating`, `debits`, `credits` (no other field,
none missing).

- `rating` contains exactly `tariffs` and `shipments` and follows the v1
  `analyze` contract in full: every field, identifier, duplicate-id and
  duplicate-tariff rule, every numeric bound and every boolean check, plus the
  v1 missing-tariff error, are reused unchanged. The only difference is that
  the `shipments` array cap is narrowed from the v1 limit to twelve.
- The payable baseline is the result of that single `analyze` run. A shipment
  with `status: "rated"` is a payable identity whose amount is fixed at its
  v1 `total_cents`; a rated shipment billed exactly zero is still a payable
  identity and may never be omitted. A shipment with `status: "oversize"`
  creates no payable; its id is reported separately as an excluded identity
  and can never be used as a zero-cents payable or referenced by a debit
  candidate.

`debits` has at most twelve entries. Each entry has exactly `debit_id`,
`gross_cents`, `shipment_candidates`:

- `debit_id` follows the common identifier rules and is unique within the
  debits array.
- `gross_cents` is an integer in `0..10^15`; booleans, floats and strings are
  rejected.
- `shipment_candidates` is an array of distinct identifiers, at most twelve,
  each referencing a `rated` shipment of this request. Referencing an unknown
  id or an oversize id, a duplicate candidate, an over-cap array or a
  non-array value makes the whole request invalid. An empty array is legal and
  means the debit declares no possible shipment identity.

`credits` has at most fourteen entries. Each entry has exactly `credit_id`,
`cents`, `debit_candidates`:

- `credit_id` follows the identifier rules and is unique within the credits
  array. Debit ids and credit ids are independent namespaces; the same literal
  may appear in both.
- `cents` is an integer in `1..10^15` (a credit is never zero); booleans,
  floats and strings are rejected.
- `debit_candidates` is an array of distinct identifiers, at most two, each
  referencing a declared debit. The same invalidity rules apply, and an empty
  array is a legal declaration with no assignable target.

Unknown or missing fields, duplicate ids, bad references, over-limit arrays
and any v1 rating invalidity fail the whole request with CLI exit code 2 and
never overwrite an existing report. A payable/debit count mismatch, unequal
totals or the absence of a complete explanation are business non-closure, not
format errors: the request is valid and exits 0.

### Complete reconciliation explanation

A complete explanation gives, jointly:

- one debit target for every credit, chosen from its declared candidates,
  with the whole credit used exactly once (no split, no omission, no reuse);
  a debit may receive several credits, and the sum assigned to a debit must
  never exceed its `gross_cents` (its net must not be negative);
- one rated shipment target for every debit, chosen from its declared
  candidates, forming a bijection: every debit is matched and every payable
  shipment is covered exactly once;
- the debit's net (`gross_cents` minus every credit assigned to it) must equal
  the exact `total_cents` of the matched shipment. All equalities are exact
  integer equations: no tolerance, no float comparison, no aggregate
  top-up, no silently dropped row. A zero-net debit can only cover a
  zero-cents rated shipment.

Credit assignment changes every debit net and therefore which shipment match
can close, while the unique-cover constraint can rule out a credit assignment
even when each per-debit and per-credit edge is locally legal. The two choices
are therefore judged together, and a single locally feasible edge is never
reported as fact unless one joint complete explanation actually uses it.

### Support evidence and status

`parcel_rating.model.reconcile(data)` returns `result` directly and never
mutates its argument; the CLI envelope is
`{"schema_version": 4, "source_sha256": ..., "result": ...}`. `result` has
exactly:

- `expected_shipments`: the read-only payables sorted by id, each
  `{id, expected_cents}`;
- `excluded_oversize_ids`: sorted oversize shipment ids;
- `expected_total_cents`: sum of every payable (always emitted);
- `net_debit_cents`: sum of every `gross_cents` minus sum of every credit
  `cents` (always emitted; aggregate integer arithmetic is not bounded by the
  per-line ceiling);
- `reconciled`: boolean, true iff at least one complete explanation exists;
- `debit_support`: one row per source debit sorted by `debit_id`, each
  exactly `{debit_id, status, shipment_ids}`;
- `credit_support`: one row per source credit sorted by `credit_id`, each
  exactly `{credit_id, status, debit_ids}`.

When `reconciled` is true, `shipment_ids` (respectively `debit_ids`) is the
full set of identities that the row is actually associated with in at least
one complete explanation, sorted by Unicode code point with no duplicates. A
set of size one has `status: "forced"`; a larger set has
`status: "ambiguous"`. Equal money never merges distinct identities. The
union of support across rows is not a witness by itself: choices from
different explanations cannot be cross-combined into a joint explanation that
does not exist, and an edge that is only locally feasible is never included.

When `reconciled` is false, every support array is empty and every row's
status is `"blocked"`. The payable list, excluded oversize ids and both
totals are still returned in full; no partial closure or self-chosen
approximate solution is ever emitted. An empty rating with no debits and no
credits is a closed zero bill; requests that contain only debits or only
credits are judged by the same complete-explanation rules with no special
zero-padding.

### Exact algorithm and the support witness guarantee

The solver neither expands money by unit, nor enumerates shipment
permutations, nor materializes the set of complete explanations:

1. **Bounded credit allocations.** Each credit has at most two declared
   targets, so the whole-credit allocations form a search tree of at most
   `2^14` leaves, explored with integer nets. Any partial assignment that would
   drive a debit negative is pruned. Allocations yielding the same resulting
   net vector are merged, and for every credit the union of the debit targets
   that can produce that vector is kept as a bitmask.
2. **Equal-amount perfect matching.** For each distinct net vector, the exact
   integer equalities turn the debit-to-shipment question into a bipartite
   perfect-matching problem over at most twelve nodes per side (a debit is
   adjacent only to declared candidates whose payable equals its net; zero net
   therefore only reaches a zero-cents payable). One augmenting-path search
   decides whether a perfect matching exists. The edges that occur in *some*
   perfect matching are then derived from one matching's directed residual
   graph: a matched edge, or an edge whose two endpoints lie in the same
   strongly connected component, is allowed. This is linear in the graph size
   and needs no permutation enumeration and no per-edge re-solve.
3. **Witnessed, exhaustive support.** An edge enters a support set only when
   (a) the credit allocation producing the current net vector assigns that
   credit to the debit and (b) the debit has an allowed equal-amount matching
   edge in the same vector. The two facts come from one joint explanation, so
   every reported association is guaranteed to have a complete explanation as
   its witness. Because every feasible credit allocation and every
   matching-allowed edge are examined, no supported association is omitted.
   The enumeration of allocations is bounded solely by the credit count (at
   most fourteen, at most two candidates each), independent of money
   magnitude; all amounts stay arbitrary-precision integers.

The independent test oracle does not call any production function: on small
inputs it re-rates with its own arithmetic, enumerates every whole-credit
allocation and every debit-to-shipment bijection directly, applies the exact
non-negativity, equality and unique-cover rules itself, and projects the
support sets from the complete explanations it found. Production and oracle
agree across deterministic generated samples and the named boundary cases
(equal totals without a cover, credit overshoot, no credits, zero-cents rated
payables, oversize exclusion, empty candidate arrays, ceiling-sized amounts,
one-sided ambiguity, equal-money distinct identities and input/candidate
reordering). A separate full-scale case exercises twelve shipments, twelve
debits and fourteen credits to confirm the bounded search at production scale
without enumerating shipment permutations or storing explanations.

## v5 SQLite pre-deposit and shipment-manifest register

The fifth entry point is a durable book of fact backed by a single SQLite
database (standard library `sqlite3` only; no network or external service).
It combines two responsibilities under one optimistic-concurrency ledger:

* **Pre-deposits.** `fund` adds integer-cent balances to named accounts.
  `amend` replaces or cancels not-yet-shipped parcels as one batch. For every
  account the invariant
  `available = funded - held - spent >= 0` always holds, and `held` equals
  exactly the sum of the frozen quotes of that account's currently held
  parcels.
* **Manifest lists.** Every non-shipped parcel belongs to exactly one open
  manifest. `seal` atomically closes named manifests, moving their members'
  frozen quotes from the corresponding accounts' `held` to `spent` and
  permanently freezing that batch's members and amounts. Sealed members can
  never be replaced, moved or cancelled.

### Interfaces and storage

* Python: `parcel_rating.register.apply(database, data)` returns the request
  receipt; `parcel_rating.register.snapshot(database)` returns the current
  state snapshot. `database` is a filesystem path; `apply` never mutates the
  caller's `data` (shipment and tariff objects are defensively copied).
* Console (a separate script independent of `parcel-rating`):
  - apply: `parcel-register --database DB --source FILE --output FILE`;
  - read-only: `parcel-register --database DB --snapshot --output FILE`
    (`--source` is forbidden together with `--snapshot`).
* A brand-new database has version 0 and is created by the first request.
  `snapshot` never creates a database: a missing path is an error. A foreign
  database or an unsupported register schema version is rejected rather than
  silently rebuilt, cleared or overwritten. A format/business failure on a
  brand-new path may leave an empty file but never an initialized register,
  a business fact or a successful request record.
* Money is arbitrary-precision Python integer cents. SQLite stores every
  monetary column as canonical decimal **text**, so neither signed-64-bit
  overflow nor floating-point conversion can corrupt a balance, quote or
  aggregate. Per-line fund cents are capped at 10^15 but cumulative balances
  and aggregates are unbounded.

### Request envelope and validation

Every request carries `request_id` and `expected_version` (a non-negative
integer; booleans are rejected). The top-level fields are exact per action —
no extra or missing fields — and inputs are read with the shared 8 MB reader
with duplicate-JSON-key and non-finite-number rejection. Identifier classes
(accounts, manifests, parcels, requests) use the common identifier rules and
are independent namespaces.

* `fund`: `{request_id, expected_version, action:"fund", entries}` with 1..1000
  entries `{account_id, cents}`, each account appearing once, `cents` an
  integer in 1..10^15. Unknown accounts are created by their first fund with
  initial held/spent of zero.
* `amend`: `{request_id, expected_version, action:"amend", upserts, cancels}`.
  `upserts` has at most 1000 items, each exactly
  `{account_id, manifest_id, shipment, tariff}`; `cancels` is at most 1000
  parcel ids; the two arrays together contain at least one item. The shipment
  reuses every v1 shipment field and the tariff every v1 complete tariff
  field and bound; service/zone must correspond. Upsert shipment ids are
  unique, cancel ids are unique, and the two sets are disjoint. Accounts must
  already exist. Unknown target manifests are created as open on success.
  Existing held parcels may be wholly replaced (account, manifest, physical
  parameters and tariff); both the source and target manifest must be open.
  Unknown parcels may be created; the ids of cancelled or shipped parcels are
  permanently barred from re-registration. Cancels may only reference
  currently held parcels. A zero-cents rated parcel is a real manifest
  member; an oversize parcel cannot be registered and cannot masquerade as a
  zero-cents member.
* `seal`: `{request_id, expected_version, action:"seal", manifest_ids}` with
  1..1000 distinct manifest ids, all existing and open. An empty open manifest
  may be sealed (zero members, zero amount). All current held members become
  shipped and the new version is recorded as `sealed_version`. Any failure
  seals none of the named manifests.

### Rating, batching and the account invariant

Each upsert is rated through the unchanged v1 single-piece analysis (exact
girth, dimensional weight, step ceiling, remote, fuel and half-up insurance
rules), producing a complete v1 rated evidence row that becomes the parcel's
frozen quote. Any oversize parcel rejects the whole batch. Quotes of parcels
not in the current upsert set are never recomputed: an accepted quote is only
replaced when its parcel is re-rated in a successful batch, and a shipped
parcel's amount is immutable even if a later request carries a changed
tariff.

An amend may move parcels across accounts and open manifests, change physical
parameters and re-rate, all in one batch. The fund check is evaluated on the
state after the entire replacement/cancel set is applied — never by charging
each upsert in input order — so a cross-account swap whose final per-account
balances are non-negative must succeed even though an intermediate state
would be negative. Old holds are replaced together with the new holds: a
batch failure releases no old hold, creates no manifest, changes no version
and consumes no request id. On success each account's `funded` is unchanged;
`held` is the sum of its held parcels' frozen `total_cents`, `spent` the sum
of its shipped parcels' frozen amounts, and `available = funded-held-spent`.
Cancelling only marks a parcel cancelled and releases its hold while
retaining its last accepted parcel, tariff, quote, account and manifest
identity for audit; a cancelled parcel is no longer an active manifest
member. Cancelling every member never deletes the (empty, open) manifest.
`seal` only performs an equal held-to-spent transfer and never deducts
availability again.

### Optimistic concurrency, idempotency and receipts

Each new request is handled in one SQLite transaction. Inside it the current
version is checked against `expected_version`, the business terminal state is
validated, the facts are posted and an immutable request receipt plus the
canonical request content are saved. A successful new request increments the
version by one; any error rolls the whole batch back. Writers are serialized
with an immediate write transaction (with a bounded busy timeout), so
concurrent new requests carrying the same `expected_version` have at most one
winner; the others get a version conflict and no lost update is possible.

The canonical request identity sorts the set-valued arrays (`entries` by
account id, `upserts` by shipment id, `cancels` and `manifest_ids` by id) and
re-serializes nested objects with sorted keys, so array permutation, nested
key order and JSON whitespace are irrelevant; every other field, including
`expected_version`, participates in the identity.

* Same id and identical canonical content: the original receipt is returned
  directly, before any current-version or membership check — even after the
  database has advanced — together with the original point-in-time snapshot.
* Same id with different canonical content: rejected; the original receipt is
  never rewritten.
* A failed request stores no business receipt and does not consume its id;
  corrected content may retry under that id. Identical content under a new id
  is a new request and goes through normal version/state checks.

The SQLite commit is the point of effect. The report file is published
afterwards as a separate step; the two are not a cross-file atomic
transaction. If report writing fails after the commit, the CLI exits 2 and
names the committed request id and version on stderr; the database is not
rolled back or deleted, and replaying the same request restores the original
receipt without posting anything twice. Format, business, version-conflict
and commit failures exit 2 without overwriting an existing report.
Database/output path collisions and symlinked database or output paths are
refused before any business write; the shared safe-replace publication
behavior is retained.

### Snapshot and receipt shapes

The state snapshot is exactly
`{version, accounts, bookings, manifests}` where `version` is the count of
committed new requests:

* `accounts`, sorted by account id, each exactly
  `{account_id, funded_cents, held_cents, spent_cents, available_cents}`.
* `bookings` keeps every parcel including cancelled ones, sorted by id, each
  exactly `{id, account_id, manifest_id, status, shipment, tariff, quote}`
  with `status` one of `held`/`cancelled`/`shipped`; `quote` is exactly the
  v1 single-piece rated result row (including `id`, `status` and all billing
  evidence) and can never be supplied directly by a request; `shipment` and
  `tariff` are the last accepted input objects.
* `manifests`, sorted by manifest id, each exactly
  `{manifest_id, status, member_ids, total_cents, sealed_version}` with
  `status` `open`/`sealed`, `member_ids` sorted and listing only the current
  held members or, once sealed, the shipped members (never cancelled
  parcels), `total_cents` the sum of those frozen quotes, and
  `sealed_version` null while open.

All snapshot sections are read from one committed read snapshot so they can
never mix two versions. The apply receipt is exactly
`{request_id, version, snapshot}` where both `version` and
`snapshot.version` are the version after that request's first success and the
snapshot is the complete state at that point in time; later requests never
mutate a stored receipt.

### Console envelope

Both CLI modes emit `{schema_version:5, source_sha256, result}`. On the apply
branch `source_sha256` is the SHA-256 of that invocation's exact source
bytes — even when the result is an old retry receipt — and `result` is the
receipt. On the snapshot branch `source_sha256` is null and `result` is the
current state snapshot. The stdout JSON and the published file are identical.

## v6 offline release evaluation

The sixth entry point simulates, offline and in request order, the sequence
of freight-summary reports that **one fixed audience** receives, and decides
for each request the first declared aggregation granularity that may be
published without exposing any too-small customer equivalence class. It is a
read-only evaluation: no report is ever actually sent, and the tool's only
output is the decision report.

### Input contract (JSON Lines)

The source is a UTF-8 JSON Lines file. Decoding errors, blank lines, lines
over 8 MB, duplicate JSON keys, non-finite numbers and non-object lines are
input errors (CLI exit code 2, existing report preserved).

* **Line 1 (header)**: exactly `{min_customers, requests}`.
  `min_customers` is an integer in 2..100 (booleans rejected). `requests` is
  an array of at most 24 request objects; it may be empty.
* **Every later line**: one shipment batch with exactly
  `{tariffs, shipments, customers}`. `tariffs` and `shipments` follow the v1
  per-piece input contract and per-batch limits unchanged (at most 500
  tariffs and 10000 shipments per batch, unique ids, every shipment matched
  to a tariff, every numeric and boolean bound). `customers` is an object
  mapping **this batch's** shipment ids to customer ids; it must cover
  exactly the batch's shipments — no missing and no extra keys. Customer ids
  follow the common identifier rules; one customer may own many shipments,
  and customer ids are stable across batches. Shipment ids are unique across
  the whole file, and the file holds at most 400000 shipments in total.

Each request has exactly `{id, services, zones, levels}`:

* `id` follows the common identifier rules and is unique across requests.
* `services` and `zones` are each `null` (no restriction) or a nonempty
  array of distinct identifiers. A rated shipment is selected only when both
  filters pass. Unknown but legal service/zone ids simply match nothing.
* `levels` is a nonempty array without duplicates whose elements are drawn
  from `service_zone`, `service`, `zone`, `total`; it is the tried order.
  The levels are tried independently as declared — the tool never adds,
  removes or reorders levels.

Across the whole file, the rated shipments may span at most 12 distinct
services and 12 distinct zones; exceeding either bound is an input error.
The complete input is validated before any request is decided, so an illegal
record fails the run even when no request would have selected it.

### Aggregation

Only shipments the v1 per-piece interface judges `rated` participate, with
their v1 `total_cents`; `oversize` shipments never enter any statistic.
Different batches may carry different tariffs for the same service/zone. A
customer's several shipments add to the shipment count and the freight sum
but count once in every row's customer set.

For one request and one level, the selected rated shipments are grouped into
**all** nonempty rows of that level (`service_zone`: one row per pair;
`service`: per service; `zone`: per zone; `total`: one row). Publishing a
level always publishes its complete row set; single rows can never be hidden
or cherry-picked.

### Release rule (membership equivalence)

Take every already published row of every earlier request plus every
candidate row of the level being tried, and view each row as the set of
customers it contains. Consider only customers appearing in at least one of
these rows. Two customers are equivalent iff their member/not-member pattern
over all these rows is identical; the equivalence classes are the maximal
groups of customers sharing one pattern. The level may publish iff **every**
nonempty equivalence class holds at least `min_customers` distinct
customers.

* Same-key rows from different requests are different observations; a
  customer may belong to several rows of one report.
* Published rows are never retracted, and their membership is never
  re-filtered by a later request's selection.
* Requests are decided in array order. For each request the declared levels
  are tried from the first; the first passing level publishes and the rest
  are not tried. A failed level leaves no trace: its rows never join the
  observable history used by later decisions.
* A request whose filters select no rated shipment is `empty`; a request
  whose levels all fail is `withheld`. Neither changes the release history.
* A rejected trial reveals nothing: the report carries no membership, class
  sizes, failed rows or freight for it. The rule judges only the membership
  semantics above; it does not claim to resist other external information or
  numeric inference.

### Result and report

`parcel_rating.release.run(source)` returns the full report; the CLI
envelope is `{"schema_version": 6, "source_sha256": ..., "result": ...}`
with the SHA-256 of the exact source bytes of that invocation. `result` is
an array in input request order, each item exactly
`{id, status, level, rows}`:

* `status` is `published`, `empty` or `withheld`. For the latter two,
  `level` is `null` and `rows` is empty.
* For `published`, `level` is the level that passed and `rows` holds every
  row of that level, each exactly `{key, customer_count, shipment_count,
  total_cents}`. `key` is the string array in the level's order
  (`service_zone` → `[service, zone]`, `service` → `[service]`,
  `zone` → `[zone]`, `total` → `[]`); rows are sorted by key in Unicode
  code-point order.

Reordering shipments, batch lines or the `services`/`zones` arrays never
changes `result` (reordering `requests` or a request's `levels` can).
`source_sha256` always reflects the raw bytes of the current invocation. On
success the CLI prints the same parsed value it publishes; any input or I/O
failure exits 2 and preserves an existing report.

### Scale and resource discipline

Supported scale: 400000 shipments, 24 requests, 12 services and 12 zones
among rated shipments, in a 128 MiB process-memory Linux container (temporary
disk is allowed). The implementation streams the file line by line: raw
shipment objects are dropped after each batch, shipment-id uniqueness and
the customer-id registry live in a temporary SQLite database rather than on
the Python heap, each `(service, zone)` cell keeps only counters and a
compact customer-index list, and the release state is partition refinement
(one class id per customer with exact rollback of failed trials) — never a
per-customer boolean vector over all published and trial rows. The
reproducible 400k-shipment experiment (hot/cold customer grouping,
cross-batch repeat customers, alternating granularity attempts) lives in
`tests/test_cli_release.py` (`ScaleReleaseTests`); it measures the CLI
process peak RSS via the child's own `resource.getrusage` self-report (a
forked child inherits the parent's maxrss, so an outside observer in a busy
process would misread it) and records the metrics to
`run/release_scale_metrics.json`.

## v7 carrier-rate price impact and scale-batch review

This entry point is a **read-only forecast**: it compares an old tariff table
with a new one at the same corrected weight per shipment. It never changes a
historical quote, never recomputes an accepted price with a later tariff, and
never produces a debit or a credit. Its only output is the impact report.

### Input contract

The top level contains exactly `old_tariffs`, `new_tariffs`, `scale_batches`,
`shipments` (no other field, none missing). All four arrays may be empty.

- `old_tariffs` and `new_tariffs` each reuse the unchanged v1 tariff fields,
  identifier rules, numeric bounds, per-table `(service, zone)` uniqueness and
  at most 500 rows. In addition the pilot accepts only `step_grams >= 250`;
  a smaller step fails the whole request for **either** table.
- `shipments` has at most 10000 entries. Each has exactly the v1 shipment
  fields `id`, `service`, `zone`, `weight_grams`, `length_mm`, `width_mm`,
  `height_mm`, `declared_cents`, `remote` plus `scale_batch_id`. Every v1
  field, identifier, numeric and boolean bound applies unchanged.
  `weight_grams` is the **original scale reading**; dimensions, declared value
  and the remote flag keep their v1 meaning. Shipment ids are globally unique
  and `scale_batch_id` must reference a declared batch. Every shipment must
  find the same `(service, zone)` row in **both** tariff tables; a key present
  in only one table is an input error.
- `scale_batches` is an array of batches, each with exactly
  `scale_batch_id`, `min_delta_grams`, `max_delta_grams`, `references`:
  - `scale_batch_id` follows the common identifier rules and is unique.
  - `min_delta_grams` and `max_delta_grams` are integers in
    `-20000..20000`; they may be equal and the lower bound may not exceed the
    upper bound. Booleans are rejected.
  - `references` holds 1..16 records, each exactly
    `reference_id`, `reading_grams`, `true_min_grams`, `true_max_grams`.
    `reference_id` is unique within its batch; the three weights lie in the v1
    actual-weight range `1..10^9` and `true_min_grams <= true_max_grams`.
    Boolean integers are rejected. Reference objects constrain the correction
    only; they are never billed as tickets.

Every declared batch must have at least one shipment, and every shipment must
reference a declared batch. Unknown/missing fields, duplicate ids or keys,
bad references, out-of-bound values and the 8 MB limit fail the whole request
(CLI exit 2) **before** any business result is produced, even when a batch is
contradictory.

### One shared correction per batch

Every ticket of a batch shares one integer correction `delta`; corrected
weight is `weight_grams + delta`. The feasible set is the closed integer
interval formed by intersecting, all together:

- the declared batch interval `[min_delta_grams, max_delta_grams]`,
- for every reference, `[true_min_grams - reading_grams,
  true_max_grams - reading_grams]`, and
- for every shipment of the batch — including tickets that are oversize on
  either table — `[1 - weight_grams, 10^9 - weight_grams]`, so no corrected
  weight leaves the v1 actual-weight range.

An empty intersection is a **legal business conclusion**: the batch is marked
`conflict`, and the report keeps the final computed `lower_delta_grams` and
`upper_delta_grams` (possibly inverted) without dropping any reference or
clipping an individual ticket's weight. Contradiction is decided only after the
complete input has been validated.

### Per-ticket comparison

For every shipment, girth is judged independently against each table's
`max_girth_mm` using the unchanged v1 per-piece rule. When both tables rate the
ticket and the batch has a feasible interval, the difference at one `delta` is
`new total_cents - old total_cents`, both computed with the **same** corrected
weight and each table's own volume weight, step ceiling, remote fee, fuel and
half-up insurance. Amounts are exact integer cents.

If either table is `oversize`, only the two verdicts are kept: the ticket
contributes to no difference and no exposure and is never treated as a zero
quote. When the batch is contradictory both verdicts are still kept but every
ticket's difference is unknown (`delta_cents: null`).

### Aggregation and witnesses

For each ticket, the report gives the minimum and maximum difference across the
feasible interval and the **smallest** correction (numeric comparison) that
attains each. For each feasible batch the same is reported for two sums over all
comparable tickets at the *same* correction:

- **net change** — signed sum of every ticket's difference;
- **increase** (涨价敞口) — sum of only the positive differences.

The two sums are recomputed independently at every candidate correction; the
per-ticket extremes are never added together, and the net change is never
clipped to nonnegative to form the increase. A feasible batch with no
comparable ticket has both sums identically zero. Batch corrections are
independent, so the portfolio extremes are the sums of each batch's respective
lower (minimum) and upper (maximum) values. An empty input is a complete zero
portfolio with both intervals `[0, 0]`. If any batch is contradictory the
portfolio is `incomplete` with both sums `null`, while every other batch is
still reported in full.

### Result and report

`parcel_rating.impact.evaluate(data)` returns the result directly and never
mutates its argument; `parcel_rating.model.impact(data)` forwards to it. The
CLI envelope is `{schema_version: 7, source_sha256, result}` with the SHA-256
of the exact source bytes. `result` has exactly:

- `shipments`, sorted by `id`, each exactly
  `{id, scale_batch_id, old_status, new_status, delta_cents}` where statuses
  are `rated`/`oversize` and `delta_cents` is
  `{min_cents, max_cents, min_at_grams, max_at_grams}` or `null`;
- `scale_batches`, sorted by `scale_batch_id`, each exactly
  `{scale_batch_id, status, lower_delta_grams, upper_delta_grams,
  net_change_cents, increase_cents}` where `status` is `feasible`/`conflict`
  and both amount intervals use the same witness-bearing shape as
  `delta_cents` (both `null` for a conflict);
- `portfolio`: `{status, net_change_cents, increase_cents}` where `status` is
  `complete`/`incomplete`; when complete each amount interval is only
  `{min_cents, max_cents}`, otherwise both are `null`.

On success the CLI publishes the report atomically and prints the identical
parsed value. Malformed structure, duplicate keys, bad references, out-of-bound
values, source/output path collisions, symlinked output and read/write
failures exit 2 and preserve an existing report. A calibration contradiction
is a normal report and exits 0. Reordering any input array (including each
batch's `references`) never changes `result`.

### Scale and determinism

A tariff total is a step function of corrected weight whose step count changes
only at `ceil(W / step_grams)` transitions; hence one ticket's difference is
piecewise constant with at most `width/step_old + width/step_new + 1` pieces
(`width <= 40000` from the declared +/-20000 range, `step_grams >= 250`).
Batch totals are obtained by an **event sweep** over the union of those
breakpoints, so runtime grows with
`tickets x (breakpoints per ticket on the two tables)` — i.e. it depends on the
declared interval width divided by the two step sizes, not on the raw weight
magnitude — and memory is bounded by the number of distinct breakpoints. The
implementation never enumerates grams and never materializes the
ticket-by-correction Cartesian product; the reproducible 10000-shipment
experiment is in `tests/test_impact_scale.py` and records its elapsed time to
`run/impact_scale_metrics.json`.
