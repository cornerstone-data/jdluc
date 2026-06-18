# Cornerstone jdLUC: pipeline technical design

This document describes the technical design for the data pipeline that implements the jdLUC methodology described in methodology.md.

The pipeline is a linear chain of five stages, each consuming the previous stage's output:

- **Ingest** — download every external source dataset into GCS as tiled GeoTIFFs (raster) or FlatGeobuf (vector).
- **Harmonize** — mosaic the ingested tiles and warp them onto a single common grid, yielding one `xarray.Dataset`.
- **Emissions** — compute per-pixel emissions as a lazy dask/xarray graph and persist to zarr.
- **Attribution** — clip per-pixel emissions to jurisdiction polygons and crop masks, rolling up to per-(jurisdiction, crop) totals.
- **Emissions factors** — join the attribution rollups to NASS yields to produce the final emissions-factor table.

There is no Earth Engine and no BigQuery: all raster work runs locally (or on any dask-capable host) over xarray/rasterio, and all outputs land in GCS as zarr and parquet.

## Platform

Raster computation is expressed as lazy `xarray.Dataset` / `dask.array` graphs built with rioxarray and rasterio; GDAL VRTs do the mosaicking and grid warping. Nothing materializes until a stage writes its result, at which point dask streams the computation tile-by-tile into the output store.

Configuration comes from a `.env` file, loaded once via `config.Config.from_dot_env()`:

- `INGEST_BUCKET_NAME` — GCS bucket for ingested source tiles.
- `SCRATCH_BUCKET_NAME` — GCS bucket for cached stage outputs (zarr / parquet).
- `GCP_PROJECT` — project for GCS billing and access.
- `USDA_NASS_API_KEY` — key for the NASS QuickStats yields API.

GCS access uses application-default credentials (`gcloud auth application-default login`).

## Geospatial grid

All rasters are harmonized onto the GLAD GLCLUC native grid: global `EPSG:4326` at 0.00025° (~30 m). `harmonize.Grid` constructs this grid from the requested tile set, with `GLAD_TILE_RESOLUTION = XY(40_000, 40_000)` pixels per 10° tile (i.e. 4,000 px/degree → 0.00025°).

Harmonization happens in two GDAL steps per source band (`harmonize.get_vrt_for_dataset_band_tile_ids`):

1. **Mosaic** — the ingested 10° tiles are assembled into a `.mosaic.vrt` whose `SimpleSource` entries place each tile at its computed offset on the common grid. Whole-world datasets (e.g. IPCC climate zones) are placed via a single windowed source instead of per-tile.
2. **Warp** — a `rasterio.vrt.WarpedVRT` pins the mosaic to `EPSG:4326` at the grid's transform/resolution, written out as a `.warped.vrt`.

Resampling is a per-dataset property (`RasterDataset.resampling`): nearest-neighbor for categorical layers (GLAD land classes, CDL, peatland mask), bilinear for continuous layers (SoilGrids, Huang BGB). `harmonize.get_dset_for_output` opens the warped VRTs with rioxarray and assembles them into one `xarray.Dataset`, one variable per fully-qualified source band (`{source}:{product}:{band}`), with dtype and no-data unified via `utils.unify_dtype_and_no_data`.

## Caching and versioning

Stage outputs are cached in GCS by the `@gcs.cache(version: int)` decorator. The cache key is a SHA1 over the function's module path, qualified name, the integer `version`, and the call arguments; the decorator picks a serializer by the function's return annotation:

- `xarray.Dataset` → **`ZarrCacher`** (writes a `.zarr` store under the scratch bucket).
- `pandas.DataFrame` → **`ParquetCacher`**.

On a cache hit the stage deserializes the stored result; on a miss it runs and serializes. Bumping the `version` int (or changing any argument) invalidates the entry. Current versions: `harmonize.workflow` v0, `emissions.workflow` v2, `attribution.workflow` v1, `emissions_factors.workflow` v0.

