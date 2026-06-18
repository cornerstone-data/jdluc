"""Compute per-pixel land-conversion emissions from the harmonized dataset.

Maps GLAD GLCLUC values to land classes, quantifies vegetation- and soil-carbon emissions
across the 2000-2020 epoch transitions, adds ongoing peatland-occupation emissions, applies the
GHGP 20-year linear discount, and scales to per-pixel tCO2e. Returns an xarray.Dataset (written
to zarr), cached to GCS (`@gcs.cache`).

Example invocation:
  uv run python jdluc/emissions.py DELAWARE
"""

import argparse
import itertools
import logging
import math
import typing

import numpy
import xarray

from jdluc import gcs, geo, harmonize, tiling
from jdluc.datasets import (
    gfw_global_peatlands,
    gfw_harris_agb,
    glad_glcluc,
    huang_bgb,
    ipcc_climate_zones,
    soilgrids_ocs,
)

logger = logging.getLogger(__name__)


def get_land_class(glad_class: xarray.DataArray) -> xarray.DataArray:
    # NB: Because the glad class values are disjoint, we can sum vegetation model carbon
    # model enum values without worrying about collisions
    ret = sum(
        glad_class.isin(values) * numpy.uint8(land_class.value)
        for land_class, values in glad_glcluc.LAND_CLASS_TO_VALUES.items()
    )
    return typing.cast(xarray.DataArray, ret).rename(None)


# IPCC 2006, Vol 4, Ch 4, §4.5 (living woody biomass).
CARBON_PER_BIOMASS_LIVE_WOOD: float = 0.47
# Global forest mean root-to-shoot ratio from Huang et al. (2021), Earth System Science Data 13:4263-4274
ROOT_TO_SHOOT_RATIO = 0.25


def get_belowground_carbon(
    aboveground_biomass: xarray.DataArray, belowground_biomass: xarray.DataArray
) -> xarray.DataArray:
    return (
        (
            # Default to BGB when it is provided
            belowground_biomass.where(belowground_biomass > 0, other=0)
            # Fallback to AGB * R2S when it isn't
            + ROOT_TO_SHOOT_RATIO
            * aboveground_biomass.where(belowground_biomass == 0, other=0)
        )
        * CARBON_PER_BIOMASS_LIVE_WOOD
    ).rename("tcarbon-per-ha")


# CDM AR-TOOL-12 dead wood and litter factors, expressed as fractions of above-ground biomass
CLIMATE_ZONE_TO_DEAD_ORGANIC_MATTER_PARAMETERS: dict[
    ipcc_climate_zones.Zone, tuple[float, float]
] = {
    ipcc_climate_zones.Zone.TROPICAL_WET: (0.06, 0.01),
    ipcc_climate_zones.Zone.TROPICAL_MOIST: (0.01, 0.01),
    ipcc_climate_zones.Zone.TROPICAL_DRY: (0.02, 0.04),
    ipcc_climate_zones.Zone.TROPICAL_MONTANE: (0.07, 0.01),
    ipcc_climate_zones.Zone.WARM_TEMPERATE_MOIST: (0.08, 0.04),
    ipcc_climate_zones.Zone.WARM_TEMPERATE_DRY: (0.08, 0.04),
    ipcc_climate_zones.Zone.COOL_TEMPERATE_MOIST: (0.08, 0.04),
    ipcc_climate_zones.Zone.COOL_TEMPERATE_DRY: (0.08, 0.04),
    ipcc_climate_zones.Zone.BOREAL_MOIST: (0.08, 0.04),
    ipcc_climate_zones.Zone.BOREAL_DRY: (0.08, 0.04),
}
assert set(ipcc_climate_zones.Zone) == set(
    CLIMATE_ZONE_TO_DEAD_ORGANIC_MATTER_PARAMETERS
)

# CDM AR-TOOL-12 (dead wood and litter pools).
CARBON_PER_BIOMASS_DEAD_WOOD = 0.50
CARBON_PER_BIOMASS_LITTER = 0.37


