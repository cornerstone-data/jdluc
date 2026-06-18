import pytest

from jdluc.datasets.glad_glcluc import flatten_ranges
from jdluc.datasets.worldbank_jurisdictions import get_ten_degree_tile_ids_for_admin_id


def test_flatten_ranges() -> None:
    assert flatten_ranges(range(0, 5), range(5, 10), range(10, 15)) == list(range(15))


@pytest.mark.integration
def test_get_ten_degree_tile_ids_for_country() -> None:
    iso_a3 = "BRA"  # Brazil
    expected = (
        "00N_040W",
        "00N_050W",
        "00N_060W",
        "00N_070W",
        "00N_080W",
        "10N_050W",
        "10N_060W",
        "10N_070W",
        "10N_080W",
        "10S_040W",
        "10S_050W",
        "10S_060W",
        "10S_070W",
        "10S_080W",
        "20S_030W",
        "20S_050W",
        "20S_060W",
        "30S_060W",
    )
    assert (
        tuple(get_ten_degree_tile_ids_for_admin_id(admin_id=iso_a3, admin_level=0))
        == expected
    )
