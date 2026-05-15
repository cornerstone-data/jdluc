"""Cross-module constants for the jdLUC pipeline.

Central home for expected versions for input datasets, simplified land use category codes,
IPCC emissions factor tables, crop group definitions, and so on.

See specs/pipeline_tech_design.md § constants.py for details.
"""

from itertools import product
from typing import Literal, NamedTuple

from jdluc.utils.transitions import encode_transition

# ===========================================================================
# DEPLOYMENT CONFIGURATION — edit these six values to point jdLUC at your GCP
# ===========================================================================
# Every GCP / GCS / BQ / GEE identifier the pipeline, and scripts reach for
# is defined here. To deploy against your GCP, edit the values below directly.
# Every consumer imports from ``utils.constants``, so there are no other call
# sites to chase.

# GCP project for application-default credentials, GEE init, and BQ clients.
GCP_PROJECT: str = 'cornerstone-data'

# BigQuery destination for the published `transitions` and `crops` tables.
# ``BQ_JOB_LOCATION`` must match the dataset's region — ``ee.batch.Export.
# table.toBigQuery`` targets it explicitly.
BQ_PROJECT: str = GCP_PROJECT
BQ_DATASET: str = 'jdluc'
BQ_JOB_LOCATION: str = 'US'

# GCS bucket the extract stage uses to mirror non-native source-file fetches
# (Harris AGB, Huang BGB, NASS yields, IPCC climate zones, GFW peatlands).
GCS_BUCKET_NAME: str = 'cornerstone-luc'

# Parent GEE folder for every jdLUC-owned asset (extract outputs, transform
# outputs, publish-stage tabular exports).
GEE_ASSET_ROOT: str = f'projects/{BQ_PROJECT:s}/assets/cornerstone-luc'

# ===========================================================================
# END deployment configuration
# ===========================================================================


class LandUseCategory(NamedTuple):
    code: int
    glad_values: list[int]


# ---------------------------------------------------------------------------
# CONUS state FIPS <-> canonical region name
# ---------------------------------------------------------------------------
# Every per-state transform asset is named using the canonical state name
# (e.g. land_use_iowa_{version}) rather than the FIPS code, so the same
# per-state asset is reused across any region that includes that state.
# Names are snake_case lowercase matching TIGER's NAME
# field. Alaska (02) and Hawaii (15) are excluded — this methodology is
# CONUS-scoped.

STATE_FIPS_TO_NAME: dict[str, str] = {
    '01': 'alabama',
    '04': 'arizona',
    '05': 'arkansas',
    '06': 'california',
    '08': 'colorado',
    '09': 'connecticut',
    '10': 'delaware',
    '11': 'district_of_columbia',
    '12': 'florida',
    '13': 'georgia',
    '16': 'idaho',
    '17': 'illinois',
    '18': 'indiana',
    '19': 'iowa',
    '20': 'kansas',
    '21': 'kentucky',
    '22': 'louisiana',
    '23': 'maine',
    '24': 'maryland',
    '25': 'massachusetts',
    '26': 'michigan',
    '27': 'minnesota',
    '28': 'mississippi',
    '29': 'missouri',
    '30': 'montana',
    '31': 'nebraska',
    '32': 'nevada',
    '33': 'new_hampshire',
    '34': 'new_jersey',
    '35': 'new_mexico',
    '36': 'new_york',
    '37': 'north_carolina',
    '38': 'north_dakota',
    '39': 'ohio',
    '40': 'oklahoma',
    '41': 'oregon',
    '42': 'pennsylvania',
    '44': 'rhode_island',
    '45': 'south_carolina',
    '46': 'south_dakota',
    '47': 'tennessee',
    '48': 'texas',
    '49': 'utah',
    '50': 'vermont',
    '51': 'virginia',
    '53': 'washington',
    '54': 'west_virginia',
    '55': 'wisconsin',
    '56': 'wyoming',
}
STATE_NAME_TO_FIPS: dict[str, str] = {
    name: fips for fips, name in STATE_FIPS_TO_NAME.items()
}
CONUS_STATE_FIPS: list[str] = sorted(STATE_FIPS_TO_NAME)


# ---------------------------------------------------------------------------
# GEE asset paths
# ---------------------------------------------------------------------------

# Root folder for all jdLUC-owned GEE assets (extract, transform, publish).
# Per-asset IDs are built as ``f'{GEE_ASSET_ROOT}/{asset_name}'``.
# ``GEE_ASSET_ROOT`` itself is defined in the deployment configuration block
# at the top of this module.

# US state boundaries (TIGER/Census 2018).
GEE_TIGER_STATES = 'TIGER/2018/States'

# USDA Cropland Data Layer (annual). 30 m, native Albers grid.
GEE_CDL_COLLECTION = 'USDA/NASS/CDL'

# GLAD cropland binary, by-year (Potapov et al. 2022). Asset IDs are formed
# as f'{GEE_GLAD_CROPLAND_PREFIX}{year}' for the supported epochs (see
# GLAD_CROPLAND_EPOCHS). Used by analyses backing scripts that compare
# CDL and GLAD cropland directly.
GEE_GLAD_CROPLAND_PREFIX = 'users/potapovpeter/Global_cropland_'
GLAD_CROPLAND_EPOCHS: list[int] = [2003, 2007, 2011, 2015, 2019]

