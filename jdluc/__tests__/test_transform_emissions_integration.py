"""Integration tests for the emissions raster (Delaware fixture).

Asserts that the exported emissions raster has the correct band structure,
dtypes, grid alignment, non-negativity, and mask invariants. Requires a
live GEE connection and previously exported Delaware emissions and
land_use assets (via cli.py).

All tests are marked @pytest.mark.integration and excluded from CI by
default (run with: ``pytest -m integration``).
"""

from typing import Any

import ee
import pytest

from jdluc.transform.land_use import LAND_USE_BAND_NAMES
from jdluc.utils.asset_management import list_assets_matching
from jdluc.utils.constants import (
    EMISSIONS_BAND_NAMES,
    EMISSIVE_ENCODED_CODES,
    GCP_PROJECT,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    GLAD_EPOCH_PAIRS,
    PEATLAND_DRAINAGE_ENCODED_CODES,
    PEATLAND_E_LM_TCO2E_HA_YR,
)
from jdluc.utils.states import get_multi_state_boundary
from jdluc.utils.version import compute_transform_version

from .conftest import DELAWARE_FIPS

REGION = 'delaware'

# scale= for Delaware-wide reduceRegion calls. 30m matches GLAD pixel size;
# Delaware at 30m is ~45M pixels, well within maxPixels=1e9.
_REDUCE_SCALE = 30
_REDUCE_MAX_PIXELS = int(1e9)


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


@pytest.fixture(scope='module')
def emissions_asset_id(_current_version: str) -> str:
    """Resolve the current-version Delaware emissions asset ID.

    Skips the entire module if no matching asset exists.
    """
    expected_suffix = f'emissions_{REGION}_{_current_version}'
    for asset_id in list_assets_matching(f'emissions_{REGION}'):
        if asset_id.endswith(expected_suffix):
            return asset_id
    pytest.skip(
        f'No emissions asset for {REGION} v{_current_version}. ' 'Run cli.py first.'
    )


@pytest.fixture(scope='module')
def land_use_asset_id(_current_version: str) -> str:
    """Resolve the current-version Delaware land_use asset ID."""
    expected_suffix = f'land_use_{REGION}_{_current_version}'
    for asset_id in list_assets_matching(f'land_use_{REGION}'):
        if asset_id.endswith(expected_suffix):
            return asset_id
    pytest.skip(
        f'No land_use asset for {REGION} v{_current_version}. ' 'Run cli.py first.'
    )


@pytest.fixture(scope='module')
def emissions_asset(emissions_asset_id: str) -> Any:
    return ee.Image(emissions_asset_id)


@pytest.fixture(scope='module')
def emissions_info(emissions_asset: Any) -> dict[str, Any]:
    return emissions_asset.getInfo()


@pytest.fixture(scope='module')
def land_use_asset(land_use_asset_id: str) -> Any:
    return ee.Image(land_use_asset_id)


@pytest.fixture(scope='module')
def delaware_geometry(gee_init: None) -> Any:
    return get_multi_state_boundary([DELAWARE_FIPS])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_asset_exists(emissions_asset_id: str, _current_version: str) -> None:
    """The current-version emissions asset exists under the expected name."""
    assert emissions_asset_id.endswith(f'emissions_{REGION}_{_current_version}')


@pytest.mark.integration
def test_band_count_and_names(emissions_info: dict[str, Any]) -> None:
    """Asset has exactly the 11 expected bands in canonical order."""
    band_names = [b['id'] for b in emissions_info['bands']]
    assert band_names == EMISSIONS_BAND_NAMES


@pytest.mark.integration
def test_band_dtypes(emissions_info: dict[str, Any]) -> None:
    """Every band is float32."""
    for band in emissions_info['bands']:
        dt = band['data_type']
        assert (
            dt['precision'] == 'float'
        ), f"Band {band['id']}: expected float precision, got {dt['precision']}"


