"""Per-state summary-table construction.

Reduces the cached land_use and emissions rasters to two GEE
FeatureCollection table assets per state: transitions_{region}_{version}
at (county_fips, epoch_transition, emissions_type) grain, and
crops_{region}_{version} at (county_fips, crop_code) grain.

See specs/pipeline_tech_design.md § summary_tables.py for details.
"""

import functools
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import ee

from jdluc.utils._ee_types import EEFeatureCollection, EEGeometry, EEImage
from jdluc.utils.asset_management import delete_asset_safely
from jdluc.utils.constants import (
    ALL_ROW_CROP_CODES,
    BUSHEL_WEIGHT_KG,
    CROP_CODE_TO_GROUP,
    CROP_DRIVER_MAPPING,
    CROP_GROUPS,
    CROPS_TABLE_COLUMNS,
    EMISSIONS_TYPE_CODE_TO_NAME,
    EMISSIONS_TYPE_CODES,
    EMISSIONS_TYPE_NAMES,
    EMISSIVE_ENCODED_CODES,
    GEE_NASS_YIELDS,
    GHGP_EPOCH_WEIGHTS_2020,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
    GLAD_EPOCH_PAIRS,
    HA_PER_ACRE,
    NASS_TO_CROP_GROUP,
    NASS_YIELD_YEARS,
    PEATLAND_DRAINAGE_ENCODED_CODES,
    PEATLAND_OCCUPATION_EPOCH_LABEL,
    TRANSITIONS_TABLE_COLUMNS,
    luc_emissions_band,
    output_asset_id,
    peatland_conversion_band,
    transitions_band,
)
from jdluc.utils.gee import asset_exists

logger = logging.getLogger(__name__)

# Pulled from the central emissions_type vocabulary so the reducer's group
# keys always match the 1..11 emissions-type codes.
_LUC_CODES: list[int] = list(range(1, 10))
_PEATLAND_CONVERSION_CODE: int = EMISSIONS_TYPE_CODES['peatland_conversion']
_PEATLAND_OCCUPATION_CODE: int = EMISSIONS_TYPE_CODES['peatland_occupation']

# ---------------------------------------------------------------------------
# Crops-table driver-pair encoded transitions
# ---------------------------------------------------------------------------
# Subsets of EMISSIVE_ENCODED_CODES partitioned by source family per
# CROP_DRIVER_MAPPING. Used in `_build_crops_metric_stack` to construct the
# per-pixel `is_forest_pair` / `is_short_veg_pair` masks via remap. Built
# once at module load from public constants — the source-family partitioning
# is fully derivable from EMISSIVE_ENCODED_CODES (ordered) + EMISSIONS_TYPE_NAMES
# (parallel list, first 9 are LUC names) + CROP_DRIVER_MAPPING.
_FOREST_ENCODED_CODES: list[int] = [
    EMISSIVE_ENCODED_CODES[i]
    for i in range(len(EMISSIVE_ENCODED_CODES))
    if CROP_DRIVER_MAPPING[EMISSIONS_TYPE_NAMES[i]] == 'pct_forest'
]
_SHORT_VEG_ENCODED_CODES: list[int] = [
    EMISSIVE_ENCODED_CODES[i]
    for i in range(len(EMISSIVE_ENCODED_CODES))
    if CROP_DRIVER_MAPPING[EMISSIONS_TYPE_NAMES[i]] == 'pct_short_veg'
]

# Output column ordering for the 10-band metric stack. The reducer is
# `ee.Reducer.sum().repeat(10).group(groupField=10, ...)`; the group-key band
# (crop_code) sits at position 10. Reducer output sums come back as a
# 10-element list in this exact order — the flatten step in `_emit_crop_row`
# indexes via `sums.get(0..9)`, so any reordering breaks the pivot.
_METRIC_BAND_NAMES: tuple[str, ...] = (
    'area_total',
    'area_peatland',
    'allocated_forest',
    'allocated_short_veg',
    'allocated_peatland_conversion',
    'allocated_peatland_occupation',
    'allocated_2005',
    'allocated_2010',
    'allocated_2015',
    'allocated_2020',
)


# ---------------------------------------------------------------------------
# Server-side lookup dictionaries (lazy)
# ---------------------------------------------------------------------------
# `build_transitions_table` (and the future server-side `build_crops_table`)
# attaches derived columns via `.map()` chains that do `ee.Dictionary` lookups
# server-side. Build the dictionaries once per process; lazy because `ee` may
# not be initialized at module import time.


@functools.lru_cache(maxsize=1)
def _emissions_type_name_dict() -> ee.Dictionary:
    """`emissions_type_code (str) → emissions_type` (e.g. `'1'` → `'forest_to_cropland'`)."""
    return ee.Dictionary({str(c): n for c, n in EMISSIONS_TYPE_CODE_TO_NAME.items()})


@functools.lru_cache(maxsize=1)
def _ghgp_weight_dict() -> ee.Dictionary:
    """`'{from}_{to}' → GHGP 20-year-lookback epoch weight` for LUC + peatland_conv rows."""
    return ee.Dictionary(
        {f'{a}_{b}': w for (a, b), w in GHGP_EPOCH_WEIGHTS_2020.items()}
    )


@functools.lru_cache(maxsize=1)
def _crop_group_dict() -> ee.Dictionary:
    """`crop_code (str) → crop_group` (e.g. `'1'` → `'corn'`, `'22'` → `'wheat'`)."""
    return ee.Dictionary({str(c): g for c, g in CROP_CODE_TO_GROUP.items()})


