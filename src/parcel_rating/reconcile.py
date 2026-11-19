"""Schema v4: external bill closure reconciliation with identity-ambiguity evidence.

The v1 per-piece rating result is treated as a **read-only payable baseline**:
``analyze`` is run over the exact supplied ``rating.tariffs`` /
``rating.shipments`` with the original exact per-piece semantics, and neither
the tariffs, the shipments nor any decision in that rating is ever modified.
A shipment rated ``rated`` is a payable identity whose amount is fixed at its
v1 ``total_cents`` (a zero total is still a payable identity); an ``oversize``
shipment creates no payable and is listed separately as an excluded identity.

A *complete reconciliation explanation* is one joint witness in which:

* every credit is assigned, whole and exactly once, to one of its declared
  debit candidates (a credit may never be split, dropped or reused), and no
  debit receives more credit than its ``gross_cents``;
* every debit is matched bijectively to one of its declared rated shipment
  candidates, and every payable shipment is covered exactly once;
* each debit's net amount (gross minus every credit assigned to it) equals the
  exact ``total_cents`` of the matched shipment, including zero-net-to-zero
  matches.

Credit assignment and shipment matching must be judged together: the credit
allocation changes every debit net and therefore which shipment match can
close, while the unique-cover constraint can in turn rule out a credit
allocation even when every per-debit and per-credit edge looks locally legal.

The solver never expands money by unit, never enumerates shipment
permutations and never materializes the set of complete explanations:

1. The bounded credit-to-debit allocations are enumerated (at most 2 choices
   per credit, hence at most ``2 ** 14``). Allocations that make some debit
   net negative are discarded. Distinct resulting net vectors are kept, each
   with the union of credit->debit edges that can produce it; amounts are
   arbitrary-precision integers, so scale never costs a unit expansion.
2. For each distinct net vector, the exact integer equalities convert the
   debit->shipment problem into a bipartite perfect-matching problem over at
   most twelve nodes per side. One augmenting-path perfect matching decides
   existence; the edges that occur in *some* perfect matching are then read
   off in linear time using the standard residual-graph characterization
   (matched edges, and allowed edges whose endpoints lie in one strongly
   connected component of the directed residual graph) -- no permutations and
   no per-edge re-solving.
3. A support edge is recorded only when an actual complete explanation uses
   it, so every reported association is witnessed by one joint explanation,
   and because every feasible credit allocation and every matching-allowed
   edge is examined, the support sets are exhaustive. The union across rows
   never implies the rows' choices can be cross-combined into one witness.
"""
from .common import ident, require, rows
from .model import analyze

RECONCILE_FIELDS = 'rating debits credits'
RATING_FIELDS = 'tariffs shipments'
DEBIT_FIELDS = 'debit_id gross_cents shipment_candidates'
CREDIT_FIELDS = 'credit_id cents debit_candidates'
SHIPMENT_LIMIT = 12
DEBIT_LIMIT = 12
CREDIT_LIMIT = 14
MONEY_MAX = 10 ** 15


def _money(value, minimum):
    """Integer cents with bool/float/string explicitly rejected."""
    if type(value) is not int or not minimum <= value <= MONEY_MAX:
        raise ValueError('money must be an integer within allowed bounds')
    return value


def _candidate_ids(value, limit, known, kind):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('candidates must be a bounded array')
    result = []
    seen = set()
    for item in value:
        candidate = ident(item)
        if candidate in seen:
            raise ValueError('duplicate candidate id')
        seen.add(candidate)
        if candidate not in known:
            raise ValueError('candidate references unknown or excluded ' + kind)
        result.append(candidate)
    return result


def _parse_reconcile(data):
    require(data, RECONCILE_FIELDS)
    require(data['rating'], RATING_FIELDS)
    # The v1 analyze contract is reused in full (all field, duplicate-id,
    # duplicate-tariff, bound and boolean checks and the missing-tariff
    # error); only the shipment array cap is narrowed to twelve.
    rows(data['rating']['shipments'], SHIPMENT_LIMIT)
    rating = analyze(data['rating'])
    payable = {}
    excluded = []
    for shipment in rating['shipments']:
        if shipment['status'] == 'rated':
            payable[shipment['id']] = shipment['total_cents']
        else:
            excluded.append(shipment['id'])

    debits = []
    debit_ids = set()
    for row in rows(data['debits'], DEBIT_LIMIT):
        require(row, DEBIT_FIELDS)
        debit_id = ident(row['debit_id'])
        if debit_id in debit_ids:
            raise ValueError('duplicate debit id')
        debit_ids.add(debit_id)
        gross = _money(row['gross_cents'], 0)
        candidates = _candidate_ids(row['shipment_candidates'], SHIPMENT_LIMIT,
                                    payable, 'rated shipment')
        debits.append({'id': debit_id, 'gross': gross, 'candidates': candidates})

    credits = []
    credit_ids = set()
    for row in rows(data['credits'], CREDIT_LIMIT):
        require(row, CREDIT_FIELDS)
        credit_id = ident(row['credit_id'])
        if credit_id in credit_ids:
            raise ValueError('duplicate credit id')
        credit_ids.add(credit_id)
        cents = _money(row['cents'], 1)
        candidates = _candidate_ids(row['debit_candidates'], 2, debit_ids,
                                    'debit')
        credits.append({'id': credit_id, 'cents': cents, 'candidates': candidates})

    return debits, credits, payable, excluded


