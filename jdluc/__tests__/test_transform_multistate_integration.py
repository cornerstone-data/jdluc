"""Integration tests for multi-state transform runs (Iowa + Nebraska + SD).

Exercises the per-state phase tracker + consolidation against
pre-exported per-state and consolidated assets for the
`great_plains_test` region (FIPS 19, 31, 46). Requires a live GEE
connection and a prior successful run via:

    uv run python jdluc/cli.py --region great_plains_test

All tests are marked @pytest.mark.integration and excluded from CI by
default (run with: ``pytest -m integration``).

The failure-injection + resume behavior is covered at the
unit-test level in test_transform.py via mocked task handles;
reproducing it here against live GEE is a manual procedure documented
below (§ Manual failure-injection procedure).
"""

import math
from typing import Any

import ee
import pytest

from jdluc.utils.asset_management import list_assets_matching
from jdluc.utils.constants import (
    CROPS_TABLE_COLUMNS,
    GCP_PROJECT,
    STATE_FIPS_TO_NAME,
    TRANSITIONS_TABLE_COLUMNS,
)
from jdluc.utils.states import (
    get_multi_state_boundary,
)
from jdluc.utils.version import compute_transform_version

# Multi-state test region: Iowa + Nebraska + South Dakota.
_STATES: list[str] = ['19', '31', '46']
_REGION_NAME = 'great_plains_test'

_ROUND_TRIP_SLACK_TCO2 = 10.0  # Larger than Delaware's ~1 because IA alone
# has ~100x more emissions; 10 tCO2 is still float32 noise.

_REDUCE_SCALE = 30
_REDUCE_MAX_PIXELS = int(1e12)  # 3 states, ~6B pixels total


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def gee_init() -> None:
    from jdluc.utils.gee import initialize_gee

    initialize_gee(GCP_PROJECT)


@pytest.fixture(scope='module')
def _current_version(gee_init: None) -> str:
    return compute_transform_version()


def _resolve_asset_id(prefix: str, region_or_state: str, version: str) -> str:
    """Find an asset ID by (prefix_region_version) suffix; skip if missing."""
    expected_suffix = f'{prefix}_{region_or_state}_{version}'
    for asset_id in list_assets_matching(f'{prefix}_{region_or_state}'):
        if asset_id.endswith(expected_suffix):
            return asset_id
    pytest.skip(
        f'No {prefix} asset for {region_or_state} v{version}. '
        f'Run cli.py --region {_REGION_NAME} first.'
    )


@pytest.fixture(scope='module')
def consolidated_asset_ids(_current_version: str) -> dict[str, str]:
    """IDs for the four consolidated region-level assets."""
    return {
        kind: _resolve_asset_id(kind, _REGION_NAME, _current_version)
        for kind in ('land_use', 'emissions', 'transitions', 'crops')
    }


@pytest.fixture(scope='module')
def per_state_asset_ids(_current_version: str) -> dict[str, dict[str, str]]:
    """{state_fips: {asset_kind: asset_id}} for every state and kind."""
    out: dict[str, dict[str, str]] = {}
    for fips in _STATES:
        state_name = STATE_FIPS_TO_NAME[fips]
        out[fips] = {
            kind: _resolve_asset_id(kind, state_name, _current_version)
            for kind in ('land_use', 'emissions', 'transitions', 'crops')
        }
    return out


@pytest.fixture(scope='module')
def region_geometry(gee_init: None) -> Any:
    return get_multi_state_boundary(_STATES)


def _reduce_band_sum(asset_id: str, band: str, geometry: Any) -> float:
    """Sum one band of an image asset over a geometry."""
    value = (
        ee.Image(asset_id)
        .select(band)
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .get(band)
        .getInfo()
    )
    return float(value) if value is not None else 0.0


def _table_size(asset_id: str) -> int:
    return int(ee.FeatureCollection(asset_id).size().getInfo())


