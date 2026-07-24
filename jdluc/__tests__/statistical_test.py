import warnings

import pytest
import xarray

from jdluc.datasets import ifpri_mapspam
from jdluc.datasets.base import BandType
from jdluc.statistical import (
    Crop,
    get_band_name_for_crop_name,
    get_band_type_for_variable_name,
    get_crop_to_share,
    get_harmonized_quantity,
)

AREA = ifpri_mapspam.Quantity.PHYSICAL_AREA
PRODUCTION = ifpri_mapspam.Quantity.PRODUCTION
# Two non-shared ("residual") crops present in both the 2005 and 2010 snapshots.
OTHER_0, OTHER_1 = sorted(
    {e.name for e in ifpri_mapspam.YEAR_TO_CROP_CLS[2005]}
    - ifpri_mapspam.SHARED_CROP_NAMES
)[:2]


def get_dset(
    quantity_year_to_values: dict[tuple[ifpri_mapspam.Quantity, int], dict[str, float]],
) -> xarray.Dataset:
    # A (scalar) band per crop in each (quantity, year)'s enum; unset crops are 0.
    return xarray.Dataset(
        {
            get_band_name_for_crop_name(
                crop_name=crop.name, quantity=quantity, year=year
            ): xarray.DataArray(float(values.get(crop.name, 0.0)))
            for (quantity, year), values in quantity_year_to_values.items()
            for crop in ifpri_mapspam.YEAR_TO_CROP_CLS[year]
        }
    )


def get_dset_for_areas(
    year_to_name_to_area: dict[int, dict[str, float]],
) -> xarray.Dataset:
    return get_dset(
        quantity_year_to_values={
            (AREA, year): areas for year, areas in year_to_name_to_area.items()
        }
    )


def test_get_crop_to_share_composition() -> None:
    # MAIZ +100, SOYB +50 (shared); one residual crop +50. total expansion == 200.
    dset = get_dset_for_areas(
        {2005: {}, 2010: {"MAIZ": 100.0, "SOYB": 50.0, OTHER_0: 50.0}}
    )
    shares = get_crop_to_share(
        after=2010, before=2005, crops=(Crop.MAIZE, Crop.SOYBEAN), dset=dset
    )
    assert float(shares[Crop.MAIZE]) == 0.5  # 100 / 200
    assert float(shares[Crop.SOYBEAN]) == 0.25  # 50 / 200
    # target shares sum to <= 1; the residual crop's 0.25 stays unattributed
    assert float(shares[Crop.MAIZE]) + float(shares[Crop.SOYBEAN]) == 0.75


def test_get_crop_to_share_is_reclassification_invariant() -> None:
    # Same total residual expansion (+50), split across the residual bands differently.
    lumped = get_dset_for_areas({2005: {}, 2010: {"MAIZ": 100.0, OTHER_0: 50.0}})
    split = get_dset_for_areas(
        {2005: {}, 2010: {"MAIZ": 100.0, OTHER_0: 20.0, OTHER_1: 30.0}}
    )
    shares_lumped = get_crop_to_share(
        after=2010, before=2005, crops=(Crop.MAIZE,), dset=lumped
    )
    shares_split = get_crop_to_share(
        after=2010, before=2005, crops=(Crop.MAIZE,), dset=split
    )
    assert float(shares_lumped[Crop.MAIZE]) == float(shares_split[Crop.MAIZE])


def test_get_crop_to_share_clips_contraction() -> None:
    # MAIZE contracts (-100); only SOYB expands -> MAIZE share is 0, not negative.
    dset = get_dset_for_areas(
        {2005: {"MAIZ": 100.0}, 2010: {"MAIZ": 0.0, "SOYB": 50.0}}
    )
    shares = get_crop_to_share(
        after=2010, before=2005, crops=(Crop.MAIZE, Crop.SOYBEAN), dset=dset
    )
    assert float(shares[Crop.MAIZE]) == 0.0
    assert float(shares[Crop.SOYBEAN]) == 1.0  # 50 / 50


