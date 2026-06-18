"""Roll up per-pixel emissions to per-(jurisdiction, crop) totals.

For each jurisdiction (World Bank admin id) and crop, clips the per-pixel emissions zarr
(`emissions.workflow`) to the jurisdiction polygon and masks to the crop's CDL codes —
restricted to GLAD 2020 cropland unless `--skip-glad-crop-filter` is set — then sums crop
area, peatland crop area, peatland-occupation emissions, and total emissions. Returns a
pandas.DataFrame indexed by (admin level, crop, jurisdiction), cached to GCS (`@gcs.cache`).

Example invocation:
  uv run python jdluc/attribution.py --admin-id USA008
"""

import argparse
import collections
import collections.abc
import dataclasses
import enum
import logging
import typing

import pandas
import xarray
from rioxarray.exceptions import NoDataInBounds

from jdluc import emissions, gcs, geo, harmonize, tiling
from jdluc.datasets import (
    gfw_global_peatlands,
    glad_glcluc,
    usda_nass_cdl,
    worldbank_jurisdictions,
)

logger = logging.getLogger(__name__)


def len_three_str(s: str) -> str:
    assert len(s) == 3, f"len({s=:s}) must be three"
    return s


def iter_tile_cluster_to_iso_a3s(
    iso_a3s: collections.abc.Sequence[str],
) -> collections.abc.Generator[tuple[set[str], set[str]]]:
    if len(iso_a3s) == 1 and iso_a3s[0] == "USA":
        logger.info("Short-circuiting USA -> CONUS")
        yield set(tiling.NAME_TO_TILE_SET[tiling.TileSetName.CONUS]), set(iso_a3s)
    else:
        logger.info("Determining tile_ids which cover each iso_a3")
        iso_a3_to_tile_ids = {
            iso_a3: worldbank_jurisdictions.get_ten_degree_tile_ids_for_admin_id(
                admin_id=iso_a3,
                admin_level=worldbank_jurisdictions.AdminLevel.NATIONAL.value,
            )
            for iso_a3 in iso_a3s
        }

        tile_clusters = tiling.get_tile_clusters(
            tile_ids={
                tile_id
                for tile_ids in iso_a3_to_tile_ids.values()
                for tile_id in tile_ids
            }
        )
        logger.info(f"Found {len(tile_clusters):d} connected clusters of tile_ids")

        for tile_cluster in tile_clusters:
            iso_a3_cluster = {
                iso_a3
                for iso_a3, tile_ids in iso_a3_to_tile_ids.items()
                if tile_cluster.issuperset(tile_ids)
            }
            assert iso_a3_cluster
            logger.info(
                f"Cluster covers {len(iso_a3_cluster):d} ISO_A3's with {len(tile_cluster):d} tile_ids"
            )
            yield tile_cluster, iso_a3_cluster


@enum.unique
class Crop(enum.Enum):
    CORN = (usda_nass_cdl.CropClass.CORN,)
    SOYBEANS = (usda_nass_cdl.CropClass.SOYBEANS,)
    WHEAT = (
        usda_nass_cdl.CropClass.DURUM_WHEAT,
        usda_nass_cdl.CropClass.SPRING_WHEAT,
        usda_nass_cdl.CropClass.WINTER_WHEAT,
    )


@dataclasses.dataclass
class JurisdictionalCropEmission:
    admin_id: str
    admin_level: str
    crop_hectares: float
    crop_name: str
    jurisdiction_name: str
    peatland_crop_hectares: float
    peatland_occupation_emissions: float
    total_emissions: float

    @classmethod
    def from_dset(
        cls,
        admin_id: str,
        admin_level: worldbank_jurisdictions.AdminLevel,
        crop: Crop,
        dset: xarray.Dataset,
        jurisdiction_name: str,
        skip_glad_crop_filter: bool,
    ) -> typing.Self:
        logger.info(f"Populating emissions for {admin_id=:s}/{crop.name=:s}")

        crop_class = dset[usda_nass_cdl.DATASET.fully_qualified_band_name]
        if not skip_glad_crop_filter:
            glad_class = dset[f"land-class:{max(glad_glcluc.YEARS):d}"]
            crop_class = crop_class.where(
                glad_class == glad_glcluc.LandClass.CROPLAND.value
            )
        crop_mask = crop_class.isin([value.value for value in crop.value])
        emissions_per_hectare = dset["emissions-per-hectare:tco2e-per-ha"]
        hectares_per_pixel = dset["hectares-per-pixel:ha"]
        is_peatland = dset[gfw_global_peatlands.DATASET.fully_qualified_band_name]
        peatland_occupation_per_hectare = dset["peatland-occupation:tco2e-per-ha"]
        crop_hectares = hectares_per_pixel.where(crop_mask)
        # Compute totals in a batch for a performance gain
        totals = (
            xarray.Dataset(
                {
                    "crop_hectares": crop_hectares,
                    "peatland_crop_hectares": crop_hectares.where(is_peatland == 1),
                    "peatland_occupation_emissions": (
                        peatland_occupation_per_hectare * hectares_per_pixel
                    ).where(crop_mask),
                    "total_emissions": (
                        emissions_per_hectare * hectares_per_pixel
                    ).where(crop_mask),
                }
            )
            .sum()
            .compute()
        )
        return cls(
            admin_id=admin_id,
            admin_level=admin_level.name,
            crop_hectares=float(totals["crop_hectares"]),
            crop_name=crop.name,
            jurisdiction_name=jurisdiction_name,
            peatland_crop_hectares=float(totals["peatland_crop_hectares"]),
            peatland_occupation_emissions=float(
                totals["peatland_occupation_emissions"]
            ),
            total_emissions=float(totals["total_emissions"]),
        )

    @classmethod
    def from_constituents(
        cls,
        admin_id: str,
        admin_level: worldbank_jurisdictions.AdminLevel,
        constituents: list[JurisdictionalCropEmission],
        jurisdiction_name: str,
    ) -> typing.Self:
        def get_sum(attr: str) -> float:
            return sum(getattr(constituent, attr) for constituent in constituents)

        a_constituent = next(iter(constituents))
        return cls(
            admin_id=admin_id,
            admin_level=admin_level.name,
            crop_hectares=get_sum("crop_hectares"),
            crop_name=a_constituent.crop_name,
            jurisdiction_name=jurisdiction_name,
            peatland_crop_hectares=get_sum("peatland_crop_hectares"),
            peatland_occupation_emissions=get_sum("peatland_occupation_emissions"),
            total_emissions=get_sum("total_emissions"),
        )


