# Cornerstone jdLUC

This repo contains an experimental methodology and data pipeline for estimating the land use change (LUC) related emissions associated with agricultural commodities. This methodology allocates LUC emissions to crops in proportion to their displacement of natural ecosystems, based on high-resolution satellite imagery. It's primarily intended for use in corporate GHG inventories, and follows what the new GHGP Land Sector and Removals Standard calls a "jurisdictional direct land use change" (jdLUC) calculation.

As a proof of concept, the initial version of this pipeline focuses on land use change emissions associated with the main row crops grown in the United States (corn, soy, and wheat). Although U.S. land use change emissions are relatively modest contributors to global totals, the U.S. agricultural sector is well studied and has strong data infrastructure, which makes it a good place to start testing methods.

## Why are we publishing this?

Land use change emissions are one of the key targets for scaled global emissions reductions over the next five years. But measurement of LUC emissions is much harder and more uncertain than energy sector emissions, requiring sophisticated analysis of remote sensing imagery and complex modeling of carbon stocks and flows.

Although there are a number of LUC datasets already available for corporate GHG inventories, the emissions factors they publish differ by factors of 5x or more, thanks to differing choices at various points in the complex LUC modeling chain. Some of the LUC methodologies are very well documented, others less so. But even with the best methodology papers, it's extremely challenging to trace the origin of emissions factor differences to specific methodology decisions, or to see which choices are causing the biggest swings.

Therefore, we've come to believe the best way for the corporate measurement ecosystem to get to credible and stable LUC numbers is to shift to open-source LUC models, at least for the base data cleaning, harmonization, and math. Runnable code is the clearest documentation and the strongest platform for collaboration.

The methodology and technical decisions in this repo are intended as a starting point for discussion and collaboration. The jdLUC space is early. But we thought the best way to get a good conversation going was to actually publish a working open-source implementation.

## Data access

Emissions factors and summary statistics:

```shell
curl -O https://storage.googleapis.com/cornerstone-luc/cornerstone-data/jdluc/conus/tables/crops.csv
curl -O https://storage.googleapis.com/cornerstone-luc/cornerstone-data/jdluc/conus/tables/transitions.csv
```

Simple raster visualizations:

- [Land use transitions, crop, and peat masks](https://cornerstone-data.projects.earthengine.app/view/jdluc-landuse-conus-20260513) 
- [Pixel-level emissions estimates](https://cornerstone-data.projects.earthengine.app/view/jdluc-emissions-conus-20260513)

The data is licensed [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/). Please follow the latest attribution guidance in ATTRIBUTION.md.

## Methodology and architecture

Once you're ready to look under the hood:

- `specs/methodology.md` describes the scientific methodology
- `specs/pipeline_tech_design.md` describes the pipeline's technical architecture

## Running and contributing

### Getting set up

You'll need access to a GCP project with the Earth Engine and BigQuery APIs enabled. You'll also need [uv](https://docs.astral.sh/uv/getting-started/installation/) installed as the Python env manager.

```bash
# Set GCP_PROJECT to your GCP project ID (the same value you set in
# jdluc/utils/constants.py).
export GCP_PROJECT=cornerstone-data

# Sync Python dependencies into the project venv.
uv sync

# Authenticate gcloud application-default credentials.
gcloud auth application-default login --project "${GCP_PROJECT}"

# Authenticate Earth Engine and point it at the same project.
uv run earthengine authenticate
uv run earthengine set_project "${GCP_PROJECT}"
```

### Running the pipeline

First, set GCP project info by editing the deployment configuration block at the top of `jdluc/utils/constants.py`.

Second, create the project folder in Google Earth Engine:

```shell
uv run earthengine --project=${GCP_PROJECT} create folder projects/${GCP_PROJECT}/assets/cornerstone-luc
```

Finally:

```bash
# Default: Delaware (single state, smallest test region)
uv run python jdluc/cli.py -v

# Multi-state (Iowa + Nebraska + South Dakota)
uv run python jdluc/cli.py --region great_plains_test -v

# Full CONUS (longer — full 48 states + DC)
uv run python jdluc/cli.py --region conus -v

# Force re-export even when an asset already exists at the target version
uv run python jdluc/cli.py --force -v
```

### Tests

```bash
uv run pytest jdluc -m "not integration"   # unit tests; offline
uv run pytest jdluc -m integration         # GEE / BQ integration
```

### Linting

```bash
uv run pre-commit run
```

### Contributing

The repo is maintained by the Cornerstone Sustainability Data Initiative team. We welcome contributions via pull requests. For more open-ended discussion, feel free to open an issue.
