import collections
import collections.abc
import enum
import logging

import pandas
import rasterio.enums
import xarray

from jdluc import emit, geo, harmonize, storage, tiling, utils
from jdluc.datasets import (
    DatasetName,
    base,
    gfw_global_peatlands,
    glad_glcluc,
    ifpri_mapspam,
    worldbank_jurisdictions,
)

logger = logging.getLogger(__name__)


@enum.unique
class Crop(enum.StrEnum):
    BARLEY = ifpri_mapspam.Crop2000.BARL.name
    BEAN = ifpri_mapspam.Crop2000.BEAN.name
    CASSAVE = ifpri_mapspam.Crop2000.CASS.name
    COTTON = ifpri_mapspam.Crop2000.COTT.name
    GROUNDNUT = ifpri_mapspam.Crop2000.GROU.name
    MAIZE = ifpri_mapspam.Crop2000.MAIZ.name
    POTATO = ifpri_mapspam.Crop2000.POTA.name
    RICE = ifpri_mapspam.Crop2000.RICE.name
    SORGHUM = ifpri_mapspam.Crop2000.SORG.name
    SOYBEAN = ifpri_mapspam.Crop2000.SOYB.name
    SUGARBEET = ifpri_mapspam.Crop2000.SUGB.name
    SUGARCANE = ifpri_mapspam.Crop2000.SUGC.name
    WHEAT = ifpri_mapspam.Crop2000.WHEA.name
    # Crops which need to be decomposed for 2000
    BANANA = ifpri_mapspam.Crop2005.BANA.name
    PLANTAIN = ifpri_mapspam.Crop2005.PLNT.name
    ARABICA_COFFEE = ifpri_mapspam.Crop2005.ACOF.name
    ROBUSTA_COFFEE = ifpri_mapspam.Crop2005.RCOF.name
    PEARL_MILLET = ifpri_mapspam.Crop2005.PMIL.name
    SMALL_MILLET = ifpri_mapspam.Crop2005.SMIL.name
    COCONUT = ifpri_mapspam.Crop2005.CNUT.name
    OILPALM = ifpri_mapspam.Crop2005.OILP.name
    SUNFLOWER = ifpri_mapspam.Crop2005.SUNF.name
    RAPESEED = ifpri_mapspam.Crop2005.RAPE.name
    SESAME_SEED = ifpri_mapspam.Crop2005.SESA.name
    OTHER_OILCROPS = ifpri_mapspam.Crop2005.OOIL.name
    CHICKPEA = ifpri_mapspam.Crop2005.CHIC.name
    COWPEA = ifpri_mapspam.Crop2005.COWP.name
    PIGEONPEA = ifpri_mapspam.Crop2005.PIGE.name
    LENTIL = ifpri_mapspam.Crop2005.LENT.name
    OTHER_PULSES = ifpri_mapspam.Crop2005.OPUL.name
    SWEET_POTATO = ifpri_mapspam.Crop2005.SWPO.name
    YAM = ifpri_mapspam.Crop2005.YAMS.name


assert all(
    e.value
    in ifpri_mapspam.SHARED_CROP_NAMES | set(ifpri_mapspam.CONSTITUENT_TO_GROUP_NAME)
    for e in Crop
)


DATASET_NAMES = (
    DatasetName.IFPRI_MAPSPAM_PHYSICAL_AREA_2000,
    DatasetName.IFPRI_MAPSPAM_PHYSICAL_AREA_2005,
    DatasetName.IFPRI_MAPSPAM_PHYSICAL_AREA_2010,
    DatasetName.IFPRI_MAPSPAM_PHYSICAL_AREA_2020,
    DatasetName.IFPRI_MAPSPAM_PRODUCTION_2000,
    DatasetName.IFPRI_MAPSPAM_PRODUCTION_2005,
    DatasetName.IFPRI_MAPSPAM_PRODUCTION_2010,
    DatasetName.IFPRI_MAPSPAM_PRODUCTION_2020,
)


