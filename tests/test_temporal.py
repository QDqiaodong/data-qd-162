import copy
import json
import time
import unittest
from fractions import Fraction
from pathlib import Path
from random import Random

from parcel_rating.model import temporal, half_up

ROOT = Path(__file__).resolve().parents[1]

TARIFF_KEYS = ('service zone base_cents step_grams per_step_cents divisor_cm3_per_kg '
               'max_girth_mm remote_cents fuel_bp insurance_bp insurance_free_cents').split()


def tariff(service='S', zone='Z', per_step=100, girth=2000, base=0, step=500,
           divisor=10 ** 9, remote=0, fuel=0, insurance=0, free=0):
    return {"service": service, "zone": zone, "base_cents": base, "step_grams": step,
            "per_step_cents": per_step, "divisor_cm3_per_kg": divisor,
            "max_girth_mm": girth, "remote_cents": remote, "fuel_bp": fuel,
            "insurance_bp": insurance, "insurance_free_cents": free}


def revision(rid, seq, start, end, supers=(), service='S', zone='Z', **tariff_kw):
    return {"revision_id": rid, "known_seq": seq, "start": start, "end": end,
            "tariff": tariff(service, zone, **tariff_kw),
            "supersedes": list(supers)}


def shipment(pid, t, service='S', zone='Z', weight=500, length=600, width=100,
             height=100, declared=0, remote=False):
    return {"id": pid, "service": service, "zone": zone, "weight_grams": weight,
            "length_mm": length, "width_mm": width, "height_mm": height,
            "declared_cents": declared, "remote": remote, "shipped_at": t}


def acceptance_data(as_of, times=(45, 55, 75, 80)):
    return {
        "revisions": [
            revision("A", 1, 0, 100, per_step=100, girth=2000),
            revision("B", 2, 20, 80, ["A"], per_step=200, girth=800),
            revision("C", 3, 40, 60, ["A"], per_step=300, girth=2000),
            revision("D", 4, 50, 70, ["B", "C"], per_step=400, girth=2000),
        ],
        "shipments": [shipment(f"P{i}", t) for i, t in enumerate(times)],
        "as_of_seq": as_of, "window_start": 0, "window_end": 100,
    }