def get_dead_organic_matter_carbon(
    aboveground_biomass: xarray.DataArray, climate_zones: xarray.DataArray
) -> xarray.DataArray:
    ret = sum(
        aboveground_biomass.where(climate_zones == climate_zone.value, other=0)
        * (
            CARBON_PER_BIOMASS_DEAD_WOOD * dead_wood_fraction
            + CARBON_PER_BIOMASS_LITTER * litter_fraction
        )
        for climate_zone, (
            dead_wood_fraction,
            litter_fraction,
        ) in CLIMATE_ZONE_TO_DEAD_ORGANIC_MATTER_PARAMETERS.items()
    )
    return typing.cast(xarray.DataArray, ret).rename("tcarbon-per-ha")


# Houghton/BLUE total vegetation carbon density for grassland / shrubland pixels
CLIMATE_ZONE_TO_GRASSLAND_TCARBON_PER_HA: dict[ipcc_climate_zones.Zone, float] = {
    ipcc_climate_zones.Zone.TROPICAL_WET: 18.0,
    ipcc_climate_zones.Zone.TROPICAL_MOIST: 18.0,
    ipcc_climate_zones.Zone.TROPICAL_DRY: 7.0,
    ipcc_climate_zones.Zone.TROPICAL_MONTANE: 7.0,
    ipcc_climate_zones.Zone.WARM_TEMPERATE_MOIST: 7.0,
    ipcc_climate_zones.Zone.WARM_TEMPERATE_DRY: 5.0,
    ipcc_climate_zones.Zone.COOL_TEMPERATE_MOIST: 7.0,
    ipcc_climate_zones.Zone.COOL_TEMPERATE_DRY: 5.0,
    ipcc_climate_zones.Zone.BOREAL_MOIST: 6.0,
    ipcc_climate_zones.Zone.BOREAL_DRY: 3.0,
}
assert set(ipcc_climate_zones.Zone) == set(CLIMATE_ZONE_TO_GRASSLAND_TCARBON_PER_HA)


def get_grassland_carbon(climate_zones: xarray.DataArray) -> xarray.DataArray:
    footprint = xarray.ones_like(climate_zones)
    ret = sum(
        tcarbon_per_ha * footprint.where(climate_zones == climate_zone.value, other=0)
        for climate_zone, tcarbon_per_ha in CLIMATE_ZONE_TO_GRASSLAND_TCARBON_PER_HA.items()
    )
    return typing.cast(xarray.DataArray, ret).rename("tcarbon-per-ha")


def get_vegetation_carbon(
    aboveground_carbon: xarray.DataArray,
    belowground_carbon: xarray.DataArray,
    dead_organic_carbon: xarray.DataArray,
    grassland_carbon: xarray.DataArray,
    land_class: xarray.DataArray,
) -> xarray.DataArray:
    # vegetation = forest + grasslands (all other GLAD classes contain zero carbon)
    forest_carbon = aboveground_carbon + belowground_carbon + dead_organic_carbon
    return (
        forest_carbon.where(land_class == glad_glcluc.LandClass.FOREST.value, other=0)
        + grassland_carbon.where(
            land_class == glad_glcluc.LandClass.GRASSLAND.value, other=0
        )
    ).rename("tcarbon-per-ha")


CO2E_PER_CARBON = 44 / 12


def get_vegetation_emissions(
    after: xarray.DataArray, before: xarray.DataArray
) -> xarray.DataArray:
    return ((before - after).clip(min=0) * CO2E_PER_CARBON).rename("tco2e-per-ha")