# Canonical asset-ID pattern for every transform/publish output raster and
# table: ``{GEE_ASSET_ROOT}/{name}_{region}_{version}``. See
# specs/pipeline_tech_design.md § Outputs.
OutputName = Literal['land_use', 'emissions', 'transitions', 'crops']


def output_asset_id(name: OutputName, region: str, version: str) -> str:
    """Canonical GEE asset ID for a transform/publish output."""
    return f'{GEE_ASSET_ROOT}/{name}_{region}_{version}'


# ---------------------------------------------------------------------------
# Canonical raster grid (GLAD GLCLUC v2 native)
# ---------------------------------------------------------------------------
# Every raster computation and export in the pipeline is pinned to this grid.
# See specs/pipeline_tech_design.md § Platform.
GLAD_CRS = 'EPSG:4326'
GLAD_CRS_TRANSFORM: list[float] = [0.00025, 0, -180, 0, -0.00025, 90]

# ---------------------------------------------------------------------------
# GLAD GLCLUC v2 epoch years
# ---------------------------------------------------------------------------
GLAD_EPOCH_YEARS: list[int] = [2000, 2005, 2010, 2015, 2020]
GLAD_EPOCH_PAIRS: list[tuple[int, int]] = [
    (GLAD_EPOCH_YEARS[i], GLAD_EPOCH_YEARS[i + 1])
    for i in range(len(GLAD_EPOCH_YEARS) - 1)
]

# ---------------------------------------------------------------------------
# Land use categories and GLAD GLC remap
# ---------------------------------------------------------------------------
# Each entry maps our category name to its integer code and the GLAD GLCLUC v2
# raw pixel values that map to it. 0 is reserved for "no transition" in the
# encoded transition bands; it is never a valid land use category.
# GLAD value ranges from specs/methodology.md § "Land cover transitions".

LAND_USE_CATEGORIES: dict[str, LandUseCategory] = {
    'forest': LandUseCategory(1, list(range(25, 49))),
    'wetland_forest': LandUseCategory(2, list(range(125, 149))),
    'short_vegetation': LandUseCategory(3, list(range(1, 25))),
    'wetland_short_vegetation': LandUseCategory(4, list(range(100, 125))),
    'cropland': LandUseCategory(5, [244]),
    'built_up': LandUseCategory(6, [250]),
    'water': LandUseCategory(7, list(range(200, 208))),
    'snow_ice': LandUseCategory(8, [241]),
    'bare': LandUseCategory(9, [0]),
}

# ---------------------------------------------------------------------------
# Extract-side: upstream-dataset versions, source URLs, pinned snapshots
# ---------------------------------------------------------------------------
# Expected versions for the four non-native datasets the extract stage
# ingests into GEE. Asset IDs are composed as
# ``{GEE_ASSET_ROOT}/{family}_{version}`` so a version bump forces a cache
# miss and re-extract.

EXPECTED_HARRIS_VERSION: str = 'v2021'  # Harris et al. (2021) publication year.
EXPECTED_HUANG_VERSION: str = 'v2021'  # Huang et al. (2021).
EXPECTED_IPCC_VERSION: str = 'v2006'  # Ogle et al. (2006).
EXPECTED_GFW_PEATLANDS_VERSION: str = 'v20230315'  # GFW Global Peatlands publication.
EXPECTED_TIGER_COUNTIES_VERSION: str = 'v2018'  # TIGER/2018/Counties vintage.

# Harris AGB tile URLs live on GFW's ArcGIS FeatureServer; `Mg_ha_1_download`
# is the per-feature signed URL. 21 tiles cover CONUS (rows 30N/40N/50N ×
# columns 070W–130W).
HARRIS_AGB_ARCGIS_FEATURESERVER: str = (
    'https://services2.arcgis.com/g8WusZB13b9OegfU/arcgis/rest/services/'
    'Aboveground_Live_Woody_Biomass_Density/FeatureServer/0/query'
)

# Huang BGB NetCDF lives at Figshare DOI 10.6084/m9.figshare.12199637.v1.
HUANG_BGB_FIGSHARE_URL: str = 'https://ndownloader.figshare.com/files/22432460'

# USDA NASS QuickStats crop archive. The URL is date-stamped per release; we
# pin to a specific snapshot so the extract version string is coupled to a
# concrete upstream file. Bumping the date requires adding an entry to
# ``_NASS_RELEASE_DATE_TO_VERSION`` below — module import fails otherwise.
NASS_QUICKSTATS_URL_TEMPLATE: str = (
    'https://www.nass.usda.gov/datasets/qs.crops_{date}.txt.gz'
)
NASS_QUICKSTATS_RELEASE_DATE: str = '20260508'

# Registered (release_date → version) mappings. Version strings encode the
# ``NASS_YIELD_YEARS`` window the transform averages over — keeping the tag
# window-aligned rather than release-aligned keeps the cache semantically
# meaningful even when NASS reissues the same year's data under a new
# release date.
_NASS_RELEASE_DATE_TO_VERSION: dict[str, str] = {
    '20260228': 'v2017_2020',
    '20260423': 'v2017_2020',
    '20260424': 'v2017_2020',
    '20260508': 'v2017_2020',
}


