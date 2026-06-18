"""Build the per-(jurisdiction, crop) emissions-factor table.

Joins the per-(admin, crop) attribution rollup (`attribution.workflow`) to NASS QuickStats
yields (4-year mean) and derives each crop's total production, emissions factor (kgCO2e per
kg), and peatland-occupation fraction. Returns a pandas.DataFrame indexed by (admin level,
crop, jurisdiction), cached to GCS (`@gcs.cache`).

Example invocation:
  uv run python jdluc/emissions_factors.py --admin-id USA008
"""

import argparse
import logging

import pandas

from jdluc import attribution, config, gcs
from jdluc.datasets import usda_nass_quickstats

logger = logging.getLogger(__name__)


NASS_YIELD_YEARS = (2017, 2018, 2019, 2020)
KG_PER_TONNE = 1000


def load_yields() -> pandas.DataFrame:
    gcs_uri = gcs.get_uri_from_bucket_name_prefix(
        bucket_name=config.Config.from_dot_env().ingest_bucket_name,
        prefix=usda_nass_quickstats.DATASET.get_gcs_prefix(tile_id="world"),
    )
    logger.info(f"Loading yields from {gcs_uri=:s}")
    return pandas.read_parquet(path=gcs_uri)


def merge_emissions_and_yields(
    emissions: pandas.DataFrame,
    raw_yields: pandas.DataFrame,
) -> pandas.DataFrame:
    reduced_yields = (
        raw_yields[raw_yields.index.get_level_values("year").isin(NASS_YIELD_YEARS)]
        .groupby(level=["admin_id", "crop_name"])["yield_kg_per_ha"]
        .mean()
        .reset_index()
    )

    flat = emissions.reset_index()
    merged = flat.merge(reduced_yields, how="left", on=["admin_id", "crop_name"])
    unmatched = int(merged["yield_kg_per_ha"].isna().sum())
    if unmatched:
        logger.warning(f"{unmatched:d} (admin, crop) row(s) had no matching NASS yield")

    production_kg = merged["crop_hectares"] * merged["yield_kg_per_ha"]
    merged["total_production_kg"] = production_kg
    merged["emissions_factor_kgco2e_per_kg"] = (
        (merged["total_emissions"] * KG_PER_TONNE)
        .div(production_kg)
        .where(production_kg > 0)
    )

    merged["peatland_occupation_fraction"] = (
        merged["peatland_occupation_emissions"]
        .div(merged["total_emissions"])
        .where(merged["total_emissions"] > 0)
    )

    return merged.set_index(
        ["admin_level", "crop_name", "jurisdiction_name"]
    ).sort_index()


@gcs.cache(version=1)
def workflow(
    crops: tuple[attribution.Crop, ...],
    iso_a3s: tuple[str, ...],
    skip_glad_crop_filter: bool,
) -> pandas.DataFrame:
    return merge_emissions_and_yields(
        emissions=attribution.workflow(
            crops=crops,
            iso_a3s=iso_a3s,
            skip_glad_crop_filter=skip_glad_crop_filter,
        ),
        raw_yields=load_yields(),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "iso_a3s", nargs=argparse.ZERO_OR_MORE, type=attribution.len_three_str
    )
    parser.add_argument("--skip-glad-crop-filter", action="store_true")
    args = parser.parse_args()

    df = workflow(
        crops=tuple(attribution.Crop),
        iso_a3s=tuple(sorted(args.iso_a3s or ["USA"])),
        skip_glad_crop_filter=args.skip_glad_crop_filter,
    )
    print(df.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