# IPCC 2019 Vol 4 Table 5.5
CLIMATE_ZONE_TO_SOC_LOSS_FRACTION: dict[ipcc_climate_zones.Zone, float] = {
    ipcc_climate_zones.Zone.TROPICAL_WET: 0.83,
    ipcc_climate_zones.Zone.TROPICAL_MOIST: 0.83,
    ipcc_climate_zones.Zone.TROPICAL_DRY: 0.92,
    # Tropical montane factors are approximated as the mean of WARM_TEMPERATE_MOIST and TROPICAL_MOIST_WET
    ipcc_climate_zones.Zone.TROPICAL_MONTANE: 0.76,
    ipcc_climate_zones.Zone.WARM_TEMPERATE_MOIST: 0.69,
    ipcc_climate_zones.Zone.WARM_TEMPERATE_DRY: 0.76,
    # Cool Temperate and Boreal share rows in Table 5.5.
    ipcc_climate_zones.Zone.COOL_TEMPERATE_MOIST: 0.70,
    ipcc_climate_zones.Zone.COOL_TEMPERATE_DRY: 0.77,
    ipcc_climate_zones.Zone.BOREAL_MOIST: 0.70,
    ipcc_climate_zones.Zone.BOREAL_DRY: 0.77,
}


def get_mineral_soil_emissions(
    climate_zones: xarray.DataArray,
    soil_organic_carbon: xarray.DataArray,
) -> xarray.DataArray:
    ret = sum(
        soil_organic_carbon.where(climate_zones == climate_zone.value, other=0)
        * (1 - soc_loss_fraction)
        * CO2E_PER_CARBON
        for climate_zone, soc_loss_fraction in CLIMATE_ZONE_TO_SOC_LOSS_FRACTION.items()
    )
    return typing.cast(xarray.DataArray, ret).rename("tco2e-per-ha")


PEATLAND_EMISSIONS_PULSE_TCO2E_PER_HA = 621


def get_soil_emissions(
    after: xarray.DataArray,
    before: xarray.DataArray,
    climate_zones: xarray.DataArray,
    is_peatland: xarray.DataArray,
    soil_organic_carbon: xarray.DataArray,
) -> xarray.DataArray:
    is_emissive = before.isin(
        (
            glad_glcluc.LandClass.FOREST.value,
            glad_glcluc.LandClass.GRASSLAND.value,
        )
    ) & (
        ~after.isin(
            (
                glad_glcluc.LandClass.FOREST.value,
                glad_glcluc.LandClass.GRASSLAND.value,
            )
        )
    )

    peatlands_conversion_emissions = (
        PEATLAND_EMISSIONS_PULSE_TCO2E_PER_HA * xarray.ones_like(soil_organic_carbon)
    )
    mineral_soil_organic_emissions = get_mineral_soil_emissions(
        climate_zones=climate_zones,
        soil_organic_carbon=soil_organic_carbon,
    )

    return (
        # is_emissive & is_peatland: peatland pulse
        peatlands_conversion_emissions.where(is_emissive & (is_peatland == 1), other=0)
        # is_emissive & ~is_peatland: mineral SOC
        + mineral_soil_organic_emissions.where(
            is_emissive & (is_peatland == 0), other=0
        )
        # ~is_emissive (else): zero
    ).rename("tco2e-per-ha")


PEATLAND_EMISSIONS_ANNUAL_TCO2E_PER_HA = 37.3


def get_peatland_occupation_emissions(
    is_peatland: xarray.DataArray, year_to_land_class: dict[int, xarray.DataArray]
) -> xarray.DataArray:
    latest_land_class = year_to_land_class[max(year_to_land_class)]
    undrained_classes = (
        # still natural
        glad_glcluc.LandClass.FOREST.value,
        glad_glcluc.LandClass.GRASSLAND.value,
        # still wet
        glad_glcluc.LandClass.OCEAN.value,
        glad_glcluc.LandClass.SNOW_ICE.value,
        glad_glcluc.LandClass.WATER.value,
    )
    return PEATLAND_EMISSIONS_ANNUAL_TCO2E_PER_HA * is_peatland.where(
        ~latest_land_class.isin(undrained_classes), other=0
    ).rename("tco2e-per-ha")