@functools.lru_cache(maxsize=1)
def _flat_yield_dict() -> dict[str, dict[str, float]]:
    """Build the per-(state_fips,crop_code) yield dict once per process."""
    return _nass_yield_dict_from_features(_load_nass_yield_features(GEE_NASS_YIELDS))


@functools.lru_cache(maxsize=1)
def _yield_bu_dict() -> ee.Dictionary:
    """`'{state_fips}|{crop_code}' → bu/acre yield` (state-level, NASS_YIELD_YEARS mean)."""
    return ee.Dictionary(
        {k: v['yield_bu_per_acre'] for k, v in _flat_yield_dict().items()}
    )


@functools.lru_cache(maxsize=1)
def _yield_kg_dict() -> ee.Dictionary:
    """`'{state_fips}|{crop_code}' → kg/ha yield` (state-level, NASS_YIELD_YEARS mean)."""
    return ee.Dictionary(
        {k: v['yield_kg_per_ha'] for k, v in _flat_yield_dict().items()}
    )


def _dict_get_or_null(dictionary: Any, key: Any) -> Any:
    """`ee.Dictionary.get(key, None)` doesn't do what you'd hope.

    GEE's `Dictionary.get(key, defaultValue)` treats a `None`/`null`
    `defaultValue` as "no default — raise on missing key" rather than as
    "return null on missing key." To actually get null-on-miss semantics
    (which the crops table needs for missing NASS yields and the
    transitions decoration needs for the crop_code=0 sentinel), branch
    explicitly via `ee.Algorithms.If(dict.contains(key), dict.get(key), None)`.
    """
    d = ee.Dictionary(dictionary)
    return ee.Algorithms.If(d.contains(key), d.get(key), None)


# ---------------------------------------------------------------------------
# NASS yield dictionary
# ---------------------------------------------------------------------------
# The NASS asset (extract/nass_yields.py) carries one feature per raw
# (state_fips, commodity_desc, unit_desc, agg_level_desc, year) row from the
# USDA QuickStats archive, pre-filtered to the three commodity families
# (CORN / SOYBEANS / WHEAT) and the YIELD statistic. This module does the
# rest of the pipeline: (a) filter to state-level bu/acre rows within
# NASS_YIELD_YEARS, (b) take the arithmetic mean per (state, commodity),
# (c) convert bu/acre -> kg/ha via BUSHEL_WEIGHT_KG / HA_PER_ACRE, and
# (d) fan out the single WHEAT row to the three wheat CDL codes so
# downstream lookups can key on crop_code directly. Keeping these steps
# at the transform layer makes the window / conversion choices visible
# in one place and keeps the extract asset window-agnostic (a new
# NASS_YIELD_YEARS window does not force a re-extract).


@functools.lru_cache(maxsize=1)
def _load_nass_yield_features(asset_id: str) -> tuple[dict[str, Any], ...]:
    """Materialize the NASS yield FeatureCollection to a tuple of property dicts.

    The NASS extract asset carries the full QuickStats archive
    pre-filtered to YIELD + (CORN/SOYBEANS/WHEAT) — order of 10⁶ rows.
    Pulling the whole thing through ``.getInfo()`` blows past GEE's
    response-size cap, so we apply the methodology's
    ``agg_level_desc == 'STATE'`` / ``unit_desc == 'BU / ACRE'`` /
    ``year ∈ NASS_YIELD_YEARS`` / ``commodity_desc ∈ NASS_TO_CROP_GROUP``
    filters server-side first. After filtering, the result is on the
    order of 50 states × 4 years × 3 commodities ≈ 600 features (a
    few hundred KB of JSON), which round-trips cheaply.

    The year window is baked in via ``NASS_YIELD_YEARS``: the asset
    version tag (``EXPECTED_NASS_VERSION = 'v2017_2020'``) already
    encodes that coupling, so a methodology window change forces a
    new asset ID, which invalidates this cache.

    Cached at module scope (keyed on the asset ID) so a multi-state
    run does the load once and reuses across every state's
    ``build_crops_table()`` call.
    """
    fc = (
        ee.FeatureCollection(asset_id)
        .filter(ee.Filter.eq('agg_level_desc', 'STATE'))
        .filter(ee.Filter.eq('unit_desc', 'BU / ACRE'))
        .filter(ee.Filter.inList('commodity_desc', list(NASS_TO_CROP_GROUP)))
        .filter(ee.Filter.inList('year', list(NASS_YIELD_YEARS)))
    )
    raw = fc.getInfo()
    features = tuple(feature['properties'] for feature in raw['features'])
    logger.info(
        f'NASS yield load: {asset_id} → {len(features)} feature(s) '
        f'after server-side filter'
    )
    return features


def _filter_average_convert_nass_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    years: Sequence[int] = NASS_YIELD_YEARS,
) -> dict[tuple[str, str], dict[str, float]]:
    """Turn raw NASS rows into a `(state_fips, commodity) → yield dict`.

    Applies the methodology's state-level yield-mean definition:

    - Keep rows with ``agg_level_desc == 'STATE'`` and
      ``unit_desc == 'BU / ACRE'`` and ``year in years``.
    - Group by ``(state_fips, commodity_desc)`` and take the arithmetic
      mean of ``value_bu_per_acre`` (4-year mean by default per
      methodology § Calculate crop yields).
    - Convert to kg/ha via ``yield_bu_per_acre * BUSHEL_WEIGHT_KG / HA_PER_ACRE``.

    Unknown ``commodity_desc`` values (not in ``NASS_TO_CROP_GROUP``) are
    dropped silently — the extract stage pre-filters to the three
    methodology commodities, so anything else is genuinely noise.
    """
    year_set = {int(y) for y in years}
    accum: dict[tuple[str, str], list[float]] = {}
    for props in rows:
        if str(props.get('agg_level_desc')) != 'STATE':
            continue
        if str(props.get('unit_desc')) != 'BU / ACRE':
            continue
        try:
            year = int(props['year'])
        except (KeyError, TypeError, ValueError):
            continue
        if year not in year_set:
            continue
        commodity = str(props.get('commodity_desc', ''))
        if commodity not in NASS_TO_CROP_GROUP:
            continue
        try:
            value = float(props['value_bu_per_acre'])
        except (KeyError, TypeError, ValueError):
            continue
        state_fips = str(props['state_fips']).zfill(2)
        accum.setdefault((state_fips, commodity), []).append(value)

    result: dict[tuple[str, str], dict[str, float]] = {}
    for key, values in accum.items():
        if not values:
            continue
        mean_bu_per_acre = sum(values) / len(values)
        commodity = key[1]
        kg_per_ha = mean_bu_per_acre * BUSHEL_WEIGHT_KG[commodity] / HA_PER_ACRE
        result[key] = {
            'yield_bu_per_acre': mean_bu_per_acre,
            'yield_kg_per_ha': kg_per_ha,
        }
    return result