class JointAcceptanceTests(unittest.TestCase):
    def test_as_of_three_fork_is_ambiguous(self):
        result = temporal(acceptance_data(3))
        self.assertEqual((result['rated_count'], result['oversize_count'],
                          result['missing_count'], result['ambiguous_count']), (1, 1, 0, 2))
        self.assertEqual(result['settled_total_cents'], 100)
        self.assertIsNone(result['total_cents'])
        by_id = {s['id']: s for s in result['shipments']}
        self.assertEqual(by_id['P0']['status'], 'ambiguous')
        self.assertEqual(by_id['P0']['frontier'], ['B', 'C'])
        self.assertIsNone(by_id['P0']['revision_id'])
        self.assertEqual(by_id['P1']['status'], 'ambiguous')
        self.assertEqual(by_id['P1']['frontier'], ['B', 'C'])
        self.assertEqual(by_id['P2']['status'], 'oversize')
        self.assertEqual(by_id['P2']['frontier'], ['B'])
        self.assertEqual(by_id['P2']['revision_id'], 'B')
        self.assertEqual(by_id['P2']['girth_mm'], 1000)
        self.assertEqual(set(by_id['P2']),
                         {'id', 'service', 'zone', 'shipped_at', 'status',
                          'frontier', 'revision_id', 'girth_mm'})
        self.assertEqual(by_id['P3']['status'], 'rated')
        self.assertEqual(by_id['P3']['frontier'], ['A'])
        self.assertEqual(by_id['P3']['total_cents'], 100)
        segments = result['timelines'][0]['segments']
        self.assertEqual([(g['start'], g['end'], g['status']) for g in segments],
                         [(0, 20, 'resolved'), (20, 40, 'resolved'),
                          (40, 60, 'ambiguous'), (60, 80, 'resolved'),
                          (80, 100, 'resolved')])
        self.assertEqual(segments[2]['frontier'], ['B', 'C'])
        self.assertIsNone(segments[2]['revision_id'])

    def test_as_of_four_merges_under_d(self):
        result = temporal(acceptance_data(4))
        self.assertEqual((result['rated_count'], result['oversize_count'],
                          result['missing_count'], result['ambiguous_count']), (2, 1, 0, 1))
        self.assertEqual(result['settled_total_cents'], 500)
        self.assertIsNone(result['total_cents'])
        by_id = {s['id']: s for s in result['shipments']}
        self.assertEqual(by_id['P0']['status'], 'ambiguous')  # point 45 stays open
        self.assertEqual(by_id['P0']['frontier'], ['B', 'C'])
        self.assertEqual(by_id['P1']['status'], 'rated')      # 55 flips to D
        self.assertEqual(by_id['P1']['revision_id'], 'D')
        self.assertEqual(by_id['P1']['total_cents'], 400)
        self.assertEqual(by_id['P2']['status'], 'oversize')   # 75 B oversize
        self.assertEqual(by_id['P3']['status'], 'rated')      # 80 back on A
        self.assertEqual(by_id['P3']['total_cents'], 100)
        segments = result['timelines'][0]['segments']
        # C leaves at 60 while D still covers: frontier unchanged -> [50,70)
        self.assertEqual([(g['start'], g['end'], g['status'], tuple(g['frontier']))
                          for g in segments],
                         [(0, 20, 'resolved', ('A',)), (20, 40, 'resolved', ('B',)),
                          (40, 50, 'ambiguous', ('B', 'C')),
                          (50, 70, 'resolved', ('D',)),
                          (70, 80, 'resolved', ('B',)),
                          (80, 100, 'resolved', ('A',))])

    def test_as_of_one_changes_authority_not_in_place_rates(self):
        result = temporal(acceptance_data(1))
        self.assertEqual(result['ambiguous_count'], 0)
        self.assertEqual(result['missing_count'], 0)
        self.assertEqual(result['total_cents'], 400)
        self.assertEqual(result['settled_total_cents'], 400)
        self.assertTrue(all(s['revision_id'] == 'A' for s in result['shipments']))
        self.assertEqual(len(result['timelines'][0]['segments']), 1)

    def test_ambiguous_timeline_without_shipment_keeps_full_bill(self):
        result = temporal(acceptance_data(4, times=(55, 75, 80)))
        segments = result['timelines'][0]['segments']
        self.assertTrue(any(g['status'] == 'ambiguous' for g in segments))
        self.assertEqual(result['ambiguous_count'], 0)
        self.assertEqual(result['total_cents'], 500)
        self.assertEqual(result['settled_total_cents'], 500)

    def test_empty_graph_and_empty_shipments(self):
        result = temporal({"revisions": [], "shipments": [], "as_of_seq": 0,
                           "window_start": 0, "window_end": 10})
        self.assertEqual(result, {"timelines": [], "shipments": [],
                                  "rated_count": 0, "oversize_count": 0,
                                  "missing_count": 0, "ambiguous_count": 0,
                                  "settled_total_cents": 0, "total_cents": 0})

    def test_shipment_key_without_any_revision_is_missing_not_input_error(self):
        data = acceptance_data(4, times=())
        data['shipments'] = [shipment("X", 10, service='ghost', zone='ZZ')]
        result = temporal(data)
        self.assertEqual(result['timelines'][0]['service'], 'S')
        missing_timeline = result['timelines'][1]
        self.assertEqual((missing_timeline['service'], missing_timeline['zone']),
                         ('ghost', 'ZZ'))
        self.assertEqual(len(missing_timeline['segments']), 1)
        segment = missing_timeline['segments'][0]
        self.assertEqual((segment['start'], segment['end'], segment['status'],
                          segment['frontier'], segment['revision_id']),
                         (0, 100, 'missing', [], None))
        self.assertEqual(result['shipments'][0]['status'], 'missing')
        self.assertEqual(result['missing_count'], 1)
        self.assertIsNone(result['total_cents'])
        self.assertEqual(set(result['shipments'][0]),
                         {'id', 'service', 'zone', 'shipped_at', 'status',
                          'frontier', 'revision_id'})