CROPLAND_EMISSIONS_VARIABLE_NAMES = [
    "emissions:tco2e-per-ha:2000-2005",
    "emissions:tco2e-per-ha:2005-2010",
    "emissions:tco2e-per-ha:2010-2015",
    "emissions:tco2e-per-ha:2015-2020",
    "peatland-occupation:tco2e-per-ha",
]
GLAD_VARIABLE_NAMES = [
    "gfw:global-peatlands:is-peatland",
    *CROPLAND_EMISSIONS_VARIABLE_NAMES,
]


def get_band_type_for_variable_name(variable_name: str) -> base.BandType:
    if "-per-ha" in variable_name:
        return base.BandType.INTENSIVE
    elif variable_name.endswith((":ha", ":tco2e")):
        return base.BandType.EXTENSIVE
    else:
        return base.BandType.CATEGORICAL


@storage.cache_to_zarr(version=1)
def get_downscaled_luc_emissions(
    skip_glad_crop_filter: bool, tile_ids: tuple[str, ...]
) -> xarray.Dataset:
    logger.info("Computing emissions on the GLAD grid")
    glad_emissions = geo.exact_merge(
        harmonize.workflow(
            dataset_names=harmonize.LUC_AND_EMISSIONS_DATASET_NAMES,
            ignore_missing_tiles=False,
            skip_ingest=False,
            tile_ids=tile_ids,
            tile_resolution=tiling.TileResolution.GLAD.value,
        ),
        # NB: this should call the harmonize workflow with identical args and hit
        # the cache from the preceding call
        emit.workflow(tile_ids=tile_ids),
    )

    logger.info("Splitting forest / peatland-conversion per span")
    forest_class = glad_glcluc.LandClass.FOREST.value
    is_peat = glad_emissions[gfw_global_peatlands.DATASET.fully_qualified_band_name]
    source_variable_names: list[str] = []
    for before, after in emit.SPAN_TO_LINEAR_DISCOUNT_WEIGHT:
        before_class = glad_emissions[f"land-class:{before:d}"]
        vegetation = glad_emissions[
            f"vegetation-emissions:tco2e-per-ha:{before:d}-{after:d}"
        ]
        soil = glad_emissions[f"soil-emissions:tco2e-per-ha:{before:d}-{after:d}"]
        source_to_darray = {
            "forest": vegetation.where(before_class == forest_class, other=0)
            + soil.where((before_class == forest_class) & (is_peat != 1), other=0),
            "peatland_conversion": soil.where(is_peat == 1, other=0),
        }
        for source, darray in source_to_darray.items():
            name = f"{source}:tco2e-per-ha:{before:d}-{after:d}"
            glad_emissions[name] = darray
            source_variable_names.append(name)

    if not skip_glad_crop_filter:
        logger.info("Masking emissions to current cropland")
        is_currently_cropland = (
            glad_emissions[f"land-class:{max(glad_glcluc.YEARS):d}"]
            == glad_glcluc.LandClass.CROPLAND.value
        )
        for variable_name in (
            *CROPLAND_EMISSIONS_VARIABLE_NAMES,
            *source_variable_names,
        ):
            glad_emissions[variable_name] = glad_emissions[variable_name].where(
                is_currently_cropland, other=0
            )

    logger.info("Downsampling emissions from the GLAD grid to the MAPSPAM grid")
    grid = harmonize.Grid.from_tile_ids_resolution(
        tile_ids=tile_ids,
        tile_resolution=tiling.TileResolution.MAPSPAM.value,  # type: ignore
    )
    return xarray.Dataset(
        {
            variable_name: geo.downscale_darray(
                darray=glad_emissions[variable_name],
                epsg=grid.epsg,
                height=grid.resolution.y,
                # NB: without an equal-area CRS, this presents a small error
                resampling=rasterio.enums.Resampling.average,
                transform=grid.transform,
                width=grid.resolution.x,
            )
            for variable_name in (*GLAD_VARIABLE_NAMES, *source_variable_names)
        }
    )