@pytest.mark.integration
def test_grid_alignment(emissions_info: dict[str, Any]) -> None:
    """Every band is pinned to the GLAD CRS and pixel grid.

    GEE stores the crs_transform with a localized origin (the asset's
    bounding box) rather than the global origin (-180, 90). So we check:
    1. CRS is EPSG:4326
    2. Pixel size matches GLAD_CRS_TRANSFORM exactly
    3. Origin is phase-aligned with the GLAD global grid
    """
    pixel_x = GLAD_CRS_TRANSFORM[0]
    pixel_y = GLAD_CRS_TRANSFORM[4]
    global_x = GLAD_CRS_TRANSFORM[2]
    global_y = GLAD_CRS_TRANSFORM[5]

    for band in emissions_info['bands']:
        bid = band['id']
        assert band['crs'] == GLAD_CRS, f'{bid}: crs={band["crs"]}'
        t = band['crs_transform']
        assert abs(t[0] - pixel_x) < 1e-12, f'{bid}: pixel_x={t[0]}'
        assert abs(t[4] - pixel_y) < 1e-12, f'{bid}: pixel_y={t[4]}'
        assert t[1] == 0 and t[3] == 0, f'{bid}: unexpected rotation'
        offset_x = (t[2] - global_x) / pixel_x
        offset_y = (t[5] - global_y) / pixel_y
        assert (
            abs(offset_x - round(offset_x)) < 1e-6
        ), f'{bid}: origin_x {t[2]} off GLAD phase'
        assert (
            abs(offset_y - round(offset_y)) < 1e-6
        ), f'{bid}: origin_y {t[5]} off GLAD phase'