EpochType = tuple[int, int]
EPOCH_TO_LINEAR_DISCOUNT_WEIGHT: dict[EpochType, float] = {
    (2000, 2005): 0.0125,
    (2005, 2010): 0.0375,
    (2010, 2015): 0.0625,
    (2015, 2020): 0.0875,
}
assert all((after - before) == 5 for (before, after) in EPOCH_TO_LINEAR_DISCOUNT_WEIGHT)
assert math.isclose(sum(EPOCH_TO_LINEAR_DISCOUNT_WEIGHT.values()), 0.2)


def get_linear_discounted_emissions(
    epoch_to_emissions: dict[EpochType, xarray.DataArray],
) -> xarray.DataArray:
    ret = sum(
        emissions * EPOCH_TO_LINEAR_DISCOUNT_WEIGHT[epoch]
        for epoch, emissions in epoch_to_emissions.items()
    )
    return typing.cast(xarray.DataArray, ret).rename("tco2e-per-ha")


def get_hectares_per_pixel(darray: xarray.DataArray) -> xarray.DataArray:
    # https://en.wikipedia.org/wiki/Earth%27s_circumference
    equator_meters_per_longitude_degrees = 40_075_017 / 360
    meters_per_latitude_degrees = 40_007_863 / 360

    # NB: coords are 1-D and tiny, so reducing them to scalar spacings is cheap/greedy
    delta_longitude_degrees = abs(float(darray.x.diff("x").mean()))
    delta_latitude_degrees = abs(float(darray.y.diff("y").mean()))

    return (
        # hectares at equator
        (delta_latitude_degrees * meters_per_latitude_degrees)
        * (delta_longitude_degrees * equator_meters_per_longitude_degrees)
        / 10_000
        # projection to latitude
        * numpy.cos(darray.y * numpy.pi / 180)
        # broadcast across x while inheriting darray's aligned 2-D chunking
        * xarray.ones_like(darray)
    ).rename("ha")


def get_dset_for_output(name_to_darray: dict[str, xarray.DataArray]) -> xarray.Dataset:
    chunk_size = geo.get_chunk_size(
        dtypes=[numpy.dtype("float32")] * len(name_to_darray), number_of_dimensions=2
    )

    def merge_name_units(name: str, units: typing.Hashable | None) -> str:
        prefix, _, suffix = name.partition(":")
        words = filter(bool, (prefix, units, suffix))
        return ":".join(map(str, words))

    return xarray.Dataset(
        {
            merge_name_units(name=name, units=darray.name): geo.unify_dtype_and_no_data(
                darray=darray
            ).chunk(chunks=chunk_size)
            for name, darray in name_to_darray.items()
        }
    )


