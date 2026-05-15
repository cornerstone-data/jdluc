"""Per-state emissions raster construction.

Builds the 11-band emissions_{region}_{version} raster from a previously
materialized land_use asset.

See specs/pipeline_tech_design.md § emissions.py for details.
"""

import logging
from dataclasses import dataclass

import ee

from jdluc.transform._export import export_image_asset_to_gee
from jdluc.transform.land_use import LAND_USE_BAND_NAMES
from jdluc.utils._ee_types import EEGeometry, EEImage
from jdluc.utils.constants import (
    CARBON_FRACTION_DEAD_WOOD,
    CARBON_FRACTION_LITTER,
    CARBON_FRACTION_LIVE,
    CO2_C_RATIO,
    DOM_FACTORS_BY_ZONE,
    EMISSIONS_BAND_NAMES,
    EMISSIVE_ENCODED_CODES,
    GEE_HARRIS_AGB,
    GEE_HUANG_BGB,
    GEE_IPCC_CLIMATE_ZONES,
    GEE_SOILGRIDS_SOC,
    GHGP_EPOCH_WEIGHTS_2020,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    GLAD_EPOCH_PAIRS,
    GRASSLAND_VEGETATION_TC_HA_BY_ZONE,
    IPCC_CLIMATE_ZONE_NATIVE_REMAP,
    IPCC_REFERENCE_SOC_STOCK_TC_HA,
    IPCC_SOC_LOSS_FRACTIONS,
    LAND_USE_CATEGORIES,
    PEATLAND_DRAINAGE_ENCODED_CODES,
    PEATLAND_E_LM_TCO2E_HA_YR,
    PEATLAND_P_LUC_TCO2_HA,
    ROOT_SHOOT_RATIO_TEMPERATE,
    luc_emissions_band,
    peatland_conversion_band,
    transitions_band,
)
from jdluc.utils.transitions import decode_from_image, decode_to_image

logger = logging.getLogger(__name__)


@dataclass
class EmissionsInputs:
    """Pre-loaded GEE inputs to the per-state emissions graph.

    Every field is an `EEImage` already clipped to the state geometry and
    pinned to the GLAD grid. Populated once by `build_emissions_image` and
    threaded through all three `calculate_*` helpers so the graph-building
    code doesn't need to re-load inputs per epoch.
    """

    agb: EEImage
    bgb: EEImage
    soc: EEImage
    climate_zone: EEImage
    is_peatland: EEImage
    crops_2020: EEImage
    pixel_area_ha: EEImage


# ---------------------------------------------------------------------------
# Per-pixel factor lookup tables (precomputed at module load)
# ---------------------------------------------------------------------------

# DOM carbon fraction per IPCC climate zone: dw_factor × C_dw + litter × C_lit.
# Applied per-pixel via `climate_zone.remap(_DOM_KEYS, _DOM_VALUES)` to build
# the dom_factor_per_pixel image.
_DOM_KEYS: list[int] = list(DOM_FACTORS_BY_ZONE.keys())
_DOM_VALUES: list[float] = [
    dw * CARBON_FRACTION_DEAD_WOOD + lit * CARBON_FRACTION_LITTER
    for (dw, lit) in DOM_FACTORS_BY_ZONE.values()
]

# Houghton/BLUE grassland total-vegetation-C (tC/ha), keyed by climate zone.
_GRASSLAND_KEYS: list[int] = list(GRASSLAND_VEGETATION_TC_HA_BY_ZONE.keys())
_GRASSLAND_VALUES: list[float] = list(GRASSLAND_VEGETATION_TC_HA_BY_ZONE.values())

# SOC loss-fraction table, flattened into parallel key/value lists for a
# single `.remap()` call per epoch. Composite key = zone*10000 + from*100 + to
# (all three codes fit in two decimal digits).
_SOC_KEYS: list[int] = [
    zone * 10000 + from_code * 100 + to_code
    for (zone, from_code, to_code) in IPCC_SOC_LOSS_FRACTIONS
]
_SOC_VALUES: list[float] = list(IPCC_SOC_LOSS_FRACTIONS.values())


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------


