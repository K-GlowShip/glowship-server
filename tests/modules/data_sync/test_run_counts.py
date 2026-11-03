import pytest

from app.modules.data_sync.application.ingest.run_counts import RunCounts
from app.modules.data_sync.infrastructure.data_sync_enums import DataSyncRunStatus


def test_no_rejection_is_succeeded():
    assert (
        RunCounts(received=10, loaded=10, rejected=0).status
        == DataSyncRunStatus.SUCCEEDED
    )
    assert (
        RunCounts(received=0, loaded=0, rejected=0).status
        == DataSyncRunStatus.SUCCEEDED
    )


def test_any_rejection_is_partial_success():
    assert (
        RunCounts(received=10, loaded=9, rejected=1).status
        == DataSyncRunStatus.PARTIAL_SUCCESS
    )
    assert (
        RunCounts(received=3, loaded=0, rejected=3).status
        == DataSyncRunStatus.PARTIAL_SUCCESS
    )


@pytest.mark.parametrize(
    ("received", "loaded", "rejected"),
    [(10, 9, 0), (1, 1, 1), (-1, -1, 0)],
)
def test_counts_must_add_up(received, loaded, rejected):
    with pytest.raises(ValueError):
        RunCounts(received=received, loaded=loaded, rejected=rejected)
