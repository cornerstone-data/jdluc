import collections.abc

import numpy
import pytest
import xarray

from jdluc.attribution import Crop, JurisdictionalCropEmission
from jdluc.datasets.worldbank_jurisdictions import AdminLevel


def get_darray_for_data(
    data: collections.abc.Sequence[collections.abc.Sequence[float]],
) -> xarray.DataArray:
    arr = numpy.array(data)
    y, x = arr.shape
    return xarray.DataArray(
        coords={"y": range(y), "x": range(x)}, data=arr, dims=("y", "x")
    )


def test_from_xarray() -> None:
    dset = xarray.Dataset(
        {
            # 99s sit on non-crop pixels — they must be excluded by the mask.
            "emissions-per-hectare:tco2e-per-ha": get_darray_for_data(
                [[1, 2, 99], [99, 4, 99]]
            ),
            "hectares-per-pixel:ha": get_darray_for_data([[10, 10, 10], [10, 10, 10]]),
            "gfw:global-peatlands:is-peatland": get_darray_for_data(
                [[1, 0, 0], [0, 1, 0]]
            ),
            "land-class:2020": get_darray_for_data([[2] * 2] * 2),
            "peatland-occupation:tco2e-per-ha": get_darray_for_data(
                [[5, 5, 5], [5, 5, 5]]
            ),
            # crop pixels: (0,0), (0,1), (1,1); of those, peatland at (0,0) and (1,1).
            "usda-nass:cdl:crop-class": get_darray_for_data([[1, 1, 0], [0, 1, 0]]),
        }
    )
    result = JurisdictionalCropEmission.from_dset(
        admin_id="USA008",
        admin_level=AdminLevel.PROVINCIAL,
        crop=Crop.CORN,
        dset=dset,
        jurisdiction_name="Delaware",
        skip_glad_crop_filter=False,
    )
    assert result == JurisdictionalCropEmission(
        admin_id="USA008",
        admin_level="PROVINCIAL",
        crop_hectares=30.0,  # 3 crop pixels × 10 ha
        crop_name="CORN",
        jurisdiction_name="Delaware",
        peatland_crop_hectares=20.0,  # only (0,0) and (1,1) are crop AND peatland
        peatland_occupation_emissions=150.0,  # 3 crop pixels × (5 × 10)
        total_emissions=70.0,  # (1+2+4) × 10; non-crop 99s excluded
    )


def test_from_xarray_masked_out_by_glad() -> None:
    dset = xarray.Dataset(
        {
            "emissions-per-hectare:tco2e-per-ha": get_darray_for_data([[1, 2], [3, 4]]),
            "hectares-per-pixel:ha": get_darray_for_data([[10, 10], [10, 10]]),
            "gfw:global-peatlands:is-peatland": get_darray_for_data([[1, 1], [1, 1]]),
            "land-class:2020": get_darray_for_data([[0] * 2] * 2),
            "peatland-occupation:tco2e-per-ha": get_darray_for_data([[5, 5], [5, 5]]),
            "usda-nass:cdl:crop-class": get_darray_for_data([[1, 1], [1, 1]]),
        }
    )
    result = JurisdictionalCropEmission.from_dset(
        admin_id="USA008",
        admin_level=AdminLevel.PROVINCIAL,
        crop=Crop.CORN,
        dset=dset,
        jurisdiction_name="Delaware",
        skip_glad_crop_filter=False,
    )
    assert result.crop_hectares == 0.0
    assert result.peatland_crop_hectares == 0.0
    assert result.peatland_occupation_emissions == 0.0
    assert result.total_emissions == 0.0


@pytest.mark.parametrize("glad_value", (0, 1, 2))
@pytest.mark.parametrize("skip_glad_crop_filter", (False, True))
def test_from_xarray_with_no_crop_pixels(
    glad_value: int, skip_glad_crop_filter: bool
) -> None:
    dset = xarray.Dataset(
        {
            "emissions-per-hectare:tco2e-per-ha": get_darray_for_data([[1, 2], [3, 4]]),
            "hectares-per-pixel:ha": get_darray_for_data([[10, 10], [10, 10]]),
            "gfw:global-peatlands:is-peatland": get_darray_for_data([[1, 1], [1, 1]]),
            "land-class:2020": get_darray_for_data([[glad_value] * 2] * 2),
            "peatland-occupation:tco2e-per-ha": get_darray_for_data([[5, 5], [5, 5]]),
            "usda-nass:cdl:crop-class": get_darray_for_data([[0, 0], [0, 0]]),
        }
    )
    result = JurisdictionalCropEmission.from_dset(
        admin_id="USA008",
        admin_level=AdminLevel.PROVINCIAL,
        crop=Crop.CORN,
        dset=dset,
        jurisdiction_name="Delaware",
        skip_glad_crop_filter=skip_glad_crop_filter,
    )
    assert result.crop_hectares == 0.0
    assert result.peatland_crop_hectares == 0.0
    assert result.peatland_occupation_emissions == 0.0
    assert result.total_emissions == 0.0


def test_from_constituents() -> None:
    constituents = [
        JurisdictionalCropEmission(
            admin_id="",
            admin_level="",
            crop_hectares=value,
            crop_name="CROP",
            jurisdiction_name="",
            peatland_crop_hectares=value,
            peatland_occupation_emissions=value,
            total_emissions=value,
        )
        for value in (1, 2, 3)
    ]
    result = JurisdictionalCropEmission.from_constituents(
        admin_id="ADMIN_ID",
        admin_level=AdminLevel.NATIONAL,
        constituents=constituents,
        jurisdiction_name="JURISDICTION_NAME",
    )
    assert result.admin_id == "ADMIN_ID"
    assert result.admin_level == AdminLevel.NATIONAL.name
    assert result.crop_hectares == 6
    assert result.jurisdiction_name == "JURISDICTION_NAME"
    assert result.peatland_crop_hectares == 6
    assert result.peatland_occupation_emissions == 6
    assert result.total_emissions == 6