def _derive_nass_version_from_release_date(release_date: str) -> str:
    """Mechanical (release_date → version) mapping.

    Import fails if ``NASS_QUICKSTATS_RELEASE_DATE`` is bumped without
    registering the corresponding version. This prevents silent staleness
    where a new release reuses the old asset ID and bypasses the extract
    cache-miss check.
    """
    try:
        return _NASS_RELEASE_DATE_TO_VERSION[release_date]
    except KeyError as exc:
        raise ValueError(
            f'NASS release date {release_date!r} has no registered version. '
            f'Add an entry to _NASS_RELEASE_DATE_TO_VERSION in '
            f'utils/constants.py when bumping NASS_QUICKSTATS_RELEASE_DATE.'
        ) from exc


EXPECTED_NASS_VERSION: str = _derive_nass_version_from_release_date(
    NASS_QUICKSTATS_RELEASE_DATE
)

# Zenodo record 7303808 hosts the Ogle et al. (2006) IPCC climate zone
# raster as a single GeoTIFF. The extract module resolves the file
# download URL through Zenodo's record API — the record-level URL below
# is the stable identifier.
IPCC_CLIMATE_ZONES_ZENODO_URL: str = 'https://zenodo.org/api/records/7303808'

# GFW Global Peatlands raster composite (CC BY 4.0): Xu et al. 2018
# (PEATMAP) above 40°N + Gumbricht et al. 2017 below 40°N + regional
# overrides (Crezee 2022 Congo, Hastie 2022 lowland Peru, Miettinen
# 2016 Indonesia/Malaysia), all rasterized to 30 m to align with the
# Hansen Global Forest Change grid. Data is distributed as 10°×10°
# GeoTIFF tiles via GFW's data API; the URL template below yields a
# direct GeoTIFF download per ``tile_id``. ``pixel_meaning=is`` selects
# the binary peatland-presence band.
#
# The API key ``GFW_DATA_API_KEY`` ships embedded in any user's CSV
# download from https://data.globalforestwatch.org/datasets/gfw::global-peatlands —
# it's a public rate-limited token, not a secret. If the key 401s
# (analogous to NASS release-date rotation), re-download the CSV
# from the GFW Open Data Portal and bump this constant.
GFW_DATA_API_KEY: str = '2d60cd88-8348-4c0f-a6d5-bd9adb585a8c'
GFW_PEATLANDS_URL_TEMPLATE: str = (
    'https://data-api.globalforestwatch.org/dataset/gfw_peatlands/'
    'v20230315/download/geotiff?grid=10/40000&tile_id={tile_id}'
    '&pixel_meaning=is&x-api-key={api_key}'
)

# ---------------------------------------------------------------------------
# Transform-stage input asset IDs (jdLUC-owned extracts + GEE-native sources)
# ---------------------------------------------------------------------------
GEE_HARRIS_AGB: str = f'{GEE_ASSET_ROOT}/harris_agb_conus_{EXPECTED_HARRIS_VERSION}'
GEE_HUANG_BGB: str = f'{GEE_ASSET_ROOT}/huang_bgb_conus_{EXPECTED_HUANG_VERSION}'
GEE_SOILGRIDS_SOC: str = 'projects/soilgrids-isric/ocs_mean'
GEE_IPCC_CLIMATE_ZONES: str = (
    f'{GEE_ASSET_ROOT}/ipcc_climate_zones_{EXPECTED_IPCC_VERSION}'
)
GEE_GFW_PEATLANDS: str = (
    f'{GEE_ASSET_ROOT}/gfw_peatlands_{EXPECTED_GFW_PEATLANDS_VERSION}'
)
# CONUS county-FIPS label raster — single-band int32 (FIPS = state_fips × 1000
# + county_fips, range 1001–56045) painted from TIGER counties at GLAD 30m.
# Pixels outside any CONUS county are masked. This asset is the single
# source of geographic truth for the transform pipeline, doubling as
# polygon mask + per-county grouping band.
GEE_COUNTY_FIPS_LABEL: str = (
    f'{GEE_ASSET_ROOT}/county_fips_conus_{EXPECTED_TIGER_COUNTIES_VERSION}'
)