def _nass_yield_dict_from_features(
    features: Sequence[Mapping[str, Any]],
    *,
    years: Sequence[int] = NASS_YIELD_YEARS,
) -> dict[str, dict[str, float]]:
    """Build the `{f"{state_fips}|{crop_code}": {yield_*}}` dict.

    Delegates the filter / average / convert to
    ``_filter_average_convert_nass_rows``, then fans the single WHEAT
    row per state out to CDL codes 22 / 23 / 24 so downstream lookups
    can key on crop_code directly.

    Separated from ``_build_nass_yield_dictionary`` so unit tests can
    exercise the fan-out and key-formatting on synthetic rows without
    initializing the GEE client.
    """
    averaged = _filter_average_convert_nass_rows(features, years=years)
    mapping: dict[str, dict[str, float]] = {}
    for (state_fips, commodity), yield_values in averaged.items():
        crop_name = NASS_TO_CROP_GROUP[commodity]
        for code in CROP_GROUPS[crop_name]:
            mapping[f'{state_fips}|{code}'] = yield_values
    return mapping


# ---------------------------------------------------------------------------
# Transitions table
# ---------------------------------------------------------------------------
# One per-county row per (epoch_transition, emissions_type) grain, per
# specs/pipeline_tech_design.md § Tables > transitions. Built from ten
# grouped reduceRegions calls (two per GLAD epoch pair for LUC +
# peatland_conversion, plus one for peatland_occupation). Each reducer
# emits one feature per county whose `groups` property carries
# (group_key, [area_ha, emissions_tco2]) tuples; a server-side `.map()`
# flattens those into per-(county, type) features tagged with the
# constant `epoch_transition` for that reducer. The ten flattened FCs
# `.merge()` server-side, a final `.map()` attaches `emissions_type` and
# `allocated_emissions_2020_tco2` (via the lazy `ee.Dictionary` lookups
# above), zero rows are dropped, and the result is exported directly via
# `Export.table.toAsset` — no `.getInfo()` round-trips during graph build.


def _pixel_area_ha_image() -> EEImage:
    """Per-pixel area in hectares, pinned to the GLAD grid.

    Matches the helper in transform/emissions.py so pixel areas used in the
    emissions raster and in these summary tables agree to the digit.
    """
    return (
        ee.Image.pixelArea()
        .divide(1e4)
        .reproject(crs=GLAD_CRS, crsTransform=GLAD_CRS_TRANSFORM)
        .rename('area_ha')
    )


# Composite-key encoding for the transitions reducer:
# fips × 100 + emissions_type_code packs (county, type) into a single int32.
# fips ∈ [1001, 56045]; emissions_type_code ∈ [1, 11]; composite ∈
# [100101, 5604511] — well within int32. Decoded in
# `_flatten_composite_groups_two_band` via integer divide / mod.
_TRANSITIONS_COMPOSITE_MULT: int = 100


def _reduce_grouped_two_band_with_fips(
    pixel_area_ha: EEImage,
    emissions: EEImage,
    group_key: EEImage,
    fips_band: EEImage,
    region_bbox: EEGeometry,
) -> Any:
    """Stack `(area_ha, emissions, composite_key)` and grouped-reduce server-side.

    Replaces the pre-Phase-11 ``reduceRegions(collection=counties)`` shape
    with a single ``reduceRegion(geometry=region_bbox)`` whose group field
    is a composite key encoding both county-FIPS and emissions-type-code.
    Investigation doc § "Polygon-shape overhead was the driver" measured
    the polygon-vertex-per-tile cost the reduceRegions(counties) pattern
    paid as ~89% of pre-Phase-11 transform cost; this rewrite eliminates
    it by reading the per-county dimension off a raster band rather than
    iterating polygon features.

    Returns the raw server-side ``ee.List`` of group dicts (each
    ``{composite_key, sum: [area_ha, emissions_tco2]}``). The caller
    flattens with ``_flatten_composite_groups_two_band``.

    `tileScale=4` keeps per-tile memory hedged.
    """
    mask = group_key.gt(0).And(fips_band.gt(0))
    composite_key = (
        fips_band.multiply(_TRANSITIONS_COMPOSITE_MULT).add(group_key).updateMask(mask)
    )
    image = (
        pixel_area_ha.updateMask(mask)
        .rename('area_ha')
        .addBands(emissions.updateMask(mask).rename('emis'))
        .addBands(composite_key.rename('composite_key'))
    )
    reducer = ee.Reducer.sum().repeat(2).group(groupField=2, groupName='composite_key')
    result = image.reduceRegion(
        reducer=reducer,
        geometry=region_bbox,
        crs=GLAD_CRS,
        crsTransform=GLAD_CRS_TRANSFORM,
        maxPixels=int(1e13),
        tileScale=4,
    )
    return ee.List(result.get('groups'))


