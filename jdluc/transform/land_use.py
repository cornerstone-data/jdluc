"""Per-state land use raster construction.

Builds the 6-band land_use_{region}_{version} raster for a state:
  - 4 packed transition bands (uint8, one per GLAD epoch pair)
  - crops_2020 (uint8, CDL crop code on GLAD cropland pixels)
  - is_peatland (uint8, binary peatland mask)

See specs/pipeline_tech_design.md § land_use.py for details.
"""

import logging

import ee

from jdluc.transform._export import export_image_asset_to_gee
from jdluc.utils._ee_types import EEGeometry, EEImage
from jdluc.utils.constants import (
    DATASET_INVENTORY,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    GLAD_EPOCH_PAIRS,
    GLAD_EPOCH_YEARS,
    LAND_USE_CATEGORIES,
    transitions_band,
)
from jdluc.utils.transitions import encode_transition_image

logger = logging.getLogger(__name__)

# Output band schema for the land_use raster.
LAND_USE_BAND_NAMES: list[str] = [
    transitions_band(fy, ty) for fy, ty in GLAD_EPOCH_PAIRS
] + ['crops_2020', 'is_peatland']


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_glad_glc(raw_image: EEImage) -> EEImage:
    """Reclassify a raw GLAD GLCLUC v2 band into 9 land use categories.

    Takes a single-band uint8 image with raw GLAD values (0-250) and returns
    a single-band uint8 image with category codes 1-9 (0 for unclassified).
    """
    raw_values: list[int] = []
    category_codes: list[int] = []
    for cat in LAND_USE_CATEGORIES.values():
        for v in cat.glad_values:
            raw_values.append(v)
            category_codes.append(cat.code)

    return (
        raw_image.remap(raw_values, category_codes, defaultValue=0)
        .uint8()
        .rename('category')
    )


# ---------------------------------------------------------------------------
# Auxiliary band loaders
# ---------------------------------------------------------------------------


def _load_crops_2020(
    state_geometry: EEGeometry, glad_category_2020: EEImage
) -> EEImage:
    """Load CDL 2020 crop codes, mask to GLAD cropland pixels.

    CDL is on a non-GLAD native grid (Albers Equal Area at 30 m); the
    actual reprojection to the GLAD grid happens at the export node via
    the Export call's pinned ``crs`` / ``crsTransform`` (the GEE
    pull-through pattern). Default nearest-neighbor resampling is the
    correct choice for categorical CDL crop codes and is GEE's default
    when no ``.resample(...)`` is specified.
    """
    cdl_2020 = (
        ee.ImageCollection(DATASET_INVENTORY['usda_cdl']['gee_asset_id'])
        .filter(ee.Filter.calendarRange(2020, 2020, 'year'))
        .first()
        .select('cropland')
        .clip(state_geometry)
    )
    cropland_mask = glad_category_2020.eq(LAND_USE_CATEGORIES['cropland'].code)
    return cdl_2020.where(cropland_mask.Not(), 0).uint8().rename('crops_2020')


def _load_is_peatland(state_geometry: EEGeometry) -> EEImage:
    """Load GFW Global Peatlands as a binary mask.

    The asset is an ``ImageCollection`` of 10°×10° tile members already
    on the GLAD 30 m grid; ``.mosaic()`` combines them into a single
    image lazily on read but drops the per-tile projection metadata —
    ``setDefaultProjection`` re-attaches the GLAD projection without
    forcing a resample (the tiles are already on that grid). The actual
    grid alignment of the export output is driven by the Export call's
    pinned ``crs`` / ``crsTransform`` (pull-through pattern). Categorical
    (binary 0/1) — default nearest-neighbor resampling is the right
    choice.
    """
    return (
        ee.ImageCollection(DATASET_INVENTORY['gfw_peatlands']['gee_asset_id'])
        .mosaic()
        .setDefaultProjection(crs=GLAD_CRS, crsTransform=GLAD_CRS_TRANSFORM)
        .clip(state_geometry)
        .gt(0)  # ensure binary 0/1
        .uint8()
        .rename('is_peatland')
    )


# ---------------------------------------------------------------------------
# Main graph builder
# ---------------------------------------------------------------------------


def build_land_use_image(geometry: EEGeometry) -> EEImage:
    """Construct the 6-band land_use raster for the requested region.

    Bands (all uint8):
      transitions_2000_2005, transitions_2005_2010,
      transitions_2010_2015, transitions_2015_2020,
      crops_2020, is_peatland

    `geometry` is an `ee.Geometry` covering the requested region — a
    single state's boundary, the union of multiple states, or CONUS. No
    per-state subdivision; the whole region is built in one graph.

    Per-input ``.clip()`` calls are scoped to ``geometry.bounds()``
    rather than ``geometry`` itself: a polygon clip pays a per-vertex-
    per-tile cost; a 4-vertex bbox clip does not. The polygon-shape
    mask needed for the asset's content footprint is applied at the
    export sink via ``fips_mask`` (see ``export_land_use_asset``), not
    at the per-input clips.

    Pure graph building -- no Export calls.
    """
    region_bbox = geometry.bounds()

    # Load and classify all five GLAD epochs
    classified: dict[int, EEImage] = {}
    for year in GLAD_EPOCH_YEARS:
        asset_id = DATASET_INVENTORY['glad_glcluc_v2']['gee_asset_id'].format(year=year)
        raw = ee.Image(asset_id).clip(region_bbox)
        classified[year] = classify_glad_glc(raw)

    # Build the four transition bands
    transition_bands: list[EEImage] = []
    for from_year, to_year in GLAD_EPOCH_PAIRS:
        band = encode_transition_image(
            classified[from_year], classified[to_year]
        ).rename(transitions_band(from_year, to_year))
        transition_bands.append(band)

    # Stack transitions
    image = transition_bands[0]
    for band in transition_bands[1:]:
        image = image.addBands(band)

    # Add crops_2020 band
    crops = _load_crops_2020(region_bbox, classified[2020])
    image = image.addBands(crops)

    # Add is_peatland band
    peatland = _load_is_peatland(region_bbox)
    image = image.addBands(peatland)

    # Enforce uint8 on all bands and verify band order
    image = image.uint8().select(LAND_USE_BAND_NAMES)

    return image


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_land_use_asset(
    image: EEImage,
    region: str,
    version: str,
    geometry: EEGeometry,
    fips_mask: EEImage,
    force: bool = False,
) -> ee.batch.Task | None:
    """Export the land_use image as a GEE asset.

    See ``transform._export.export_image_asset_to_gee`` for the canonical
    export shape (``region=geometry.bounds()``, GLAD CRS pinning,
    cache-probe semantics). Returns the task handle for polling, or
    None on cache hit.
    """
    return export_image_asset_to_gee(
        image=image,
        asset_kind='land_use',
        region=region,
        version=version,
        geometry=geometry,
        fips_mask=fips_mask,
        force=force,
    )