Provenance is tracked at the ingest layer instead: each ingested GeoTIFF carries metadata tags (`watershed-data-version` = the dataset's declared version, `watershed-processing-version` = git SHA, `watershed-processing-time`, `watershed-source-name`, `watershed-product-name`, `watershed-remote-url`), and tiles are stored under a deterministic prefix `{source}/{product}/{version}/{partitioning}/{tile_id}` so a given source version is addressable and re-fetchable.

## Architecture

Modules live flat under `jdluc/`:

- **`ingest.py`** — ingest-stage entry point (`python -m jdluc.ingest <TILE_SET> <DATASET>`).
- **`datasets/`** — one module per source dataset, each exposing a `RasterDataset`, `VectorDataset`, or `TabularDataset` (defined in `datasets/base.py`) plus any source-specific download/preprocess logic.
- **`harmonize.py`** — mosaic + warp into a common-grid `xarray.Dataset`.
- **`emissions.py`** — per-pixel emissions → zarr.
- **`attribution.py`** — per-(jurisdiction, crop) rollups.
- **`emissions_factors.py`** — final EF table.
- **`config.py`, `gcs.py`, `tiling.py`, `utils.py`** — shared infrastructure (config; GCS I/O + content-addressed cache via `@gcs.cache`; tile sets / partitionings / tile-id helpers; VRT / zarr / misc helpers). The dataset base classes (`RasterDataset` / `VectorDataset` / `TabularDataset`) and `AssetType` live in `datasets/base.py`.

### Layering enforcement

The stages form a strict dependency chain enforced by an [import-linter](https://import-linter.readthedocs.io/) `layers` contract ("ETL pipeline direction" in `pyproject.toml`), highest to lowest:

```
emissions_factors → attribution → emissions → harmonize → ingest
```

Higher layers may import lower; never the reverse. The shared infrastructure modules (`config.py`, `gcs.py`, `tiling.py`, `utils.py`) and the `datasets/` modules sit outside the chain and may be imported anywhere.

## Outputs

### Per-pixel emissions (zarr)

`emissions.workflow` returns an `xarray.Dataset` (cached as zarr). Variable names carry unit suffixes via `merge_name_units` (e.g. `emissions-per-hectare:tco2e-per-ha`):

| Variable(s) | Description |
|---|---|
| `land-class:{year}` | Per-year `LandClass` code (2000, 2005, 2010, 2015, 2020) |
| `aboveground-carbon`, `belowground-carbon`, `dead-organic-matter-carbon`, `grassland-carbon` | Carbon-stock layers (`:tcarbon-per-ha`) |
| `vegetation-carbon:{year}` | Per-year total vegetation carbon |
| `vegetation-emissions:{b}-{a}`, `soil-emissions:{b}-{a}`, `emissions:{b}-{a}` | Per-epoch-transition fluxes (`tco2e-per-ha`) |
| `peatland-occupation` | Annual peatland occupation (land-management) emissions |
| `emissions-per-hectare` | 20-year linearly-discounted LUC + peatland occupation |
| `hectares-per-pixel` | Pixel area |
| `emissions` | `emissions-per-hectare × hectares-per-pixel` (tCO2e) |

### Per-(jurisdiction, crop) emissions factors (parquet)

`emissions_factors.workflow` returns a `pandas.DataFrame` indexed by `(admin_level, crop_name, jurisdiction_name)`:

| Column | Description |
|---|---|
| `crop_hectares` | Crop area in the jurisdiction |
| `peatland_crop_hectares` | Crop area on peatland |
| `peatland_occupation_emissions` | Annual peatland-occupation emissions on crop pixels |
| `total_emissions` | Total allocated emissions (tCO2e) |
| `total_production_kg` | `crop_hectares × NASS yield (kg/ha)` |
| `emissions_factor_kgco2e_per_kg` | `total_emissions × 1000 / total_production_kg` |
| `peatland_occupation_fraction` | `peatland_occupation_emissions / total_emissions` |

The `attribution.workflow` rollup (same index, columns `crop_hectares`, `peatland_crop_hectares`, `peatland_occupation_emissions`, `total_emissions`) is the EF table's direct input. The legacy `transitions` table has no equivalent — per-transition detail now lives in the per-pixel zarr variables.

## Stage details

### Ingest (`ingest.py`, `datasets/`)

`python -m jdluc.ingest <TILE_SET> <DATASET>` (positional; `--concurrency`, `--overwrite` optional). `workflow()` validates that every tile in the set is valid for the dataset's partitioning, then fans the per-tile `dataset.ingest_a_tile(...)` calls out over a thread pool, returning a `{tile_id: result | Exception}` map (per-tile failures are isolated; the process exit code is the failure count).

Each `datasets/` module declares a `RasterDataset` or `VectorDataset`:

- **`RasterDataset`** downloads its source, writes a GeoTIFF tagged with provenance metadata, and uploads to the ingest bucket. Bands are namespaced `{source}:{product}:{band}`.
- **`VectorDataset`** stages a vector file, converts it to FlatGeobuf (`utils.convert_vector_to_flatgeobuf`, projecting the configured id/name columns), and uploads.

Tile sets (`tiling.TileSetName`): `BAY_AREA`, `CONUS`, `DELAWARE`, `GFW`, `WHOLE_WORLD`. Partitionings (`tiling.Partitioning`): `TEN_DEGREE_TILE` (most rasters), `WHOLE_WORLD` (IPCC climate zones), and the vector partitioning for World Bank boundaries.

#### Source dataset inventory

| Dataset (`DatasetName`) | Source | Partitioning | Kind |
|---|---|---|---|
| `GLAD_GLCLUC` | GLAD/Hansen GeoTIFFs (`…/GLCLU2000-2020/v2/{year}/{tile}.tif`) | 10° tile | raster |
| `USDA_NASS_CDL` | USDA Cropland Data Layer | 10° tile | raster |
| `SOILGRIDS_OCS` | ISRIC SoilGrids 0–30 cm OCS (WCS) | 10° tile | raster |
| `GFW_HARRIS_AGB` | GFW data-api (WHRC AGB 2000 v1.4) | 10° tile | raster |
| `HUANG_BGB` | Figshare (doi:10.6084/m9.figshare.12199637) | 10° tile | raster |
| `IPCC_CLIMATE_ZONES` | Zenodo (doi:10.5281/zenodo.7303808) | whole-world | raster |
| `GFW_GLOBAL_PEATLANDS` | GFW data-api (`gfw_peatlands` v20230315) | 10° tile | raster |
| `USDA_NASS_QUICKSTATS` | USDA NASS QuickStats API (yields) | whole-world | tabular |
| `WORLD_BANK_ADMIN_0/1/2` | World Bank Official Boundaries (`.gpkg`, CC BY 4.0) | vector | vector |

### Harmonize (`harmonize.py`)

`python jdluc/harmonize.py <TILE_SET>`. `workflow(dataset_names, tile_ids)` (`@gcs.cache(version=0)`) builds the common `Grid` from the tile set, emits one warped VRT per source band (see [Geospatial grid](#geospatial-grid)), and returns the assembled `xarray.Dataset`. `DATASET_NAMES` is the fixed set of seven raster datasets, asserted to equal every `RasterDataset` in the registry.

### Emissions (`emissions.py`)

`python jdluc/emissions.py <TILE_SET>`. `workflow(tile_ids)` (`@gcs.cache(version=2)`) calls `harmonize.workflow` then:

1. **Land classes** — `get_land_class` maps each year's GLAD band into the 7-member `LandClass` enum (disjoint value ranges summed without collision).
2. **Vegetation carbon & emissions** — above-ground carbon (Harris AGB × CF), below-ground carbon (Huang BGB with R:S fallback), dead organic matter (climate-zone factors), and grassland carbon; differenced across consecutive epochs into `vegetation-emissions:{b}-{a}`.
3. **Soil emissions** — IPCC stock-change `F_LU` deltas keyed on land class + climate zone, plus peatland transformation, into `soil-emissions:{b}-{a}`.
4. **Peatland occupation** — annual land-management emissions on cultivated peatland pixels.
5. **Allocation & area** — sum vegetation + soil per epoch, apply the GHGP linear-discount weights (`EPOCH_TO_LINEAR_DISCOUNT_WEIGHT`), add peatland occupation, and scale by `get_hectares_per_pixel`.

Returns the [per-pixel zarr dataset](#per-pixel-emissions-zarr).

### Attribution (`attribution.py`)

`python jdluc/attribution.py --admin-id USA008 [--skip-glad-crop-filter]` (`--admin-id` repeatable; defaults to Delaware `USA008`). `workflow(admin_ids, crops, skip_glad_crop_filter)` (`@gcs.cache(version=1)`) returns a `pandas.DataFrame` indexed by `(admin_level, crop_name, jurisdiction_name)`.

For each (admin id, crop): the per-pixel emissions are clipped to the jurisdiction polygon from the matching World Bank `VectorDataset` (`rio.clip`, `drop=False`), masked to the crop's CDL `CropClass` codes (corn = 1, soy = 5, wheat = 22/23/24 via the `Crop` enum), and — unless `--skip-glad-crop-filter` — further restricted to pixels where `land-class:2020 == CROPLAND`. The masked emissions and areas are summed into a `JurisdictionalCropEmission`. World Bank admin levels (`AdminLevel`: `NATIONAL`=0, `PROVINCIAL`=1, `DISTRICT`=2) replace TIGER county FIPS; US states are the provincial level (`USA001`…`USA051`).

### Emissions factors (`emissions_factors.py`)

`python jdluc/emissions_factors.py --admin-id USA008`. `workflow(admin_ids, crops, skip_glad_crop_filter)` (`@gcs.cache(version=0)`) takes the `attribution.workflow` rollup, joins it to NASS yields — loaded by `load_yields()` from the ingested `USDA_NASS_QUICKSTATS` parquet — on `(admin_id, crop_name)` using the 4-year (`NASS_YIELD_YEARS` = 2017–2020) mean yield, and computes `total_production_kg`, `emissions_factor_kgco2e_per_kg`, and `peatland_occupation_fraction`. Returns the [EF table](#per-jurisdiction-crop-emissions-factors-parquet).

### Yields (`datasets/usda_nass_quickstats.py`)

Yields are not a standalone stage — they are ingested like any other source. The `USDA_NASS_QUICKSTATS` `TabularDataset`'s `get_records_for_tile_id` pulls state-level YIELD records (corn, soybeans, wheat) from the NASS QuickStats API (`https://quickstats.nass.usda.gov/api/api_GET/`, key from `.env`), converts bu/acre → kg/ha using 7 CFR 810 bushel weights and `HA_PER_ACRE = 0.40468564`, and keys results by World Bank admin id via `STATE_FIPS_TO_ADMIN_ID`. The ingested parquet is read back by `emissions_factors.load_yields()`.

## Testing

Tests live in `jdluc/__tests__/` (`test_utils.py`, `test_datasets.py`, `test_harmonize.py`, `test_emissions.py`). Unit tests cover pure logic (land-class mapping, grid math, chunking, transition/epoch weighting) with no network.

## Reproducibility

Two mechanisms make outputs recoverable:

- **Ingested sources** are mirrored in the ingest bucket under versioned, provenance-tagged prefixes, so a given source version is re-readable even if the upstream publisher changes.
- **Stage outputs** are content-addressed by `gcs.cache` (module + qualname + version + args), so an output's cache key identifies the exact code path and inputs that produced it. Bumping a stage `version` or any input forces recomputation.

## Scale

The pipeline runs on a single dask-capable host — no distributed cluster. The benchmark below is an **M4 MacBook (48 GB RAM)** with `NUMBER_OF_DASK_WORKERS=12` (threaded scheduler, `scheduler="threads"`), ingest `--concurrency=4`, and the ~4 GiB chunk target from `geo.get_chunk_size` (`max_bytes_per_chunk = 1<<32`). Figures are for the **CONUS** tile set, the largest of the three run (`DELAWARE` and `BAY_AREA` were also run for iteration but are far smaller/faster and not separately benchmarked).

| Stage | Wall-clock (CONUS) | Cached output | Output size |
|---|---|---|---|
| Ingest | ~30 m | source tiles (GeoTIFF) in ingest bucket | 36.4 GiB |
| Harmonize | ~40 m | common-grid `xarray.Dataset` (zarr) | 1.3 TiB |
| Emissions | ~2 h | per-pixel emissions (zarr) | 3.7 TiB |
| Attribution | ~1 h | rollup (parquet) | < 1 MiB |
| Emissions factors | ~5 s | EF table (parquet) | < 1 MiB |

End-to-end ≈ **5 h** for CONUS. **Harmonize, emissions, and attribution are the cost centers**. Storage is dominated by the two zarr stores — the harmonized dataset (1.3 TiB) and the per-pixel emissions (3.7 TiB); attribution is compute-heavy despite its kilobyte-scale parquet output, because it streams the full per-pixel zarr through the clip/mask/rollup, and the final emissions-factors join is effectively instantaneous. The 48 GB host comfortably absorbed 12 threaded workers at the chunk target.