def _load_agb(state_geometry: EEGeometry) -> EEImage:
    """Harris et al. (2021) above-ground biomass, Mg biomass/ha.

    Tiles are uploaded pre-aligned to the GLAD grid, but
    ``ImageCollection.mosaic()`` drops the per-tile projection metadata
    and returns a degenerate ``scale=111319 m`` default. ``setDefaultProjection``
    re-attaches the GLAD projection without forcing a resample (the tiles
    already live on that grid). Critical at multi-state scope: without
    this, downstream operations fall back to the 1°/EPSG:4326 default
    inherited from the missing-projection mosaic, which forces redundant
    reprojection of every other input that touches the same expression.
    """
    return (
        ee.ImageCollection(GEE_HARRIS_AGB)
        .mosaic()
        .setDefaultProjection(crs=GLAD_CRS, crsTransform=GLAD_CRS_TRANSFORM)
        .clip(state_geometry)
        .select([0])
        .toFloat()
        .rename('agb')
    )


def _load_bgb(state_geometry: EEGeometry, agb_image: EEImage) -> EEImage:
    """Huang et al. (2021) below-ground biomass, Mg biomass/ha.

    Huang ships at ~1 km on a non-GLAD grid; ``.resample('bilinear')`` sets
    the resampling kernel for continuous data, and the actual reprojection
    onto ``GLAD_CRS_TRANSFORM`` happens once at the export node via the
    Export call's pinned ``crs`` / ``crsTransform`` (the GEE pull-through
    pattern). Calling ``.reproject()`` here forces eager materialization of
    the entire reprojected raster at union scope, which trips
    ``"User memory limit exceeded"`` on the diagnostic path and per-tile
    memory pressure on the export path. NoData pixels are filled via IPCC
    Tier 1 temperate R:S applied to the AGB input.
    """
    huang = (
        ee.Image(GEE_HUANG_BGB).clip(state_geometry).select([0]).resample('bilinear')
    )
    fallback = agb_image.multiply(ROOT_SHOOT_RATIO_TEMPERATE)
    return huang.unmask(fallback).toFloat().rename('bgb')


def _load_soc(state_geometry: EEGeometry) -> EEImage:
    """SoilGrids SOC stock (0–30 cm), tC/ha.

    Native 250 m Interrupted Goode Homolosine grid. ``.resample('bilinear')``
    sets the kernel for continuous-data resampling; the actual reprojection
    onto the GLAD grid happens at the export node, not here (see ``_load_bgb``
    for the rationale). NoData pixels (<1% of conversion area) are gap-filled
    with the IPCC reference SOC stock for warm-temperate-moist mineral soil
    (IPCC 2019 Vol 4 Table 2.3).
    """
    return (
        ee.Image(GEE_SOILGRIDS_SOC)
        .clip(state_geometry)
        .select([0])
        .resample('bilinear')
        .unmask(IPCC_REFERENCE_SOC_STOCK_TC_HA)
        .toFloat()
        .rename('soc')
    )


def _load_ipcc_climate(state_geometry: EEGeometry) -> EEImage:
    """IPCC climate-zone raster, remapped to canonical 1–10 codes.

    The asset is pre-aligned to the GLAD grid, so no reprojection.
    Native Ogle codes are remapped to our canonical vocabulary via
    IPCC_CLIMATE_ZONE_NATIVE_REMAP; pixels outside the vocabulary (polar
    zones 11–12) fall through to 0.
    """
    return (
        ee.Image(GEE_IPCC_CLIMATE_ZONES)
        .clip(state_geometry)
        .select([0])
        .remap(
            list(IPCC_CLIMATE_ZONE_NATIVE_REMAP.keys()),
            list(IPCC_CLIMATE_ZONE_NATIVE_REMAP.values()),
            defaultValue=0,
        )
        .rename('climate_zone')
    )


