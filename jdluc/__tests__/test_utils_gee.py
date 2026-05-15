"""Unit tests for ``utils/gee.py`` helpers (no GEE credentials)."""

from unittest.mock import patch

import ee
import pytest

from jdluc.utils.gee import (
    asset_exists,
    asset_is_populated,
    wait_for_tasks,
)

# ---------- asset_exists ----------------------------------------------------


def test_asset_exists_true_when_getAsset_succeeds() -> None:
    with patch.object(ee.data, 'getAsset', return_value={'name': 'x', 'type': 'IMAGE'}):
        assert asset_exists('projects/p/assets/x') is True


def test_asset_exists_false_when_getAsset_raises() -> None:
    with patch.object(ee.data, 'getAsset', side_effect=ee.EEException('not found')):
        assert asset_exists('projects/p/assets/x') is False


# ---------- asset_is_populated ----------------------------------------------


def test_asset_is_populated_false_when_asset_absent() -> None:
    with patch.object(ee.data, 'getAsset', side_effect=ee.EEException('not found')):
        assert asset_is_populated('projects/p/assets/x') is False


def test_asset_is_populated_true_for_existing_image() -> None:
    """Non-collection assets are always treated as populated when present."""
    with patch.object(ee.data, 'getAsset', return_value={'type': 'IMAGE'}):
        assert asset_is_populated('projects/p/assets/x') is True


def test_asset_is_populated_true_for_existing_table() -> None:
    """Tables (FeatureCollections) are always populated when present."""
    with patch.object(ee.data, 'getAsset', return_value={'type': 'TABLE'}):
        assert asset_is_populated('projects/p/assets/x') is True


def test_asset_is_populated_false_for_empty_image_collection() -> None:
    """An IC with no children is the partial-ingest trap we want to catch."""
    with (
        patch.object(ee.data, 'getAsset', return_value={'type': 'IMAGE_COLLECTION'}),
        patch.object(ee.data, 'listAssets', return_value={'assets': []}),
    ):
        assert asset_is_populated('projects/p/assets/ic') is False


def test_asset_is_populated_true_for_image_collection_with_children() -> None:
    with (
        patch.object(ee.data, 'getAsset', return_value={'type': 'IMAGE_COLLECTION'}),
        patch.object(
            ee.data,
            'listAssets',
            return_value={'assets': [{'name': 'projects/p/assets/ic/tile_x'}]},
        ),
    ):
        assert asset_is_populated('projects/p/assets/ic') is True


# ---------- wait_for_tasks --------------------------------------------------


def _row(task_id: str, state: str, error_message: str = '') -> dict[str, str]:
    return {'id': task_id, 'state': state, 'error_message': error_message}


def test_wait_for_tasks_single_completed_returns_empty() -> None:
    """One task, COMPLETED on first poll → empty failure dict, no sleep."""
    with (
        patch.object(
            ee.data, 'getTaskStatus', return_value=[_row('T1', 'COMPLETED')]
        ) as mock_status,
        patch('jdluc.utils.gee.time.sleep') as mock_sleep,
    ):
        failed = wait_for_tasks(['T1'], poll_interval_s=0.01, timeout_s=5.0)
    assert failed == {}
    assert mock_status.call_count == 1
    mock_sleep.assert_not_called()


def test_wait_for_tasks_mixed_completed_and_failed() -> None:
    """One COMPLETED + one FAILED in same poll → only failed in result."""
    rows = [
        _row('T1', 'COMPLETED'),
        _row('T2', 'FAILED', 'manifest invalid'),
    ]
    with (
        patch.object(ee.data, 'getTaskStatus', return_value=rows),
        patch('jdluc.utils.gee.time.sleep'),
    ):
        failed = wait_for_tasks(
            ['T1', 'T2'],
            asset_ids_for_logging={'T1': 'asset/one', 'T2': 'asset/two'},
            poll_interval_s=0.01,
            timeout_s=5.0,
        )
    assert failed == {'T2': 'manifest invalid'}


def test_wait_for_tasks_two_poll_sequence() -> None:
    """First poll RUNNING, second poll COMPLETED → returns after second poll."""
    sequence = [
        [_row('T1', 'RUNNING')],
        [_row('T1', 'COMPLETED')],
    ]
    with (
        patch.object(ee.data, 'getTaskStatus', side_effect=sequence) as mock_status,
        patch('jdluc.utils.gee.time.sleep') as mock_sleep,
    ):
        failed = wait_for_tasks(['T1'], poll_interval_s=0.01, timeout_s=5.0)
    assert failed == {}
    assert mock_status.call_count == 2
    # Sleep once between the two polls.
    assert mock_sleep.call_count == 1


def test_wait_for_tasks_timeout_when_perpetually_running() -> None:
    """Forever-RUNNING → TimeoutError after the configured budget."""

    def _always_running(ids: list[str]) -> list[dict[str, str]]:
        return [_row(tid, 'RUNNING') for tid in ids]

    fake_now = iter([0.0, 0.0, 100.0, 200.0])

    def _monotonic() -> float:
        try:
            return next(fake_now)
        except StopIteration:
            return 1e9

    with (
        patch.object(ee.data, 'getTaskStatus', side_effect=_always_running),
        patch('jdluc.utils.gee.time.sleep'),
        patch('jdluc.utils.gee.time.monotonic', _monotonic),
        pytest.raises(TimeoutError),
    ):
        wait_for_tasks(['T1'], poll_interval_s=0.01, timeout_s=10.0)


def test_wait_for_tasks_chunks_at_100() -> None:
    """250 tasks → getTaskStatus called with chunk sizes 100, 100, 50."""
    task_ids = [f'T{i}' for i in range(250)]
    chunk_sizes: list[int] = []

    def _capture(ids: list[str]) -> list[dict[str, str]]:
        chunk_sizes.append(len(ids))
        return [_row(tid, 'COMPLETED') for tid in ids]

    with (
        patch.object(ee.data, 'getTaskStatus', side_effect=_capture),
        patch('jdluc.utils.gee.time.sleep'),
    ):
        failed = wait_for_tasks(task_ids, poll_interval_s=0.01, timeout_s=5.0)

    assert failed == {}
    # Set comparison — wait_for_tasks builds the chunk list from a set,
    # so chunk *order* isn't deterministic, but chunk *sizes* are.
    assert sorted(chunk_sizes) == [50, 100, 100]