# ---------------------------------------------------------------------------
# Dataset inventory
# ---------------------------------------------------------------------------
# Non-native datasets (those ingested by the extract stage) additionally
# carry ``expected_version`` and ``source_url`` — the orchestrator reads
# both when deciding whether to re-extract. Native datasets omit them.
DATASET_INVENTORY: dict[str, dict[str, str]] = {
    'glad_glcluc_v2': {
        'gee_asset_id': 'projects/glad/GLCLU2020/v2/LCLUC_{year}',
        'resolution_hint': '0.00025deg (~30m)',
        'native_or_extracted': 'native',
    },
    'usda_cdl': {
        'gee_asset_id': GEE_CDL_COLLECTION,
        'resolution_hint': '30m (native Albers)',
        'native_or_extracted': 'native',
    },
    'gfw_peatlands': {
        'gee_asset_id': GEE_GFW_PEATLANDS,
        'resolution_hint': '0.00025deg (~30m), GLAD grid',
        'native_or_extracted': 'extracted',
        'expected_version': EXPECTED_GFW_PEATLANDS_VERSION,
        # source_url is the URL template (api-key elided); per-tile URLs
        # are formatted at extract time. Same posture as
        # HARRIS_AGB_ARCGIS_FEATURESERVER's role for harris_agb.
        'source_url': GFW_PEATLANDS_URL_TEMPLATE.replace('{api_key}', '<api-key>'),
    },
    'tiger_states': {
        'gee_asset_id': GEE_TIGER_STATES,
        'resolution_hint': 'vector',
        'native_or_extracted': 'native',
    },
    'county_fips': {
        'gee_asset_id': GEE_COUNTY_FIPS_LABEL,
        'resolution_hint': '0.00025deg (~30m), GLAD grid',
        'native_or_extracted': 'extracted',
        'expected_version': EXPECTED_TIGER_COUNTIES_VERSION,
        # Source is the GEE-native TIGER counties FeatureCollection;
        # source_url documents the TIGER 2018 vintage upstream.
        'source_url': ('https://www2.census.gov/geo/tiger/TIGER2018/COUNTY/'),
    },
    'harris_agb': {
        'gee_asset_id': GEE_HARRIS_AGB,
        'resolution_hint': '0.00025deg (~30m), GLAD grid',
        'native_or_extracted': 'extracted',
        'expected_version': EXPECTED_HARRIS_VERSION,
        'source_url': HARRIS_AGB_ARCGIS_FEATURESERVER,
    },
    'huang_bgb': {
        'gee_asset_id': GEE_HUANG_BGB,
        'resolution_hint': '0.0083deg (~1km)',
        'native_or_extracted': 'extracted',
        'expected_version': EXPECTED_HUANG_VERSION,
        'source_url': HUANG_BGB_FIGSHARE_URL,
    },
    'soilgrids_soc': {
        'gee_asset_id': GEE_SOILGRIDS_SOC,
        'resolution_hint': '250m (native Interrupted Goode Homolosine)',
        'native_or_extracted': 'native',
    },
    'ipcc_climate_zones': {
        'gee_asset_id': GEE_IPCC_CLIMATE_ZONES,
        'resolution_hint': '0.00025deg (~30m), GLAD grid (upsampled from 0.5deg)',
        'native_or_extracted': 'extracted',
        'expected_version': EXPECTED_IPCC_VERSION,
        'source_url': IPCC_CLIMATE_ZONES_ZENODO_URL,
    },
}

# Dataset-family keys the extract orchestrator iterates. Order reflects
# extract wall-clock cost (county_fips is smallest — server-side paint with no
# HTTP/GCS round-trip; harris is largest); the orchestrator is sequential and
# this keeps fast-failing mis-configurations surfacing first.
NON_NATIVE_DATASETS: list[str] = [
    'county_fips',
    'ipcc_climate_zones',
    'gfw_peatlands',
    'huang_bgb',
    'nass_yields',
    'harris_agb',
]

# ---------------------------------------------------------------------------
# Emissive LUC transition vocabulary
# ---------------------------------------------------------------------------
# The 9-pair emissive LUC vocabulary drives the transform-stage vegetation +
# SOC LUC emission paths and the `transitions` summary table. The vocabulary is
# defined as (short_name, from_category_key, to_category_key) tuples so that
# downstream derivations — EMISSIONS_TYPE_NAMES, EMISSIVE_LUC_PAIRS,
# EMISSIVE_ENCODED_CODES, PEATLAND_DRAINAGE_PAIRS — are all keyed off a single
# source of truth. Category short names in EMISSIONS_TYPE_NAMES follow the
# methodology spec (`short_veg`, not `short_vegetation`).
# See specs/methodology.md § Land cover transitions and
# specs/pipeline_tech_design.md § Rasters.

_EMISSIVE_TRANSITION_CATEGORIES: list[tuple[str, str, str]] = [
    ('forest_to_cropland', 'forest', 'cropland'),
    ('forest_to_built_up', 'forest', 'built_up'),
    ('forest_to_short_veg', 'forest', 'short_vegetation'),
    ('wetland_forest_to_cropland', 'wetland_forest', 'cropland'),
    ('wetland_forest_to_built_up', 'wetland_forest', 'built_up'),
    ('short_veg_to_cropland', 'short_vegetation', 'cropland'),
    ('short_veg_to_built_up', 'short_vegetation', 'built_up'),
    ('wetland_short_veg_to_cropland', 'wetland_short_vegetation', 'cropland'),
    ('wetland_short_veg_to_built_up', 'wetland_short_vegetation', 'built_up'),
]

EMISSIVE_LUC_PAIRS: list[tuple[int, int]] = [
    (LAND_USE_CATEGORIES[f].code, LAND_USE_CATEGORIES[t].code)
    for (_, f, t) in _EMISSIVE_TRANSITION_CATEGORIES
]
EMISSIVE_ENCODED_CODES: list[int] = [
    encode_transition(f, t) for (f, t) in EMISSIVE_LUC_PAIRS
]

# Peatland conversion emissions are driven by drainage, which is imposed by
# cropland/built_up destinations — not by forest→short_veg transitions alone.
_PEATLAND_DRAINAGE_DEST_CODES: frozenset[int] = frozenset(
    {LAND_USE_CATEGORIES['cropland'].code, LAND_USE_CATEGORIES['built_up'].code}
)
PEATLAND_DRAINAGE_PAIRS: list[tuple[int, int]] = [
    (f, t) for (f, t) in EMISSIVE_LUC_PAIRS if t in _PEATLAND_DRAINAGE_DEST_CODES
]
PEATLAND_DRAINAGE_ENCODED_CODES: list[int] = [
    encode_transition(f, t) for (f, t) in PEATLAND_DRAINAGE_PAIRS
]

