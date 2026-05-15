"""Integration tests for the transitions + crops summary tables (Delaware).

Asserts schema, cardinality, round-trip consistency against the emissions
raster, non-negativity, row-level allocated <= total invariant, crops
emissions-factor plausibility, pct_* column sums, NASS yield attachment,
and county_fips well-formedness. Requires a live GEE connection and
previously exported Delaware land_use, emissions, transitions, and crops
assets (via cli.py).

All tests are marked @pytest.mark.integration and excluded from CI by
default (run with: ``pytest -m integration``).
"""

import math
from typing import Any

import ee
import pytest

from jdluc.utils.asset_management import list_assets_matching
from jdluc.utils.constants import (
    BUSHEL_WEIGHT_KG,
    CROP_CODE_TO_GROUP,
    CROP_GROUPS,
    CROPS_TABLE_COLUMNS,
    EMISSIONS_TYPE_NAMES,
    GCP_PROJECT,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    GLAD_EPOCH_PAIRS,
    HA_PER_ACRE,
    NASS_TO_CROP_GROUP,
    PEATLAND_OCCUPATION_EPOCH_LABEL,
    TRANSITIONS_TABLE_COLUMNS,
)
from jdluc.utils.states import get_multi_state_boundary
from jdluc.utils.version import compute_transform_version

from .conftest import DELAWARE_FIPS

REGION = 'delaware'

# Delaware has three counties; their 5-digit STATEFP+COUNTYFP codes.
# Kent=10001, New Castle=10003, Sussex=10005.
_DELAWARE_COUNTY_FIPS: set[str] = {'10001', '10003', '10005'}

# Round-trip consistency slack: max(absolute floor, relative).
#
# The table side reduces per (county, emissions_type, epoch) and then
# computes ``allocated = Σ_(rows) total × weight[epoch]`` — equivalent
# to ``Σ_epoch ((Σ_pixels luc[epoch]) × weight[epoch])``. The raster side
# materializes ``allocated_luc_emissions_2020`` as a per-pixel band
# ``Σ_epoch (luc[epoch] × weight[epoch])``, which is mathematically
# identical but accumulates float32 add-chain drift across the ~5M
# Delaware pixels: empirically ~0.18% on Delaware (~260 tCO2 on a
# ~143K-tCO2 total). Both numbers are correct for what they represent
# — the table sum is the more numerically accurate scalar, the raster
# band is the per-pixel product downstream consumers actually need.
# We allow up to 0.5% relative drift with a 1 tCO2 floor for tiny
# magnitudes.
_ROUND_TRIP_SLACK_TCO2 = 1.0
_ROUND_TRIP_SLACK_RELATIVE = 5e-3


def _round_trip_slack(raster_sum: float) -> float:
    return max(_ROUND_TRIP_SLACK_TCO2, _ROUND_TRIP_SLACK_RELATIVE * abs(raster_sum))


_REDUCE_MAX_PIXELS = int(1e9)