@gcs.cache(version=2)
def workflow(tile_ids: tiling.TileSetType) -> xarray.Dataset:
    logger.info(f"Running the land conversion and emissions worflow for {tile_ids=:}")
    dset = harmonize.workflow(dataset_names=harmonize.DATASET_NAMES, tile_ids=tile_ids)

    logger.info("Mapping GLCLUC values to land classes")
    year_to_land_class = {
        year: get_land_class(glad_class=dset[band_name])
        for year, band_name in zip(
            glad_glcluc.YEARS,
            glad_glcluc.DATASET.fully_qualified_band_names,
            strict=True,
        )
    }

    logger.info("Quantifying vegetation carbon stock and emissions")
    aboveground_biomass = dset[gfw_harris_agb.DATASET.fully_qualified_band_name]
    aboveground_carbon = (aboveground_biomass * CARBON_PER_BIOMASS_LIVE_WOOD).rename(
        "tcarbon-per-ha"
    )
    belowground_carbon = get_belowground_carbon(
        aboveground_biomass=aboveground_biomass,
        belowground_biomass=dset[huang_bgb.DATASET.fully_qualified_band_name],
    )
    climate_zones = dset[ipcc_climate_zones.DATASET.fully_qualified_band_name]
    dead_organic_matter_carbon = get_dead_organic_matter_carbon(
        aboveground_biomass=aboveground_biomass, climate_zones=climate_zones
    )
    grassland_carbon = get_grassland_carbon(climate_zones=climate_zones)
    year_to_vegetation_carbon = {
        year: get_vegetation_carbon(
            aboveground_carbon=aboveground_carbon,
            belowground_carbon=belowground_carbon,
            dead_organic_carbon=dead_organic_matter_carbon,
            grassland_carbon=grassland_carbon,
            land_class=land_class,
        )
        for year, land_class in year_to_land_class.items()
    }
    epoch_to_vegetation_emissions = {
        (before, after): get_vegetation_emissions(
            after=year_to_vegetation_carbon[after],
            before=year_to_vegetation_carbon[before],
        )
        for (before, after) in itertools.pairwise(sorted(year_to_land_class))
    }

    logger.info("Quantifying soil emissions")
    is_peatland = dset[gfw_global_peatlands.DATASET.fully_qualified_band_name]
    epoch_to_soil_emissions = {
        (before, after): get_soil_emissions(
            after=year_to_land_class[after],
            before=year_to_land_class[before],
            climate_zones=climate_zones,
            is_peatland=is_peatland,
            soil_organic_carbon=dset[soilgrids_ocs.DATASET.fully_qualified_band_name],
        )
        for (before, after) in itertools.pairwise(sorted(year_to_land_class))
    }

    logger.info("Summing emissions from vegetation and soil")
    epoch_to_emissions: dict[EpochType, xarray.DataArray] = {
        epoch: (
            epoch_to_vegetation_emissions[epoch] + epoch_to_soil_emissions[epoch]
        ).rename("tco2e-per-ha")
        for epoch in epoch_to_vegetation_emissions
    }

    logger.info("Quantifying and adding peatland occupation emissions")
    peatland_occupation_emissions: xarray.DataArray = get_peatland_occupation_emissions(
        is_peatland=is_peatland, year_to_land_class=year_to_land_class
    )
    emissions_per_hectare: xarray.DataArray = (
        get_linear_discounted_emissions(epoch_to_emissions=epoch_to_emissions)
        + peatland_occupation_emissions
    )

    logger.info("Scaling by area")
    hectares_per_pixel = get_hectares_per_pixel(darray=emissions_per_hectare)
    emissions = (emissions_per_hectare * hectares_per_pixel).rename("tco2e")

    return get_dset_for_output(
        name_to_darray={
            f"land-class:{year:d}": darray
            for year, darray in year_to_land_class.items()
        }
        | {
            "aboveground-carbon": aboveground_carbon,
            "belowground-carbon": belowground_carbon,
            "dead-organic-matter-carbon": dead_organic_matter_carbon,
            "grassland-carbon": grassland_carbon,
        }
        | {
            f"vegetation-carbon:{year:d}": darray
            for year, darray in year_to_vegetation_carbon.items()
        }
        | {
            f"vegetation-emissions:{before:d}-{after:d}": darray
            for (before, after), darray in epoch_to_vegetation_emissions.items()
        }
        | {
            f"soil-emissions:{before:d}-{after:d}": darray
            for (before, after), darray in epoch_to_soil_emissions.items()
        }
        | {
            f"emissions:{before:d}-{after:d}": darray
            for (before, after), darray in epoch_to_emissions.items()
        }
        | {
            "peatland-occupation": peatland_occupation_emissions,
            "emissions-per-hectare": emissions_per_hectare,
            "hectares-per-pixel": hectares_per_pixel,
            "emissions": emissions,
        }
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tile_set_name", choices=sorted(e.name for e in tiling.TileSetName)
    )
    args = parser.parse_args()

    tile_set_name = tiling.TileSetName[str(args.tile_set_name)]
    workflow(tile_ids=tiling.NAME_TO_TILE_SET[tile_set_name])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
