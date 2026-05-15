"""CONUS county-FIPS label raster extract.

Builds a single-band int32 image at the GLAD 0.00025° grid by painting
the GEOID property of every CONUS feature in ``TIGER/2018/Counties``
into ``ee.Image.constant(0).int()`` and ``selfMask()``-ing the
unpainted background. Each pixel inside a CONUS county carries the
county's 5-digit FIPS as an integer (e.g. Polk County, IA → 19153);
pixels outside CONUS or in AK / HI / territories are masked.

``paint(FC, value)`` implements the pixel-center rasterization rule at
GLAD 30m, near-identical to a majority-area rule (XOR < 0.003%).

The extract is server-side only (no HTTP, no GCS staging): TIGER
counties are a GEE-native FeatureCollection and the export is a
single ``Export.image.toAsset`` task pinned to GLAD CRS / CRS_TRANSFORM
over the bounding rectangle of the CONUS county union.

The asset is the single source of geographic truth for the transform
pipeline: polygon mask via ``fips.mask()``, per-county grouping band
via ``fips`` itself.
"""

import logging

import ee

from jdluc.utils._ee_types import EEFeature, EEFeatureCollection, EEGeometry, EEImage
from jdluc.utils.constants import (
    CONUS_STATE_FIPS,
    GEE_COUNTY_FIPS_LABEL,
    GEE_TIGER_COUNTIES,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
)
from jdluc.utils.gee import (
    asset_exists,
    delete_asset_if_present,
    wait_for_export_task,
)

logger = logging.getLogger(__name__)

# Output band name; the transform stage selects this when consuming the
# asset.
BAND_NAME: str = 'county_fips'

# Property attached server-side: integer parse of TIGER's GEOID string.
# GEOID is STATEFP+COUNTYFP zero-padded (e.g. "19153"); parsing as int
# yields the methodology-standard FIPS code (1001..56045 across CONUS).
_FIPS_PROPERTY: str = 'fips_int'


def _tag_with_fips_int(feature: EEFeature) -> EEFeature:
    """Attach an int-typed `fips_int` property derived from GEOID.

    GEOID is TIGER's zero-padded 5-digit STATEFP+COUNTYFP string;
    parsing as int yields the methodology FIPS code (1001..56045).
    Pulled out as a named helper rather than an inline lambda so the
    server-side `ee.Number.parse(ee.String(...)).toInt()` chain is
    unit-testable in isolation.
    """
    return feature.set(
        _FIPS_PROPERTY,
        ee.Number.parse(ee.String(feature.get('GEOID'))).toInt(),
    )


def _build_conus_counties_fc() -> EEFeatureCollection:
    """Filter TIGER counties to CONUS and tag each feature with `fips_int`.

    Filtering and tagging stay server-side; the python list of state
    FIPS codes is just used to build the inList filter. Tagging adds
    the int-typed FIPS property `paint` reads in `_build_fips_image`.
    """
    return (
        ee.FeatureCollection(GEE_TIGER_COUNTIES)
        .filter(ee.Filter.inList('STATEFP', CONUS_STATE_FIPS))
        .map(_tag_with_fips_int)
    )


def _build_fips_image(counties_fc: EEFeatureCollection) -> EEImage:
    """Paint the FIPS int property into a self-masked int32 image.

    The painted-and-masked image is the asset's content. ``selfMask()``
    drops the unpainted background (value=0); since the smallest CONUS
    FIPS is 1001 (Autauga, AL), no real county pixel is ever 0, so the
    mask is unambiguous.

    No inline ``.reproject()``: per the investigation doc § "Bisect
    probe: inline paint doesn't amortize, asset-loaded mask does",
    ``.reproject()`` re-evaluates the polygon paint per tile rather
    than materializing once, so it adds cost to every consumer call
    against an un-materialized graph. The grid is instead pinned at
    the export sink via the ``crs`` / ``crsTransform`` arguments, so
    the on-disk asset is GLAD-aligned and downstream transform reads
    come from the materialized asset (no per-tile paint re-evaluation).
    """
    return (
        ee.Image.constant(0)
        .int()
        .paint(counties_fc, _FIPS_PROPERTY)
        .selfMask()
        .rename(BAND_NAME)
    )


def _start_fips_export(
    image: EEImage, asset_id: str, region: EEGeometry, description: str
) -> ee.batch.Task:
    """Submit the GLAD-grid asset export and return the task handle.

    Pins ``crs`` and ``crsTransform`` to GLAD's so the produced asset
    is byte-aligned with every other transform-stage output and no
    consumer ever pays a per-tile reprojection at read time.
    """
    task = ee.batch.Export.image.toAsset(
        image=image,
        description=description,
        assetId=asset_id,
        region=region,
        crs=GLAD_CRS,
        crsTransform=GLAD_CRS_TRANSFORM,
        maxPixels=int(1e13),
    )
    task.start()
    logger.info(
        f'county_fips: export task started (asset={asset_id}, '
        f'description={description})'
    )
    return task


def extract_county_fips(gcp_project: str, force: bool = False) -> str:
    """Materialize the CONUS county-FIPS label asset and return its ID.

    Args:
        gcp_project: GCP project. Accepted for orchestrator-API parity
            with other extractors; this extract is server-side only and
            does not touch GCS or HTTP, so the project is unused inside
            this function (auth is the caller's responsibility, mirroring
            the other extractors).
        force: If True, delete any existing target asset before re-export.

    Returns:
        The materialized GEE asset ID (= ``GEE_COUNTY_FIPS_LABEL``).
    """
    del gcp_project  # API parity; see docstring.

    if force:
        delete_asset_if_present(GEE_COUNTY_FIPS_LABEL)

    counties_fc = _build_conus_counties_fc()
    image = _build_fips_image(counties_fc)
    region = counties_fc.geometry().bounds()

    task = _start_fips_export(
        image=image,
        asset_id=GEE_COUNTY_FIPS_LABEL,
        region=region,
        description='extract_county_fips_conus',
    )
    wait_for_export_task(task, GEE_COUNTY_FIPS_LABEL)

    if not asset_exists(GEE_COUNTY_FIPS_LABEL):
        raise RuntimeError(
            f'county_fips: export task completed but asset is not present: '
            f'{GEE_COUNTY_FIPS_LABEL}'
        )

    logger.info(f'county_fips: extract complete → {GEE_COUNTY_FIPS_LABEL}')
    return GEE_COUNTY_FIPS_LABEL