class FrontierSemanticsTests(unittest.TestCase):
    def test_transitive_precedence_through_noncovering_intermediate(self):
        data = {"revisions": [
            revision("A", 1, 0, 100),
            revision("B", 2, 20, 30, ["A"]),
            revision("C", 3, 40, 60, ["B"])],
            "shipments": [shipment("P1", 45)],
            "as_of_seq": 3, "window_start": 0, "window_end": 100}
        result = temporal(data)
        self.assertEqual(result['shipments'][0]['frontier'], ['C'])
        self.assertEqual(result['shipments'][0]['revision_id'], 'C')

    def test_intermediate_completely_outside_window_still_connects(self):
        # B is visible but never effective inside the request window; ancestry
        # must be built before any interval clipping.
        data = {"revisions": [
            revision("A", 1, 0, 100),
            revision("B", 2, 200, 300, ["A"]),
            revision("C", 3, 40, 60, ["B"])],
            "shipments": [shipment("P1", 45), shipment("P2", 10)],
            "as_of_seq": 3, "window_start": 0, "window_end": 100}
        result = temporal(data)
        by_id = {s['id']: s for s in result['shipments']}
        self.assertEqual(by_id['P1']['frontier'], ['C'])
        self.assertEqual(by_id['P2']['frontier'], ['A'])
        segments = result['timelines'][0]['segments']
        self.assertEqual([(g['start'], g['end'], g['status']) for g in segments],
                         [(0, 40, 'resolved'), (40, 60, 'resolved'),
                          (60, 100, 'resolved')])

    def test_independent_equal_price_fork_remains_ambiguous(self):
        payload = dict(per_step=200, girth=2000)
        data = {"revisions": [
            revision("A", 1, 0, 100, **payload),
            revision("B", 2, 20, 80, ["A"], **payload),
            revision("C", 3, 40, 60, ["A"], **payload)],
            "shipments": [shipment("P1", 45)],
            "as_of_seq": 3, "window_start": 0, "window_end": 100}
        result = temporal(data)
        self.assertEqual(result['shipments'][0]['status'], 'ambiguous')
        self.assertEqual(result['shipments'][0]['frontier'], ['B', 'C'])

    def test_window_gap_is_a_missing_segment(self):
        data = {"revisions": [revision("A", 1, 0, 40),
                              revision("B", 2, 60, 100)],
                "shipments": [shipment("P1", 50)],
                "as_of_seq": 2, "window_start": 0, "window_end": 100}
        result = temporal(data)
        segments = result['timelines'][0]['segments']
        self.assertEqual([(g['start'], g['end'], g['status']) for g in segments],
                         [(0, 40, 'resolved'), (40, 60, 'missing'),
                          (60, 100, 'resolved')])
        self.assertEqual(result['shipments'][0]['status'], 'missing')

    def test_half_open_endpoints_and_exact_equality(self):
        data = {"revisions": [revision("A", 1, 0, 50),
                              revision("B", 2, 50, 100)],
                "shipments": [shipment("P1", 49), shipment("P2", 50)],
                "as_of_seq": 2, "window_start": 0, "window_end": 100}
        result = temporal(data)
        by_id = {s['id']: s for s in result['shipments']}
        self.assertEqual(by_id['P1']['revision_id'], 'A')
        self.assertEqual(by_id['P2']['revision_id'], 'B')
        self.assertEqual([(g['start'], g['end']) for g in
                         result['timelines'][0]['segments']], [(0, 50), (50, 100)])

    def test_dominated_record_changes_coverage_but_not_frontier(self):
        # At 60 C leaves coverage while D stays dominant: the raw intervals
        # [50,60) and [60,70) must merge into one [50,70) D segment.
        data = acceptance_data(4, times=())
        segments = temporal(data)['timelines'][0]['segments']
        d_segments = [g for g in segments if g['frontier'] == ['D']]
        self.assertEqual(len(d_segments), 1)
        self.assertEqual((d_segments[0]['start'], d_segments[0]['end']), (50, 70))

    def test_equal_amounts_different_frontier_do_not_merge(self):
        # B replaces A at 30; identical money fields but a different identity
        # must keep two adjacent resolved segments.
        data = {"revisions": [
            revision("A", 1, 0, 30, per_step=100),
            revision("B", 2, 30, 100, per_step=100)],
            "shipments": [], "as_of_seq": 2,
            "window_start": 0, "window_end": 100}
        segments = temporal(data)['timelines'][0]['segments']
        self.assertEqual([(g['start'], g['end'], g['revision_id']) for g in segments],
                         [(0, 30, 'A'), (30, 100, 'B')])

    def test_future_only_key_gets_full_missing_timeline(self):
        data = {"revisions": [revision("A", 9, 0, 100, service='S', zone='Z')],
                "shipments": [shipment("P1", 10)],
                "as_of_seq": 3, "window_start": 0, "window_end": 100}
        result = temporal(data)
        segments = result['timelines'][0]['segments']
        self.assertEqual(len(segments), 1)
        self.assertEqual((segments[0]['start'], segments[0]['end']), (0, 100))
        self.assertEqual(segments[0]['status'], 'missing')
        self.assertEqual(result['shipments'][0]['status'], 'missing')

    def test_many_revisions_sharing_one_boundary(self):
        revs = [revision("R0", 1, 0, 50, per_step=100)]
        for i in range(1, 40):
            revs.append(revision(f"R{i}", i + 1, 50, 100, [f"R{i-1}"],
                                 per_step=100 + i))
        data = {"revisions": revs, "shipments": [shipment("P1", 50)],
                "as_of_seq": 40, "window_start": 0, "window_end": 100}
        result = temporal(data)
        # Only one boundary inside the window despite 40 records meeting there.
        self.assertEqual([(g['start'], g['end']) for g in
                          result['timelines'][0]['segments']], [(0, 50), (50, 100)])
        self.assertEqual(result['shipments'][0]['revision_id'], 'R39')

    def test_long_ancestor_chain(self):
        revs = [revision("R0", 1, 0, 100)]
        for i in range(1, 300):
            revs.append(revision(f"R{i}", i + 1, 0, 100, [f"R{i-1}"]))
        data = {"revisions": revs, "shipments": [shipment("P1", 50)],
                "as_of_seq": 301, "window_start": 0, "window_end": 100}
        result = temporal(data)
        self.assertEqual(result['timelines'][0]['segments'][0]['frontier'], ['R299'])
        self.assertEqual(result['shipments'][0]['revision_id'], 'R299')

    def test_multi_parent_merge_and_fork(self):
        data = {"revisions": [
            revision("A", 1, 0, 100),
            revision("B", 2, 10, 90, ["A"]),
            revision("C", 3, 20, 80, ["A"]),
            revision("D", 4, 30, 70, ["B", "C"])],
            "shipments": [shipment("P1", 25), shipment("P2", 50)],
            "as_of_seq": 4, "window_start": 0, "window_end": 100}
        result = temporal(data)
        by_id = {s['id']: s for s in result['shipments']}
        self.assertEqual(by_id['P1']['status'], 'ambiguous')
        self.assertEqual(by_id['P1']['frontier'], ['B', 'C'])
        self.assertEqual(by_id['P2']['revision_id'], 'D')