def _flatten_composite_groups_two_band(
    groups: Any,
    epoch_label: str,
) -> EEFeatureCollection:
    """Flatten composite-key group dicts into per-(county, type) features.

    `groups` is the server-side ee.List returned by
    `_reduce_grouped_two_band_with_fips`. Each entry is
    `{composite_key, sum: [area_ha, emissions_tco2]}` where
    ``composite_key = fips × 100 + emissions_type_code``. Output features
    decode the composite back to the canonical (5-digit zero-padded
    `county_fips` string, `emissions_type_code` int) pair the
    pre-Phase-11 schema produced.

    Pure server-side: no `.getInfo()`. Output features carry
    placeholder Point(0, 0) geometry — `Export.table.toAsset` rejects
    null-geometry features, and the summary tables are tabular so any
    geometry would do.
    """
    epoch_label_ee = ee.String(epoch_label)

    def _emit(g: Any) -> Any:
        d = ee.Dictionary(g)
        composite = ee.Number(d.get('composite_key'))
        fips = composite.divide(_TRANSITIONS_COMPOSITE_MULT).floor().toInt()
        emissions_type_code = composite.mod(_TRANSITIONS_COMPOSITE_MULT).toInt()
        sums = ee.List(d.get('sum'))
        return ee.Feature(
            ee.Geometry.Point(0, 0),
            {
                # Match the pre-Phase-11 5-digit zero-padded string schema
                # so downstream BigQuery consumers see no
                # type drift in the county_fips column.
                'county_fips': fips.format('%05d'),
                'epoch_transition': epoch_label_ee,
                'emissions_type_code': emissions_type_code,
                'total_area_ha': ee.Number(sums.get(0)),
                'total_emissions_tco2': ee.Number(sums.get(1)),
            },
        )

    return ee.FeatureCollection(groups.map(_emit))


def _decorate_transitions_row(feature: Any) -> Any:
    """Attach `emissions_type` (name) and `allocated_emissions_2020_tco2`.

    Server-side counterpart of the pre-Phase-8 client-side pandas
    decoration. `peatland_occupation` rows get full-weight allocation
    (annual emission, no GHGP lookback discount); LUC + peatland_conversion
    rows get the GHGP per-epoch weight via the `_ghgp_weight_dict` lookup.
    """
    code = ee.Number(feature.get('emissions_type_code'))
    epoch = ee.String(feature.get('epoch_transition'))
    total = ee.Number(feature.get('total_emissions_tco2'))
    weight = ee.Algorithms.If(
        code.eq(_PEATLAND_OCCUPATION_CODE),
        ee.Number(1.0),
        ee.Number(_ghgp_weight_dict().get(epoch, 0)),
    )
    return feature.set(
        {
            # `_dict_get_or_null` covers code=0 sentinel rows emitted by the
            # group_key=0 multiply-mask zero-out — they're filtered downstream
            # but the .map() must not raise on them first.
            'emissions_type': _dict_get_or_null(
                _emissions_type_name_dict(), code.format('%d')
            ),
            'allocated_emissions_2020_tco2': total.multiply(ee.Number(weight)),
        }
    )