def test_get_crop_to_share_keeps_missings() -> None:
    # Nothing expands anywhere -> total is masked to NaN, so shares are NaN (not 0/0 errors).
    dset = get_dset_for_areas({2005: {}, 2010: {}})
    shares = get_crop_to_share(after=2010, before=2005, crops=(Crop.MAIZE,), dset=dset)
    assert shares[Crop.MAIZE] == 0


def test_get_crop_to_share_tolerates_nodata_absent_crops() -> None:
    dset = get_dset_for_areas({2005: {}, 2010: {"MAIZ": 100.0}})
    dset[get_band_name_for_crop_name(crop_name=OTHER_0, quantity=AREA, year=2010)] = (
        xarray.DataArray(float("nan"))
    )
    shares = get_crop_to_share(after=2010, before=2005, crops=(Crop.MAIZE,), dset=dset)
    assert float(shares[Crop.MAIZE]) == 1.0  # 100 / 100; NaN absent crop counts as 0 ha


def test_get_crop_to_share_decomposes_2000_group_by_reference_fractions() -> None:
    # 2000 only has the coarse BANP group (100 ha).  2005 splits it 30/10, so BANA
    # gets 75% -> area(BANA, 2000) == 75.  BANA grows to 175 (+100), PLNT flat at 25.
    dset = get_dset_for_areas(
        {
            2000: {"BANP": 100.0},
            2005: {"BANA": 30.0, "PLNT": 10.0},
            2020: {"BANA": 175.0, "PLNT": 25.0},
        }
    )
    shares = get_crop_to_share(
        after=2020, before=2000, crops=(Crop.BANANA, Crop.PLANTAIN), dset=dset
    )
    assert float(shares[Crop.BANANA]) == 1.0  # +100 over residual (200 - 100)
    assert float(shares[Crop.PLANTAIN]) == 0.0  # decomposed 25 -> 25, no expansion


def test_get_crop_to_share_decomposes_2000_group_by_equal_split_when_reference_absent() -> (
    None
):
    # 2000 has the coarse BANP group (100 ha) but 2005 has neither constituent,
    # so denom == 0 and the split falls back to 1/len(siblings) == 0.5 each:
    # area(BANA, 2000) == area(PLNT, 2000) == 50.
    dset = get_dset_for_areas(
        {
            2000: {"BANP": 100.0},
            2005: {},  # both BANA and PLNT absent -> reference denom is 0
            2020: {"BANA": 80.0, "PLNT": 120.0},
        }
    )
    shares = get_crop_to_share(
        after=2020, before=2000, crops=(Crop.BANANA, Crop.PLANTAIN), dset=dset
    )
    # BANA: 80 - 50 = +30 ; PLNT: 120 - 50 = +70 ; residual total expansion == 100
    assert float(shares[Crop.BANANA]) == 0.30
    assert float(shares[Crop.PLANTAIN]) == 0.70


def test_get_crop_to_share_decomposition_does_not_warn_on_zero_reference() -> None:
    dset = get_dset_for_areas(
        {2000: {"BANP": 100.0}, 2005: {}, 2020: {"BANA": 80.0, "PLNT": 120.0}}
    )
    with warnings.catch_warnings():
        warnings.simplefilter(
            "error", RuntimeWarning
        )  # any 0/0 divide becomes a failure
        get_crop_to_share(
            after=2020, before=2000, crops=(Crop.BANANA, Crop.PLANTAIN), dset=dset
        )


def test_get_harmonized_quantity_reads_snapshot() -> None:
    # Shared crop, no decomposition -> a straight per-year production lookup.
    dset = get_dset({(PRODUCTION, 2020): {"MAIZ": 42.0}})
    got = get_harmonized_quantity(
        crop_name="MAIZ", dset=dset, quantity=PRODUCTION, year=2020
    )
    assert float(got) == 42.0