# Full 11-entry vocabulary: 9 emissive LUC pairs + 2 peatland band categories.
# transform/summary_tables.py reduces emissions bands keyed off these names.
EMISSIONS_TYPE_NAMES: list[str] = [
    name for (name, _, _) in _EMISSIVE_TRANSITION_CATEGORIES
] + ['peatland_conversion', 'peatland_occupation']

# ---------------------------------------------------------------------------
# IPCC climate zones (10-zone canonical vocabulary)
# ---------------------------------------------------------------------------
# Codes 1–10 follow the ordering of the Houghton/BLUE grassland vegetation-C
# table in specs/methodology.md § Grassland and shrubland. The Ogle et al.
# Zenodo raster (DOI 10.5281/zenodo.7303808) uses a different integer code
# assignment; IPCC_CLIMATE_ZONE_NATIVE_REMAP rewrites native codes into the
# canonical ones at read time (in transform/emissions.py::_load_ipcc_climate).
# Polar zones (Ogle native 11, 12) fall through to 0 (out-of-vocabulary;
# methodology's Houghton table has no polar row, and CONUS has no polar
# pixels).

IPCC_CLIMATE_ZONES: dict[str, int] = {
    'tropical_wet': 1,
    'tropical_moist': 2,
    'tropical_dry': 3,
    'tropical_montane': 4,
    'warm_temperate_moist': 5,
    'warm_temperate_dry': 6,
    'cool_temperate_moist': 7,
    'cool_temperate_dry': 8,
    'boreal_moist': 9,
    'boreal_dry': 10,
}
IPCC_CLIMATE_ZONE_NAMES: dict[int, str] = {
    code: name for name, code in IPCC_CLIMATE_ZONES.items()
}

# Ogle native code → canonical code. Derived from
# ipcc_climate_zones_2019.R in the Zenodo record.
IPCC_CLIMATE_ZONE_NATIVE_REMAP: dict[int, int] = {
    1: IPCC_CLIMATE_ZONES['tropical_montane'],
    2: IPCC_CLIMATE_ZONES['tropical_wet'],
    3: IPCC_CLIMATE_ZONES['tropical_moist'],
    4: IPCC_CLIMATE_ZONES['tropical_dry'],
    5: IPCC_CLIMATE_ZONES['warm_temperate_moist'],
    6: IPCC_CLIMATE_ZONES['warm_temperate_dry'],
    7: IPCC_CLIMATE_ZONES['cool_temperate_moist'],
    8: IPCC_CLIMATE_ZONES['cool_temperate_dry'],
    9: IPCC_CLIMATE_ZONES['boreal_moist'],
    10: IPCC_CLIMATE_ZONES['boreal_dry'],
    # Ogle native 11 (polar_moist) and 12 (polar_dry) have no methodology
    # analog and fall through to 0 via defaultValue in .remap().
}

# ---------------------------------------------------------------------------
# Vegetation carbon tables
# ---------------------------------------------------------------------------
# Houghton/BLUE total vegetation carbon density (tC/ha) for grassland /
# shrubland pixels, keyed by IPCC climate zone. See specs/methodology.md
# § Grassland and shrubland.
GRASSLAND_VEGETATION_TC_HA_BY_ZONE: dict[int, float] = {
    IPCC_CLIMATE_ZONES['tropical_wet']: 18.0,
    IPCC_CLIMATE_ZONES['tropical_moist']: 18.0,
    IPCC_CLIMATE_ZONES['tropical_dry']: 7.0,
    IPCC_CLIMATE_ZONES['tropical_montane']: 7.0,
    IPCC_CLIMATE_ZONES['warm_temperate_moist']: 7.0,
    IPCC_CLIMATE_ZONES['warm_temperate_dry']: 5.0,
    IPCC_CLIMATE_ZONES['cool_temperate_moist']: 7.0,
    IPCC_CLIMATE_ZONES['cool_temperate_dry']: 5.0,
    IPCC_CLIMATE_ZONES['boreal_moist']: 6.0,
    IPCC_CLIMATE_ZONES['boreal_dry']: 3.0,
}

# CDM AR-TOOL-12 dead wood and litter factors, expressed as fractions of
# above-ground biomass. See specs/methodology.md § Dead organic matter. The
# methodology's 5-row table collapses to the 10 IPCC zones below: the
# tropical rows split across zones 1–4; temperate and boreal share the same
# (0.08, 0.04) row per CDM AR-TOOL-12.
DOM_FACTORS_BY_ZONE: dict[int, tuple[float, float]] = {
    IPCC_CLIMATE_ZONES['tropical_wet']: (0.06, 0.01),
    IPCC_CLIMATE_ZONES['tropical_moist']: (0.01, 0.01),
    IPCC_CLIMATE_ZONES['tropical_dry']: (0.02, 0.04),
    IPCC_CLIMATE_ZONES['tropical_montane']: (0.07, 0.01),
    IPCC_CLIMATE_ZONES['warm_temperate_moist']: (0.08, 0.04),
    IPCC_CLIMATE_ZONES['warm_temperate_dry']: (0.08, 0.04),
    IPCC_CLIMATE_ZONES['cool_temperate_moist']: (0.08, 0.04),
    IPCC_CLIMATE_ZONES['cool_temperate_dry']: (0.08, 0.04),
    IPCC_CLIMATE_ZONES['boreal_moist']: (0.08, 0.04),
    IPCC_CLIMATE_ZONES['boreal_dry']: (0.08, 0.04),
}

