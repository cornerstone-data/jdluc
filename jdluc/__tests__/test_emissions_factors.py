import numpy
import pandas

from jdluc.emissions_factors import merge_emissions_and_yields

EMISSIONS = pandas.DataFrame.from_records(
    [
        # normal row
        ("PROVINCIAL", "CORN", "Delaware", "USA008", 100.0, 10.0, 50.0, 200.0),
        # zero production (crop_hectares == 0)
        ("PROVINCIAL", "SOYBEANS", "Delaware", "USA008", 0.0, 0.0, 0.0, 80.0),
        # unmatched yield + zero total_emissions
        ("PROVINCIAL", "WHEAT", "Iowa", "USA016", 50.0, 0.0, 0.0, 0.0),
    ],
    columns=[
        "admin_level",
        "crop_name",
        "jurisdiction_name",
        "admin_id",
        "crop_hectares",
        "peatland_crop_hectares",
        "peatland_occupation_emissions",
        "total_emissions",
    ],
).set_index(["admin_level", "crop_name", "jurisdiction_name"])
RAW_YIELDS = pandas.DataFrame.from_records(
    [
        # CORN: 2016 must be excluded; mean(8, 10, 12, 10) over 2017-2020 == 10
        *(
            ("PROVINCIAL", "USA008", "Delaware", "CORN", year, val)
            for year, val in (
                (2016, 999.0),
                (2017, 8.0),
                (2018, 10.0),
                (2019, 12.0),
                (2020, 10.0),
            )
        ),
        # SOYBEANS: mean == 30
        *(
            ("PROVINCIAL", "USA008", "Delaware", "SOYBEANS", year, 30.0)
            for year in (2017, 2018, 2019, 2020)
        ),
        # (USA016, WHEAT): deliberately absent -> unmatched
    ],
    columns=[
        "admin_level",
        "admin_id",
        "jurisdiction_name",
        "crop_name",
        "year",
        "yield_kg_per_ha",
    ],
).set_index(["admin_level", "admin_id", "jurisdiction_name", "crop_name", "year"])


def test_merge_emissions_and_yields() -> None:
    result = merge_emissions_and_yields(emissions=EMISSIONS, raw_yields=RAW_YIELDS)
    assert isinstance(result, pandas.DataFrame)

    iter_result = result.iterrows()
    key, corn = next(iter_result)
    assert key == ("PROVINCIAL", "CORN", "Delaware")
    assert corn["yield_kg_per_ha"] == 10.0  # 4-year mean, 2016 excluded
    assert corn["total_production_kg"] == 1000.0  # 100 ha × 10
    assert corn["emissions_factor_kgco2e_per_kg"] == 200.0  # 200 t × 1000 / 1000 kg
    assert corn["peatland_occupation_fraction"] == 0.25  # 50 / 200

    # zero production -> EF guarded to NaN (not inf); fraction still defined (0/80)
    key, soy = next(iter_result)
    assert key == ("PROVINCIAL", "SOYBEANS", "Delaware")
    assert soy["total_production_kg"] == 0.0
    assert numpy.isnan(soy["emissions_factor_kgco2e_per_kg"])
    assert soy["peatland_occupation_fraction"] == 0.0

    # unmatched yield -> NaN yield/production/EF; zero total_emissions -> NaN fraction
    key, wheat = next(iter_result)
    assert key == ("PROVINCIAL", "WHEAT", "Iowa")
    assert numpy.isnan(wheat["yield_kg_per_ha"])
    assert numpy.isnan(wheat["total_production_kg"])
    assert numpy.isnan(wheat["emissions_factor_kgco2e_per_kg"])
    assert numpy.isnan(wheat["peatland_occupation_fraction"])