class RatingEvidenceTests(unittest.TestCase):
    def test_resolved_shipment_carries_full_v1_evidence(self):
        # Tariff identical to the v1 fixture tariff, parcel identical to its
        # P-1: base 250 + 10*40 + remote 125 = 775; fuel 66; insurance 500.
        data = {"revisions": [revision("A", 1, 0, 100, base=250, per_step=40,
                                       fuel=850, insurance=100, free=50000,
                                       remote=125, divisor=5000, girth=3000)],
                "shipments": [shipment("P1", 40, weight=900, length=400, width=300,
                                       height=200, declared=100000, remote=True)],
                "as_of_seq": 1, "window_start": 0, "window_end": 100}
        result = temporal(data)
        rated = result['shipments'][0]
        self.assertEqual(rated['status'], 'rated')
        self.assertEqual((rated['dimensional_weight_grams'],
                          rated['chargeable_weight_grams']), ('4800', 5000))
        self.assertEqual((rated['base_cents'], rated['fuel_cents'],
                          rated['insurance_cents'], rated['total_cents']),
                         (775, 66, 500, 1341))
        self.assertEqual(result['settled_total_cents'], 1341)
        self.assertEqual(result['total_cents'], 1341)

    def test_oversize_is_settled_but_unpaid(self):
        data = {"revisions": [revision("A", 1, 0, 100, girth=999)],
                "shipments": [shipment("P1", 40)],
                "as_of_seq": 1, "window_start": 0, "window_end": 100}
        result = temporal(data)
        self.assertEqual(result['oversize_count'], 1)
        self.assertEqual(result['rated_count'], 0)
        self.assertEqual(result['settled_total_cents'], 0)
        self.assertEqual(result['total_cents'], 0)
        self.assertEqual(set(result['shipments'][0]),
                         {'id', 'service', 'zone', 'shipped_at', 'status',
                          'frontier', 'revision_id', 'girth_mm'})
        self.assertEqual(result['shipments'][0]['girth_mm'], 1000)


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, payload, needle=''):
        with self.assertRaises(ValueError) as ctx:
            temporal(payload)
        self.assertIn(needle, str(ctx.exception))

    def base(self, **overrides):
        data = acceptance_data(2)
        data.update(overrides)
        return data

    def test_top_level_shape(self):
        good = self.base()
        self.assert_invalid({}, 'fields')
        bad = dict(good); bad['extra'] = 1
        self.assert_invalid(bad, 'fields')
        bad = dict(good); del bad['as_of_seq']
        self.assert_invalid(bad, 'fields')

    def test_time_coordinates_reject_bool_and_range(self):
        for field in ('window_start', 'window_end', 'as_of_seq'):
            bad = self.base(); bad[field] = True
            self.assert_invalid(bad)
        bad = self.base(); bad['as_of_seq'] = 10 ** 15 + 1
        self.assert_invalid(bad)
        bad = self.base(); bad['window_start'] = bad['window_end']
        self.assert_invalid(bad, 'window_start')
        bad = self.base(); bad['window_start'] = -1
        self.assert_invalid(bad)

    def test_revision_shape_and_bounds(self):
        bad = self.base(); bad['revisions'] = [dict(bad['revisions'][0], known_seq=0)]
        self.assert_invalid(bad)
        bad = self.base(); bad['revisions'] = [dict(bad['revisions'][0], start=100)]
        self.assert_invalid(bad, 'start')
        bad = self.base(); bad['revisions'][0]['revision_id'] = ' spaced '
        self.assert_invalid(bad)
        bad = self.base(); bad['revisions'][0]['tariff'] = {'service': 'S'}
        self.assert_invalid(bad, 'fields')
        bad = self.base(); bad['revisions'][0]['supersedes'] = ['A']
        self.assert_invalid(bad, 'itself')
        bad = self.base(); bad['revisions'][0]['supersedes'] = 'A'
        self.assert_invalid(bad, 'array')

    def test_duplicate_revision_id(self):
        bad = self.base()
        bad['revisions'][1]['revision_id'] = bad['revisions'][0]['revision_id']
        self.assert_invalid(bad, 'duplicate revision')

    def test_unknown_cross_key_and_equal_seq_parents(self):
        bad = self.base(); bad['revisions'][1]['supersedes'] = ['NOPE']
        self.assert_invalid(bad, 'unknown supersedes')
        bad = self.base()
        bad['revisions'][1]['supersedes'] = []
        bad['revisions'][2]['supersedes'] = []
        bad['revisions'][2]['known_seq'] = bad['revisions'][1]['known_seq']
        bad['revisions'][1]['supersedes'] = ['C']
        self.assert_invalid(bad, 'known_seq')
        bad = self.base()
        other = revision("X", 5, 0, 100, service='other', zone='Z')
        bad['revisions'].append(other)
        bad['revisions'][1]['supersedes'] = ['X']
        self.assert_invalid(bad, 'service and zone')

    def test_duplicate_supersedes_entry(self):
        bad = self.base()
        bad['revisions'][3]['supersedes'] = ['B', 'B']
        self.assert_invalid(bad, 'duplicate supersedes')

    def test_invisible_revision_still_validated(self):
        # A structurally illegal future revision fails the whole request.
        bad = self.base()
        bad['revisions'].append(revision("F", 99, 0, 100, ['GHOST']))
        self.assert_invalid(bad, 'unknown supersedes')

    def test_shipment_validation(self):
        bad = self.base(); bad['shipments'][0]['shipped_at'] = 100
        self.assert_invalid(bad, 'shipped_at')
        bad = self.base(); bad['shipments'][0]['shipped_at'] = -1
        self.assert_invalid(bad)
        bad = self.base(); bad['shipments'][0]['remote'] = 0
        self.assert_invalid(bad, 'boolean')
        bad = self.base(); bad['shipments'][1]['id'] = bad['shipments'][0]['id']
        self.assert_invalid(bad, 'duplicate shipment')
        bad = self.base(); del bad['shipments'][0]['zone']
        self.assert_invalid(bad, 'fields')

    def test_array_limits(self):
        bad = self.base()
        bad['revisions'] = [revision(f"R{i}", i + 1, 0, 100) for i in range(501)]
        self.assert_invalid(bad, 'bounded')
        bad = self.base()
        bad['shipments'] = [shipment(f"P{i:05d}", i % 100) for i in range(10001)]
        self.assert_invalid(bad, 'bounded')
        # Dense same-key graph: record R_i supersedes every earlier record,
        # giving 200*199/2 = 19900 edges; one extra parent on the last record
        # crosses the 20000-edge ceiling.
        revs = [revision("R0", 1, 0, 100)]
        for i in range(1, 200):
            revs.append(revision(f"R{i}", i + 1, 0, 100,
                                 [f"R{j}" for j in range(i)]))
        over = copy.deepcopy(self.base())
        over['revisions'] = revs
        self.assertEqual(sum(len(r['supersedes']) for r in revs), 19900)
        revs[-1]['supersedes'].append('R0')  # duplicate -> rejected first
        over_dup = copy.deepcopy(self.base())
        over_dup['revisions'] = revs
        self.assert_invalid(over_dup, 'duplicate supersedes')
        # Build a genuinely distinct 20001-edge graph with 201 records.
        more = [revision("R0", 1, 0, 100)]
        for i in range(1, 201):
            more.append(revision(f"R{i}", i + 1, 0, 100,
                                 [f"R{j}" for j in range(i)]))
        self.assertEqual(sum(len(r['supersedes']) for r in more), 20100)
        over_edge = copy.deepcopy(self.base())
        over_edge['revisions'] = more
        self.assert_invalid(over_edge, '20000')


