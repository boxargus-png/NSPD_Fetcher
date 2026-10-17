import copy
import unittest
from unittest.mock import Mock, patch

from tests.support import plugin_module

dedup = plugin_module("feature_dedup")


def feature():
    return {"type": "Feature", "id": 291607188,
            "geometry": {"type": "Point", "coordinates": [1000.1234, 2000.5678]},
            "properties": {"category": 36368, "options": {"cad_num": "52:18:0050232:698"}}}


class DeduplicationTests(unittest.TestCase):
    def test_exact_repeats_ignore_dictionary_order_and_keep_first_without_mutation(self):
        first = feature()
        repeated = dict(reversed(list(copy.deepcopy(first).items())))
        repeated["properties"] = dict(reversed(list(repeated["properties"].items())))
        other = dict(copy.deepcopy(first), id=42)
        features = [first, repeated, other, copy.deepcopy(first), copy.deepcopy(other)]
        original = copy.deepcopy(features)
        result = dedup.deduplicate_features(features)
        self.assertEqual(result, [first, other])
        self.assertIs(result[0], first)
        self.assertEqual(features, original)

    def test_any_difference_in_identity_geometry_or_attributes_keeps_both(self):
        first = feature()
        variants = []
        different_id = copy.deepcopy(first)
        different_id["id"] += 1
        variants.append(different_id)
        different_category = copy.deepcopy(first)
        different_category["properties"]["category"] += 1
        variants.append(different_category)
        different_geometry = copy.deepcopy(first)
        different_geometry["geometry"]["coordinates"][0] += 0.000001
        variants.append(different_geometry)
        different_attributes = copy.deepcopy(first)
        different_attributes["properties"]["options"]["cad_num"] = "52:18:0050232:699"
        variants.append(different_attributes)
        different_metadata = dict(copy.deepcopy(first), bbox=[0, 0, 2, 2])
        variants.append(different_metadata)
        for second in variants:
            with self.subTest(second=second):
                self.assertEqual(dedup.deduplicate_features([first, second, copy.deepcopy(second)]),
                                 [first, second])

    def test_missing_or_unusable_identity_never_merges(self):
        for field in ("id", "category"):
            for value in (None, "", " ", True, [], {}, 1.5):
                record = feature()
                target = record if field == "id" else record["properties"]
                target[field] = value
                with self.subTest(field=field, value=value):
                    self.assertEqual(len(dedup.deduplicate_features([record, copy.deepcopy(record)])), 2)
            del target[field]
            self.assertEqual(len(dedup.deduplicate_features([record, copy.deepcopy(record)])), 2)

    def test_no_type_conversion_or_vertex_reordering(self):
        first = feature()
        second = copy.deepcopy(first)
        second["id"] = str(second["id"])
        self.assertEqual(len(dedup.deduplicate_features([first, second])), 2)
        for a, b in ((True, 1), (1, "1"), (1, 1.0), (None, "")):
            first["properties"]["value"] = a
            second = copy.deepcopy(first)
            second["properties"]["value"] = b
            self.assertEqual(len(dedup.deduplicate_features([first, second])), 2)
        first["geometry"] = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
        second = copy.deepcopy(first)
        second["geometry"]["coordinates"][0].reverse()
        self.assertEqual(len(dedup.deduplicate_features([first, second])), 2)

    def test_null_geometry_can_repeat_but_missing_geometry_is_preserved(self):
        record = feature()
        record["geometry"] = None
        self.assertEqual(dedup.deduplicate_features([record, copy.deepcopy(record)]), [record])
        del record["geometry"]
        self.assertEqual(len(dedup.deduplicate_features([record, copy.deepcopy(record)])), 2)

    def test_non_json_values_do_not_cause_accidental_merging(self):
        record = feature()
        record["properties"]["value"] = float("nan")
        self.assertEqual(len(dedup.deduplicate_features([record, copy.deepcopy(record)])), 2)

    def test_hash_collision_still_requires_matching_content(self):
        first = feature()
        second = dict(copy.deepcopy(first), id=42)
        constant_hash = Mock()
        constant_hash.digest.return_value = b"same hash"
        with patch.object(dedup.hashlib, "sha256", return_value=constant_hash):
            self.assertEqual(dedup.deduplicate_features([first, second, copy.deepcopy(first)]),
                             [first, second])