def get_band_name_for_crop_name(
    crop_name: str,
    quantity: ifpri_mapspam.Quantity,
    year: int,
) -> str:
    # NB: this works because enum and band orders are the same
    crop_cls = ifpri_mapspam.YEAR_TO_CROP_CLS[year]
    name_to_idx = {name: idx for idx, name in enumerate(e.name for e in crop_cls)}
    idx = name_to_idx[crop_name]
    year_to_dataset = (
        ifpri_mapspam.YEAR_TO_PHYSICAL_AREA_DATASET
        if quantity == ifpri_mapspam.Quantity.PHYSICAL_AREA
        else ifpri_mapspam.YEAR_TO_PRODUCTION_DATASET
    )
    dataset = year_to_dataset[year]
    return dataset.fully_qualified_band_names[idx]


def get_harmonized_quantity(
    crop_name: str,
    dset: xarray.Dataset,
    quantity: ifpri_mapspam.Quantity,
    year: int,
) -> xarray.DataArray:
    def snapshot(
        crop_name: str, quantity: ifpri_mapspam.Quantity, year: int
    ) -> xarray.DataArray:
        variable_name = get_band_name_for_crop_name(
            crop_name=crop_name, quantity=quantity, year=year
        )
        return dset[variable_name].fillna(0)

    if (
        year == ifpri_mapspam.YEAR_TO_DECOMPOSE
        and crop_name not in ifpri_mapspam.SHARED_CROP_NAMES
    ):
        # Decompose the grouped crops into their constituents, assuming the within-group
        # proportions match that of the reference year
        group_name = ifpri_mapspam.CONSTITUENT_TO_GROUP_NAME[crop_name]
        siblings = sorted(ifpri_mapspam.GROUP_TO_CONSTITUENT_NAMES[group_name])
        reference_group_area = sum(
            snapshot(
                crop_name=s,
                quantity=ifpri_mapspam.Quantity.PHYSICAL_AREA,
                year=ifpri_mapspam.DECOMPOSITION_REFERENCE_YEAR,
            )
            for s in siblings
        )
        assert isinstance(reference_group_area, xarray.DataArray)
        constituent_share = xarray.where(
            reference_group_area > 0,
            snapshot(
                crop_name=crop_name,
                quantity=ifpri_mapspam.Quantity.PHYSICAL_AREA,
                year=ifpri_mapspam.DECOMPOSITION_REFERENCE_YEAR,
            )
            # NB: this avoids `RuntimeWarning: invalid value encountered in divide`
            / reference_group_area.where(reference_group_area > 0, other=1),
            # Group absent in the reference year -> split it evenly
            1 / len(siblings),
        )
        return (
            snapshot(
                crop_name=group_name,
                quantity=quantity,
                year=ifpri_mapspam.YEAR_TO_DECOMPOSE,
            )
            * constituent_share
        )
    else:
        # Simple lookup
        return snapshot(
            crop_name=ifpri_mapspam.map_2005_name_to_year(
                crop_name=crop_name, year=year
            ),
            quantity=quantity,
            year=year,
        )


def get_crop_to_share(
    after: int, before: int, crops: tuple[Crop, ...], dset: xarray.Dataset
) -> dict[Crop, xarray.DataArray]:
    def snapshot_area(crop_name: str, year: int) -> xarray.DataArray:
        variable_name = get_band_name_for_crop_name(
            crop_name=crop_name,
            quantity=ifpri_mapspam.Quantity.PHYSICAL_AREA,
            year=year,
        )
        return dset[variable_name].fillna(0)

    def expansion(crop_name: str) -> xarray.DataArray:
        return (
            get_harmonized_quantity(
                crop_name=crop_name,
                dset=dset,
                quantity=ifpri_mapspam.Quantity.PHYSICAL_AREA,
                year=after,
            )
            - get_harmonized_quantity(
                crop_name=crop_name,
                dset=dset,
                quantity=ifpri_mapspam.Quantity.PHYSICAL_AREA,
                year=before,
            )
        ).clip(min=0)

    common_expansion = sum(map(expansion, sorted(ifpri_mapspam.SHARED_CROP_NAMES)))

    def residual(year: int) -> xarray.DataArray:
        # NB: no expansion logic is required so we can do simple snapshot lookups
        ret = sum(
            snapshot_area(crop_name=e.name, year=year)
            for e in ifpri_mapspam.YEAR_TO_CROP_CLS[year]
            if e.name not in ifpri_mapspam.SHARED_CROP_NAMES
        )
        assert isinstance(ret, xarray.DataArray)
        return ret

    residual_expansion = (residual(year=after) - residual(year=before)).clip(min=0)
    total_expansion = common_expansion + residual_expansion
    total_expansion = total_expansion.where(total_expansion > 0)
    return {
        # Share is zero when there is no expansion at all
        crop: (expansion(crop_name=crop.value) / total_expansion).fillna(0)
        for crop in crops
    }