def build_transitions_table(
    land_use_asset: EEImage,
    emissions_asset: EEImage,
    fips_band: EEImage,
    region_bbox: EEGeometry,
) -> EEFeatureCollection:
    """Build the `transitions` summary FeatureCollection server-side.

    Ten composite-key grouped reducers (two per GLAD epoch pair for LUC +
    peatland_conversion, plus one for peatland_occupation) each emit a
    server-side ``ee.List`` of group dicts keyed on
    ``fips × 100 + emissions_type_code``. Each list is flattened to
    per-(county, type) features tagged with the constant `epoch_transition`
    for that reducer; `.merge()` chains the ten flattened FCs;
    `.map(_decorate_transitions_row)` attaches the `emissions_type` name
    and `allocated_emissions_2020_tco2` weight; zero rows are filtered;
    `.select(...)` projects to the canonical column set. No `.getInfo()`
    round-trips during graph build.

    ``fips_band`` is the run-scoped FIPS-valued image (county FIPS where
    the pixel is in one of the run's CONUS counties, masked elsewhere) —
    it carries both the per-county grouping dimension and the implicit
    polygon-shape mask. ``region_bbox`` is the bounding rectangle of the
    run's region; the reducer iterates pixels inside it. The bbox+mask
    pattern avoids the polygon-vertex-per-tile cost of
    ``reduceRegions(collection=counties)``.
    """
    pixel_area = _pixel_area_ha_image()
    is_peatland = land_use_asset.select('is_peatland')
    crops_2020 = land_use_asset.select('crops_2020')

    flattened: list[EEFeatureCollection] = []

    # LUC + peatland_conversion: two reducers per epoch.
    for from_y, to_y in GLAD_EPOCH_PAIRS:
        epoch_label = f'{from_y}_{to_y}'
        encoded = land_use_asset.select(transitions_band(from_y, to_y))

        luc_group_key = encoded.remap(
            EMISSIVE_ENCODED_CODES, _LUC_CODES, defaultValue=0
        )
        flattened.append(
            _flatten_composite_groups_two_band(
                _reduce_grouped_two_band_with_fips(
                    pixel_area_ha=pixel_area,
                    emissions=emissions_asset.select(luc_emissions_band(from_y, to_y)),
                    group_key=luc_group_key,
                    fips_band=fips_band,
                    region_bbox=region_bbox,
                ),
                epoch_label=epoch_label,
            )
        )

        pc_group_key = encoded.remap(
            PEATLAND_DRAINAGE_ENCODED_CODES,
            [_PEATLAND_CONVERSION_CODE] * len(PEATLAND_DRAINAGE_ENCODED_CODES),
            defaultValue=0,
        ).multiply(is_peatland)
        flattened.append(
            _flatten_composite_groups_two_band(
                _reduce_grouped_two_band_with_fips(
                    pixel_area_ha=pixel_area,
                    emissions=emissions_asset.select(
                        peatland_conversion_band(from_y, to_y)
                    ),
                    group_key=pc_group_key,
                    fips_band=fips_band,
                    region_bbox=region_bbox,
                ),
                epoch_label=epoch_label,
            )
        )

    # Peatland_occupation: single reducer, constant code on peatland × cropland.
    po_group_key = is_peatland.multiply(crops_2020.gt(0)).multiply(
        _PEATLAND_OCCUPATION_CODE
    )
    flattened.append(
        _flatten_composite_groups_two_band(
            _reduce_grouped_two_band_with_fips(
                pixel_area_ha=pixel_area,
                emissions=emissions_asset.select('peatland_occupation_2020'),
                group_key=po_group_key,
                fips_band=fips_band,
                region_bbox=region_bbox,
            ),
            epoch_label=PEATLAND_OCCUPATION_EPOCH_LABEL,
        )
    )

    # Merge all 10 flattened FCs server-side.
    merged = flattened[0]
    for fc in flattened[1:]:
        merged = merged.merge(fc)

    # Decorate, then drop both zero-contribution rows and group_key=0
    # sentinel rows (the latter are emitted by the .multiply(mask) zero-out
    # in _reduce_grouped_two_band; they always carry area=emis=0 anyway,
    # but the explicit code>0 filter keeps the schema unambiguous).
    decorated = merged.map(_decorate_transitions_row)
    nonzero = decorated.filter(
        ee.Filter.Or(
            ee.Filter.gt('total_area_ha', 0),
            ee.Filter.gt('total_emissions_tco2', 0),
        )
    ).filter(ee.Filter.gt('emissions_type_code', 0))

    # Project to canonical column set; emissions_type_code is internal.
    return nonzero.select(propertySelectors=list(TRANSITIONS_TABLE_COLUMNS))


# ---------------------------------------------------------------------------
# Crops table
# ---------------------------------------------------------------------------
# One per-county row per crop_code grain. Built from a single composite-
# key grouped `reduceRegion` over a 10-band metric stack + a composite-
# key band — each metric band is a per-pixel contribution to one output
# column (area_total, area_peatland, allocated_<driver>,
# allocated_<epoch>) by construction, so the reducer sums plus a server-
# side flatten produce per-(county, crop) totals + the four
# `pct_<driver>` and four `pct_epoch_*` shares without any cross-row
# pivot. NASS yield join via `ee.Dictionary.get(key, None)`; null-safe
# `pct_*` and `emissions_factor_kgco2e_per_kg` via `ee.Algorithms.If`
# fallbacks (`divide(0)` would otherwise serialize as integer 0, not null).

# Composite-key encoding for the crops reducer:
# fips × 1000 + crop_code packs (county, crop) into a single int32. CDL
# crop codes go up to 254, so 3 digits are required (vs the transitions
# table's 2). Max composite = 56045 × 1000 + 254 = 56,045,254 — well
# within int32. Decoded in `_emit_crop_row` via integer divide / mod.
_CROPS_COMPOSITE_MULT: int = 1000


