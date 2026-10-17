"""Request and batch results, independent of Qt and HTTP libraries."""
from enum import Enum
from dataclasses import dataclass


@dataclass(frozen=True)
class FetchProgress:
    """Immutable, geometry-free snapshot safe to deliver to the GUI thread."""
    phase: str = "preparing"
    total: int = 0
    completed: int = 0
    current: int = 0
    detail: str = ""
    received: int = 0
    retained: int = 0
    excluded: int = 0
    unchecked: int = 0
    errors: int = 0
    empty: int = 0
    wait_seconds: int = 0


class FetchStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"


def feature_collection(features=None):
    return {"type": "FeatureCollection", "features": list(features or [])}


class RequestOutcome:
    def __init__(self, key, features=None, error=None, received_count=None):
        self.key = key
        self.features = list(features or [])
        self.error = error
        self.received_count = len(self.features) if received_count is None else received_count

    @property
    def not_found(self):
        return self.error is None and self.received_count == 0


class FetchResult:
    def __init__(self):
        self.status = FetchStatus.PENDING
        self.outcomes = []
        self.collections = {}
        self.completed_categories = []

    def record(self, key, category, features=None, error=None, received_count=None):
        outcome = RequestOutcome(key, features, error, received_count)
        self.outcomes.append(outcome)
        collection = self.collections.setdefault(category, feature_collection())
        collection["features"].extend(outcome.features)

    @property
    def errors(self):
        return [outcome for outcome in self.outcomes if outcome.error is not None]

    @property
    def not_found(self):
        return [outcome.key for outcome in self.outcomes if outcome.not_found]

    @property
    def feature_count(self):
        return sum(len(collection["features"]) for collection in self.collections.values())

    @property
    def received_count(self):
        return sum(outcome.received_count for outcome in self.outcomes)

    def finish(self, canceled=False):
        if canceled:
            self.status = FetchStatus.CANCELED
        elif self.errors:
            successful = any(outcome.error is None for outcome in self.outcomes)
            self.status = FetchStatus.PARTIAL if successful else FetchStatus.FAILED
        else:
            self.status = FetchStatus.SUCCESS if self.feature_count else FetchStatus.EMPTY

