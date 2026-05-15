"""Shared image-export helper for the transform stage.

`land_use.py` and `emissions.py` produce ~95% identical
``Export.image.toAsset`` shapes. This module owns the canonical export
shape so future changes to projection pinning, mask application, or
cache-skip semantics live in one place.
"""

import logging
from typing import Literal

import ee

from jdluc.utils._ee_types import EEGeometry, EEImage
from jdluc.utils.asset_management import delete_asset_safely
from jdluc.utils.constants import (
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    output_asset_id,
)

logger = logging.getLogger(__name__)


def export_image_asset_to_gee(
    *,
    image: EEImage,
    asset_kind: Literal['land_use', 'emissions'],
    region: str,
    version: str,
    geometry: EEGeometry,
    fips_mask: EEImage,
    force: bool,
) -> ee.batch.Task | None:
    """Export an image asset with the canonical jdLUC export shape.

    The export uses ``region=geometry.bounds()`` (a 4-vertex rectangle)
    rather than the polygon itself, avoiding polygon-vertex-per-tile
    overhead. The asset's polygon-shape masking is preserved by
    ``image.updateMask(fips_mask)`` immediately before the export —
    ``fips_mask`` is the run-scoped county-FIPS mask (1 inside the
    requested states' CONUS counties, masked elsewhere), sourced from
    the canonical ``GEE_COUNTY_FIPS_LABEL`` asset.

    ``crs`` and ``crsTransform`` are pinned on the export per the
    GEE-canonical "pull-through" pattern: pinning at the output node
    propagates back through the lazy graph, so each input is
    materialized in the GLAD target grid exactly once at the export
    node. Eager ``.reproject()`` calls on the inputs are deliberately
    omitted — they trigger per-tile memory pressure (see the GEE
    Projections guide's "Use reproject() with caution!" note).

    Cache semantics: probes via ``ee.data.getAsset`` when ``force`` is
    False; on a hit, returns None and emits a ``cached, skipping export``
    log line. When ``force`` is True, deletes the existing asset before
    submitting the new task. Returns the running task handle on a fresh
    submission.
    """
    asset_id = output_asset_id(asset_kind, region, version)

    if not force:
        try:
            ee.data.getAsset(asset_id)
            logger.info(f'{asset_kind} asset cached, skipping export: {asset_id}')
            return None
        except ee.EEException as e:
            if 'does not exist' not in str(e):
                raise
    else:
        delete_asset_safely(asset_id)

    masked_image = image.updateMask(fips_mask)
    task = ee.batch.Export.image.toAsset(
        image=masked_image,
        assetId=asset_id,
        description=asset_id.rsplit('/', 1)[-1],
        region=geometry.bounds(),
        crs=GLAD_CRS,
        crsTransform=GLAD_CRS_TRANSFORM,
        maxPixels=int(1e13),
    )
    task.start()
    logger.info(f'Started {asset_kind} export: {asset_id}')
    return task