def _build_crops_metric_stack(
    land_use_asset: EEImage,
    emissions_asset: EEImage,
    fips_band: EEImage,
) -> EEImage:
    """Build the 11-band image consumed by the single crops-table reducer.

    Bands 0..9 are the per-pixel contributions to the 10 output metrics
    (canonical order in `_METRIC_BAND_NAMES`); band 10 is the composite
    key (fips × 1000 + crop_code), the grouped reducer's groupField.
    Every metric band is `0` on non-row-crop pixels by construction (the
    `is_row_crop` factor), and all sums are computed server-side at
    `Σ_epoch (epoch_weight × source_band × driver_mask) × is_row_crop`
    so the per-`(county, crop)` totals fall out of one grouped reducer
    pass.

    `forest_to_short_veg` (the only emissive LUC pair whose destination
    isn't cropland or built_up) is automatically excluded — `is_row_crop`
    is 0 on its destination pixels because `crops_2020` is masked to GLAD
    cropland in `_load_crops_2020`. No explicit guard needed.

    ``fips_band`` is the run-scoped FIPS-valued image used to compute
    the composite key. Pixels outside any of the run's counties have
    ``fips_band`` masked, so the composite key is masked too and the
    reducer skips them.
    """
    # `.unmask(0)` everywhere a band derived from the cached land_use or
    # emissions asset gets multiplied into the metric stack. Without it the
    # grouped reducer's per-band sampling drifts when two of the multiplied
    # bands have inherited inconsistent masks (e.g. is_peatland inherits the
    # GFW Peatlands source's mask, transitions / crops_2020 don't), which silently
    # under-counts on some county polygons.
    pixel_area = _pixel_area_ha_image()
    is_peatland = land_use_asset.select('is_peatland').unmask(0)
    crops_2020 = land_use_asset.select('crops_2020').unmask(0)
    is_row_crop = crops_2020.remap(
        ALL_ROW_CROP_CODES, [1] * len(ALL_ROW_CROP_CODES), defaultValue=0
    ).unmask(0)

    # Per-pixel crop_code on row-crop pixels (0 elsewhere). Combined
    # with fips_band into a composite key by the caller; not exported as
    # its own band of the metric stack.
    crop_code = crops_2020.multiply(is_row_crop)

    # Area metrics.
    area_total = pixel_area.multiply(is_row_crop).rename('area_total')
    area_peatland = (
        pixel_area.multiply(is_row_crop).multiply(is_peatland).rename('area_peatland')
    )

    # Per-epoch contribution lists (avoids the .add() accumulator pattern;
    # empirically produces consistent per-(county, crop) sums across
    # mathematically-equivalent driver vs. epoch decompositions, where the
    # accumulator pattern silently dropped contributions on some county
    # polygons).
    forest_contribs: list[Any] = []
    short_veg_contribs: list[Any] = []
    pc_contribs: list[Any] = []
    epoch_contribs: dict[int, list[Any]] = {2005: [], 2010: [], 2015: [], 2020: []}

    # All per-pixel contributions are renamed to `'v'` so `ImageCollection.sum()`
    # sees a homogeneous collection.
    def _v(image: Any) -> Any:
        return image.rename('v')

    for from_y, to_y in GLAD_EPOCH_PAIRS:
        weight = GHGP_EPOCH_WEIGHTS_2020[(from_y, to_y)]
        encoded = land_use_asset.select(transitions_band(from_y, to_y)).unmask(0)
        luc_emis = emissions_asset.select(luc_emissions_band(from_y, to_y)).unmask(0)
        pc_emis = emissions_asset.select(peatland_conversion_band(from_y, to_y)).unmask(
            0
        )

        is_forest_pair = (
            encoded.remap(
                _FOREST_ENCODED_CODES, [1] * len(_FOREST_ENCODED_CODES), defaultValue=0
            )
        ).unmask(0)
        is_short_veg_pair = (
            encoded.remap(
                _SHORT_VEG_ENCODED_CODES,
                [1] * len(_SHORT_VEG_ENCODED_CODES),
                defaultValue=0,
            )
        ).unmask(0)

        # Per-driver contributions, gated to row-crop. peatland_conversion
        # bands are pre-masked to is_peatland in transform/emissions.py; no
        # need to re-apply that mask here.
        forest_contribs.append(
            _v(luc_emis.multiply(weight).multiply(is_forest_pair).multiply(is_row_crop))
        )
        short_veg_contribs.append(
            _v(
                luc_emis.multiply(weight)
                .multiply(is_short_veg_pair)
                .multiply(is_row_crop)
            )
        )
        pc_contribs.append(_v(pc_emis.multiply(weight).multiply(is_row_crop)))

        # Per-epoch bucket: append LUC and PC contributions separately so the
        # grouped reducer sees the same single-source-band multiplication chain
        # it sees for the per-driver bands. Combining them via `.add()` and a
        # single mask multiply silently undercounts on the grouped-reducer
        # path (verified empirically: direct `reduceRegion` on the combined
        # image gives the correct sum, but the grouped reducer misses ~30% of
        # contributions on some county polygons). `ImageCollection.sum()`
        # collapses both contributions per pixel before grouping.
        epoch_contribs[to_y].append(_v(luc_emis.multiply(weight).multiply(is_row_crop)))
        epoch_contribs[to_y].append(_v(pc_emis.multiply(weight).multiply(is_row_crop)))

    # peatland_occupation: full weight (1.0). Already gated on is_peatland
    # in the source band; narrow further to is_row_crop here. Also goes
    # into the 2020 epoch bucket per metric definition.
    occupation = emissions_asset.select('peatland_occupation_2020').unmask(0)
    allocated_po = occupation.multiply(is_row_crop)
    epoch_contribs[2020].append(_v(allocated_po))

    allocated_forest = ee.ImageCollection(forest_contribs).sum()
    allocated_short_veg = ee.ImageCollection(short_veg_contribs).sum()
    allocated_pc = ee.ImageCollection(pc_contribs).sum()
    allocated_2005 = ee.ImageCollection(epoch_contribs[2005]).sum()
    allocated_2010 = ee.ImageCollection(epoch_contribs[2010]).sum()
    allocated_2015 = ee.ImageCollection(epoch_contribs[2015]).sum()
    allocated_2020 = ee.ImageCollection(epoch_contribs[2020]).sum()

    # Composite key: (fips × 1000 + crop_code), masked outside the run's
    # counties (fips_band's mask) AND off non-row-crop pixels. The reducer
    # skips masked pixels, so non-row-crop / out-of-region pixels never
    # appear in any output group. fips_band is unmasked (`.unmask(0)`) for
    # the multiply so we don't drop pixels where fips_band's mask differs
    # from crops_2020's mask; the final mask is the explicit `valid` band.
    valid = fips_band.gt(0).And(is_row_crop.eq(1))
    composite_key = (
        fips_band.unmask(0)
        .multiply(_CROPS_COMPOSITE_MULT)
        .add(crop_code)
        .updateMask(valid)
        .rename('composite_key')
    )

    stack = (
        area_total.addBands(area_peatland)
        .addBands(allocated_forest.rename('allocated_forest'))
        .addBands(allocated_short_veg.rename('allocated_short_veg'))
        .addBands(allocated_pc.rename('allocated_peatland_conversion'))
        .addBands(allocated_po.rename('allocated_peatland_occupation'))
        .addBands(allocated_2005.rename('allocated_2005'))
        .addBands(allocated_2010.rename('allocated_2010'))
        .addBands(allocated_2015.rename('allocated_2015'))
        .addBands(allocated_2020.rename('allocated_2020'))
        .addBands(composite_key)
    )
    # `.unmask(0)` on the metric bands neutralizes per-band mask drift
    # (peatland-derived bands inherit the GFW Peatlands source's mask,
    # others don't); the composite_key band keeps its mask so the
    # reducer skips out-of-region / non-row-crop pixels.
    return stack.unmask(0).updateMask(stack.select('composite_key').mask())