class CausalityTests(unittest.TestCase):
    def test_knowledge_cutoff_changes_only_affected_shipment(self):
        at_three = temporal(acceptance_data(3))
        at_four = temporal(acceptance_data(4))
        before = {s['id']: s for s in at_three['shipments']}
        after = {s['id']: s for s in at_four['shipments']}
        # P1 at 55 is the only shipment inside the newly resolved [50,70).
        self.assertEqual(before['P1']['status'], 'ambiguous')
        self.assertEqual(after['P1']['status'], 'rated')
        self.assertEqual(after['P1']['revision_id'], 'D')
        # Every unaffected shipment keeps its exact identity and amounts.
        for sid in ('P0', 'P2', 'P3'):
            self.assertEqual(before[sid], after[sid])
        # Only the [40,60) ambiguous region and the new D region change;
        # the A/B segments outside [40,80) keep identical boundaries.
        def outer_segments(result):
            return [(g['start'], g['end'], g['status'], tuple(g['frontier']))
                    for g in result['timelines'][0]['segments']
                    if g['end'] <= 40 or g['start'] >= 80]
        self.assertEqual(outer_segments(at_three), outer_segments(at_four))

    def test_dominated_boundary_change_keeps_frontier_and_bill(self):
        # At as_of=4, C is dominated by D wherever D covers. Shrink C's end
        # from 60 to 55: on [55,60) the covering set loses C, but the
        # frontier stays {D}. Canonical segments, every shipment result and
        # the settled bill must remain identical to the original request.
        original = temporal(acceptance_data(4))
        changed = acceptance_data(4)
        changed['revisions'][2]['end'] = 55
        result = temporal(changed)
        self.assertEqual(result['timelines'], original['timelines'])
        self.assertEqual(result['shipments'], original['shipments'])
        self.assertEqual(result['settled_total_cents'],
                         original['settled_total_cents'])

    def test_effective_boundary_change_moves_authority_and_classification(self):
        # Extend B to start at 10 instead of 20: the [10,20) segment flips
        # from A to B, and a shipment at 15 flips with it.
        changed = acceptance_data(3)
        changed['revisions'][1]['start'] = 10
        changed['shipments'] = [shipment("Q", 15)]
        result = temporal(changed)
        segment = next(g for g in result['timelines'][0]['segments']
                       if g['start'] <= 15 < g['end'])
        self.assertEqual(segment['revision_id'], 'B')
        self.assertEqual(result['shipments'][0]['revision_id'], 'B')
        boundaries = [(g['start'], g['end']) for g in
                      result['timelines'][0]['segments']]
        self.assertIn((10, 40), [(s, e) for s, e in boundaries])

    def test_documented_entry_points(self):
        import parcel_rating.model as model
        # The public contract is parcel_rating.model.temporal(data); the
        # package also keeps its v1/v2 functions importable from the model.
        self.assertIs(model.temporal, temporal)
        self.assertTrue(callable(model.analyze))
        self.assertTrue(callable(model.plan))