def _feasible_net_vectors(debits, credits):
    """Enumerate whole-credit allocations and keep distinct net vectors.

    Returns a list of ``(net_tuple, credit_edge_union)`` where
    ``credit_edge_union[c]`` is the bitmask of debit indices that credit ``c``
    is assigned to in at least one allocation producing this exact net vector.
    An allocation whose net would go negative for any debit is rejected.
    """
    vectors = {}

    def extend(credit_index, nets, edge_masks):
        if credit_index == len(credits):
            key = tuple(nets)
            existing = vectors.get(key)
            if existing is None:
                vectors[key] = list(edge_masks)
            else:
                for c, mask in enumerate(edge_masks):
                    existing[c] |= mask
            return
        cents = credits[credit_index]['cents']
        for debit_index in credits[credit_index]['candidate_index']:
            if nets[debit_index] >= cents:
                next_nets = list(nets)
                next_nets[debit_index] -= cents
                next_masks = list(edge_masks)
                next_masks[credit_index] = 1 << debit_index
                extend(credit_index + 1, next_nets, next_masks)

    initial_nets = [debit['gross'] for debit in debits]
    extend(0, initial_nets, [0] * len(credits))
    return list(vectors.items())


def _perfect_matching_allowed_edges(adjacency, n):
    """Return ``(matching, allowed)`` for a bipartite graph (debits on the
    left, shipments on the right), or ``(None, None)`` when no perfect
    matching exists.

    Existence uses Kuhn augmenting paths. The allowed-edge characterization
    needs no permutation enumeration: build the directed residual graph from
    one perfect matching, compute its strongly connected components, and an
    edge is allowed iff it is itself matched or its endpoints share an SCC.
    """
    match_shipment = [-1] * n

    def augment(left, seen):
        for right in adjacency[left]:
            if seen & (1 << right):
                continue
            seen |= 1 << right
            if match_shipment[right] == -1 or augment(match_shipment[right], seen):
                match_shipment[right] = left
                return True
        return False

    for left in range(n):
        if not augment(left, 0):
            return None, None

    match_debit = [0] * n
    for right, left in enumerate(match_shipment):
        match_debit[left] = right

    # Directed residual graph over 2*n nodes (debits 0..n-1, shipments n..2n-1).
    graph = [[] for _ in range(2 * n)]
    reverse = [[] for _ in range(2 * n)]

    def link(source, target):
        graph[source].append(target)
        reverse[target].append(source)

    for left, rights in enumerate(adjacency):
        for right in rights:
            if match_debit[left] == right:
                link(n + right, left)      # matched edge: right -> left
            else:
                link(left, n + right)      # unmatched edge: left -> right

    # Kosaraju SCC, iterative so call depth stays independent of input data.
    visited = [False] * (2 * n)
    order = []
    for start in range(2 * n):
        if visited[start]:
            continue
        visited[start] = True
        stack = [(start, 0)]
        while stack:
            node, edge_index = stack[-1]
            if edge_index < len(graph[node]):
                nxt = graph[node][edge_index]
                stack[-1] = (node, edge_index + 1)
                if not visited[nxt]:
                    visited[nxt] = True
                    stack.append((nxt, 0))
            else:
                order.append(node)
                stack.pop()
    component = [-1] * (2 * n)
    component_id = 0
    for start in reversed(order):
        if component[start] != -1:
            continue
        component[start] = component_id
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in reverse[node]:
                if component[nxt] == -1:
                    component[nxt] = component_id
                    stack.append(nxt)
        component_id += 1

    allowed = [0] * n
    for left, rights in enumerate(adjacency):
        mask = 0
        for right in rights:
            if match_debit[left] == right or component[left] == component[n + right]:
                mask |= 1 << right
        allowed[left] = mask
    return match_debit, allowed