def _emit_crop_row(group_dict: Any) -> Any:
    """Server-side `ee.Feature` emission for one (county, crop) row.

    Reads the 10 reducer-sum values out of the group's `sum` list (canonical
    order per `_METRIC_BAND_NAMES`), decodes the composite key into
    (county_fips, crop_code), looks up the crop_group + state-level
    NASS yields via `ee.Dictionary`, and computes total_production_*,
    emissions_factor, and the eight `pct_*` columns. All divisions go
    through the `If(denom > 0, ratio, None)` fallback because GEE coerces
    `divide(0)` to integer 0 in the asset rather than null (see DD6).

    The group key is the ``fips × 1000 + crop_code`` composite produced
    by `_build_crops_metric_stack`; this function decodes it via integer
    divide / mod. ``county_fips`` is emitted as a 5-digit zero-padded
    string; ``state_fips`` is derived from its first two digits.
    """
    g = ee.Dictionary(group_dict)
    composite = ee.Number(g.get('composite_key'))
    fips_int = composite.divide(_CROPS_COMPOSITE_MULT).floor().toInt()
    county_fips = fips_int.format('%05d')
    state_fips = ee.String(county_fips).slice(0, 2)
    crop_code = composite.mod(_CROPS_COMPOSITE_MULT).toInt()
    sums = ee.List(g.get('sum'))

    area_total = ee.Number(sums.get(0))
    area_peatland = ee.Number(sums.get(1))
    alloc_forest = ee.Number(sums.get(2))
    alloc_short_veg = ee.Number(sums.get(3))
    alloc_pc = ee.Number(sums.get(4))
    alloc_po = ee.Number(sums.get(5))
    alloc_2005 = ee.Number(sums.get(6))
    alloc_2010 = ee.Number(sums.get(7))
    alloc_2015 = ee.Number(sums.get(8))
    alloc_2020 = ee.Number(sums.get(9))

    total_allocated = alloc_forest.add(alloc_short_veg).add(alloc_pc).add(alloc_po)

    crop_code_str = crop_code.format('%d')
    yield_key = ee.String(state_fips).cat('|').cat(crop_code_str)
    yield_bu = _dict_get_or_null(_yield_bu_dict(), yield_key)
    yield_kg = _dict_get_or_null(_yield_kg_dict(), yield_key)

    # Production: null when state-level NASS yield is missing. (yield_bu /
    # yield_kg are server-side numeric-or-null; If treats null as falsy and
    # picks the else branch.)
    production_kg = ee.Algorithms.If(
        yield_kg, area_total.multiply(ee.Number(yield_kg)), None
    )
    production_bu = ee.Algorithms.If(
        yield_bu,
        area_total.multiply(ee.Number(yield_bu)).divide(HA_PER_ACRE),
        None,
    )

    # EF: null when production is null OR zero. Area > 0 is filtered at the
    # FC level, so production = 0 only happens when yield is null
    # (handled by the outer If).
    ef = ee.Algorithms.If(
        production_kg,
        total_allocated.multiply(1000).divide(ee.Number(production_kg)),
        None,
    )

    def _safe_share(numer: Any) -> Any:
        return ee.Algorithms.If(
            total_allocated.gt(0), numer.divide(total_allocated), None
        )

    return ee.Feature(
        ee.Geometry.Point(0, 0),
        {
            'county_fips': county_fips,
            'crop_code': crop_code,
            # Default `None` covers crop_code=0 (non-row-crop sentinel from the
            # group_key=0 reducer output) — filtered downstream by
            # `Filter.gt('total_crop_area_ha', 0)` and `Filter.neq('crop_group', None)`.
            'crop_group': _dict_get_or_null(_crop_group_dict(), crop_code_str),
            'total_crop_area_ha': area_total,
            'peatland_crop_area_ha': area_peatland,
            'yield_bu_per_acre': yield_bu,
            'yield_kg_per_ha': yield_kg,
            'total_production_kg': production_kg,
            'total_production_bu': production_bu,
            'total_allocated_emissions_tco2': total_allocated,
            'emissions_factor_kgco2e_per_kg': ef,
            'pct_forest': _safe_share(alloc_forest),
            'pct_short_veg': _safe_share(alloc_short_veg),
            'pct_peatland_conversion': _safe_share(alloc_pc),
            'pct_peatland_occupation': _safe_share(alloc_po),
            'pct_epoch_2005': _safe_share(alloc_2005),
            'pct_epoch_2010': _safe_share(alloc_2010),
            'pct_epoch_2015': _safe_share(alloc_2015),
            'pct_epoch_2020': _safe_share(alloc_2020),
        },
    )


