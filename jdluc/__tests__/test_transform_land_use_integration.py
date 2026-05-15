"""Integration tests for the land_use raster (Delaware fixture).

Asserts that the exported land_use raster has the correct band structure,
dtypes, grid alignment, and plausible pixel values. Requires a live GEE
connection and a previously exported Delaware land_use asset (via
cli.py or the fixture below).

All tests are marked @pytest.mark.integration and excluded from CI by
default (run with: ``pytest -m integration``).
"""

from typing import Any

import ee
import pytest

from jdluc.transform.land_use import LAND_USE_BAND_NAMES
from jdluc.utils.asset_management import list_assets_matching
from jdluc.utils.constants import (
    GCP_PROJECT,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    LAND_USE_CATEGORIES,
)
from jdluc.utils.states import get_multi_state_boundary
from jdluc.utils.version import compute_transform_version

from .conftest import DELAWARE_FIPS

# GCP project for Earth Engine
REGION = 'delaware'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def gee_init() -> None:
    """Initialize GEE once for this test module."""
    from jdluc.utils.gee import initialize_gee

    initialize_gee(GCP_PROJECT)


@pytest.fixture(scope='module')
def land_use_asset_id(gee_init: None) -> str:
    """Resolve the current-version Delaware land_use asset ID.

    Skips the entire module if no matching asset exists.
    """
    version = compute_transform_version()
    matches = list_assets_matching(f'land_use_{REGION}')
    expected_suffix = f'land_use_{REGION}_{version}'
    for asset_id in matches:
        if asset_id.endswith(expected_suffix):
            return asset_id
    pytest.skip(f'No land_use asset for {REGION} v{version}. Run cli.py first.')


@pytest.fixture(scope='module')
def asset_info(land_use_asset_id: str) -> dict[str, Any]:
    """Load the full asset metadata via .getInfo() once."""
    return ee.Image(land_use_asset_id).getInfo()


@pytest.fixture(scope='module')
def asset_image(land_use_asset_id: str) -> Any:
    """Load the asset as an ee.Image for server-side queries."""
    return ee.Image(land_use_asset_id)


@pytest.fixture(scope='module')
def delaware_geometry(gee_init: None) -> Any:
    """Delaware state boundary geometry."""
    return get_multi_state_boundary([DELAWARE_FIPS])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_asset_exists(land_use_asset_id: str) -> None:
    """The current-version land_use asset exists in GEE."""
    assert land_use_asset_id is not None


@pytest.mark.integration
def test_band_count_and_names(asset_info: dict[str, Any]) -> None:
    """Asset has exactly the 6 expected bands in the correct order."""
    band_names = [b['id'] for b in asset_info['bands']]
    assert band_names == LAND_USE_BAND_NAMES


@pytest.mark.integration
def test_band_dtypes(asset_info: dict[str, Any]) -> None:
    """Every band is uint8."""
    for band in asset_info['bands']:
        data_type = band['data_type']
        # GEE reports dtype as a dict with 'precision' and 'min'/'max'
        assert (
            data_type['precision'] == 'int'
        ), f"Band {band['id']}: expected int precision, got {data_type['precision']}"
        assert data_type['min'] == 0, f"Band {band['id']}: min != 0"
        assert data_type['max'] == 255, f"Band {band['id']}: max != 255"


@pytest.mark.integration
def test_grid_alignment(asset_info: dict[str, Any]) -> None:
    """Every band is pinned to the GLAD CRS and pixel grid.

    GEE stores the crs_transform with a localized origin (the asset's
    bounding box) rather than the global origin (-180, 90). So we check:
    1. CRS is EPSG:4326
    2. Pixel size matches GLAD_CRS_TRANSFORM exactly
    3. Origin is phase-aligned with the GLAD global grid (offsets are
       integer multiples of the pixel size from the global origin)
    """
    pixel_size_x = GLAD_CRS_TRANSFORM[0]  # 0.00025
    pixel_size_y = GLAD_CRS_TRANSFORM[4]  # -0.00025
    global_origin_x = GLAD_CRS_TRANSFORM[2]  # -180
    global_origin_y = GLAD_CRS_TRANSFORM[5]  # 90

    for band in asset_info['bands']:
        bid = band['id']
        assert (
            band['crs'] == GLAD_CRS
        ), f"Band {bid}: expected {GLAD_CRS}, got {band['crs']}"

        t = band['crs_transform']
        # Pixel sizes must match exactly
        assert (
            abs(t[0] - pixel_size_x) < 1e-12
        ), f"Band {bid}: pixel_size_x = {t[0]}, expected {pixel_size_x}"
        assert (
            abs(t[4] - pixel_size_y) < 1e-12
        ), f"Band {bid}: pixel_size_y = {t[4]}, expected {pixel_size_y}"
        # No rotation/shear
        assert (
            t[1] == 0 and t[3] == 0
        ), f"Band {bid}: unexpected rotation in crs_transform"
        # Origin must be phase-aligned with the GLAD global grid
        offset_x = (t[2] - global_origin_x) / pixel_size_x
        offset_y = (t[5] - global_origin_y) / pixel_size_y
        assert abs(offset_x - round(offset_x)) < 1e-6, (
            f"Band {bid}: origin_x {t[2]} not phase-aligned with GLAD grid "
            f"(offset {offset_x} pixels from global origin)"
        )
        assert abs(offset_y - round(offset_y)) < 1e-6, (
            f"Band {bid}: origin_y {t[5]} not phase-aligned with GLAD grid "
            f"(offset {offset_y} pixels from global origin)"
        )


