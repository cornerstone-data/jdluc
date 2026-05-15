# Cornerstone jdLUC: pipeline technical design

This document describes the technical design for a data pipeline to implement the jdLUC methodology described in methodology.md.

The pipeline follows an **Extract → Transform → Publish** architecture, inspired by [Cornerstone's MRIO pipeline architecture](https://github.com/cornerstone-data/papers/blob/public-review-draft-tech-arch-vision/architecture_vision/Cornerstone_Architecture_Vision.md):

- **Extract**: Ingest external datasets into Google Earth Engine (GEE) as rasters and tables.
- **Transform**: The main computation pipeline; builds a time series of land use transitions, accompanying emissions, and summary tables.
- **Publish**: Publish outputs to BigQuery for downstream queries.

## Platform

All raster processing runs on Google Earth Engine via the `earthengine-api` Python package. GEE provides native raster algebra optimized for planetary-scale compute, and many of the input datasets (GLAD GLC, CDL, Harris AGB, SoilGrids, etc.) are already in its catalog.

GEE uses a lazy evaluation architecture: operations on `Image`, `ImageCollection`, and `FeatureCollection` objects build server-side computation graphs. Per-pixel logic is composed from raster algebra functions (`.add()`, `.multiply()`, etc.), conditionals (`.where()`), and masking (`.mask()`, `.updateMask()`). Multiple bands can be stacked into a single target image via `.addBands()`. Computation actually runs when results are materialized with functions like `.getInfo()` (synchronously retrieve computed results), `.evaluate()` (asynchronously retrieve computed results), `Export.image.toAsset()` / `Export.table.toAsset()` (asynchronously materialize a raster or table as a persistent GEE asset), and `Export.image.toDrive()` (asynchronously export a raster to Google Drive as a GeoTIFF or other format).

The pipeline takes advantage of GEE's lazy evaluation approach to construct the main stages of the computation graph as raster algebra, then materializes results at chosen stage boundaries where we want to persist fixed assets.

## Geospatial grid

All raster computation uses the GLAD GLCLUC native grid, pinned as `GLAD_CRS_TRANSFORM = [0.00025, 0, -180, 0, -0.00025, 90]` (global `EPSG:4326`). Every `Export.image.toAsset()` call in the pipeline passes `crs='EPSG:4326'` and `crsTransform=GLAD_CRS_TRANSFORM`. We're extremely explicit because GEE's implicit projection resolution is famously unpredictable when inputs have different native grids — the output origin is inherited from somewhere in the computation graph, and which input wins is brittle to reason about. See GEE's [Projections guide](https://developers.google.com/earth-engine/guides/projections).

Inputs land on this grid at one of two points:

- **Extract-time** for non-GEE-native datasets — i.e. reprojected during upload;
- **Transform-time** for GEE-native datasets — read from the GEE catalog and reprojected in the computation graph

Reprojections use nearest-neighbor interpolation for categorical data types and bilinear interpolation for continuous data types.

## Architecture

**`pipeline.py`** — the overall entry point; orchestrates the pipeline;

**`extract/`** — makes all input datasets available as GEE assets.

- `extract.py` — extract stage entry point; orchestrates extraction across datasets; 
- `[dataset_name].py` — one module per external dataset; each module exposes a function that downloads the dataset, does any minimally necessary preprocessing (e.g. merging tiles to a single raster), and uploads the data as a GEE asset
- GEE-native datasets are used directly: GLAD GLC, CDL, SoilGrids SOC, TIGER state boundaries.

**`transform/`** — computes emissions from GEE assets at region scope.

- `transform.py` — transform stage entry point; orchestrates the four sequential exports per pipeline run (one task per stage, no per-state subdivision).
- `land_use.py` — builds the `land_use` raster from GLAD GLC + CDL + GFW Peatlands for the requested region geometry.
- `emissions.py` — computes per-pixel emissions from transitions and peatland management for the same region.
- `summary_tables.py` — reduces the raster assets to `transitions` and `crops` tables via a single `reduceRegions` over the region's counties; output FeatureCollections stay server-side from `reduceRegions` through `Export.table.toAsset`.

**`publish/`** — make pipeline outputs accessible to consumers.

- `publish.py`: publish stage entry point; orchestrates publish jobs
- `bigquery.py`: exports the `transitions` and `crops` tables to BigQuery for easy SQL querying.

**`utils/`**

- `constants.py` — GEE asset IDs, land use codes, IPCC tables, crop groups, etc.
- `gee.py` — GEE initialization
- `states.py` — state boundary helpers
- `transitions.py` — land use transition encoding/decoding helpers
- `version.py` — pipeline versioning from git state.
- `asset_management.py` — listing, version-parsing, and safe-deletion helpers for GEE assets

### Layering enforcement

The four pipeline packages form a strict dependency chain — `extract → transform → publish → pipeline` — enforced by [import-linter](https://import-linter.readthedocs.io/) contracts. Higher layers may import lower; never the reverse. `jdluc.utils` is shared and every layer is free to import from it.

## Outputs

### Rasters

Two regional rasters per pipeline run:

#### `land_use_{region}_{version}`


| Bands                                                             | Count | Type  | Description                                     |
| ----------------------------------------------------------------- | ----- | ----- | ----------------------------------------------- |
| `transitions_2000_2005`,`…_2005_2010`,`…_2010_2015`,`…_2015_2020` | 4     | uint8 | Land use transition per GLAD GLC epoch boundary |
| `crops_2020`                                                      | 1     | uint8 | CDL crop code for GLAD cropland pixels, 2020    |
| `is_peatland`                                                     | 1     | uint8 | peatland mask                                   |


The `transitions_{epoch}` bands preserve the full 9×9 space of land use category transition pairs, including nonemissive ones. `0` denotes "no transition" (same category at both epoch boundaries). See `utils/transitions.py` for the `uint8` encoding scheme for transition pairs.

#### `emissions_{region}_{version}`

LUC and peatland emissions per epoch transition + 2020-allocated values


| Bands                                                                        | Count | Type    | Description                                                                                   |
| ---------------------------------------------------------------------------- | ----- | ------- | --------------------------------------------------------------------------------------------- |
| `luc_emissions_2000_2005`, `…_2005_2010`, `…_2010_2015`, `…_2015_2020`       | 4     | float32 | Forest + short vegetation + SOC emissions per epoch transition (no peatland), tCO2e per pixel |
| `peatland_conversion_2000_2005`, `…_2005_2010`, `…_2010_2015`, `…_2015_2020` | 4     | float32 | Peatland transformation (land use change) emissions per epoch transition, tCO2e per pixel     |
| `peatland_occupation_2020`                                                   | 1     | float32 | Annual peatland occupation (land management) emissions, tCO2e per pixel                       |
| `allocated_luc_emissions_2020`                                               | 1     | float32 | 20-year weighted sum of LUC transition emissions, tCO2e per pixel                             |
| `allocated_peatland_emissions_2020`                                          | 1     | float32 | 20-year weighted peatland conversion emissions + occupation, tCO2e per pixel                  |


### Tables

#### `transitions_{region}_{version}`

Grain: one row per `(county_fips, epoch_transition, emissions_type)`.


| Column                        | Type    | Description                                            |
| ----------------------------- | ------- | ------------------------------------------------------ |
| county_fips                   | STRING  | 5-digit combined state+county FIPS code                |
| epoch_transition              | STRING  | Epoch transition (e.g. "2000_2005", "2015_2020")       |
| emissions_type                | STRING  | One of the types below                                 |
| total_area_ha                 | FLOAT64 | Total land area affected by this transition (hectares) |
| total_emissions_tco2          | FLOAT64 | Emissions from this transition                         |
| allocated_emissions_2020_tco2 | FLOAT64 | 2020-allocated emissions                               |

**`emissions_type` values:**

- **LUC transition types**: `forest_to_cropland`, `forest_to_built_up`, `forest_to_short_veg`, `wetland_forest_to_cropland`, `wetland_forest_to_built_up`, `short_veg_to_cropland`, `short_veg_to_built_up`, `wetland_short_veg_to_cropland`, `wetland_short_veg_to_built_up` — carry LUC-only emissions (forest + short vegetation + SOC, no peatland). Note: GLAD GLC does not distinguish pastureland from grassland, so there are no separate pastureland transition types.
- **`peatland_conversion`**: peatland transformation emissions on transition pixels, one row per `(county_fips, epoch_transition)`
- **`peatland_occupation`**: annual peatland occupation emissions for epoch_transition=2020 only, on all peatland pixels under cultivation. One row per county. `allocated_emissions_2020_tco2 = total_emissions_tco2` for these rows.

#### `crops_{region}_{version}`

Grain: one row per `(county_fips, crop_code)`. Yields are applied uniformly from state-level NASS data (see known issues in methodology.md); county-level emissions factors still vary because per-hectare emissions vary across counties.


| Column                         | Type    | Description                                                                                          |
| ------------------------------ | ------- | ---------------------------------------------------------------------------------------------------- |
| county_fips                    | STRING  | 5-digit combined state+county FIPS code                                                              |
| crop_code                      | INT64   | CDL code                                                                                             |
| crop_group                     | STRING  | 'corn', 'soybeans', 'wheat'                                                                          |
| total_production_kg            | FLOAT64 | County 2020 production in kg (county area × state-averaged yield)                                    |
| total_production_bu            | FLOAT64 | County 2020 production in bushels (county area × state-averaged yield)                               |
| total_crop_area_ha             | FLOAT64 | Total ha of this crop in county (2020)                                                               |
| peatland_crop_area_ha          | FLOAT64 | Ha of this crop on peatland in county (2020)                                                         |
| yield_kg_per_ha                | FLOAT64 | NASS state-level yield rate (4-year mean, 2017-2020); applied uniformly to all counties in the state |
| yield_bu_per_acre              | FLOAT64 | NASS state-level yield rate (native unit); applied uniformly to all counties in the state            |
| total_allocated_emissions_tco2 | FLOAT64 | Sum of allocated_emissions_2020_tco2 across all emissions types                                      |
| emissions_factor_kgco2e_per_kg | FLOAT64 | allocated / production × 1000                                                                        |
| pct_forest                     | FLOAT64 | % of allocated from forest transitions                                                               |
| pct_short_veg                  | FLOAT64 | % from short vegetation transitions                                                                  |
| pct_peatland_conversion        | FLOAT64 | % from peatland conversion                                                                           |
| pct_peatland_occupation        | FLOAT64 | % from peatland occupation                                                                           |
| pct_epoch_2005…pct_epoch_2020  | FLOAT64 | % of allocated by epoch transition                                                                   |


## Stage Details

### Overall entry point

The overall pipeline entry point is `run_pipeline()` in `pipeline.py`.

#### `run_pipeline(gcp_project, states, region_name, force=False) -> PipelineResult`

Computes and publishes assets for the states defined in the `states` array, running the `extract/`, `transform/`, and `publish/` steps in sequence. `states` can also take the special value `['CONUS']` to compute outputs for all 48 continental US states + the District of Columbia. `region_name` is the identifier used in the regional raster and table asset names.

On success, returns a `PipelineResult` dataclass with the pipeline version, region name, materialized asset IDs (or `ee.Image` / `ee.FeatureCollection` handles) from each stage, and a `from_cache` flag indicating whether any work was done on this invocation. On failure, raises the originating exception after logging to stderr.

Each stage checks for previously-computed assets at its expected version and reuses them on cache hit, so re-runs after partial failures avoid recomputing completed work. Certain parts of the pipeline have very simple retry logic, but transient failures (GEE quota limits, timeouts, etc.) are mostly handled by simply re-running the pipeline.

### Extract

The Extract stage makes all input datasets available as GEE assets.

#### Entry point

The entry point to the extract steps is `extract_all(gcp_project, force=False)` in `extract.py`.

`extract_all()` begins by loading the dataset inventory stored in `utils/constants.py`:


| Dataset                  | Source                                         | GEE Asset ID                                                          | Resolution            | GEE-native? |
| ------------------------ | ---------------------------------------------- | --------------------------------------------------------------------- | --------------------- | ----------- |
| GLAD GLCLUC v2           | GEE catalog                                    | `projects/glad/GLCLU2020/v2/LCLUC_{year}`                             | 0.00025° (~30m)       | GEE-native  |
| USDA CDL                 | GEE catalog                                    | `USDA/NASS/CDL`                                                       | 30m (native Albers)   | GEE-native  |
| SoilGrids SOC (0–30cm)   | GEE catalog                                    | `projects/soilgrids-isric/ocs_mean`                                   | 250m (native IGH)     | GEE-native  |
| US state boundaries      | GEE catalog                                    | `TIGER/2018/States`                                                   | Vector                | GEE-native  |
| Harris et al. (2021) AGB | Global Forest Watch (ArcGIS)                   | `projects/cornertone-luc/assets/high-res-luc/harris_agb_conus`        | 0.00025° (~30m)       | No          |
| Huang et al. (2021) BGB  | Figshare (doi:10.6084/m9.figshare.12199637.v1) | `projects/cornertone-luc/assets/high-res-luc/huang_bgb_conus`         | 0.0083° (~1km)        | No          |
| NASS crop yields         | USDA QuickStats                                | `projects/cornertone-luc/assets/high-res-luc/nass_yields`             | State-level (tabular) | No          |
| IPCC climate zones       | Zenodo (doi:10.5281/zenodo.7303808)            | `projects/cornertone-luc/assets/high-res-luc/ipcc_climate_zones`      | 0.5° (~50km)          | No          |
| GFW Global Peatlands     | Global Forest Watch Open Data (CC BY 4.0)      | `projects/cornertone-luc/assets/high-res-luc/gfw_peatlands_v20230315` | 0.00025° (~30m)       | No          |


All raster pixel sizes are shown as ~meter-equivalent at CONUS mid-latitudes. CDL and SoilGrids have projected native coordinate reference systems (Albers Equal Area and Interrupted Goode Homolosine, respectively), so meters are the primary unit for those two rows; all others are natively on a WGS84 degree grid.

For each non-GEE-native dataset, `extract_all()` checks whether the target GEE asset already exists, in the expected version. If the asset is missing (or `force=True`), `extract_all()` calls the applicable dataset function to download, convert, and then upload that dataset to GEE. Conversion steps are minimal: just what's necessary to get the dataset into GEE and pinned onto `GLAD_CRS_TRANSFORM` ([Platform](#Platform)). All other computation stays in the transform pipeline. As it completes each dataset, `extract_all()` logs status: cached / extracted / failed.

#### Asset versioning

Extract assets are each tagged with a suffix derived from the upstream dataset's version — e.g., `harris_agb_conus_v2021`, `nass_yields_v2017_2020`. The expected version for each dataset is defined in the `constants.py` dataset inventory table. `extract_all()` compares the expected version against what exists in GEE: if the expected asset is missing, it runs the extraction steps. If prior versions of a dataset also exist, `extract_all()` logs a warning but does not delete them. 

Unlike later stages of the pipeline, extract assets are *not* tagged with a SHA of the `extract/` pipeline code version that uploaded them. The extract logic is simple and stable; on the rare occasions it changes, there is a simple utility in `cli/` to flush cached assets. 

To update a source dataset (e.g., extending NASS yields through 2024), update the expected version in `constants.py` and re-run `run_extract`. The old asset remains until manually deleted.

#### Source-data mirroring

Every non-native input dataset (Harris AGB, Huang BGB, NASS QuickStats, IPCC climate zones, GFW Peatlands) is mirrored to `gs://cornerstone-luc/luc_high_res/extract_mirror/{dataset}/` on first successful upstream fetch. Subsequent extract runs read from the mirror first and fall back to upstream only on mirror miss. All five sources permit mirroring with attribution (Harris CC-BY, Huang CC-BY-4.0, NASS US-gov public domain, IPCC open Zenodo, GFW Peatlands CC-BY-4.0).

Most pipeline runs are already insulated from upstream availability because the transform stage reads the cached GEE extract asset, not the raw source — if a publisher takes down their site tomorrow, the pipeline keeps working as long as `{dataset}_{version}` exists in GEE. But the mirroring protects against change to extract-stage logic that requires re-deriving the GEE asset from the original source. The extract modules do a small but real amount of processing between the source file and the GEE asset.

GEE-native assets are **not** mirrored. The reproducibility argument doesn't apply — we don't do any extract-stage processing on them (they're read directly from the GEE catalog by transform), so there's no "re-derive the GEE asset from source" operation to guard. Upstream removal of the catalog asset itself IS a risk, but empirically low: GLAD has retained v1 (`projects/glad/GLCLU2020/LCLUC_{2000,2020}`) years after v2 shipped, indicating stable version stewardship; CDL / TIGER / SoilGrids have institutional backing that makes deprecation risk low.

#### Dataset details

None of the following extract steps require API keys or other credentials beyond standard GCP auth (`gcloud auth login`) and GEE auth (`earthengine authenticate`) for the upload side.

GEE image ingestion requires the source file to live in a GCS bucket first, so the Harris and Huang extractors both include a transient GCS staging step (upload → ingest → delete GCS object). NASS and IPCC avoid this because GEE's table-asset and small-image paths accept direct uploads.

Tile-based extracts (Harris AGB, GFW Peatlands) fan out their per-tile work into three phases: concurrent download + GCS staging via a thread pool, synchronous batch submission of all ingest tasks, single bulk poll until all reach a terminal state. GEE caps per-project concurrent ingest tasks at ~20 (empirical on the Limited tier), so the wall-time floor is `total_tile_minutes / 20` regardless of how clients submit — sequential per-tile submit-and-wait would leave 19/20 slots idle. Per-tile failures are isolated: a single failed tile logs but doesn't abort siblings; the resulting ImageCollection is partial and can be repaired by re-running with `force=True`.

##### Harris AGB

1. **Discover**: Query the Global Forest Watch ArcGIS FeatureServer (`services2.arcgis.com/.../Aboveground_Live_Woody_Biomass_Density/FeatureServer/0/query`) for each target tile ID to retrieve the per-tile pre-signed download URL from the feature's `Mg_ha_1_download` attribute. No API key is required; the FeatureServer is public.
2. **Download**: Fetch the 21 10°×10° GeoTIFF tiles covering CONUS (rows 30N/40N/50N × columns 070W–130W). Units are Mg/ha at ~30m (0.00025°) resolution.
3. **Upload**: Stage tiles in GCS and ingest them into GEE *as separate images in a single `ImageCollection`* at `projects/cornerstone-luc/assets/high-res-luc/harris_agb_conus` (not pre-merged into a single mosaic — GEE handles the mosaicking lazily at read time).

##### Huang BGB

1. **Download**: Fetch `data_code_to_submit.zip` (~877 MB) directly from the paper's Figshare deposit (`https://ndownloader.figshare.com/files/22432460`, DOI `10.6084/m9.figshare.12199637.v1`, no auth required) and extract `pergridarea_bgb.nc` from it.
2. **Convert**: Open the NetCDF, select the `AROOT` variable, clip to a CONUS bounding box, tag with EPSG:4326, and write as a LZW-compressed float32 GeoTIFF.
3. **Upload**: Stage the GeoTIFF in GCS and ingest into GEE as an `Image` asset at `projects/cornerstone-luc/assets/high-res-luc/huang_bgb_conus`.

##### NASS crop yields

1. **Download**: Fetch the full USDA NASS QuickStats crops file (`https://www.nass.usda.gov/datasets/qs.crops_{YYYYMMDD}.txt.gz`, ~1.5 GB gzipped, no auth required). The filename is time-stamped by release date, so each dataset version corresponds to a different URL — the expected URL is stored in `constants.py` alongside the version string.
2. **Upload**: Ingest the raw filtered records as a GEE `FeatureCollection` table asset at `projects/cornerstone-luc/assets/high-res-luc/nass_yields`, keyed by state centroid. All filtering, unit conversion (bu/acre → kg/ha using 7 CFR 810 bushel weights), and multi-year averaging happens in the transform stage.

##### IPCC climate zones

1. **Download**: Fetch the IPCC climate zone raster from Zenodo (DOI: 10.5281/zenodo.7303808, Ogle et al. 2006) as a plain HTTP download; no auth required.
2. **Upload**: Ingest into GEE as an `Image` asset at `projects/cornerstone-luc/assets/high-res-luc/ipcc_climate_zones`.

##### GFW Peatlands

1. **Discover**: Tile download URLs are template-constructed from a stable GFW Open Data API key plus `tile_id` (e.g. `40N_080W`) — no per-tile FeatureServer roundtrip. The 19-tile CONUS list (`CONUS_TILE_IDS` in `extract/gfw_peatlands.py`) hard-codes the v20230315 manifest; two of Harris AGB's 21 CONUS tiles (30N_070W, 30N_130W) are absent because GFW omits tiles with zero peatland pixels.
2. **Download**: Fetch the 19 10°×10° GeoTIFF tiles. Each is a uint8 single-band mask at ~30m (0.00025°), already aligned to the Hansen Global Forest Change grid (which matches `GLAD_CRS_TRANSFORM`) — no local reprojection or rasterization needed.
3. **Upload**: Stage each tile in GCS and ingest into GEE as separate images in a single `ImageCollection` at `projects/cornerstone-luc/assets/high-res-luc/gfw_peatlands_v20230315` (mirroring the Harris AGB collection shape; `transform/land_use.py::_load_is_peatland` lazy-mosaics on read).

GFW publishes Global Peatlands as a 30m composite raster: Xu et al. (2018) PEATMAP above 40°N (the source for CONUS), Gumbricht et al. (2017) elsewhere, with regional overrides for Indonesia/Malaysia (Miettinen 2016), the lowland Peruvian Amazon (Hastie 2022), and the Congo basin (Crezee 2022). 

### Transform

The Transform stage is the main computation pipeline. It reads from GEE assets (both native catalog datasets and extracted assets from the `extract/` stage), computes per-pixel emissions, and exports results as versioned regional GEE raster and table assets.

#### Overall stage architecture

##### Module structure

**`transform.py`** orchestrates four GEE exports per pipeline run, in three sequential steps, all scoped to the requested region's union geometry:

1. `land_use.py` exports the `land_use_{region}_{version}` raster.
2. `emissions.py` exports the `emissions_{region}_{version}` raster, reading the materialized `land_use` asset.
3. `summary_tables.py` exports `transitions_{region}_{version}` and `crops_{region}_{version}` in parallel, both via a single server-side `reduceRegions` over the region's TIGER counties.

A pipeline run targets a "region" which is either an array of `states` or the special 'CONUS' value. The region resolves once to (a) an `ee.Geometry` for raster builds (`get_multi_state_boundary`) and (b) an `ee.FeatureCollection` of counties. Both are passed through to the per-stage builders.

GEE's auto-tiling handles CONUS-scale single-task exports comfortably, which is how each of the land_use, emissions, transitions, and crops exports are run. We tested single, full-region jobs up to CONUS scale versus sharding large regions into state-level GEE tasks, and the single-region approach is 2-10x faster. GEE limits parallelization at the user task level, whereas internal parallelization within a single task is very high: a full CONUS pipeline run and a Delaware run both take similar amounts of time.

##### Asset versioning

Assets are tagged with the pipeline version that produced them. Version strings are computed using `utils/version.py` and appended to file names.

At runtime, the transform stage checks for an asset at the exact expected version string. If missing, it computes the asset; if present, it reuses the existing assets. 

Prior-version assets are silently ignored (this is unlike the extract stage, which warns about prior versions; transform assets turn over more frequently, so we decided warning on their presence by default would be noisy).

#### Module details

##### transform.py (entry point)

`run_transform(gcp_project, states, region_name, force=False) -> TransformResult` is the entry point.

Resolves the region geometry and counties FeatureCollection once, then advances through four global phases: `LAND_USE_PENDING` → `LAND_USE_DONE` → `EMISSIONS_PENDING` → `EMISSIONS_DONE` → `TABLES_PENDING` → `TABLES_DONE` | `FAILED`. The `transitions` and `crops` exports submit in parallel during `TABLES_PENDING`. A polling loop (~30s cadence) drives phase transitions because GEE's batch API lacks callbacks. Each stage's downstream consumer reads the prior stage's materialized asset by ID, not its pre-export DAG.

On success returns a `TransformResult` dataclass carrying the four asset IDs (`land_use_asset_id`, `emissions_asset_id`, `transitions_table_id`, `crops_table_id`) and the pipeline version. On failure raises `TransformError` with the failed phase and error details.

##### land_use.py

Builds the GEE computation graph for land use classification and transition detection across the five GLAD GLC epochs, and exports the result as a versioned raster asset.

###### `build_land_use_image(geometry) -> EEImage`

Constructs the 6-band `land_use` raster for the requested region geometry:

1. Loads GLAD GLCLUC v2 for each of the five epochs (2000, 2005, 2010, 2015, 2020), clipped to `geometry`.
2. Classifies each epoch via `classify_glad_glc`.
3. Calls `detect_transitions` for each consecutive epoch pair, producing the four `transitions_{from}_{to}` bands.
4. Loads CDL 2020, reprojects onto `GLAD_CRS_TRANSFORM` (nearest-neighbor), masks to GLAD 2020 cropland pixels, producing the `crops_2020` band.
5. Loads the PEATMAP (Xu et al. 2018) mask asset, reprojects onto `GLAD_CRS_TRANSFORM` (nearest-neighbor), producing the `is_peatland` band.

Returns the 6-band image described in the `land_use` raster spec above. Pure graph building — no materialized assets.

###### `export_land_use_asset(image, region, version, geometry, force) -> ee.batch.Task`

Submits an `ee.batch.Export.image.toAsset()` job for the image with `region=geometry`, `maxPixels=int(1e13)`. The build graph projects every band onto `GLAD_CRS_TRANSFORM` via explicit `.reproject(...)` inside `build_land_use_image`, so the export omits the `crs` / `crsTransform` keyword arguments — passing them on top of an already-grid-aligned input triggers a no-op-but-not-actually-no-op reprojection. Asset ID follows `land_use_{region}_{version}`. Returns the task handle for `transform.py` to poll.

###### `classify_glad_glc(image) -> EEImage`

Reclassifies raw GLAD GLCLUC pixel values into simplified category codes (defined in `constants.py`):


| GLAD GLCLUC pixel values | Land use category                                          |
| ------------------------ | ---------------------------------------------------------- |
| 25–48                    | Forest (terra firma tree cover, by canopy height)          |
| 125–148                  | Wetland forest                                             |
| 1–24                     | Short vegetation (grassland, shrubland, sparse vegetation) |
| 100–124                  | Wetland short vegetation                                   |
| 244                      | Cropland                                                   |
| 250                      | Built-up                                                   |
| 200–207                  | Water                                                      |
| 241                      | Snow/ice                                                   |
| 0                        | Bare                                                       |


Category codes are single-digit integers (1–9), stored as uint8.

###### `detect_transitions(classified_from, classified_to) -> EEImage`

Compares two classified epoch images pixel-by-pixel and returns a single uint8 band encoding the transition type, produced via `utils/transitions.encode_transition()`. Pixels where the category is unchanged between epochs are set to 0. See `utils/transitions.py` for the encoding scheme.

##### emissions.py

Builds the GEE computation graph for per-pixel emissions from a cached `land_use` asset, and exports the result as a versioned raster asset.

###### `build_emissions_image(land_use_asset_id, geometry) -> EEImage`

Constructs the 11-band `emissions` raster for the requested region geometry. Takes the asset ID of the cached `land_use` raster — not an in-memory image — and loads it via `ee.Image(land_use_asset_id)`. This ensures emissions are computed against the finalized, exported land use raster rather than re-deriving land use from raw GLAD inputs at emissions-export time.

Loads all emissions input datasets (Harris AGB, Huang BGB, SoilGrids SOC, IPCC climate zones, GFW Peatlands) from their GEE assets, clipped to `geometry`. SoilGrids and Huang BGB are reprojected onto `GLAD_CRS_TRANSFORM` at read time via explicit `.reproject()` (bilinear for both continuous layers); Harris AGB, IPCC climate zones, and GFW Peatlands arrive pre-aligned from extract — they were rasterized to the GLAD grid at ingest time. Then:

1. Iterates over the four epoch pairs, calling `calculate_epoch_emissions` to produce `luc_emissions_{epoch}` and `peatland_conversion_{epoch}` bands.
2. Calls `calculate_peatland_occupation` to produce the single `peatland_occupation_2020` band.
3. Calls `calculate_allocated_emissions_2020` to produce the two `allocated_*_2020` bands.

Stacks into the 11-band image described in the `emissions` raster spec above. Pure graph building — no exports.

###### `export_emissions_asset(image, region, version, geometry, force) -> ee.batch.Task`

Submits an `ee.batch.Export.image.toAsset()` job for the image with `region=geometry`, `maxPixels=int(1e13)`, and `crs` / `crsTransform` omitted (the input is already on `GLAD_CRS_TRANSFORM` via the per-band `.reproject(...)` inside `build_emissions_image`). Asset ID follows `emissions_{region}_{version}`. Returns the task handle.

###### `calculate_epoch_emissions(transition_band, inputs) -> EEImage`

Takes a single epoch's encoded transition band plus the pre-loaded input datasets, and returns a two-band image: `luc_emissions` and `peatland_conversion`.

Decodes the transition band into from/to code bands via the helpers in `utils/transitions.py`, then computes LUC emissions via chained `.where()` expressions keyed on the decoded from/to pair — a single logical pass over the raster that handles all transition types simultaneously, no per-type loop. For each emissive transition, the value is the sum of applicable carbon stock deltas:

- **Forest → lower-carbon category**: AGB + BGB + DOM loss from forest stock, plus SOC delta weighted by the pixel's IPCC climate zone factor
- **Short vegetation → lower-carbon category**: IPCC grassland carbon stock loss, plus climate-weighted SOC delta
- **Non-emissive transitions** (to-higher-carbon or inert pairs like forest → water): zero

`peatland_conversion` adds the peatland transformation component on pixels where `is_peatland AND transition is emissive`, using the IPCC peatland conversion emissions factor.

###### `calculate_peatland_occupation(land_use_image, inputs) -> EEImage`

Returns a single-band image: annual peatland occupation (land-management) emissions for 2020. Masked to pixels that are both cropland (per GLAD 2020) and peatland. Value is the IPCC peatland cultivation emissions factor (`PEATLAND_EF_TCO2E_HA_YR`) multiplied by pixel area. Independent of whether a conversion transition occurred in the analysis window.

###### `calculate_allocated_emissions_2020(luc_epoch_bands, peatland_epoch_bands, occupation_band) -> EEImage`

Applies GHGP 20-year lookback weighting to produce two bands:

- `allocated_luc_emissions_2020`: weighted sum across the four `luc_emissions_{epoch}` bands
- `allocated_peatland_emissions_2020`: weighted sum across the four `peatland_conversion_{epoch}` bands, plus the `peatland_occupation_2020` annual value at full weight

Epoch weights follow the linear discount formula in the methodology, using the midpoint year of each epoch window relative to 2020.

##### summary_tables.py

`compute_region_tables(land_use_asset, emissions_asset, region, version, counties, force=False) -> dict`

Top-level entry point for region-scope table computation. Checks for cached `transitions` and `crops` table assets; if both exist, returns their paths. Otherwise builds and exports both via a single server-side `reduceRegions` call per table. Returns a dict of asset paths.

The aggregation FeatureCollection stays server-side end-to-end: `reduceRegions(...)` returns an `ee.FeatureCollection`, `.map(...)` chains attach derived columns, `.merge(...)` combines per-reducer outputs, and `Export.table.toAsset` writes the result. No `.getInfo()` round-trips during graph construction. Both `reduceRegions` calls pass `tileScale=4` to bound per-tile memory at CONUS scale.

###### `build_transitions_table(land_use_asset, emissions_asset, counties) -> FeatureCollection`

Runs one `reduceRegions` per (epoch, emissions-type) combination over `counties`, plus one for peatland occupation. Grain: `(county_fips, epoch_transition, emissions_type)`. Per-reducer outputs are merged into a single `FeatureCollection` server-side. No crop grouping — all transitions are captured regardless of current land use.

###### `build_crops_table(land_use_asset, emissions_asset, counties) -> FeatureCollection`

For each epoch transition:

- masks to pixels that both transitioned and have a 2020 crop code in the `land_use` raster,
- groups by composite key (`type_code × 256 + crop_code`) so all (transition, crop) combinations resolve in a single `reduceRegions` call,
- sums emissions, allocating each pixel to the crop grown on it, summed within county boundaries.

State-level NASS yields are embedded as an `ee.Dictionary` keyed by `f'{state_fips}|{crop_code}'` (~50 states × 3 crops is small enough to ship as a constant, avoiding an `ee.Join` against a separate asset). The dictionary is looked up via `.map()` over the FeatureCollection to attach `yield_kg_per_ha` and `yield_bu_per_acre` to each county row, producing one row per `(county_fips, crop_code)`. Emissions are divided by production to get final emissions factors.

### Publish

The Publish stage makes Transform outputs accessible to consumers. Currently this is BigQuery for downstream queries.

#### Entry point

`run_publish(gcp_project, region_name, version, force=False) -> PublishResult` in `publish.py` orchestrates all publish targets. Today this is a thin passthrough to two BigQuery export jobs (one for `transitions`, one for `crops`) — mildly overbuilt for the current scope — but maintaining symmetry with Extract and Transform gives `pipeline.py` a single handle per stage and provides a stable home for future publish targets (tile-serving, GCS exports, report generation, etc.) without further refactoring.

#### BigQuery export (`bigquery.py`)

Exports the `transitions` and `crops` tables to BigQuery for downstream SQL queries. Reads from the regional `transitions` and `crops` GEE table assets produced by the Transform stage. Both tables carry the same region/transform/publish version triplet so a given pipeline run produces a coherent pair.

### Utils

#### constants.py

Central home for cross-module constants: GEE asset IDs and expected versions for each input dataset, simplified land use category codes, IPCC emissions factor tables (climate zone × stock change), crop group definitions, and the dataset inventory consumed by `extract_all()`.

#### gee.py

Provides `initialize_gee(project)`, which authenticates (if credentials aren't already cached) and initializes the Earth Engine client against the high-volume endpoint (`earthengine-highvolume.googleapis.com`) with the given GCP project for billing and asset ownership. The high-volume endpoint is required for the parallel batch-export volume this pipeline generates.

Called once per process — by `pipeline.py` at the top of `run_pipeline()`, or by a test fixture when stage entry points (`extract_all`, `run_transform`, `run_publish`) are invoked directly. Stage entry points themselves assume the client is already initialized.

Also exposes the ingest-task primitives used by the extract stage: `start_ingestion_no_wait` and `wait_for_tasks` for tile-based fan-out (see § Extract), and `start_ingestion_and_wait` (a thin wrapper around the two) for single-asset extracts.

#### states.py

Provides `get_multi_state_boundary(state_fips_list)` helper, which returns `ee.Geometry` objects from the `TIGER/2018/States` asset.

#### transitions.py

Encoding and decoding helpers for land use transitions. Transitions are stored as a single uint8 value with the upper 4 bits holding `from_code` and the lower 4 bits holding `to_code`. `0x00` indicates no transition; otherwise the encoded value is `(from_code << 4) | to_code`. For example, forest (1) → cropland (5) encodes as `0x15` (decimal 21). This scheme supports up to 16 land use categories per side — comfortably above the current 9.

Provides two forms of each operation — scalar (`int → int`) for lookup-table construction and `.where()` matchers, and raster (`ee.Image → ee.Image`) for the GEE pipeline. The two share a bit layout by construction (both live in this single module).

Scalar:

- `encode_transition(from_code: int, to_code: int) -> int`: returns `(from_code << 4) | to_code`, or `0` when `from_code == to_code`.
- `decode_from(encoded: int) -> int`: returns `encoded >> 4`.
- `decode_to(encoded: int) -> int`: returns `encoded & 0xF`.

Raster:

- `encode_transition_image(from_img, to_img) -> EEImage`: packs two category bands via `from_img.leftShift(4).bitwiseOr(to_img).uint8()`, with `0` written where `from == to`.
- `decode_from_image(encoded) -> EEImage`: `encoded.rightShift(4)`.
- `decode_to_image(encoded) -> EEImage`: `encoded.bitwiseAnd(0xF)`.

#### asset_management.py

Reusable helpers for working with GEE asset IDs: `list_assets_matching(prefix)`, `parse_version_from_asset_id(asset_id)`, `delete_asset_safely(asset_id)`, and related utilities.

#### version.py

Exposes two stage-scoped version functions whose outputs are embedded in the cached asset names for each stage:

- `compute_transform_version()` — SHA derived from the git state of `transform/` plus `utils/`
- `compute_publish_version()` — SHA derived from the git state of `publish/` plus `utils/`

Extract-stage assets are versioned by their upstream dataset version only (see the Extract section) and do not call into this module.

For each function, the resulting version string has two forms:

- **Clean** (working tree matches HEAD for the hashed files): `{HEAD SHA[:12]}` (e.g., `a0d76ac0aa12`)
- **Dirty** (working tree differs): `{HEAD SHA[:12]}-dirty-{sha256(diff)[:8]}` (e.g., `a0d76ac0aa12-dirty-3f2b8c01`)

## CLI

The `cli.py` entrypoint runs the full pipeline for a named canned region (e.g., `--region=delaware`)

## Testing

**Unit tests** validate pure logic — category mappings, version string formatting, yield calculations, transition encoding/decoding — with no external dependencies. Fast, runnable in CI without credentials.

**Integration tests** run against live GEE on a small test region (Delaware) and assert that outputs have the expected bands, non-zero values, and plausible magnitudes. A session-scoped `conftest.py` fixture handles `initialize_gee` once per run. Integration tests are marked `@pytest.mark.integration` and excluded from CI by default (`pytest -m "not integration"`); they run on demand locally or in manual pre-merge validation.

### Selected basic invariants

Some simple invariants tested by the integration suite should include:

- **Non-negativity**: all emissions and area values are ≥ 0 at both pixel level and aggregated-row level. Zero-valued non-emissive transitions are exactly zero, not small negatives.
- **Allocated ≤ total**: for every row, `allocated_emissions_2020_tco2 ≤ total_emissions_tco2`. Follows logically from the GHGP 20-year lookback being a weighted subset of total epoch emissions.
- **Components sum to total**: per-category emissions components (forest + short-vegetation + SOC + peatland) sum to the total reported per row, within floating-point tolerance.
- **Epoch sensitivity**: the four per-epoch transition bands are not all equal — the pipeline is actually sensitive to when a transition occurred within the analysis window.

## Reproducibility

An additional benefit of the asset caching strategy described throughout the pipeline above is that it should usually be possible to recover the full history that produced any pipeline output. This is not a perfect guarantee, but it should be possible to at least get very close, most of the time.

### Input data

Depends on whether the input is GEE-native:

- **Non-GEE-native inputs** (Harris AGB, Huang BGB, NASS QuickStats, IPCC climate zones, GFW Peatlands): the original source files are archived in the GCS extract mirror (see "Source-data mirroring" above), regardless of whether the upstream publisher still hosts them.
- **GEE-native inputs** (GLAD GLCLUC, CDL, SoilGrids, TIGER): we do not archive a Watershed-side copy of these assets, but upstream stability is high.

### Code recovery

- **Extract-stage outputs** carry an upstream-dataset-version suffix (e.g. `harris_agb_conus_v2021`). File creation date can be cross-referenced to commit history if the logic to generate these assets from the archived GCS files appears to have changed relative to the current HEAD. But extract logic should be slow changing so this should be rare. 
- **Transform and Publish outputs** carry a commit SHA, provided the working tree was clean when the code was run. This allows recovery of the exact committed code. A `-dirty` suffix means the exact working-tree state is not recoverable from git alone, but it's at least possible to recover the last pre-run commit.

## Scale

The cost and speed results below are empirically measured outcomes for the current implementation at the "Limited" GEE subscription tier (the lowest cost tier, which therefore gets lowest priority on GEE's shared resource pool). We've benchmarked two single-state runs of differing sizes (Delaware, Iowa), one regional run (Great Plains = IA + NE + SD), and one full CONUS run.

Further pipeline optimization is likely possible.

### Cost

Compute and cost by stage for the four benchmark regions. This reflects `transform/` steps only — we consider `extract/` to be a well-amortized one-time cost, such that the dollar cost becomes fairly trivial, and the BigQuery `publish/` exports are also negligible (~0 EECU-hr / <1 min wall-clock for both tables on CONUS). Costs are stated at GEE's list rate of $.40 per EECU.


| Stage       | DE EECU-hr | DE $      | IA EECU-hr | IA $      | Great Plains EECU-hr | Great Plains $      | CONUS EECU-hr | CONUS $     |
| ----------- | ---------- | --------- | ---------- | --------- | ----------- | ---------- | ------------- | ----------- |
| land_use    | 0.030      | $0.012    | 0.70       | $0.28     | 2.49        | $1.00      | 46.14         | $18.46      |
| emissions   | 0.129      | $0.052    | 1.78       | $0.71     | 8.03        | $3.21      | 127.64        | $51.06      |
| transitions | 0.108      | $0.043    | 1.78       | $0.71     | 7.98        | $3.19      | 294.49        | $117.79     |
| crops       | 0.158      | $0.063    | 2.67       | $1.07     | 12.52       | $5.01      | 184.43        | $73.77      |
| **Total**   | **0.42**   | **$0.17** | **6.92**   | **$2.77** | **31.02**   | **$12.41** | **652.70**    | **$261.08** |


Region size and per-pixel cost, calculated two ways: per **bounding-box pixel** (what the pipeline actually streams over, including over-water area that the FIPS mask later drops cheaply), and per **polygon pixel** (real on-shore land area at 30 m):


| Region          | Bbox px | Polygon px | nEECU-hr / bbox px | nEECU-hr / polygon px |
| --------------- | ------- | ---------- | ------------------ | --------------------- |
| DE              | 17.9 M  | 7 M        | 24                 | 60                    |
| IA              | 250 M   | 161 M      | 28                 | 43                    |
| Great Plains    | 900 M   | 600 M      | 35                 | 52                    |
| CONUS           | 23 B    | 8.4 B      | 28                 | 78                    |


The smaller-region (DE → GP3) trend looked like a pretty steady ^1.10 power law in bbox pixels. CONUS broke that: per-bbox-pixel cost came in *lower* than GP3 (28 vs 35), even as per-polygon-pixel cost rose meaningfully (52 → 78). Both seem to reflect the same underlying fact — the CONUS bounding box is a much worse fit for the polygon than our 3 state Great Plains test region (bbox/polygon ratio 2.7× vs 1.5×). This chunk of the CONUS bbox sits over the Atlantic, Pacific, and Gulf and gets masked out by the within-pipeline `fips_mask` cheaply. Overall, cost-per-area is generally climbing modestly with region, with bbox pixels providing the slightly more reliable, but still imperfect, scaling predictor.

#### Global extrapolation

CONUS land area is ~7.6 M km² vs ~134 M km² of global ice-free land — a ~17.6× pixel-count ratio at a uniform 30 m grid (≈148 B polygon pixels). Bbox is harder to calculate, but let's assume we can maintain bbox/polygon ratios across whatever shards we decide to use for a global run. The U.S. is not particularly favorable in this regard, so that doesn't seem unduly optimistic.

Anchoring on the CONUS actual (652.7 EECU-hr / $261.08) and assuming roughly linear scaling, similar to the GP3 -> CONUS trend: $4,600. The power-law (^1.10) scale from CONUS is not much worse: 17.6^1.10 ≈ 23.5× → $6,100.

So plausible range **$4,500–$6,000** for a single global run.

### Speed

#### Extract

The extract stage is limited by GEE's per-project ingest concurrency cap, which is maxed out by the tile-based datasets (Harris AGB and the GFW Peatlands raster). Our concurrency cap is currently about ~20 simultaneous tasks on the Limited tier. With this concurrency, the tile-based datasets finish in ~30 minutes for full CONUS extract (which the pipeline performs even for regional runs). The single-asset extracts (Huang BGB, NASS, IPCC, county_fips) take 1-30 minutes each. The county_fips paint adds ~30 minutes one-time at first run and is then cache-hit forever. Total clean-cache extract stage is on the order of two hours for all assets.

#### Transform/Publish

The transform stage is effectively O(1), subject to GEE's willingness to allocate resources.

**Per-stage GEE wall-clock**


| Stage                               | Delaware   | Iowa        | Great Plains | CONUS                 |
| ----------------------------------- | ---------- | ----------- | ------------ | --------------------- |
| land_use                            | 4.2 min    | 23.9 min    | 17.8 min     | 130.6 min             |
| emissions                           | 3.7 min    | 11.7 min    | 24.4 min     | 144.3 min             |
| transitions (parallel w/crops)      | 0.9 min    | 4.1 min     | 3.6 min      | 66.3 min              |
| crops (parallel w/transitions)      | 0.9 min    | 3.1 min     | 2.6 min      | 22.5 min              |
| **GEE-side pipeline total**         | **~9 min** | **~40 min** | **~46 min**  | **~341 min (5.7 hr)** |

In our smaller-region runs, the Great Plains land_use stage actually ran faster than IA's despite 3× more pixels. But that parallelism saturated for us between the Great Plains and CONUS scale: CONUS was \~20× EECU and \~7× wall-clock vs. Great Plains. It's unclear whether this would work out the same on a second trial, or whether it's subject to the priority of other jobs in the GEE queue at runtime.

At the Limited tier, we also observed queue waits of 5-15 min per task, which were very significant for the smaller regions, but a smaller fraction of total wall-clock as exports get bigger.

#### Global extrapolation

With job-level parallelism saturating at approximately Great Plains scale, the CONUS → Global jump (\~21× pixels) likely lands in the 10× wall-clock range, even assuming we reintroduce some parallelism on our side by splitting the global run into multiple jobs. This comes out to ~2-3 days of run time. A higher service tier could potentially bring this back inside a day.