def _load_pixel_area_ha() -> EEImage:
    """Per-pixel area in hectares.

    `ee.Image.pixelArea()` is a virtual image — area is computed on demand
    at whatever output projection the consumer requests. Returning it
    un-reprojected lets the export's pinned ``crs`` / ``crsTransform``
    materialize area at the GLAD grid exactly once at the output node;
    eagerly ``.reproject()``-ing here forces the full GLAD-extent area
    raster to be computed up front (1.3 B float32 cells at multi-state
    scope), which is the textbook "request all inputs at very small scale
    over a wide spatial extent" anti-pattern from the Projections guide.
    """
    return ee.Image.pixelArea().divide(1e4).rename('area_ha')


def _load_land_use_bands(land_use_asset_id: str, state_geometry: EEGeometry) -> EEImage:
    """Load the six-band cached land_use asset, clipped to the state.

    Single source of truth for the four `transitions_{epoch}` bands,
    `crops_2020`, and `is_peatland` — everything in emissions.py that reads
    the land_use side of the pipeline goes through this loader.
    """
    return ee.Image(land_use_asset_id).clip(state_geometry).select(LAND_USE_BAND_NAMES)


# ---------------------------------------------------------------------------
# Per-epoch emissions calculation
# ---------------------------------------------------------------------------


def calculate_epoch_emissions(
    encoded_transition: EEImage,
    inputs: EmissionsInputs,
    epoch: tuple[int, int],
) -> EEImage:
    """Compute LUC + peatland conversion emissions for a single epoch.

    Returns a two-band float32 image `(luc_emissions_{epoch},
    peatland_conversion_{epoch})`. Vegetation and SOC paths are gated on the
    9-pair LUC-emissive mask; peatland conversion is gated on the narrower
    8-pair peatland-drainage mask (forest→short_veg excluded — vegetation
    disturbance alone does not drain a peatland).
    """
    from_year, to_year = epoch
    from_code = decode_from_image(encoded_transition)
    to_code = decode_to_image(encoded_transition)

    # Per-pixel factor images keyed by the pixel's IPCC climate zone.
    dom_factor = inputs.climate_zone.remap(
        _DOM_KEYS, _DOM_VALUES, defaultValue=0
    ).toFloat()
    grassland_c = inputs.climate_zone.remap(
        _GRASSLAND_KEYS, _GRASSLAND_VALUES, defaultValue=0
    ).toFloat()

    # Per-pixel forest vegetation-C stock: AGB_C + BGB_C + DOM_C, tC/ha.
    forest_c = (
        inputs.agb.multiply(CARBON_FRACTION_LIVE)
        .add(inputs.bgb.multiply(CARBON_FRACTION_LIVE))
        .add(inputs.agb.multiply(dom_factor))
    )

    forest = LAND_USE_CATEGORIES['forest'].code
    wetland_forest = LAND_USE_CATEGORIES['wetland_forest'].code
    short_veg = LAND_USE_CATEGORIES['short_vegetation'].code
    wetland_short_veg = LAND_USE_CATEGORIES['wetland_short_vegetation'].code

    # stock_from: per-pixel source-side vegetation C. 0 for any source
    # category other than the four in the emissive vocabulary.
    stock_from = (
        ee.Image.constant(0)
        .toFloat()
        .where(from_code.eq(forest), forest_c)
        .where(from_code.eq(wetland_forest), forest_c)
        .where(from_code.eq(short_veg), grassland_c)
        .where(from_code.eq(wetland_short_veg), grassland_c)
    )
    # stock_to: per-pixel destination-side vegetation C. Only short-veg
    # destinations re-accumulate grassland stock; cropland/built_up/water/
    # snow_ice/bare destinations all retain 0.
    stock_to = (
        ee.Image.constant(0)
        .toFloat()
        .where(to_code.eq(short_veg), grassland_c)
        .where(to_code.eq(wetland_short_veg), grassland_c)
    )

    # Emissive-transition masks. `is_emissive` gates the vegetation + SOC
    # paths (9-pair vocabulary); `is_peatland_drainage` gates peatland
    # conversion (8-pair subset — cropland/built_up destinations only).
    is_emissive = encoded_transition.remap(
        EMISSIVE_ENCODED_CODES,
        [1] * len(EMISSIVE_ENCODED_CODES),
        defaultValue=0,
    )
    is_peatland_drainage = encoded_transition.remap(
        PEATLAND_DRAINAGE_ENCODED_CODES,
        [1] * len(PEATLAND_DRAINAGE_ENCODED_CODES),
        defaultValue=0,
    )

    # Vegetation emissions: clamp negative stock deltas to 0 so inert or
    # stock-gaining transitions don't introduce removals.
    veg_tco2 = (
        stock_from.subtract(stock_to)
        .max(0)
        .multiply(CO2_C_RATIO)
        .multiply(inputs.pixel_area_ha)
        .multiply(is_emissive)
    )

    # SOC loss fraction via composite-key .remap(), zero for non-emissive
    # pairs by construction.
    soc_key = (
        inputs.climate_zone.multiply(10000).add(from_code.multiply(100)).add(to_code)
    )
    loss_fraction = soc_key.remap(_SOC_KEYS, _SOC_VALUES, defaultValue=0).toFloat()
    soc_tco2 = (
        inputs.soc.multiply(loss_fraction)
        .multiply(CO2_C_RATIO)
        .multiply(inputs.pixel_area_ha)
        .multiply(is_emissive)
    )

    luc_emissions = (
        veg_tco2.add(soc_tco2)
        .unmask(0)
        .toFloat()
        .rename(luc_emissions_band(from_year, to_year))
    )
    peatland_conversion = (
        inputs.is_peatland.multiply(is_peatland_drainage)
        .multiply(PEATLAND_P_LUC_TCO2_HA)
        .multiply(inputs.pixel_area_ha)
        .unmask(0)
        .toFloat()
        .rename(peatland_conversion_band(from_year, to_year))
    )
    return luc_emissions.addBands(peatland_conversion)