# Crop-code -> NASS commodity name (e.g., 1 -> 'CORN'), used to look up the
# expected yield_kg_per_ha / yield_bu_per_acre ratio.
_CROP_CODE_TO_COMMODITY: dict[int, str] = {
    code: commodity
    for commodity, group in NASS_TO_CROP_GROUP.items()
    for code in CROP_GROUPS[group]
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def gee_init() -> None:
    """Initialize GEE once for this test module."""
    from jdluc.utils.gee import initialize_gee

    initialize_gee(GCP_PROJECT)


@pytest.fixture(scope='module')
def _current_version(gee_init: None) -> str:
    return compute_transform_version()


def _resolve_asset_id(prefix: str, version: str) -> str:
    expected_suffix = f'{prefix}_{REGION}_{version}'
    for asset_id in list_assets_matching(f'{prefix}_{REGION}'):
        if asset_id.endswith(expected_suffix):
            return asset_id
    pytest.skip(f'No {prefix} asset for {REGION} v{version}. Run cli.py first.')


@pytest.fixture(scope='module')
def transitions_asset_id(_current_version: str) -> str:
    return _resolve_asset_id('transitions', _current_version)


@pytest.fixture(scope='module')
def crops_asset_id(_current_version: str) -> str:
    return _resolve_asset_id('crops', _current_version)


@pytest.fixture(scope='module')
def emissions_asset_id(_current_version: str) -> str:
    return _resolve_asset_id('emissions', _current_version)


@pytest.fixture(scope='module')
def transitions_features(transitions_asset_id: str) -> list[dict[str, Any]]:
    fc = ee.FeatureCollection(transitions_asset_id)
    info = fc.getInfo()
    return [f['properties'] for f in info['features']]


@pytest.fixture(scope='module')
def crops_features(crops_asset_id: str) -> list[dict[str, Any]]:
    fc = ee.FeatureCollection(crops_asset_id)
    info = fc.getInfo()
    return [f['properties'] for f in info['features']]


@pytest.fixture(scope='module')
def emissions_asset(emissions_asset_id: str) -> Any:
    return ee.Image(emissions_asset_id)


@pytest.fixture(scope='module')
def delaware_geometry(gee_init: None) -> Any:
    return get_multi_state_boundary([DELAWARE_FIPS])


@pytest.fixture(scope='module')
def raster_allocated_totals(
    emissions_asset: Any, delaware_geometry: Any
) -> dict[str, float]:
    """Reduce the two `allocated_*_2020` emissions bands over Delaware.

    Returns {'luc': float, 'peatland': float}. Used by the round-trip
    consistency tests to validate the `transitions` table's allocated
    column sums against the raster ground truth. The reduction pins
    crs / crsTransform to the canonical GLAD grid so it matches the
    table-side reducer in transform/summary_tables.py exactly — passing
    `scale=` without crs/crsTransform here would force an on-the-fly
    reprojection and silently diverge from the table by several percent.
    """
    reduced = (
        emissions_asset.select(
            ['allocated_luc_emissions_2020', 'allocated_peatland_emissions_2020']
        )
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=delaware_geometry,
            crs=GLAD_CRS,
            crsTransform=GLAD_CRS_TRANSFORM,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .getInfo()
    )
    return {
        'luc': float(reduced['allocated_luc_emissions_2020']),
        'peatland': float(reduced['allocated_peatland_emissions_2020']),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_transitions_schema_columns(
    transitions_features: list[dict[str, Any]],
) -> None:
    """Every row has exactly the `TRANSITIONS_TABLE_COLUMNS` keys."""
    assert transitions_features, 'transitions table has no rows'
    for row in transitions_features:
        assert set(row.keys()) == set(
            TRANSITIONS_TABLE_COLUMNS
        ), f'Row keys {set(row.keys())} != expected {set(TRANSITIONS_TABLE_COLUMNS)}'


@pytest.mark.integration
def test_crops_schema_columns(crops_features: list[dict[str, Any]]) -> None:
    """Every row has exactly the `CROPS_TABLE_COLUMNS` keys."""
    assert crops_features, 'crops table has no rows'
    for row in crops_features:
        assert set(row.keys()) == set(
            CROPS_TABLE_COLUMNS
        ), f'Row keys {set(row.keys())} != expected {set(CROPS_TABLE_COLUMNS)}'


@pytest.mark.integration
def test_transitions_cardinality(
    transitions_features: list[dict[str, Any]],
) -> None:
    """Row count is within the structural bound.

    Upper bound: 3 counties x (9 LUC + 1 peatland_conversion) x 4 epochs
    + 3 counties x 1 peatland_occupation = 123 rows. Lower bound of 20
    is a coarse check that Delaware actually has transitions populated
    across multiple (county, epoch, type) combinations.
    """
    n_rows = len(transitions_features)
    assert 20 <= n_rows <= 123, f'transitions row count out of bounds: {n_rows}'


@pytest.mark.integration
def test_transitions_round_trip_luc(
    transitions_features: list[dict[str, Any]],
    raster_allocated_totals: dict[str, float],
) -> None:
    """Sum of allocated LUC rows matches raster `allocated_luc_emissions_2020`."""
    luc_type_names = {
        name
        for name in EMISSIONS_TYPE_NAMES
        if name not in ('peatland_conversion', 'peatland_occupation')
    }
    table_sum = sum(
        float(r['allocated_emissions_2020_tco2'])
        for r in transitions_features
        if r['emissions_type'] in luc_type_names
    )
    raster_sum = raster_allocated_totals['luc']
    slack = _round_trip_slack(raster_sum)
    assert abs(table_sum - raster_sum) <= slack, (
        f'LUC round-trip mismatch: table={table_sum}, raster={raster_sum}, '
        f'slack={slack}'
    )


@pytest.mark.integration
def test_transitions_round_trip_peatland(
    transitions_features: list[dict[str, Any]],
    raster_allocated_totals: dict[str, float],
) -> None:
    """Sum of allocated peatland rows matches raster `allocated_peatland_emissions_2020`."""
    peatland_type_names = {'peatland_conversion', 'peatland_occupation'}
    table_sum = sum(
        float(r['allocated_emissions_2020_tco2'])
        for r in transitions_features
        if r['emissions_type'] in peatland_type_names
    )
    raster_sum = raster_allocated_totals['peatland']
    slack = _round_trip_slack(raster_sum)
    assert abs(table_sum - raster_sum) <= slack, (
        f'Peatland round-trip mismatch: table={table_sum}, raster={raster_sum}, '
        f'slack={slack}'
    )


@pytest.mark.integration
def test_non_negativity_transitions(
    transitions_features: list[dict[str, Any]],
) -> None:
    """All numeric columns in transitions rows are >= 0."""
    numeric_cols = (
        'total_area_ha',
        'total_emissions_tco2',
        'allocated_emissions_2020_tco2',
    )
    for row in transitions_features:
        for col in numeric_cols:
            assert row[col] >= 0.0, f'{col}={row[col]} negative in row {row}'


@pytest.mark.integration
def test_non_negativity_crops(crops_features: list[dict[str, Any]]) -> None:
    """All numeric columns in crops rows are >= 0 (NaN pct columns allowed)."""
    non_nan_cols = (
        'total_production_kg',
        'total_production_bu',
        'total_crop_area_ha',
        'peatland_crop_area_ha',
        'yield_kg_per_ha',
        'yield_bu_per_acre',
        'total_allocated_emissions_tco2',
        'emissions_factor_kgco2e_per_kg',
    )
    pct_cols = (
        'pct_forest',
        'pct_short_veg',
        'pct_peatland_conversion',
        'pct_peatland_occupation',
        'pct_epoch_2005',
        'pct_epoch_2010',
        'pct_epoch_2015',
        'pct_epoch_2020',
    )
    for row in crops_features:
        for col in non_nan_cols:
            value = row[col]
            assert (
                value is not None and value >= 0.0
            ), f'{col}={value} negative or null in row {row}'
        for col in pct_cols:
            value = row[col]
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue  # legitimate NaN on zero-allocated rows
            assert value >= 0.0, f'{col}={value} negative in row {row}'


@pytest.mark.integration
def test_allocated_le_total_per_row(
    transitions_features: list[dict[str, Any]],
) -> None:
    """Every transitions row satisfies allocated <= total + slack.

    peatland_occupation rows carry weight 1.0 and should have
    allocated == total (within slack).
    """
    for row in transitions_features:
        total = float(row['total_emissions_tco2'])
        allocated = float(row['allocated_emissions_2020_tco2'])
        assert (
            allocated <= total + _ROUND_TRIP_SLACK_TCO2
        ), f'allocated={allocated} > total={total} in row {row}'
        if row['emissions_type'] == 'peatland_occupation':
            assert abs(allocated - total) <= _ROUND_TRIP_SLACK_TCO2, (
                f'peatland_occupation allocated({allocated}) != total({total}) '
                f'in row {row}'
            )


@pytest.mark.integration
def test_crops_ef_plausibility(
    crops_features: list[dict[str, Any]],
) -> None:
    """Each crop group has at least one row, and every row's EF is in [0, 5].

    Delaware is a low-deforestation mid-Atlantic state; methodology
    expectations for row-crop EFs are "a few kg CO2e / kg at most."
    """
    groups_seen: set[str] = set()
    for row in crops_features:
        group = row['crop_group']
        assert group in ('corn', 'soybeans', 'wheat'), f'Unexpected crop_group: {group}'
        groups_seen.add(group)
        ef = row['emissions_factor_kgco2e_per_kg']
        # Zero-allocated rows produce EF = 0.0 which is fine. NaN would be
        # a bug (shouldn't happen since we drop total_crop_area_ha == 0
        # rows before the EF divide).
        assert ef is not None and not (
            isinstance(ef, float) and math.isnan(ef)
        ), f'NaN EF in row {row}'
        assert 0.0 <= ef <= 5.0, f'EF={ef} outside [0, 5] kgCO2e/kg in row {row}'
    assert groups_seen == {'corn', 'soybeans', 'wheat'}, (
        f'Missing crop groups: {{\"corn\", \"soybeans\", \"wheat\"}} '
        f'vs seen {groups_seen}'
    )


@pytest.mark.integration
def test_pct_columns_sum_to_one(crops_features: list[dict[str, Any]]) -> None:
    """pct_* driver set and pct_epoch_* set each sum to 1.0 per row.

    Skips rows where total_allocated_emissions_tco2 == 0 (pct columns are
    NaN by design on those rows). Tolerance is 1.5%, the documented
    grouped-reducer drift on small denominators.
    """
    driver_cols = (
        'pct_forest',
        'pct_short_veg',
        'pct_peatland_conversion',
        'pct_peatland_occupation',
    )
    epoch_cols = (
        'pct_epoch_2005',
        'pct_epoch_2010',
        'pct_epoch_2015',
        'pct_epoch_2020',
    )
    _PCT_TOLERANCE = 1.5e-2
    for row in crops_features:
        total_alloc = float(row['total_allocated_emissions_tco2'])
        if total_alloc == 0.0:
            continue
        driver_sum = sum(float(row[c]) for c in driver_cols)
        epoch_sum = sum(float(row[c]) for c in epoch_cols)
        assert (
            abs(driver_sum - 1.0) < _PCT_TOLERANCE
        ), f'driver pct sum = {driver_sum} in row {row}'
        assert (
            abs(epoch_sum - 1.0) < _PCT_TOLERANCE
        ), f'epoch pct sum = {epoch_sum} in row {row}'


@pytest.mark.integration
def test_crops_yield_attached_and_self_consistent(
    crops_features: list[dict[str, Any]],
) -> None:
    """Every crops row has positive yields and the unit-conversion is consistent.

    `yield_kg_per_ha / yield_bu_per_acre` must equal
    `BUSHEL_WEIGHT_KG[commodity] / HA_PER_ACRE` to within 1e-3.
    """
    for row in crops_features:
        yield_bu = float(row['yield_bu_per_acre'])
        yield_kg = float(row['yield_kg_per_ha'])
        assert yield_bu > 0, f'yield_bu_per_acre={yield_bu} non-positive in row {row}'
        assert yield_kg > 0, f'yield_kg_per_ha={yield_kg} non-positive in row {row}'
        crop_code = int(row['crop_code'])
        commodity = _CROP_CODE_TO_COMMODITY[crop_code]
        expected_ratio = BUSHEL_WEIGHT_KG[commodity] / HA_PER_ACRE
        actual_ratio = yield_kg / yield_bu
        assert abs(actual_ratio - expected_ratio) < 1e-3, (
            f'yield ratio mismatch for crop_code={crop_code}: '
            f'actual={actual_ratio}, expected={expected_ratio}'
        )


@pytest.mark.integration
def test_delaware_county_fips_well_formed(
    transitions_features: list[dict[str, Any]],
    crops_features: list[dict[str, Any]],
) -> None:
    """All county_fips values are in the Delaware set and well-formed (5 digits)."""
    for label, rows in (
        ('transitions', transitions_features),
        ('crops', crops_features),
    ):
        seen_fips = {str(row['county_fips']) for row in rows}
        for fips in seen_fips:
            assert (
                len(fips) == 5 and fips.isdigit()
            ), f'{label}: malformed county_fips {fips!r}'
            assert fips.startswith('10'), f'{label}: non-Delaware county_fips {fips!r}'
        assert seen_fips.issubset(
            _DELAWARE_COUNTY_FIPS
        ), f'{label}: unexpected counties {seen_fips - _DELAWARE_COUNTY_FIPS}'
        # At least one row per Delaware county should be present.
        assert (
            seen_fips == _DELAWARE_COUNTY_FIPS
        ), f'{label}: missing counties {_DELAWARE_COUNTY_FIPS - seen_fips}'


@pytest.mark.integration
def test_transitions_epoch_labels_are_valid(
    transitions_features: list[dict[str, Any]],
) -> None:
    """Every row's epoch_transition is either a GLAD epoch pair or '2020'."""
    valid_labels = {f'{a}_{b}' for (a, b) in GLAD_EPOCH_PAIRS} | {
        PEATLAND_OCCUPATION_EPOCH_LABEL
    }
    for row in transitions_features:
        assert (
            row['epoch_transition'] in valid_labels
        ), f'Unexpected epoch_transition: {row["epoch_transition"]!r}'


@pytest.mark.integration
def test_crops_crop_code_to_group_consistent(
    crops_features: list[dict[str, Any]],
) -> None:
    """Every row's crop_group matches the CDL crop_code mapping."""
    for row in crops_features:
        crop_code = int(row['crop_code'])
        assert row['crop_group'] == CROP_CODE_TO_GROUP[crop_code], (
            f'crop_group={row["crop_group"]!r} != expected '
            f'{CROP_CODE_TO_GROUP[crop_code]!r} for crop_code={crop_code}'
        )