# ---------------------------------------------------------------------------
# Carbon fractions and CO₂/C ratio
# ---------------------------------------------------------------------------
# IPCC 2006, Vol 4, Ch 4, §4.5 (living woody biomass).
CARBON_FRACTION_LIVE: float = 0.47
# CDM AR-TOOL-12 (dead wood and litter pools).
CARBON_FRACTION_DEAD_WOOD: float = 0.50
CARBON_FRACTION_LITTER: float = 0.37
CO2_C_RATIO: float = 44.0 / 12.0

# Global forest mean root-to-shoot ratio from Huang et al. (2021),
# Earth System Science Data 13:4263-4274. Used only for Huang BGB NoData
# gap-fill.
ROOT_SHOOT_RATIO_TEMPERATE: float = 0.25

# IPCC default reference SOC stock for warm temperate moist mineral soil,
# low-activity clay (IPCC 2019 Refinement, Vol 4, Table 2.3). Used as the
# SoilGrids NoData gap-fill value.
IPCC_REFERENCE_SOC_STOCK_TC_HA: float = 63.0

# ---------------------------------------------------------------------------
# IPCC SOC stock-change factors (IPCC 2019, Vol 4, Ch 5, Table 5.5)
# ---------------------------------------------------------------------------
# SOC loss fraction per emissive (from, to) transition, keyed by IPCC climate
# zone. IPCC 2019 Tier 1 defines a single F_LU per climate regime for
# long-term cultivated cropland (Table 5.5), applied identically regardless
# of source land use: native forest and native grassland are both at F_LU = 1
# (reference) per Table 5.10. Management and input factors default to 1
# (full tillage, nominal input), so the loss fraction is simply 1 - F_LU.
# forest→short_veg has loss 0 (both at reference).
# See specs/methodology.md § Soil organic carbon for mineral soils.

_F_LU_CROPLAND_BY_ZONE: dict[int, float] = {
    # zone_code → F_LU for long-term cultivated cropland (IPCC 2019 Vol 4
    # Table 5.5). Same factor applies to forest-family and short-veg-family
    # sources, and to both cropland and built_up destinations.
    IPCC_CLIMATE_ZONES['tropical_wet']: 0.83,  # Tropical Moist/Wet
    IPCC_CLIMATE_ZONES['tropical_moist']: 0.83,  # Tropical Moist/Wet
    IPCC_CLIMATE_ZONES['tropical_dry']: 0.92,  # Tropical Dry
    # Tropical montane has no explicit F_LU row in Table 5.5; per footnote 4,
    # montane factors are approximated as the mean of temperate and tropical
    # stock changes. Using the mean of Warm Temperate Moist (0.69) and
    # Tropical Moist/Wet (0.83) = 0.76.
    IPCC_CLIMATE_ZONES['tropical_montane']: 0.76,
    IPCC_CLIMATE_ZONES['warm_temperate_moist']: 0.69,
    IPCC_CLIMATE_ZONES['warm_temperate_dry']: 0.76,
    # Cool Temperate and Boreal share rows in Table 5.5.
    IPCC_CLIMATE_ZONES['cool_temperate_moist']: 0.70,
    IPCC_CLIMATE_ZONES['cool_temperate_dry']: 0.77,
    IPCC_CLIMATE_ZONES['boreal_moist']: 0.70,
    IPCC_CLIMATE_ZONES['boreal_dry']: 0.77,
}


def _build_soc_loss_fractions() -> dict[tuple[int, int, int], float]:
    code = {name: cat.code for name, cat in LAND_USE_CATEGORIES.items()}
    forest_family = ('forest', 'wetland_forest')
    short_veg_family = ('short_vegetation', 'wetland_short_vegetation')
    developed = ('cropland', 'built_up')
    result: dict[tuple[int, int, int], float] = {}
    for zone, f_lu in _F_LU_CROPLAND_BY_ZONE.items():
        loss = 1.0 - f_lu
        for src, dst in product(forest_family + short_veg_family, developed):
            result[(zone, code[src], code[dst])] = loss
        # forest → short_vegetation is emissive for vegetation C, but SOC
        # loss is 0 (both are reference conditions in IPCC Table 5.10).
        result[(zone, code['forest'], code['short_vegetation'])] = 0.0
    return result


IPCC_SOC_LOSS_FRACTIONS: dict[tuple[int, int, int], float] = _build_soc_loss_fractions()

# ---------------------------------------------------------------------------
# Peatland parameters (methodology § Peatland emissions, supplement § GHGP
# parameterization)
# ---------------------------------------------------------------------------
PEATLAND_P_LUC_TCO2_HA: float = 621.0
PEATLAND_E_LM_TCO2E_HA_YR: float = 37.3

