# Cornerstone jdLUC

This repo contains an experimental methodology and data pipeline for estimating the land use change (LUC) related emissions associated with agricultural commodities. This methodology allocates LUC emissions to crops in proportion to their displacement of natural ecosystems, based on high-resolution satellite imagery. It's primarily intended for use in corporate GHG inventories, and follows what the new GHGP Land Sector and Removals Standard calls a "jurisdictional direct land use change" (jdLUC) calculation.

As a proof of concept, the initial version of this pipeline focuses on land use change emissions associated with the main row crops grown in the United States (corn, soy, and wheat). Although U.S. land use change emissions are relatively modest contributors to global totals, the U.S. agricultural sector is well studied and has strong data infrastructure, which makes it a good place to start testing methods.

## Why are we publishing this?

Land use change emissions are one of the key targets for scaled global emissions reductions over the next five years. But measurement of LUC emissions is much harder and more uncertain than energy sector emissions, requiring sophisticated analysis of remote sensing imagery and complex modeling of carbon stocks and flows.

Although there are a number of LUC datasets already available for corporate GHG inventories, the emissions factors they publish differ by factors of 5x or more, thanks to differing choices at various points in the complex LUC modeling chain. Some of the LUC methodologies are very well documented, others less so. But even with the best methodology papers, it's extremely challenging to trace the origin of emissions factor differences to specific methodology decisions, or to see which choices are causing the biggest swings.

Therefore, we've come to believe the best way for the corporate measurement ecosystem to get to credible and stable LUC numbers is to shift to open-source LUC models, at least for the base data cleaning, harmonization, and math. Runnable code is the clearest documentation and the strongest platform for collaboration.

The methodology and technical decisions in this repo are intended as a starting point for discussion and collaboration. The jdLUC space is early. But we thought the best way to get a good conversation going was to actually publish a working open-source implementation.

## Data access

Harmonized datasets and emissions layers:

```python
>>> import xarray
>>> harmonized = xarray.open_zarr("gs://cornerstone-luc/cornerstone-data/jdluc/conus-v2/harmonize.zarr", consolidated=False)
>>> emissions = xarray.open_zarr("gs://cornerstone-luc/cornerstone-data/jdluc/conus-v2/emissions.zarr", consolidated=False)
```

Emissions factors:

```python
>>> import pandas
>>> emission_factors = pandas.read_parquet("gs://cornerstone-luc/cornerstone-data/jdluc/conus-v2/emissions-factors.parquet")
```

The data is licensed [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/). Please follow the latest attribution guidance in ATTRIBUTION.md.

## Methodology and architecture

Once you're ready to look under the hood:

- `specs/methodology.md` describes the scientific methodology
- `specs/pipeline_tech_design.md` describes the pipeline's technical architecture

## Running and contributing

### Getting set up

You'll need [uv](https://docs.astral.sh/uv/getting-started/installation/) installed as the Python env manager, plus a GCP project with GCS access and a USDA NASS QuickStats API key.

```bash
# Copy the example env file and fill in your values.
cp .env.example .env
# Set INGEST_BUCKET_NAME, GCP_PROJECT, SCRATCH_BUCKET_NAME, and USDA_NASS_API_KEY in .env.

# Sync Python dependencies into the project venv.
uv sync

# Authenticate gcloud application-default credentials (for GCS access).
gcloud auth application-default login --project "${GCP_PROJECT}"
```

Obtain a NASS QuickStats API key at https://quickstats.nass.usda.gov/api and set it as `USDA_NASS_API_KEY` in `.env`.

### Running the pipeline

The pipeline runs as a sequence of per-stage entry points, parameterized by a tile set (`DELAWARE`, `CONUS`, `BAY_AREA`, `GFW`, or `WHOLE_WORLD`):

```bash
# 1. Ingest each source dataset (positional args; --concurrency / --overwrite optional).
uv run python -m jdluc.ingest <TILE_SET> <DATASET>

# 2. Harmonize ingested tiles onto the common grid.
uv run python jdluc/harmonize.py <TILE_SET>

# 3. Compute per-pixel land-conversion emissions (writes zarr).
uv run python jdluc/emissions.py <TILE_SET>

# 4. Build the emissions-factor table (Delaware example; --skip-glad-crop-filter optional).
uv run python jdluc/emissions_factors.py --admin-id USA008
```

### Tests

```bash
uv run pytest jdluc
```

### Linting

```bash
uv run pre-commit run
```

### Contributing

The repo is maintained by the Cornerstone Sustainability Data Initiative team. We welcome contributions via pull requests. For more open-ended discussion, feel free to open an issue.