@gcs.cache(version=1)
def workflow_for_tile_ids(
    crops: tuple[Crop, ...],
    iso_a3: str,
    skip_glad_crop_filter: bool,
    tile_ids: tuple[str, ...],
) -> pandas.DataFrame:
    logger.info("Calling harmonize and emissions workflows and merging into one dset")
    merged = xarray.merge(
        [
            harmonize.workflow(
                dataset_names=harmonize.DATASET_NAMES, tile_ids=tile_ids
            ),
            emissions.workflow(tile_ids=tile_ids),
        ],
        combine_attrs="identical",
        compat="identical",
        join="exact",
    )

    def it() -> collections.abc.Generator[JurisdictionalCropEmission]:
        for (
            admin_id,
            admin_name,
            geometry,
        ) in worldbank_jurisdictions.iter_province_for_iso_a3(iso_a3=iso_a3):
            logger.info(f"Clipping to provincial geometry for {admin_id=:s}")
            try:
                clipped = geo.clip_dset(
                    dset=merged,
                    geometry=geometry,
                )
            except NoDataInBounds as exc:
                logger.warning(repr(exc))
            else:
                for crop in crops:
                    yield JurisdictionalCropEmission.from_dset(
                        admin_id=str(admin_id),
                        admin_level=worldbank_jurisdictions.AdminLevel.PROVINCIAL,
                        crop=crop,
                        dset=clipped,
                        jurisdiction_name=admin_name,
                        skip_glad_crop_filter=skip_glad_crop_filter,
                    )

    provincials = list(it())

    national_name = str(
        worldbank_jurisdictions.get_jurisdiction_for_admin_level(
            admin_level=worldbank_jurisdictions.AdminLevel.NATIONAL
        ).loc[iso_a3]["name"]
    )
    logger.info(f"{iso_a3=:s} -> {national_name:s}")

    logger.info("Merging provincial emissions into national emissions")
    provincials.append(
        JurisdictionalCropEmission.from_constituents(
            admin_id=iso_a3,
            admin_level=worldbank_jurisdictions.AdminLevel.NATIONAL,
            constituents=provincials,
            jurisdiction_name=national_name,
        )
    )

    return pandas.DataFrame.from_records(
        data=map(dataclasses.asdict, provincials)
    ).set_index(["admin_level", "crop_name", "jurisdiction_name"])


def workflow(
    crops: tuple[Crop, ...],
    iso_a3s: collections.abc.Sequence[str],
    skip_glad_crop_filter: bool,
) -> pandas.DataFrame:
    dfs = (
        workflow_for_tile_ids(
            crops=crops,
            iso_a3=iso_a3,
            skip_glad_crop_filter=skip_glad_crop_filter,
            tile_ids=tuple(sorted(tile_cluster)),
        )
        for tile_cluster, iso_a3_cluster in iter_tile_cluster_to_iso_a3s(
            iso_a3s=iso_a3s
        )
        for iso_a3 in iso_a3_cluster
    )
    return pandas.concat(list(dfs)).sort_index()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso_a3s", nargs=argparse.ZERO_OR_MORE, type=len_three_str)
    parser.add_argument("--skip-glad-crop-filter", action="store_true")
    args = parser.parse_args()

    df = workflow(
        crops=tuple(Crop),
        iso_a3s=args.iso_a3s or ["USA"],
        skip_glad_crop_filter=args.skip_glad_crop_filter,
    )
    print(df.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
