"""Integration test for extract/county_fips.py at IA scale.

Exercises the painting recipe against live GEE without doing the full
CONUS export. Filters TIGER 2018 counties to Iowa (FIPS 19), runs the
production helpers (`_tag_with_fips_int`, `_build_fips_image`) to build
the painted image, then samples each county at its centroid and
confirms the painted FIPS matches the county's GEOID.

This is the "reproduces the expected IA mask" check for the painted
county-FIPS asset. Marked ``@pytest.mark.integration`` — needs GEE
credentials but no GCS staging.
"""

from __future__ import annotations

from typing import Any

import ee
import pytest

from jdluc.extract import county_fips
from jdluc.utils.constants import (
    GCP_PROJECT,
    GEE_TIGER_COUNTIES,
    GLAD_CRS,
)
from jdluc.utils.gee import initialize_gee


@pytest.fixture(scope='module')
def _ee_initialized() -> None:
    initialize_gee(GCP_PROJECT)


@pytest.mark.integration
def test_paint_recipe_assigns_correct_fips_at_iowa_county_centroids(
    _ee_initialized: None,
) -> None:
    """Paint IA counties; sample at each centroid; FIPS should match GEOID."""
    iowa_counties = (
        ee.FeatureCollection(GEE_TIGER_COUNTIES)
        .filter(ee.Filter.eq('STATEFP', '19'))
        .map(county_fips._tag_with_fips_int)
    )
    fips_image = county_fips._build_fips_image(iowa_counties)

    # Replace each county polygon with its centroid; the centroid is
    # not guaranteed to be inside an irregular polygon, so we tolerate
    # a small number of mismatches and only fail on a meaningful drift.
    centroids = iowa_counties.map(
        lambda f: f.setGeometry(f.geometry().centroid(maxError=10))
    )

    sampled = fips_image.sampleRegions(
        collection=centroids,
        scale=30,
        projection=GLAD_CRS,
        geometries=False,
    ).getInfo()
    features = sampled.get('features', [])

    assert len(features) >= 90, (
        f'Expected ~99 Iowa counties; got {len(features)} features. '
        f'TIGER may have changed.'
    )

    mismatches: list[tuple[int, Any]] = []
    masked: list[int] = []
    for f in features:
        props = f.get('properties', {})
        expected = int(props['GEOID'])
        actual = props.get(county_fips.BAND_NAME)
        if actual is None:
            masked.append(expected)
        elif int(actual) != expected:
            mismatches.append((expected, actual))

    assert not mismatches, (
        f'Painted FIPS disagrees with GEOID at {len(mismatches)} centroids: '
        f'{mismatches[:5]}'
    )
    # Centroid-outside-polygon is a known artifact for ≤ a handful of
    # irregular county shapes; reject only if the rate is unreasonable.
    assert len(masked) <= 3, (
        f'{len(masked)} centroids fell outside their polygon (masked). '
        f'Expected ≤ 3 for IA: {masked}'
    )


@pytest.mark.integration
def test_paint_recipe_masks_pixels_outside_any_county(
    _ee_initialized: None,
) -> None:
    """Pixels outside any IA county are masked (selfMask drops the constant=0)."""
    iowa_counties = (
        ee.FeatureCollection(GEE_TIGER_COUNTIES)
        .filter(ee.Filter.eq('STATEFP', '19'))
        .map(county_fips._tag_with_fips_int)
    )
    fips_image = county_fips._build_fips_image(iowa_counties)

    # Pick a point clearly inside Lake Michigan (no county). Sampling
    # there should yield a masked / null value.
    offshore_point = ee.Geometry.Point([-87.0, 43.5])
    sampled = fips_image.sampleRegions(
        collection=ee.FeatureCollection([ee.Feature(offshore_point)]),
        scale=30,
        projection=GLAD_CRS,
        geometries=False,
    ).getInfo()
    features = sampled.get('features', [])
    # sampleRegions drops features that fall on masked pixels — so the
    # output collection should be empty for a fully-masked sample point.
    assert features == [], f'Expected offshore point to be masked; got {features}'


@pytest.mark.integration
def test_paint_recipe_pixel_value_at_known_polk_county_point(
    _ee_initialized: None,
) -> None:
    """A known IA point gets the right county FIPS at GLAD 30 m.

    Independent of the centroid-sweep test above: anchors the recipe
    against a hand-chosen point with a known answer. Picks Des Moines
    (~ -93.6, 41.6) which is squarely inside Polk County, IA
    (GEOID=19153).
    """
    iowa_counties = (
        ee.FeatureCollection(GEE_TIGER_COUNTIES)
        .filter(ee.Filter.eq('STATEFP', '19'))
        .map(county_fips._tag_with_fips_int)
    )
    fips_image = county_fips._build_fips_image(iowa_counties)

    point = ee.Geometry.Point([-93.6, 41.6])
    sampled = fips_image.sampleRegions(
        collection=ee.FeatureCollection([ee.Feature(point)]),
        scale=30,
        projection=GLAD_CRS,
        geometries=False,
    ).getInfo()
    features = sampled.get('features', [])
    assert (
        len(features) == 1
    ), f'Expected 1 sample at the known IA point; got {features}'
    fips = features[0]['properties'].get(county_fips.BAND_NAME)
    assert (
        fips == 19153
    ), f'Expected Polk County, IA (FIPS 19153) at (-93.6, 41.6); got {fips}'