class PermutationAndImmutabilityTests(unittest.TestCase):
    def test_permutation_invariance_and_no_mutation(self):
        rng = Random(7)
        data = acceptance_data(4)
        data['revisions'].append(revision("E", 5, 10, 90, service='S', zone='Q'))
        data['shipments'].append(shipment("Q1", 30, service='S', zone='Q'))
        snapshot = json.dumps(data, sort_keys=True)
        expected = temporal(data)
        self.assertEqual(json.dumps(data, sort_keys=True), snapshot)
        shuffled = copy.deepcopy(data)
        rng.shuffle(shuffled['revisions'])
        rng.shuffle(shuffled['shipments'])
        for row in shuffled['revisions']:
            rng.shuffle(row['supersedes'])
        self.assertEqual(temporal(shuffled), expected)
        self.assertEqual(json.dumps(shuffled, sort_keys=True),
                         json.dumps(copy.deepcopy(shuffled), sort_keys=True))


# ---------------------------------------------------------------------------
# Independent small-scale oracle: every integer point is enumerated and
# reachability is recomputed pairwise by DFS over the ORIGINAL graph.
# ---------------------------------------------------------------------------

def oracle_reachable(revisions, visible, start, target):
    stack = [start]
    seen = {start}
    while stack:
        cur = stack.pop()
        for parent in revisions[cur]['supersedes']:
            if parent in visible and parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return target in seen