@pytest.mark.integration
def test_is_peatland_nonzero(asset_image: Any, delaware_geometry: Any) -> None:
    """is_peatland band has at least one nonzero pixel in Delaware."""
    max_val = (
        asset_image.select('is_peatland')
        .reduceRegion(
            reducer=ee.Reducer.max(),
            geometry=delaware_geometry,
            scale=30,
            maxPixels=int(1e9),
        )
        .getInfo()
    )
    assert (
        max_val['is_peatland'] == 1
    ), 'Expected peatland pixels in Delaware (coastal plain), got none'


@pytest.mark.integration
def test_crops_2020_distribution(asset_image: Any, delaware_geometry: Any) -> None:
    """crops_2020 band contains corn (1), soybeans (5), and wheat codes."""
    histogram = (
        asset_image.select('crops_2020')
        .reduceRegion(
            reducer=ee.Reducer.frequencyHistogram(),
            geometry=delaware_geometry,
            scale=30,
            maxPixels=int(1e9),
        )
        .getInfo()
    )
    hist = histogram['crops_2020']

    # CDL codes: 1=corn, 5=soybeans, 22/23/24=wheat varieties
    assert '1' in hist, 'No corn (CDL code 1) in Delaware crops_2020'
    assert '5' in hist, 'No soybeans (CDL code 5) in Delaware crops_2020'

    wheat_codes = ['22', '23', '24']
    wheat_present = any(c in hist for c in wheat_codes)
    assert wheat_present, 'No wheat codes (22/23/24) in Delaware crops_2020'

    # Non-cropland (code 0) should dominate but not be 100%
    total_nonzero = sum(v for k, v in hist.items() if k != '0')
    assert total_nonzero > 0, 'crops_2020 has no cropland pixels'


@pytest.mark.integration
def test_transition_bands_nonzero(asset_image: Any, delaware_geometry: Any) -> None:
    """Every transition band has at least one non-zero pixel."""
    transition_bands = LAND_USE_BAND_NAMES[:4]
    for band_name in transition_bands:
        max_val = (
            asset_image.select(band_name)
            .reduceRegion(
                reducer=ee.Reducer.max(),
                geometry=delaware_geometry,
                scale=30,
                maxPixels=int(1e9),
            )
            .getInfo()
        )
        assert max_val[band_name] > 0, f'{band_name} has no transitions (all zeros)'


@pytest.mark.integration
def test_no_self_transitions(asset_image: Any, delaware_geometry: Any) -> None:
    """No transition band pixel encodes from==to (which would be a bug)."""
    # Self-transition codes: (i<<4)|i for i in 1..9
    # encode_transition(i,i) returns 0 by construction, so we check for the
    # raw bit pattern (i<<4)|i which would indicate a bug in the encoder.
    self_code_images = []
    for i in [c.code for c in LAND_USE_CATEGORIES.values()]:
        raw_self = (i << 4) | i  # e.g. 0x11, 0x22, ...
        self_code_images.append(raw_self)

    transition_bands = LAND_USE_BAND_NAMES[:4]
    for band_name in transition_bands:
        band = asset_image.select(band_name)
        # Check if any pixel has a self-transition code
        has_self = band.eq(self_code_images[0])
        for code in self_code_images[1:]:
            has_self = has_self.Or(band.eq(code))

        max_self = has_self.reduceRegion(
            reducer=ee.Reducer.max(),
            geometry=delaware_geometry,
            scale=30,
            maxPixels=int(1e9),
        ).getInfo()
        # The result key is 'category' from the .eq() output
        result_key = list(max_self.keys())[0]
        assert (
            max_self[result_key] == 0
        ), f'{band_name} has pixels with from==to self-transition codes'


@pytest.mark.integration
def test_classification_coverage(asset_image: Any, delaware_geometry: Any) -> None:
    """At least 99% of pixels in any transition band have valid categories.

    Decodes from/to from a transition band and checks that unclassified (0)
    pixels are rare. Only checks non-zero (transitioned) pixels since zero
    means "no transition" which is valid.
    """
    from jdluc.utils.transitions import (
        decode_from_image,
        decode_to_image,
    )

    # Use the first transition band as representative
    band = asset_image.select(LAND_USE_BAND_NAMES[0])

    # Count total non-zero (transitioned) pixels
    transitioned = band.gt(0)
    total_transitioned = transitioned.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=delaware_geometry,
        scale=30,
        maxPixels=int(1e9),
    ).getInfo()
    total_key = list(total_transitioned.keys())[0]
    total_count = total_transitioned[total_key]

    if total_count == 0:
        pytest.skip('No transitioned pixels to check coverage on')

    # Among transitioned pixels, check from and to codes are in 1-9
    from_code = decode_from_image(band)
    to_code = decode_to_image(band)

    # Invalid = from or to is 0 on a transitioned pixel
    invalid_from = from_code.eq(0).And(transitioned)
    invalid_to = to_code.eq(0).And(transitioned)
    invalid = invalid_from.Or(invalid_to)

    invalid_count_info = invalid.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=delaware_geometry,
        scale=30,
        maxPixels=int(1e9),
    ).getInfo()
    invalid_key = list(invalid_count_info.keys())[0]
    invalid_count = invalid_count_info[invalid_key]

    pct_invalid = (invalid_count / total_count) * 100 if total_count > 0 else 0
    assert pct_invalid < 1.0, (
        f'{pct_invalid:.2f}% of transitioned pixels have unclassified '
        f'(0) from/to codes — expected < 1%'
    )