def _table_column_sum(asset_id: str, column: str) -> float:
    value = ee.FeatureCollection(asset_id).aggregate_sum(column).getInfo()
    return float(value) if value is not None else 0.0


# ---------------------------------------------------------------------------
# Multi-state happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_all_per_state_assets_exist(
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """Each of 3 states has all 4 per-state assets at the current version."""
    for fips in _STATES:
        for kind in ('land_use', 'emissions', 'transitions', 'crops'):
            assert per_state_asset_ids[fips][
                kind
            ], f'missing per-state {kind} for fips {fips}'


@pytest.mark.integration
def test_all_consolidated_assets_exist(
    consolidated_asset_ids: dict[str, str],
) -> None:
    """All 4 consolidated assets exist under the great_plains_test label."""
    for kind in ('land_use', 'emissions', 'transitions', 'crops'):
        assert consolidated_asset_ids[kind].endswith(
            f'{kind}_{_REGION_NAME}_{compute_transform_version()}'
        ), f'consolidated {kind} asset ID mismatch'


@pytest.mark.integration
def test_per_state_emissions_non_negative(
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """Each state's allocated_luc_emissions_2020 band totals to >= 0."""
    for fips in _STATES:
        total = _reduce_band_sum(
            per_state_asset_ids[fips]['emissions'],
            'allocated_luc_emissions_2020',
            get_multi_state_boundary([fips]),
        )
        assert total >= 0.0, f'negative allocated LUC total in state {fips}'


@pytest.mark.integration
def test_per_state_transitions_have_rows(
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """Each state's transitions table has >= 20 rows (meaningful coverage)."""
    for fips in _STATES:
        size = _table_size(per_state_asset_ids[fips]['transitions'])
        assert size >= 20, f'state {fips} transitions table has only {size} rows'


@pytest.mark.integration
def test_per_state_crops_have_three_groups(
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """Each state's crops table covers corn, soybeans, and wheat."""
    for fips in _STATES:
        fc = ee.FeatureCollection(per_state_asset_ids[fips]['crops'])
        groups = set(fc.aggregate_array('crop_group').getInfo())
        assert groups == {
            'corn',
            'soybeans',
            'wheat',
        }, f'state {fips} crops table missing groups: {groups}'


# ---------------------------------------------------------------------------
# Consolidated / per-state equivalence
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_consolidated_emissions_matches_per_state_sum(
    consolidated_asset_ids: dict[str, str],
    per_state_asset_ids: dict[str, dict[str, str]],
    region_geometry: Any,
) -> None:
    """Σ per-state allocated LUC = consolidated allocated LUC within slack."""
    consolidated_sum = _reduce_band_sum(
        consolidated_asset_ids['emissions'],
        'allocated_luc_emissions_2020',
        region_geometry,
    )
    per_state_sum = sum(
        _reduce_band_sum(
            per_state_asset_ids[fips]['emissions'],
            'allocated_luc_emissions_2020',
            get_multi_state_boundary([fips]),
        )
        for fips in _STATES
    )
    # Larger slack than Delaware: 3 states sum up to ~1e6 tCO2, 10 is noise.
    assert (
        abs(consolidated_sum - per_state_sum) <= _ROUND_TRIP_SLACK_TCO2
    ), f'consolidated={consolidated_sum}, per-state sum={per_state_sum}'


@pytest.mark.integration
def test_consolidated_peatland_matches_per_state_sum(
    consolidated_asset_ids: dict[str, str],
    per_state_asset_ids: dict[str, dict[str, str]],
    region_geometry: Any,
) -> None:
    """Same round-trip check on the peatland band."""
    consolidated_sum = _reduce_band_sum(
        consolidated_asset_ids['emissions'],
        'allocated_peatland_emissions_2020',
        region_geometry,
    )
    per_state_sum = sum(
        _reduce_band_sum(
            per_state_asset_ids[fips]['emissions'],
            'allocated_peatland_emissions_2020',
            get_multi_state_boundary([fips]),
        )
        for fips in _STATES
    )
    assert abs(consolidated_sum - per_state_sum) <= _ROUND_TRIP_SLACK_TCO2


@pytest.mark.integration
def test_consolidated_transitions_row_count_equals_per_state_sum(
    consolidated_asset_ids: dict[str, str],
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """`FC.size()` on consolidated = Σ `FC.size()` on per-state tables."""
    consolidated_size = _table_size(consolidated_asset_ids['transitions'])
    per_state_size = sum(
        _table_size(per_state_asset_ids[fips]['transitions']) for fips in _STATES
    )
    assert (
        consolidated_size == per_state_size
    ), f'consolidated={consolidated_size}, per-state sum={per_state_size}'


@pytest.mark.integration
def test_consolidated_crops_row_count_equals_per_state_sum(
    consolidated_asset_ids: dict[str, str],
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """`FC.size()` round-trip on the crops tables."""
    consolidated_size = _table_size(consolidated_asset_ids['crops'])
    per_state_size = sum(
        _table_size(per_state_asset_ids[fips]['crops']) for fips in _STATES
    )
    assert consolidated_size == per_state_size


@pytest.mark.integration
def test_consolidated_total_emissions_matches_per_state_sum(
    consolidated_asset_ids: dict[str, str],
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """total_emissions_tco2 aggregate sum across transitions tables."""
    consolidated_sum = _table_column_sum(
        consolidated_asset_ids['transitions'], 'total_emissions_tco2'
    )
    per_state_sum = sum(
        _table_column_sum(
            per_state_asset_ids[fips]['transitions'], 'total_emissions_tco2'
        )
        for fips in _STATES
    )
    assert math.isclose(
        consolidated_sum, per_state_sum, abs_tol=_ROUND_TRIP_SLACK_TCO2
    ), f'consolidated={consolidated_sum}, per-state sum={per_state_sum}'


@pytest.mark.integration
def test_consolidated_allocated_crops_matches_per_state_sum(
    consolidated_asset_ids: dict[str, str],
    per_state_asset_ids: dict[str, dict[str, str]],
) -> None:
    """total_allocated_emissions_tco2 aggregate sum across crops tables."""
    consolidated_sum = _table_column_sum(
        consolidated_asset_ids['crops'], 'total_allocated_emissions_tco2'
    )
    per_state_sum = sum(
        _table_column_sum(
            per_state_asset_ids[fips]['crops'], 'total_allocated_emissions_tco2'
        )
        for fips in _STATES
    )
    assert math.isclose(
        consolidated_sum, per_state_sum, abs_tol=_ROUND_TRIP_SLACK_TCO2
    ), f'consolidated={consolidated_sum}, per-state sum={per_state_sum}'


@pytest.mark.integration
def test_consolidated_schema_matches_spec(
    consolidated_asset_ids: dict[str, str],
) -> None:
    """Consolidated transitions and crops tables carry the canonical columns."""
    trans_fc = ee.FeatureCollection(consolidated_asset_ids['transitions'])
    first_trans = trans_fc.first().toDictionary().getInfo()
    assert set(first_trans.keys()) == set(TRANSITIONS_TABLE_COLUMNS)

    crops_fc = ee.FeatureCollection(consolidated_asset_ids['crops'])
    first_crops = crops_fc.first().toDictionary().getInfo()
    assert set(first_crops.keys()) == set(CROPS_TABLE_COLUMNS)


@pytest.mark.integration
def test_counties_span_all_three_states(
    consolidated_asset_ids: dict[str, str],
) -> None:
    """Every county_fips in consolidated transitions starts with 19/31/46."""
    fc = ee.FeatureCollection(consolidated_asset_ids['transitions'])
    county_fips_list = set(fc.aggregate_array('county_fips').getInfo())
    state_prefixes = {str(c)[:2] for c in county_fips_list}
    assert state_prefixes == {
        '19',
        '31',
        '46',
    }, f'unexpected state prefixes in consolidated transitions: {state_prefixes}'
