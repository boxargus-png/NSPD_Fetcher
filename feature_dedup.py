"""Conservative deduplication of original NSPD records, before OGR/Processing."""
import hashlib
import json


def _identifier(value):
    return type(value) is int or (isinstance(value, str) and bool(value.strip()))


def _signature(feature):
    # Ignore dictionary order, but preserve value types, array/vertex order
    # and every field, including any extra server metadata. Never round.
    return json.dumps(feature, sort_keys=True, separators=(",", ":"), allow_nan=False)


def deduplicate_features(features):
    """Keep the first exact record; missing/unusable identities never merge.

    Returned items are original references in their original order. The caller
    must copy them before normalization. Only fingerprints and references are
    retained here, without another serialized copy of every large geometry.
    """
    unique = []
    seen = {}
    for feature in features:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if (not isinstance(properties, dict) or "geometry" not in feature
                or not _identifier(properties.get("category"))
                or not _identifier(feature.get("id"))):
            unique.append(feature)
            continue
        try:
            signature = _signature(feature)
        except (TypeError, ValueError):
            unique.append(feature)
            continue
        digest = hashlib.sha256(signature.encode("ascii")).digest()
        candidates = seen.setdefault(digest, [])
        # Compare the actual records too: a hash collision cannot delete data.
        if any(_signature(candidate) == signature for candidate in candidates):
            continue
        candidates.append(feature)
        unique.append(feature)
    return unique