GLAD_TO_MAPSPAM_SPAN = {
    (2000, 2005): (2000, 2005),
    (2005, 2010): (2005, 2010),
    # NB: because MAPSPAM has no 2015 snapshot, we assume crop expansion is constant between
    # 2010 and 2020 -- and then use that one crop expansion for both spans
    (2010, 2015): (2010, 2020),
    (2015, 2020): (2010, 2020),
}
assert set(GLAD_TO_MAPSPAM_SPAN) == set(emit.SPAN_TO_LINEAR_DISCOUNT_WEIGHT)


def get_crop_name_to_totals(
    dset: xarray.Dataset,
    crop_to_span_to_share: dict[Crop, dict[emit.SpanType, xarray.DataArray]],
    whole_period_shares: dict[Crop, xarray.DataArray],
) -> dict[str, dict[str, float]]:
    peatland_fraction = dset[gfw_global_peatlands.DATASET.fully_qualified_band_name]
    hectares = emit.get_hectares_per_pixel(darray=peatland_fraction)
    peatland_occupation = dset["peatland-occupation:tco2e-per-ha"] * hectares

    crop_to_totals: dict[Crop, dict[str, xarray.DataArray]] = collections.defaultdict(
        dict
    )
    for crop, span_to_share in crop_to_span_to_share.items():
        conversion = emit.get_linear_discounted_emissions(
            span_to_emissions={
                (before, after): dset[f"emissions:tco2e-per-ha:{before:d}-{after:d}"]
                * hectares
                * span_to_share[GLAD_TO_MAPSPAM_SPAN[(before, after)]]
                for (
                    before,
                    after,
                ) in emit.SPAN_TO_LINEAR_DISCOUNT_WEIGHT
            }
        )
        peatland = peatland_occupation * whole_period_shares[crop]
        crop_name = ifpri_mapspam.map_2005_name_to_year(
            crop_name=crop.value,
            year=2020,
        )
        crop_hectares = dset[
            get_band_name_for_crop_name(
                crop_name=crop_name,
                quantity=ifpri_mapspam.Quantity.PHYSICAL_AREA,
                year=2020,
            )
        ]
        assert isinstance(crop_hectares, xarray.DataArray)

        crop_to_totals[crop]["crop_hectares"] = crop_hectares
        for source in ("forest", "peatland_conversion"):
            crop_to_totals[crop][f"{source:s}_emissions_mt"] = (
                emit.get_linear_discounted_emissions(
                    span_to_emissions={
                        (before, after): dset[
                            f"{source:s}:tco2e-per-ha:{before:d}-{after:d}"
                        ]
                        * hectares
                        * span_to_share[GLAD_TO_MAPSPAM_SPAN[(before, after)]]
                        for (before, after) in emit.SPAN_TO_LINEAR_DISCOUNT_WEIGHT
                    }
                )
            )
        crop_to_totals[crop]["peatland_crop_hectares"] = (
            crop_hectares * peatland_fraction
        )
        crop_to_totals[crop]["peatland_occupation_emissions_mt"] = peatland
        crop_to_totals[crop]["emissions_mt"] = conversion + peatland
        # Reduce production over the windows using the same linear temporal discounting
        weight_total = sum(emit.SPAN_TO_LINEAR_DISCOUNT_WEIGHT.values())
        crop_to_totals[crop]["production_mt"] = (
            sum(
                weight
                * (
                    get_harmonized_quantity(
                        crop_name=crop.value,
                        dset=dset,
                        quantity=ifpri_mapspam.Quantity.PRODUCTION,
                        year=mapspam_before,
                    )
                    + get_harmonized_quantity(
                        crop_name=crop.value,
                        dset=dset,
                        quantity=ifpri_mapspam.Quantity.PRODUCTION,
                        year=mapspam_after,
                    )
                )
                / 2
                for (
                    before,
                    after,
                ), weight in emit.SPAN_TO_LINEAR_DISCOUNT_WEIGHT.items()
                for (mapspam_before, mapspam_after) in [
                    GLAD_TO_MAPSPAM_SPAN[(before, after)]
                ]
            )  # type: ignore
            / weight_total
        )
    return utils.get_sum_totals(enum_to_name_to_darray=crop_to_totals)