# ---------------------------------------------------------------------------
# Peatland occupation (annual, undiscounted)
# ---------------------------------------------------------------------------


def calculate_peatland_occupation(inputs: EmissionsInputs) -> EEImage:
    """Compute the 2020 peatland-occupation emissions band.

    `E_LM × pixel_area_ha` applied on pixels that are both peatland and
    classified as cropland in GLAD 2020 (encoded as "CDL crop code present"
    in the land_use asset's `crops_2020` band). Masking via multiplication
    rather than `.updateMask()` keeps the output a fully-populated float32
    grid with 0.0 outside the mask (Design decision 6).
    """
    is_cropland_2020 = inputs.crops_2020.gt(0)
    mask = inputs.is_peatland.multiply(is_cropland_2020)
    return (
        mask.multiply(PEATLAND_E_LM_TCO2E_HA_YR)
        .multiply(inputs.pixel_area_ha)
        .unmask(0)
        .toFloat()
        .rename('peatland_occupation_2020')
    )


# ---------------------------------------------------------------------------
# 2020-epoch GHGP allocation
# ---------------------------------------------------------------------------


def calculate_allocated_emissions_2020(
    luc_epoch_bands: list[EEImage],
    peatland_epoch_bands: list[EEImage],
    occupation_band: EEImage,
) -> EEImage:
    """Compute the two 2020-allocated emissions bands.

    Per-epoch LUC and peatland-conversion emissions are discounted by the
    GHGP 2020-epoch weights; peatland occupation is added at full weight
    to the peatland-allocated band (it's an annual land-management emission,
    not a conversion pulse).
    """
    if len(luc_epoch_bands) != len(GLAD_EPOCH_PAIRS) or len(
        peatland_epoch_bands
    ) != len(GLAD_EPOCH_PAIRS):
        raise ValueError(
            'expected one band per GLAD_EPOCH_PAIRS entry; got '
            f'{len(luc_epoch_bands)} LUC, {len(peatland_epoch_bands)} peatland'
        )

    allocated_luc = ee.Image.constant(0).toFloat()
    allocated_peat = ee.Image.constant(0).toFloat()
    for epoch, luc, peat in zip(
        GLAD_EPOCH_PAIRS, luc_epoch_bands, peatland_epoch_bands
    ):
        weight = GHGP_EPOCH_WEIGHTS_2020[epoch]
        allocated_luc = allocated_luc.add(luc.multiply(weight))
        allocated_peat = allocated_peat.add(peat.multiply(weight))
    allocated_peat = allocated_peat.add(occupation_band)

    return (
        allocated_luc.toFloat()
        .rename('allocated_luc_emissions_2020')
        .addBands(allocated_peat.toFloat().rename('allocated_peatland_emissions_2020'))
    )