def reconcile(data):
    """Decide external-bill closure and return normalized support evidence.

    The payable baseline is read-only: ``data`` is never mutated, no external
    bill amount is injected into the rating input, and tariffs, rates and
    shipment decisions are never adjusted to force closure.
    """
    debits, credits, payable, excluded = _parse_reconcile(data)
    payable_ids = sorted(payable)
    shipment_index = {shipment_id: i for i, shipment_id in enumerate(payable_ids)}
    m = len(debits)
    k = len(credits)

    debit_support = [0] * m
    credit_support = [0] * k
    reconciled = False

    # Cardinality and aggregate equalities are necessary conditions; checking
    # them first only short-circuits work, the exact explanation test below is
    # the authoritative closure decision. With no debits there is nothing to
    # allocate or cover, so closure holds exactly when there is no payable and
    # no credit left to place (an empty zero bill or an oversize-only rating).
    totals_match = (m == len(payable_ids)
                    and sum(d['gross'] for d in debits)
                    - sum(c['cents'] for c in credits)
                    == sum(payable.values()))
    if totals_match and m == 0:
        reconciled = (k == 0 and not payable_ids)
    elif totals_match and m:
        index_by_id = {debit['id']: i for i, debit in enumerate(debits)}
        for credit in credits:
            credit['candidate_index'] = [index_by_id[cid]
                                         for cid in credit['candidates']]
        adjacency = []
        for debit in debits:
            mask = 0
            for shipment_id in debit['candidates']:
                mask |= 1 << shipment_index[shipment_id]
            adjacency.append(mask)

        # One enumeration of the bounded credit allocations. For every net
        # vector that admits a perfect equal-amount matching, the matching's
        # allowed-edge mask witnesses both support kinds at once: each union
        # member is backed by the same joint complete explanation.
        for net_vector, credit_edges in _feasible_net_vectors(debits, credits):
            # Exact integer equalities turn the identity question into an
            # equal-amount bipartite matching problem (zero net may only match
            # a zero-cents rated shipment, which the equality enforces).
            net_adjacency = []
            for left, net in enumerate(net_vector):
                rights = []
                mask = adjacency[left]
                while mask:
                    bit = mask & -mask
                    right = bit.bit_length() - 1
                    if payable[payable_ids[right]] == net:
                        rights.append(right)
                    mask ^= bit
                net_adjacency.append(rights)
            _, allowed = _perfect_matching_allowed_edges(net_adjacency, m)
            if allowed is None:
                continue
            reconciled = True
            for left, edge_mask in enumerate(allowed):
                debit_support[left] |= edge_mask
            for c in range(k):
                # A credit->debit edge belongs to a complete explanation only
                # when that debit also has a witnessing shipment match.
                for left in range(m):
                    if credit_edges[c] & (1 << left) and allowed[left]:
                        credit_support[c] |= 1 << left

    result = {
        'expected_shipments': [
            {'id': shipment_id, 'expected_cents': payable[shipment_id]}
            for shipment_id in payable_ids
        ],
        'excluded_oversize_ids': sorted(excluded),
        'expected_total_cents': sum(payable.values()),
        'net_debit_cents': (sum(debit['gross'] for debit in debits)
                            - sum(credit['cents'] for credit in credits)),
        'reconciled': reconciled,
        'debit_support': [],
        'credit_support': [],
    }

    if reconciled:
        for i, debit in enumerate(debits):
            ids = sorted(payable_ids[j]
                         for j in range(len(payable_ids))
                         if debit_support[i] & (1 << j))
            result['debit_support'].append({
                'debit_id': debit['id'],
                'status': 'forced' if len(ids) == 1 else 'ambiguous',
                'shipment_ids': ids,
            })
        for c, credit in enumerate(credits):
            ids = sorted(debits[j]['id']
                         for j in range(m)
                         if credit_support[c] & (1 << j))
            result['credit_support'].append({
                'credit_id': credit['id'],
                'status': 'forced' if len(ids) == 1 else 'ambiguous',
                'debit_ids': ids,
            })
    else:
        for debit in debits:
            result['debit_support'].append(
                {'debit_id': debit['id'], 'status': 'blocked', 'shipment_ids': []})
        for credit in credits:
            result['credit_support'].append(
                {'credit_id': credit['id'], 'status': 'blocked', 'debit_ids': []})

    result['debit_support'].sort(key=lambda row: row['debit_id'])
    result['credit_support'].sort(key=lambda row: row['credit_id'])
    return result