def _region_min(image: Any, band: str, geometry: Any) -> float:
    return (
        image.select(band)
        .reduceRegion(
            reducer=ee.Reducer.min(),
            geometry=geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .get(band)
        .getInfo()
    )


def _region_sum(image: Any, band: str, geometry: Any) -> float:
    return (
        image.select(band)
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .get(band)
        .getInfo()
    )


@pytest.mark.integration
@pytest.mark.parametrize('band_name', EMISSIONS_BAND_NAMES)
def test_non_negativity(
    emissions_asset: Any, delaware_geometry: Any, band_name: str
) -> None:
    """Every band is non-negative (float32 epsilon tolerance)."""
    min_val = _region_min(emissions_asset, band_name, delaware_geometry)
    assert (
        min_val is not None and min_val >= -1e-6
    ), f'{band_name}: min={min_val} violates non-negativity'


@pytest.mark.integration
def test_allocated_luc_at_most_sum_of_epochs(
    emissions_asset: Any, delaware_geometry: Any
) -> None:
    """sum(allocated_luc_emissions_2020) ≤ Σ sum(luc_emissions_{epoch}) + slack.

    Slack is 1 tCO₂, well below Delaware's per-epoch sums (millions).
    """
    allocated = _region_sum(
        emissions_asset, 'allocated_luc_emissions_2020', delaware_geometry
    )
    per_epoch = sum(
        _region_sum(emissions_asset, f'luc_emissions_{a}_{b}', delaware_geometry)
        for (a, b) in GLAD_EPOCH_PAIRS
    )
    assert (
        allocated <= per_epoch + 1.0
    ), f'allocated_luc={allocated} > sum_per_epoch={per_epoch}'


@pytest.mark.integration
def test_allocated_peatland_at_most_sum(
    emissions_asset: Any, delaware_geometry: Any
) -> None:
    """allocated_peatland ≤ Σ peatland_conversion + peatland_occupation + slack."""
    allocated = _region_sum(
        emissions_asset, 'allocated_peatland_emissions_2020', delaware_geometry
    )
    per_epoch = sum(
        _region_sum(emissions_asset, f'peatland_conversion_{a}_{b}', delaware_geometry)
        for (a, b) in GLAD_EPOCH_PAIRS
    )
    occupation = _region_sum(
        emissions_asset, 'peatland_occupation_2020', delaware_geometry
    )
    assert allocated <= per_epoch + occupation + 1.0, (
        f'allocated_peatland={allocated} > '
        f'per_epoch+occupation={per_epoch + occupation}'
    )


@pytest.mark.integration
@pytest.mark.parametrize('epoch', GLAD_EPOCH_PAIRS)
def test_luc_emissions_gated_on_emissive_mask(
    emissions_asset: Any,
    land_use_asset: Any,
    delaware_geometry: Any,
    epoch: tuple[int, int],
) -> None:
    """luc_emissions_{epoch} is zero on pixels whose transition is not in the
    9-pair emissive vocabulary."""
    a, b = epoch
    transition = land_use_asset.select(f'transitions_{a}_{b}')
    is_emissive = transition.remap(
        EMISSIVE_ENCODED_CODES, [1] * len(EMISSIVE_ENCODED_CODES), defaultValue=0
    )
    non_emissive = is_emissive.Not()
    leaked = (
        emissions_asset.select(f'luc_emissions_{a}_{b}')
        .multiply(non_emissive)
        .reduceRegion(
            reducer=ee.Reducer.max(),
            geometry=delaware_geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .values()
        .getInfo()[0]
    )
    assert (
        leaked == 0
    ), f'luc_emissions_{a}_{b} has {leaked} tCO₂ on non-emissive pixels'


@pytest.mark.integration
@pytest.mark.parametrize('epoch', GLAD_EPOCH_PAIRS)
def test_peatland_conversion_gated_on_drainage_mask(
    emissions_asset: Any,
    land_use_asset: Any,
    delaware_geometry: Any,
    epoch: tuple[int, int],
) -> None:
    """peatland_conversion_{epoch} is zero on pixels whose destination is not
    cropland or built_up — specifically verifies forest_to_short_veg on
    peatland does NOT produce a conversion pulse (Design decision 5)."""
    a, b = epoch
    transition = land_use_asset.select(f'transitions_{a}_{b}')
    is_drainage = transition.remap(
        PEATLAND_DRAINAGE_ENCODED_CODES,
        [1] * len(PEATLAND_DRAINAGE_ENCODED_CODES),
        defaultValue=0,
    )
    non_drainage = is_drainage.Not()
    leaked = (
        emissions_asset.select(f'peatland_conversion_{a}_{b}')
        .multiply(non_drainage)
        .reduceRegion(
            reducer=ee.Reducer.max(),
            geometry=delaware_geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .values()
        .getInfo()[0]
    )
    assert (
        leaked == 0
    ), f'peatland_conversion_{a}_{b} has {leaked} tCO₂ on non-drainage pixels'


@pytest.mark.integration
def test_peatland_occupation_gated_on_peatland_and_cropland(
    emissions_asset: Any, land_use_asset: Any, delaware_geometry: Any
) -> None:
    """peatland_occupation_2020 is zero outside (is_peatland & crops_2020>0)."""
    is_peatland = land_use_asset.select('is_peatland').gt(0)
    is_cropland = land_use_asset.select('crops_2020').gt(0)
    outside_mask = is_peatland.multiply(is_cropland).Not()
    leaked = (
        emissions_asset.select('peatland_occupation_2020')
        .multiply(outside_mask)
        .reduceRegion(
            reducer=ee.Reducer.max(),
            geometry=delaware_geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .values()
        .getInfo()[0]
    )
    assert (
        leaked == 0
    ), f'peatland_occupation_2020 has {leaked} tCO₂ outside peatland∩cropland'


@pytest.mark.integration
def test_peatland_occupation_uniform_per_pixel(
    emissions_asset: Any, delaware_geometry: Any
) -> None:
    """Every non-zero peatland_occupation pixel equals E_LM × pixel_area_ha
    (up to 1e-3 tCO₂e). Skips if Delaware has no peatland∩cropland pixels."""
    occ = emissions_asset.select('peatland_occupation_2020')
    area_ha = (
        ee.Image.pixelArea()
        .divide(1e4)
        .reproject(crs=GLAD_CRS, crsTransform=GLAD_CRS_TRANSFORM)
    )
    expected = area_ha.multiply(PEATLAND_E_LM_TCO2E_HA_YR)
    # Only compare on pixels where occupation is non-zero
    on_mask = occ.gt(0)
    max_count = (
        on_mask.reduceRegion(
            reducer=ee.Reducer.max(),
            geometry=delaware_geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .values()
        .getInfo()[0]
    )
    if max_count == 0:
        pytest.skip('No peatland∩cropland pixels in Delaware')

    diff = occ.subtract(expected).abs().updateMask(on_mask)
    max_diff = (
        diff.reduceRegion(
            reducer=ee.Reducer.max(),
            geometry=delaware_geometry,
            scale=_REDUCE_SCALE,
            maxPixels=_REDUCE_MAX_PIXELS,
        )
        .values()
        .getInfo()[0]
    )
    assert max_diff < 1e-3, (
        f'peatland_occupation non-uniform: max |actual - E_LM*area_ha| = ' f'{max_diff}'
    )


@pytest.mark.integration
def test_epoch_sensitivity(emissions_asset: Any, delaware_geometry: Any) -> None:
    """The four luc_emissions_{epoch} regional sums are not all identical."""
    sums = [
        _region_sum(emissions_asset, f'luc_emissions_{a}_{b}', delaware_geometry)
        for (a, b) in GLAD_EPOCH_PAIRS
    ]
    assert (
        max(sums) - min(sums) > 1e-3
    ), f'luc_emissions regional sums identical across epochs: {sums}'


@pytest.mark.integration
def test_transition_bands_order_check(
    land_use_asset: Any,
) -> None:
    """Defensive: the land_use asset's bands still match LAND_USE_BAND_NAMES.

    If a future change ever reorders the land_use schema, the emissions
    tests that rely on transitions_{epoch} / is_peatland / crops_2020 band
    names break too — this test surfaces it loudly.
    """
    actual = land_use_asset.bandNames().getInfo()
    assert actual == LAND_USE_BAND_NAMES
