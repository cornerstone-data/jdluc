import pytest

from jdluc.continents import (
    ISO_3166_TO_CONTINENT,
    Continent,
    iter_tile_cluster_to_iso_3166s,
)
from jdluc.tiling import GLOBAL_FOREST_WATCH_TILE_IDS


def test_continent_tile_ids_are_valid() -> None:
    for continent in Continent:
        assert set(GLOBAL_FOREST_WATCH_TILE_IDS).issuperset(continent.value)


@pytest.mark.integration
def test_continenets_are_supersets() -> None:
    from jdluc.datasets.worldbank_jurisdictions import (
        AdminLevel,
        get_ten_degree_tile_ids_for_admin_id,
    )

    for iso_3166, continent in ISO_3166_TO_CONTINENT.items():
        tile_ids = set(
            get_ten_degree_tile_ids_for_admin_id(
                admin_id=iso_3166, admin_level=AdminLevel.NATIONAL.value
            )
        ) & set(GLOBAL_FOREST_WATCH_TILE_IDS)
        if iso_3166 == "GRL":
            # Drop Greenland because it doesn't contain much agriculture
            assert tile_ids - set(continent.value) == {
                "80N_070W",
                "70N_030W",
                "80N_080W",
            }
        elif iso_3166 == "RUS":
            # Drop Kamchatka the because it doesn't contain much agriculture
            assert tile_ids - set(continent.value) == {"70N_170W", "70N_180W"}
        elif iso_3166 == "USA":
            assert tile_ids - set(continent.value) == {
                # Drop Hawaii
                "20N_160W",
                "30N_160W",
                "30N_170W",
                # Drop Alaska
                "60N_150W",
                "60N_160W",
                "60N_170E",
                "60N_170W",
                "60N_180W",
                "70N_160W",
                "70N_170W",
                "70N_180W",
                "80N_150W",
                "80N_160W",
                "80N_170W",
            }
        elif iso_3166 in {"ASM", "FJI", "TON", "WLF", "WSM"}:
            # Don't cross the international date line
            assert tile_ids - set(continent.value) == {"10S_180W"}
        elif continent == Continent.UNCLASSIFIED:
            assert not tile_ids
        else:
            assert set(continent.value).issuperset(tile_ids)


def test_iter_tile_cluster_to_iso_3166s() -> None:
    it = iter_tile_cluster_to_iso_3166s(
        iso_3166s=(
            # AFRICA
            "AGO",
            "COD",
            "GHA",
            "ZAF",
            # ASIA
            "KHM",
            "IND",
            "THA",
            # EUROPE
            "FRA",
            "NOR",
            "TUR",
            # N AMERICA
            "CAN",
            "MEX",
            "USA",
            # OCEANIA
            "IDN",
            "MYS",
            "PNG",
            # RUSSIA
            "RUS",
            # S AMERICA
            "ARG",
            "BOL",
            "COL",
            # Unclassified
            "COK",
            "GUM",
        )
    )
    result = {tuple(sorted(tile_ids)): iso_3166s for tile_ids, iso_3166s in it}
    assert result == {
        Continent.AFRICA.value: {"AGO", "COD", "GHA", "ZAF"},
        Continent.ASIA.value: {"KHM", "IND", "THA"},
        Continent.EUROPE.value: {"FRA", "NOR", "TUR"},
        Continent.NORTH_AMERICA.value: {"CAN", "MEX", "USA"},
        Continent.OCEANIA.value: {"IDN", "MYS", "PNG"},
        Continent.RUSSIA: {
            "RUS",
        },
        Continent.SOUTH_AMERICA.value: {"ARG", "BOL", "COL"},
        Continent.UNCLASSIFIED.value: {"COK", "GUM"},
    }