def build_crops_table(
    land_use_asset: EEImage,
    emissions_asset: EEImage,
    fips_band: EEImage,
    region_bbox: EEGeometry,
) -> EEFeatureCollection:
    """Build the `crops` summary FeatureCollection server-side.

    Single composite-key grouped ``reduceRegion`` over a 10-band metric
    stack + a composite-key band built by `_build_crops_metric_stack`.
    The composite key is ``fips × 1000 + crop_code``; the reducer skips
    pixels where the key is masked (out-of-region or non-row-crop).
    Server-side flatten via `_emit_crop_row` decodes each composite back
    to (county_fips string, crop_code int) and emits per-(county, crop)
    features with every derived column attached in one ``.map()`` pass.
    Final filters drop zero-area rows. No ``.getInfo()`` round-trips
    during graph construction.

    The bbox+mask shape (single ``reduceRegion`` with the composite key
    as groupField) avoids the polygon-vertex-per-tile cost of
    ``reduceRegions(collection=counties)``.
    """
    stack = _build_crops_metric_stack(
        land_use_asset=land_use_asset,
        emissions_asset=emissions_asset,
        fips_band=fips_band,
    )
    reducer = (
        ee.Reducer.sum().repeat(10).group(groupField=10, groupName='composite_key')
    )
    reduced = stack.reduceRegion(
        reducer=reducer,
        geometry=region_bbox,
        crs=GLAD_CRS,
        crsTransform=GLAD_CRS_TRANSFORM,
        maxPixels=int(1e13),
        tileScale=4,
    )
    groups = ee.List(reduced.get('groups'))

    fc = ee.FeatureCollection(groups.map(_emit_crop_row))

    return (
        fc.filter(ee.Filter.gt('total_crop_area_ha', 0))
        .filter(ee.Filter.neq('crop_group', None))
        .select(propertySelectors=list(CROPS_TABLE_COLUMNS))
    )


# ---------------------------------------------------------------------------
# Region-level orchestration (compute_region_tables + helpers)
# ---------------------------------------------------------------------------
# Entry point for run_transform's third stage. Takes the already-resolved
# `counties` FeatureCollection (caller-side: `transform.py` resolves it
# once for the whole region), runs both table builders against the cached
# land_use and emissions assets, kicks off both exports in parallel so
# run_transform waits on them with max(t_transitions, t_crops) latency
# rather than sum, and returns the asset IDs plus task handles.


def export_table_asset(
    fc: EEFeatureCollection,
    asset_id: str,
    force: bool = False,
) -> ee.batch.Task | None:
    """Export a FeatureCollection as a GEE table asset.

    Mirrors `transform.land_use.export_land_use_asset` /
    `transform.emissions.export_emissions_asset` for tables: cache probe
    via `ee.data.getAsset`, optional force-delete, then
    `Export.table.toAsset`. No CRS/transform — those are raster-only.
    Returns the task handle for polling, or `None` if the asset already
    exists and `force` is False.
    """
    if not force:
        try:
            ee.data.getAsset(asset_id)
            logger.info(f'table asset cached, skipping export: {asset_id}')
            return None
        except ee.EEException as e:
            if 'does not exist' not in str(e):
                raise
    else:
        delete_asset_safely(asset_id)

    task = ee.batch.Export.table.toAsset(
        collection=fc,
        assetId=asset_id,
        description=asset_id.rsplit('/', 1)[-1],
    )
    task.start()
    logger.info(f'Started table export: {asset_id}')
    return task


def compute_region_tables(
    land_use_asset_id: str,
    emissions_asset_id: str,
    region: str,
    version: str,
    fips_band: EEImage,
    region_bbox: EEGeometry,
    force: bool = False,
) -> dict[str, Any]:
    """Build and export both summary tables for the requested region.

    Returns a dict with `transitions_asset_id`, `crops_asset_id`,
    `transitions_task`, and `crops_task`. Either task is `None` on cache
    hit. Both exports are kicked off before either is waited on, so
    `run_transform` can wait in parallel: total wall-clock =
    max(transitions_task, crops_task), not sum.

    Cache-checks both target asset IDs **before** constructing either
    FeatureCollection graph. The build_*_table calls trigger eager
    server-side `value:compute` requests (NASS yield join, county
    reductions) that take 10s of minutes per region even when the
    output asset is already cached. Probing existence first turns a
    fully-cached region into a near-instant skip.

    Both table builders consume ``fips_band`` as the per-county
    grouping key (composite-keyed in a single ``reduceRegion`` over
    ``region_bbox``) rather than iterating a vector FeatureCollection
    per tile.
    """
    transitions_asset_id = output_asset_id('transitions', region, version)
    crops_asset_id = output_asset_id('crops', region, version)

    transitions_cached = (not force) and asset_exists(transitions_asset_id)
    crops_cached = (not force) and asset_exists(crops_asset_id)

    if transitions_cached and crops_cached:
        logger.info(
            f'region {region!r}: transitions + crops both cached, '
            f'skipping graph construction'
        )
        return {
            'transitions_asset_id': transitions_asset_id,
            'crops_asset_id': crops_asset_id,
            'transitions_task': None,
            'crops_task': None,
        }

    land_use_asset = ee.Image(land_use_asset_id)
    emissions_asset = ee.Image(emissions_asset_id)

    if transitions_cached:
        logger.info(f'table asset cached, skipping export: {transitions_asset_id}')
        transitions_task = None
    else:
        logger.info(f'Building transitions table for region {region!r}')
        transitions_fc = build_transitions_table(
            land_use_asset=land_use_asset,
            emissions_asset=emissions_asset,
            fips_band=fips_band,
            region_bbox=region_bbox,
        )
        transitions_task = export_table_asset(
            fc=transitions_fc,
            asset_id=transitions_asset_id,
            force=force,
        )

    if crops_cached:
        logger.info(f'table asset cached, skipping export: {crops_asset_id}')
        crops_task = None
    else:
        logger.info(f'Building crops table for region {region!r}')
        crops_fc = build_crops_table(
            land_use_asset=land_use_asset,
            emissions_asset=emissions_asset,
            fips_band=fips_band,
            region_bbox=region_bbox,
        )
        crops_task = export_table_asset(
            fc=crops_fc,
            asset_id=crops_asset_id,
            force=force,
        )

    return {
        'transitions_asset_id': transitions_asset_id,
        'crops_asset_id': crops_asset_id,
        'transitions_task': transitions_task,
        'crops_task': crops_task,
    }
