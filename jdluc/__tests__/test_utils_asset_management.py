"""Unit tests for ``utils/asset_management.py`` (no GEE credentials)."""

from unittest.mock import patch

import ee
import pytest

from jdluc.utils.asset_management import (
    delete_asset_safely,
    list_assets_matching,
    parse_version_from_asset_id,
)
from jdluc.utils.constants import GEE_ASSET_ROOT

# ---------- parse_version_from_asset_id -------------------------------------


@pytest.mark.parametrize(
    'asset_id, expected',
    [
        # Clean SHA
        (f'{GEE_ASSET_ROOT}/land_use_delaware_a0d76ac0aa12', 'a0d76ac0aa12'),
        # Dirty SHA
        (
            f'{GEE_ASSET_ROOT}/emissions_delaware_a0d76ac0aa12-dirty-3f2b8c01',
            'a0d76ac0aa12-dirty-3f2b8c01',
        ),
        # Extract-style, single segment
        (f'{GEE_ASSET_ROOT}/harris_agb_conus_v2021', 'v2021'),
        # Extract-style with multi-segment version
        (f'{GEE_ASSET_ROOT}/nass_yields_v2017_2020', 'v2017_2020'),
        # No parseable version suffix — name with only base
        (f'{GEE_ASSET_ROOT}/ipcc_climate_zones', None),
        # Garbage suffix
        (f'{GEE_ASSET_ROOT}/land_use_delaware_notasha', None),
        # Short hex (11 chars) does not satisfy the 12-char SHA pattern
        (f'{GEE_ASSET_ROOT}/land_use_delaware_abcdef012345_', None),
    ],
)
def test_parse_version_from_asset_id(asset_id: str, expected: str | None) -> None:
    assert parse_version_from_asset_id(asset_id) == expected


# ---------- list_assets_matching --------------------------------------------


def test_list_assets_matching_filters_by_prefix() -> None:
    """Only assets whose name starts with the full prefix are returned."""
    assets = [
        {'name': f'{GEE_ASSET_ROOT}/land_use_delaware_abcdef012345'},
        {'name': f'{GEE_ASSET_ROOT}/land_use_iowa_abcdef012345'},
        {'name': f'{GEE_ASSET_ROOT}/emissions_delaware_abcdef012345'},
    ]

    with patch.object(
        ee.data, 'listAssets', return_value={'assets': assets}
    ) as mock_list:
        result = list_assets_matching('land_use_delaware')

    assert result == [f'{GEE_ASSET_ROOT}/land_use_delaware_abcdef012345']
    mock_list.assert_called_once_with({'parent': GEE_ASSET_ROOT})


def test_list_assets_matching_paginates() -> None:
    """nextPageToken drives a follow-up call with the token attached."""
    page1 = {
        'assets': [{'name': f'{GEE_ASSET_ROOT}/land_use_delaware_a' * 1}],
        'nextPageToken': 'tok',
    }
    page2 = {
        'assets': [{'name': f'{GEE_ASSET_ROOT}/land_use_delaware_b'}],
    }

    with patch.object(ee.data, 'listAssets', side_effect=[page1, page2]) as mock_list:
        result = list_assets_matching('land_use_delaware')

    assert result == [
        f'{GEE_ASSET_ROOT}/land_use_delaware_a',
        f'{GEE_ASSET_ROOT}/land_use_delaware_b',
    ]
    assert mock_list.call_count == 2
    mock_list.assert_any_call({'parent': GEE_ASSET_ROOT})
    mock_list.assert_any_call({'parent': GEE_ASSET_ROOT, 'pageToken': 'tok'})


def test_list_assets_matching_handles_empty_assets_field() -> None:
    with patch.object(ee.data, 'listAssets', return_value={}):
        assert list_assets_matching('land_use_delaware') == []


# ---------- delete_asset_safely ---------------------------------------------


def test_delete_asset_safely_happy_path() -> None:
    with patch.object(ee.data, 'deleteAsset') as mock_delete:
        ok = delete_asset_safely(f'{GEE_ASSET_ROOT}/land_use_delaware_x')
    assert ok is True
    mock_delete.assert_called_once_with(f'{GEE_ASSET_ROOT}/land_use_delaware_x')


def test_delete_asset_safely_dry_run_skips_deletion() -> None:
    with patch.object(ee.data, 'deleteAsset') as mock_delete:
        ok = delete_asset_safely(f'{GEE_ASSET_ROOT}/land_use_delaware_x', dry_run=True)
    assert ok is True
    mock_delete.assert_not_called()


def test_delete_asset_safely_returns_false_on_ee_exception() -> None:
    with patch.object(ee.data, 'deleteAsset', side_effect=ee.EEException('boom')):
        ok = delete_asset_safely(f'{GEE_ASSET_ROOT}/land_use_delaware_x')
    assert ok is False