# ---------------------------------------------------------------------------
# GHGP 2020-epoch allocation weights
# ---------------------------------------------------------------------------
# Linear discount weights applied to per-epoch LUC and peatland-conversion
# emissions when summing into the 2020 allocated band. Keyed by epoch
# (from_year, to_year) tuple so the mapping is immune to reordering of
# GLAD_EPOCH_PAIRS. See specs/methodology.md § "Allocating emissions to crop
# years".
GHGP_EPOCH_WEIGHTS_2020: dict[tuple[int, int], float] = {
    (2000, 2005): 0.0125,
    (2005, 2010): 0.0375,
    (2010, 2015): 0.0625,
    (2015, 2020): 0.0875,
}

# ---------------------------------------------------------------------------
# Per-epoch band-name helpers
# ---------------------------------------------------------------------------
# Centralized so a typo anywhere is detected at import time rather than
# silently producing a band the consumer never reads.


def transitions_band(from_year: int, to_year: int) -> str:
    """Land_use band name for the (from_year, to_year) transition epoch."""
    return f'transitions_{from_year}_{to_year}'


def luc_emissions_band(from_year: int, to_year: int) -> str:
    """Emissions band: per-epoch land-use-change CO2e (tCO2/ha)."""
    return f'luc_emissions_{from_year}_{to_year}'


def peatland_conversion_band(from_year: int, to_year: int) -> str:
    """Emissions band: per-epoch peatland conversion CO2 (tCO2/ha)."""
    return f'peatland_conversion_{from_year}_{to_year}'


# ---------------------------------------------------------------------------
# emissions raster band names (11 bands, spec order)
# ---------------------------------------------------------------------------
# See specs/pipeline_tech_design.md § Rasters for the full band definition.
EMISSIONS_BAND_NAMES: list[str] = (
    [luc_emissions_band(a, b) for (a, b) in GLAD_EPOCH_PAIRS]
    + [peatland_conversion_band(a, b) for (a, b) in GLAD_EPOCH_PAIRS]
    + [
        'peatland_occupation_2020',
        'allocated_luc_emissions_2020',
        'allocated_peatland_emissions_2020',
    ]
)

# ---------------------------------------------------------------------------
# emissions_type code vocabulary
# ---------------------------------------------------------------------------
# Integer codes for the 11-entry EMISSIONS_TYPE_NAMES vocabulary, used by
# transform/summary_tables.py as the group key for per-county reduceRegions.
# Codes are 1-indexed so that 0 can remain the "non-emissive" sentinel that
# gets masked out of each reducer's input.
EMISSIONS_TYPE_CODES: dict[str, int] = {
    name: code for code, name in enumerate(EMISSIONS_TYPE_NAMES, start=1)
}
EMISSIONS_TYPE_CODE_TO_NAME: dict[int, str] = {
    code: name for name, code in EMISSIONS_TYPE_CODES.items()
}

# ---------------------------------------------------------------------------
# Epoch transition labels
# ---------------------------------------------------------------------------
# Canonical string labels for the four GLAD epoch windows, used as the
# `epoch_transition` column value in the `transitions` summary table rows for
# LUC and peatland_conversion. Peatland_occupation rows use a distinct
# single-year label because occupation is an annual emission, not a window.
EPOCH_TRANSITION_LABELS: list[str] = [f'{a}_{b}' for (a, b) in GLAD_EPOCH_PAIRS]
PEATLAND_OCCUPATION_EPOCH_LABEL: str = '2020'

# ---------------------------------------------------------------------------
# Row crop vocabulary (methodology § Allocating emissions to crops)
# ---------------------------------------------------------------------------
# CDL crop codes grouped by the three crop families the methodology
# emissions-factors against. Double-crop codes (26, 225, 236, 238, 240, 254)
# are intentionally excluded and therefore flow through neither the emissions
# numerator nor the production denominator — a known issue, tracked in
# specs/methodology.md § Appendix 2.
CROP_GROUPS: dict[str, list[int]] = {
    'corn': [1],
    'soybeans': [5],
    'wheat': [22, 23, 24],  # durum, hard red spring, hard red winter
}
CROP_CODE_TO_GROUP: dict[int, str] = {
    code: group for group, codes in CROP_GROUPS.items() for code in codes
}
ALL_ROW_CROP_CODES: list[int] = sorted(CROP_CODE_TO_GROUP)

# NASS QuickStats commodity name → our crop group key.
NASS_TO_CROP_GROUP: dict[str, str] = {
    'CORN': 'corn',
    'SOYBEANS': 'soybeans',
    'WHEAT': 'wheat',
}

# NASS QuickStats yield years averaged into the per-state yield rate.
# 4-year arithmetic mean smooths single-year outliers (methodology
# § Calculate crop yields).
NASS_YIELD_YEARS: list[int] = [2017, 2018, 2019, 2020]

# ---------------------------------------------------------------------------
# Unit conversion constants (methodology § Unit conversion)
# ---------------------------------------------------------------------------
# Standard bushel weights per 7 CFR 810 (US Grain Standards Act), expressed
# in kg: 56 lb/bu × 0.45359237 kg/lb for corn; 60 lb/bu for soybeans and
# wheat.
BUSHEL_WEIGHT_KG: dict[str, float] = {
    'CORN': 56 * 0.45359237,
    'SOYBEANS': 60 * 0.45359237,
    'WHEAT': 60 * 0.45359237,
}
# Exact SI definition (1 acre = 0.40468564 hectares).
HA_PER_ACRE: float = 0.40468564