# ---------------------------------------------------------------------------
# Main graph builder
# ---------------------------------------------------------------------------


def build_emissions_image(land_use_asset_id: str, geometry: EEGeometry) -> EEImage:
    """Construct the 11-band emissions raster for the requested region.

    Reads the cached land_use asset (via its asset ID — not a fresh in-memory
    build — so emissions are computed against the finalized exported
    raster), loads the four GEE-side input datasets, then stitches per-epoch
    LUC + peatland-conversion bands, the 2020 peatland-occupation band, and
    the two 2020-allocated bands into a single 11-band float32 image.

    `geometry` is an `ee.Geometry` covering the requested region —
    single-state, multi-state union, or CONUS. No per-state subdivision;
    the whole region is built in one graph.

    Per-input ``.clip()`` calls are scoped to ``geometry.bounds()``
    rather than ``geometry`` itself: a polygon clip pays a per-vertex-
    per-tile cost; a 4-vertex bbox clip does not. The polygon-shape
    mask needed for the asset's content footprint is applied at the
    export sink via ``fips_mask`` (see ``export_emissions_asset``), not
    at the per-input clips.

    Pure graph building — no exports, no `.getInfo()`.
    """
    region_bbox = geometry.bounds()
    land_use = _load_land_use_bands(land_use_asset_id, region_bbox)

    agb = _load_agb(region_bbox)
    inputs = EmissionsInputs(
        agb=agb,
        bgb=_load_bgb(region_bbox, agb_image=agb),
        soc=_load_soc(region_bbox),
        climate_zone=_load_ipcc_climate(region_bbox),
        is_peatland=land_use.select('is_peatland'),
        crops_2020=land_use.select('crops_2020'),
        pixel_area_ha=_load_pixel_area_ha(),
    )

    luc_bands: list[EEImage] = []
    peat_bands: list[EEImage] = []
    for from_year, to_year in GLAD_EPOCH_PAIRS:
        encoded = land_use.select(transitions_band(from_year, to_year))
        epoch_image = calculate_epoch_emissions(encoded, inputs, (from_year, to_year))
        luc_bands.append(epoch_image.select(luc_emissions_band(from_year, to_year)))
        peat_bands.append(
            epoch_image.select(peatland_conversion_band(from_year, to_year))
        )

    occupation = calculate_peatland_occupation(inputs)
    allocated = calculate_allocated_emissions_2020(luc_bands, peat_bands, occupation)

    image = luc_bands[0]
    for band in luc_bands[1:] + peat_bands + [occupation, allocated]:
        image = image.addBands(band)

    return image.toFloat().select(EMISSIONS_BAND_NAMES)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_emissions_asset(
    image: EEImage,
    region: str,
    version: str,
    geometry: EEGeometry,
    fips_mask: EEImage,
    force: bool = False,
) -> ee.batch.Task | None:
    """Export the emissions image as a GEE asset.

    See ``transform._export.export_image_asset_to_gee`` for the canonical
    export shape (``region=geometry.bounds()``, GLAD CRS pinning,
    cache-probe semantics). Returns the task handle for polling, or
    None on cache hit.
    """
    return export_image_asset_to_gee(
        image=image,
        asset_kind='emissions',
        region=region,
        version=version,
        geometry=geometry,
        fips_mask=fips_mask,
        force=force,
    )