def oracle_rate(parcel, t_row):
    weight, length, width, height, declared, remote = parcel
    girth = length + 2 * (width + height)
    if girth > t_row['max_girth_mm']:
        return 'oversize', {'girth_mm': girth}
    dim = Fraction(length * width * height, t_row['divisor_cm3_per_kg'])
    charge = max(Fraction(weight), dim)
    steps = -(-charge.numerator // (charge.denominator * t_row['step_grams']))
    chargeable = steps * t_row['step_grams']
    base = (t_row['base_cents'] + steps * t_row['per_step_cents']
            + (t_row['remote_cents'] if remote else 0))
    fuel = half_up(Fraction(base * t_row['fuel_bp'], 10000))
    insured = max(0, declared - t_row['insurance_free_cents'])
    insurance = half_up(Fraction(insured * t_row['insurance_bp'], 10000))
    return 'rated', {
        'actual_weight_grams': weight,
        'dimensional_weight_grams': str(dim),
        'chargeable_weight_grams': chargeable,
        'weight_steps': steps, 'girth_mm': girth,
        'base_cents': base, 'fuel_cents': fuel,
        'insurance_cents': insurance,
        'total_cents': base + fuel + insurance,
    }


def oracle_timeline(data):
    revisions = {r['revision_id']: r for r in data['revisions']}
    visible = {rid: r for rid, r in revisions.items()
               if r['known_seq'] <= data['as_of_seq']}
    keys = sorted({(r['tariff']['service'], r['tariff']['zone'])
                   for r in revisions.values()}
                  | {(s['service'], s['zone']) for s in data['shipments']})
    w0, w1 = data['window_start'], data['window_end']
    timelines = {}
    for key in keys:
        recs = [r for r in visible.values()
                if (r['tariff']['service'], r['tariff']['zone']) == key]
        raw = []
        for t in range(w0, w1):
            cover = [r['revision_id'] for r in recs if r['start'] <= t < r['end']]
            frontier = tuple(sorted(
                rid for rid in cover
                if not any(other != rid
                           and oracle_reachable(revisions, visible, other, rid)
                           for other in cover)))
            status = ('missing' if not frontier else
                      'resolved' if len(frontier) == 1 else 'ambiguous')
            raw.append((status, frontier))
        segments = []
        for offset, (status, frontier) in enumerate(raw):
            t = w0 + offset
            if segments and segments[-1]['_status'] == status \
                    and tuple(segments[-1]['frontier']) == frontier:
                segments[-1]['end'] = t + 1
            else:
                segments.append({'start': t, 'end': t + 1, 'status': status,
                                 'frontier': list(frontier),
                                 'revision_id': frontier[0] if status == 'resolved' else None,
                                 '_status': status})
        for segment in segments:
            del segment['_status']
        timelines[key] = segments
    return timelines, visible, revisions


class OracleComparisonTests(unittest.TestCase):
    def build_case(self, seed):
        rng = Random(seed)
        w0, w1 = 0, 24
        n_keys = 1 + rng.randrange(4)
        keys = [(f"S{k}", "Z") for k in range(n_keys)]
        revisions = []
        rid_counter = 0
        for key_index, key in enumerate(keys):
            n_rev = rng.randrange(0, 7)
            local = []
            for _ in range(n_rev):
                rid = f"K{key_index}-R{rid_counter:03d}"
                rid_counter += 1
                seq = rng.randrange(1, 9)
                start = rng.randrange(-0, 18)
                end = rng.randrange(max(start + 1, 1), 25)
                parents = []
                if local and rng.randrange(2):
                    # Only same-key records can be parents, and only records
                    # with a strictly smaller known_seq.
                    pool = [(pid, pseq) for pid, pseq in local if pseq < seq]
                    rng.shuffle(pool)
                    take = rng.randrange(1, max(2, len(pool) + 1))
                    parents = [pid for pid, _ in pool[:take]]
                local.append((rid, seq))
                revisions.append(revision(
                    rid, seq, start, end, parents, service=key[0], zone=key[1],
                    per_step=rng.choice((0, 50, 100, 200)),
                    girth=rng.choice((800, 2000, 5000)),
                    fuel=rng.choice((0, 50, 850)),
                    insurance=rng.choice((0, 100)), free=rng.choice((0, 5000))))
        as_of = rng.randrange(0, 10)
        shipments = []
        for i in range(rng.randrange(0, 9)):
            if rng.randrange(5) == 0:
                service, zone = f"ghost{i}", "ZZ"  # key with no revisions
            else:
                service, zone = keys[rng.randrange(n_keys)]
            length = rng.choice((100, 600, 2001))
            shipments.append(shipment(
                f"P{i:02d}", rng.randrange(w0, w1), service=service, zone=zone,
                weight=rng.choice((100, 500, 900)), length=length,
                width=100, height=100,
                declared=rng.choice((0, 10000, 60000)),
                remote=rng.choice((True, False))))
        return {"revisions": revisions, "shipments": shipments,
                "as_of_seq": as_of, "window_start": w0, "window_end": w1}

    def test_matches_pointwise_oracle(self):
        for seed in range(120):
            with self.subTest(seed=seed):
                data = self.build_case(seed)
                result = temporal(copy.deepcopy(data))
                expected_timelines, visible, revisions = oracle_timeline(data)
                self.assertEqual([(t['service'], t['zone']) for t in result['timelines']],
                                 sorted(expected_timelines))
                for timeline in result['timelines']:
                    key = (timeline['service'], timeline['zone'])
                    self.assertEqual(timeline['segments'], expected_timelines[key])
                    # Coverage of the whole window, no gaps or overlaps.
                    segments = timeline['segments']
                    self.assertEqual(segments[0]['start'], data['window_start'])
                    self.assertEqual(segments[-1]['end'], data['window_end'])
                    for a, b in zip(segments, segments[1:]):
                        self.assertEqual(a['end'], b['start'])
                by_id = {s['id']: s for s in result['shipments']}
                for row in data['shipments']:
                    got = by_id[row['id']]
                    segments = expected_timelines[(row['service'], row['zone'])]
                    segment = next(g for g in segments
                                   if g['start'] <= row['shipped_at'] < g['end'])
                    self.assertEqual(got['frontier'], segment['frontier'])
                    self.assertEqual(got['revision_id'], segment['revision_id'])
                    if segment['status'] != 'resolved':
                        self.assertEqual(got['status'], segment['status'])
                        continue
                    t_row = visible[segment['revision_id']]['tariff']
                    parcel = (row['weight_grams'], row['length_mm'], row['width_mm'],
                              row['height_mm'], row['declared_cents'], row['remote'])
                    status, detail = oracle_rate(parcel, t_row)
                    self.assertEqual(got['status'], status)
                    for field, value in detail.items():
                        self.assertEqual(got[field], value, (seed, field))
                counts = (result['rated_count'] + result['oversize_count']
                          + result['missing_count'] + result['ambiguous_count'])
                self.assertEqual(counts, len(data['shipments']))
                rated_total = sum(s['total_cents'] for s in result['shipments']
                                  if s['status'] == 'rated')
                self.assertEqual(rated_total, result['settled_total_cents'])
                if result['missing_count'] + result['ambiguous_count']:
                    self.assertIsNone(result['total_cents'])
                else:
                    self.assertEqual(result['total_cents'], result['settled_total_cents'])


class ScaleTests(unittest.TestCase):
    def test_full_limits_with_huge_coordinates(self):
        # 500 revisions across 4 keys, exactly 20000 supersedes edges,
        # 10000 shipments and coordinates spanning the full 0..10^15 range.
        rng = Random(11)
        revisions = []
        per_key = 125
        edge_count = 0
        window = 10 ** 15
        for k in range(4):
            span = window // 4
            base = k * span
            for i in range(per_key):
                rid = f"R{k:02d}-{i:03d}"
                seq = k * per_key + i + 1
                width = max(1, span // per_key)
                start = base + i * width
                end = min(window, start + 2 * width)
                parents = [f"R{k:02d}-{j:03d}"
                           for j in range(max(0, i - 50), i)]
                if k == 0 and i >= 51:
                    parents.append(f"R{k:02d}-000")
                if k == 1 and 51 <= i < 77:
                    parents.append(f"R{k:02d}-000")
                edge_count += len(parents)
                revisions.append(revision(rid, seq, start, end, parents,
                                          service=f"S{k}", zone='Z',
                                          per_step=10 + i, girth=10 ** 9))
        self.assertEqual(edge_count, 20000)
        shipments = []
        for i in range(10000):
            k = rng.randrange(5)  # key 4 exists in shipments only
            shipments.append(shipment(
                f"P{i:05d}", rng.randrange(0, window),
                service=f"S{k}", zone='Z', length=100, width=100, height=100,
                weight=100))
        data = {"revisions": revisions, "shipments": shipments,
                "as_of_seq": 10 ** 15, "window_start": 0, "window_end": window}
        started = time.perf_counter()
        result = temporal(data)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 15.0)
        self.assertEqual(len(result['timelines']), 5)
        for timeline in result['timelines']:
            self.assertEqual(timeline['segments'][0]['start'], 0)
            self.assertEqual(timeline['segments'][-1]['end'], window)
            for a, b in zip(timeline['segments'], timeline['segments'][1:]):
                self.assertEqual(a['end'], b['start'])
        self.assertEqual(result['rated_count'] + result['oversize_count']
                         + result['missing_count'] + result['ambiguous_count'], 10000)
        # Every shipment reuses a segment identity rather than a fresh lookup.
        segment_ids = {(t['service'], t['zone'], tuple(g['frontier']))
                       for t in result['timelines'] for g in t['segments']}
        for row in result['shipments']:
            self.assertIn((row['service'], row['zone'], tuple(row['frontier'])),
                          segment_ids)


if __name__ == '__main__':
    unittest.main()