# ---------------------------------------------------------------------------
# Summary-table input asset IDs (counties and NASS yields)
# ---------------------------------------------------------------------------
GEE_TIGER_COUNTIES: str = 'TIGER/2018/Counties'
GEE_NASS_YIELDS: str = f'{GEE_ASSET_ROOT}/nass_yields_{EXPECTED_NASS_VERSION}'

# Extend DATASET_INVENTORY with the two summary-table inputs.
DATASET_INVENTORY['tiger_counties'] = {
    'gee_asset_id': GEE_TIGER_COUNTIES,
    'resolution_hint': 'vector',
    'native_or_extracted': 'native',
}
DATASET_INVENTORY['nass_yields'] = {
    'gee_asset_id': GEE_NASS_YIELDS,
    'resolution_hint': 'state-level (tabular)',
    'native_or_extracted': 'extracted',
    'expected_version': EXPECTED_NASS_VERSION,
    'source_url': NASS_QUICKSTATS_URL_TEMPLATE.format(
        date=NASS_QUICKSTATS_RELEASE_DATE
    ),
}

# ---------------------------------------------------------------------------
# Summary-table column schemas
# ---------------------------------------------------------------------------
# Single source of truth for both builders and integration tests.
# See specs/pipeline_tech_design.md § Tables.
TRANSITIONS_TABLE_COLUMNS: list[str] = [
    'county_fips',
    'epoch_transition',
    'emissions_type',
    'total_area_ha',
    'total_emissions_tco2',
    'allocated_emissions_2020_tco2',
]
CROPS_TABLE_COLUMNS: list[str] = [
    'county_fips',
    'crop_code',
    'crop_group',
    'total_production_kg',
    'total_production_bu',
    'total_crop_area_ha',
    'peatland_crop_area_ha',
    'yield_kg_per_ha',
    'yield_bu_per_acre',
    'total_allocated_emissions_tco2',
    'emissions_factor_kgco2e_per_kg',
    'pct_forest',
    'pct_short_veg',
    'pct_peatland_conversion',
    'pct_peatland_occupation',
    'pct_epoch_2005',
    'pct_epoch_2010',
    'pct_epoch_2015',
    'pct_epoch_2020',
]


def _build_crop_driver_mapping() -> dict[str, str]:
    """emissions_type → pct column for the `crops` table driver breakdown.

    Groups the 9 LUC emissive types by source family: forest / wetland_forest
    collapse to `pct_forest`, short_veg / wetland_short_veg to `pct_short_veg`.
    Peatland rows each get their own column. `*_to_built_up` and
    `forest_to_short_veg` entries exist for completeness but are filtered out
    by `is_row_crop` before crop-row aggregation, since those destinations
    cannot be 2020 cropland pixels.
    """
    forest_family = {'forest', 'wetland_forest'}
    short_veg_family = {'short_vegetation', 'wetland_short_vegetation'}
    mapping: dict[str, str] = {}
    for name, src, _dst in _EMISSIVE_TRANSITION_CATEGORIES:
        if src in forest_family:
            mapping[name] = 'pct_forest'
        elif src in short_veg_family:
            mapping[name] = 'pct_short_veg'
        else:
            raise ValueError(f'Unmapped source family for {name!r}: {src!r}')
    mapping['peatland_conversion'] = 'pct_peatland_conversion'
    mapping['peatland_occupation'] = 'pct_peatland_occupation'
    return mapping


CROP_DRIVER_MAPPING: dict[str, str] = _build_crop_driver_mapping()


# ---------------------------------------------------------------------------
# Publish stage — BigQuery destination
# ---------------------------------------------------------------------------
# The publish stage exports the regional `transitions` and `crops` GEE
# table assets into BigQuery under compound-keyed table names of the form
# ``{PREFIX}_{region}_{transform_sha}_{publish_sha}``. Both tables land in
# the same project / dataset; the dataset location is pinned so
# ``ee.batch.Export.table.toBigQuery`` can target it explicitly.

# ``BQ_PROJECT``, ``BQ_DATASET``, and ``BQ_JOB_LOCATION`` are defined in the
# deployment configuration block at the top of this module (US multi-region
# by default; export tasks must target the same location as the destination
# dataset).
BQ_TRANSITIONS_TABLE_PREFIX: str = 'luc_transitions'
BQ_CROPS_TABLE_PREFIX: str = 'luc_crops'

# GCS path prefix for cloud-optimised GeoTIFF raster exports (land_use,
# emissions). Objects land at gs://{GCS_BUCKET_NAME}/{GCS_RASTER_PREFIX}/...
GCS_RASTER_PREFIX: str = f'{BQ_PROJECT:s}/{BQ_DATASET:s}/rasters'

# GCS path prefix for CSV table exports (transitions, crops).
# Objects land at gs://{GCS_BUCKET_NAME}/{GCS_TABLE_PREFIX}/{table_name}-*.csv
GCS_TABLE_PREFIX: str = f'{BQ_PROJECT:s}/{BQ_DATASET:s}/tables'