@storage.cache_to_parquet(version=0)
def workflow(
    crop_names: tuple[str, ...],
    iso_3166: str,
    skip_glad_crop_filter: bool,
    tile_ids: tuple[str, ...],
) -> pandas.DataFrame:
    from rioxarray.exceptions import NoDataInBounds

    crops = tuple(Crop[crop_name] for crop_name in crop_names)
    logger.info(f"Computing emissions for {crops=:}")
    merged = geo.exact_merge(
        # NB: this is deferred because it is expensive and would like to cache it
        get_downscaled_luc_emissions(
            skip_glad_crop_filter=skip_glad_crop_filter, tile_ids=tile_ids
        ),
        # MAPSPAM
        harmonize.workflow(
            dataset_names=DATASET_NAMES,
            ignore_missing_tiles=False,
            skip_ingest=False,
            tile_ids=tile_ids,
            tile_resolution=tiling.TileResolution.MAPSPAM.value,
        ),
    )

    def it() -> collections.abc.Iterator[dict[str, float | str]]:
        for jurisdiction in worldbank_jurisdictions.iter_jurisdiction_for_iso_3166(
            admin_level=worldbank_jurisdictions.AdminLevel.PROVINCIAL,
            iso_3166=iso_3166,
        ):
            logger.info(f"Clipping to provincial geometry from {jurisdiction.id=:s}")
            try:
                clipped = geo.clip_dset(dset=merged, geometry=jurisdiction.geometry)
            except NoDataInBounds as exc:
                logger.warning(repr(exc))
            else:
                span_to_crop_to_share = {
                    (before, after): get_crop_to_share(
                        after=after,
                        before=before,
                        crops=crops,
                        dset=clipped,
                    )
                    for (before, after) in GLAD_TO_MAPSPAM_SPAN.values()
                }
                crop_to_span_to_share: dict[
                    Crop, dict[emit.SpanType, xarray.DataArray]
                ] = collections.defaultdict(dict)
                for span, crop_to_share in span_to_crop_to_share.items():
                    for crop, share in crop_to_share.items():
                        crop_to_span_to_share[crop][span] = share

                whole_period_shares = get_crop_to_share(
                    after=max(ifpri_mapspam.YEARS),
                    before=min(ifpri_mapspam.YEARS),
                    crops=crops,
                    dset=clipped,
                )

                logger.info(
                    f"Populating emissions for {jurisdiction.id=!s}/{len(crops)=:d} crops"
                )
                crop_name_to_totals = get_crop_name_to_totals(
                    crop_to_span_to_share=crop_to_span_to_share,
                    dset=clipped,
                    whole_period_shares=whole_period_shares,
                )
                for crop_name, totals in crop_name_to_totals.items():
                    yield totals | {
                        "admin_id": jurisdiction.id,
                        "admin_level": worldbank_jurisdictions.AdminLevel.PROVINCIAL.name,
                        "crop_name": crop_name,
                        "jurisdiction_name": jurisdiction.name,
                    }

    return pandas.DataFrame.from_records(data=it()).set_index(
        ["admin_level", "crop_name", "jurisdiction_name"]
    )