def test_get_harmonized_quantity_splits_group_production_by_area_share() -> None:
    # 2000 only has the coarse BANP group (200 t production). 2005 areas split
    # BANA/PLNT 30/10, so the split uses the *area* share (0.75 / 0.25), not a
    # production share: BANA -> 200 x 0.75 == 150, PLNT -> 200 x 0.25 == 50.
    dset = get_dset(
        {
            (AREA, 2005): {"BANA": 30.0, "PLNT": 10.0},
            (PRODUCTION, 2000): {"BANP": 200.0},
        }
    )
    banana = get_harmonized_quantity(
        crop_name="BANA", dset=dset, quantity=PRODUCTION, year=2000
    )
    plantain = get_harmonized_quantity(
        crop_name="PLNT", dset=dset, quantity=PRODUCTION, year=2000
    )
    assert float(banana) == 150.0
    assert float(plantain) == 50.0


@pytest.mark.parametrize(
    ("variable_name", "expected"),
    (
        ("gfw:global-peatlands:is-peatland", BandType.CATEGORICAL),
        ("gfw:harris-agb:aboveground-biomass-mg-per-ha", BandType.INTENSIVE),
        ("glad:glcluc:year=2000", BandType.CATEGORICAL),
        ("glad:glcluc:year=2005", BandType.CATEGORICAL),
        ("glad:glcluc:year=2010", BandType.CATEGORICAL),
        ("glad:glcluc:year=2015", BandType.CATEGORICAL),
        ("glad:glcluc:year=2020", BandType.CATEGORICAL),
        ("huang:bgb:belowground-biomass-mg-per-ha", BandType.INTENSIVE),
        ("ipcc:climate-zones:climate-zone", BandType.CATEGORICAL),
        (
            "soilgrids:organic-carbon-stocks:organic-soil-carbon-mg-per-ha",
            BandType.INTENSIVE,
        ),
        ("land-class:2000", BandType.CATEGORICAL),
        ("land-class:2005", BandType.CATEGORICAL),
        ("land-class:2010", BandType.CATEGORICAL),
        ("land-class:2015", BandType.CATEGORICAL),
        ("land-class:2020", BandType.CATEGORICAL),
        ("vegetation-emissions:tco2e-per-ha:2000-2005", BandType.INTENSIVE),
        ("vegetation-emissions:tco2e-per-ha:2005-2010", BandType.INTENSIVE),
        ("vegetation-emissions:tco2e-per-ha:2010-2015", BandType.INTENSIVE),
        ("vegetation-emissions:tco2e-per-ha:2015-2020", BandType.INTENSIVE),
        ("soil-emissions:tco2e-per-ha:2000-2005", BandType.INTENSIVE),
        ("soil-emissions:tco2e-per-ha:2005-2010", BandType.INTENSIVE),
        ("soil-emissions:tco2e-per-ha:2010-2015", BandType.INTENSIVE),
        ("soil-emissions:tco2e-per-ha:2015-2020", BandType.INTENSIVE),
        ("emissions:tco2e-per-ha:2000-2005", BandType.INTENSIVE),
        ("emissions:tco2e-per-ha:2005-2010", BandType.INTENSIVE),
        ("emissions:tco2e-per-ha:2010-2015", BandType.INTENSIVE),
        ("emissions:tco2e-per-ha:2015-2020", BandType.INTENSIVE),
        ("peatland-occupation:tco2e-per-ha", BandType.INTENSIVE),
        ("emissions-per-hectare:tco2e-per-ha", BandType.INTENSIVE),
        ("hectares-per-pixel:ha", BandType.EXTENSIVE),
    ),
)
def test_get_downsampling_for_variable_name(variable_name: str, expected: str) -> None:
    assert get_band_type_for_variable_name(variable_name=variable_name) == expected
