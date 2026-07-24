import collections
import logging

import geopandas
import shapely

from jdluc.continents import ISO_3166_TO_CONTINENT, Continent
from jdluc.datasets.worldbank_jurisdictions import (
    AdminLevel,
    get_jurisdiction_for_admin_level,
)
from jdluc.tiling import get_box_for_tile_id

logger = logging.getLogger(__name__)


def dump_boundaries(
    iso_3166_to_continent: dict[str, Continent], path_to_geojson: str
) -> None:
    nationals = get_jurisdiction_for_admin_level(admin_level=AdminLevel.NATIONAL)

    continent_to_iso_3166s: dict[Continent, list[str]] = collections.defaultdict(list)
    for iso_3166, cluster in iso_3166_to_continent.items():
        continent_to_iso_3166s[cluster].append(iso_3166)
    logger.info("Merging national boundaries")
    continent_to_geometry = {
        continent.name: shapely.unary_union(
            [
                nationals.loc[iso_3166].geometry.simplify(tolerance=0.1)
                for iso_3166 in iso_3166s
            ]
        )
        for continent, iso_3166s in continent_to_iso_3166s.items()
    }
    gdf = geopandas.GeoDataFrame(
        crs=4326,
        geometry=[geometry for _, geometry in sorted(continent_to_geometry.items())],
        index=sorted(continent_to_geometry),
    )
    logger.info(f"Writing to {path_to_geojson=:s}")
    gdf.to_file(filename=path_to_geojson, driver="GeoJSON")


def dump_tiles(
    continent_to_tile_ids: dict[str, set[str]], path_to_geojson: str
) -> None:
    logger.info("Merging tiles")
    continent_to_geometry = {
        continent: shapely.unary_union(list(map(get_box_for_tile_id, tile_ids)))
        for continent, tile_ids in continent_to_tile_ids.items()
    }
    gdf = geopandas.GeoDataFrame(
        crs=4326,
        geometry=[geometry for _, geometry in sorted(continent_to_geometry.items())],
        index=sorted(continent_to_tile_ids),
    )
    logger.info(f"Writing to {path_to_geojson=:s}")
    gdf.to_file(filename=path_to_geojson, driver="GeoJSON")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    for continent in Continent:
        logger.info(f"{continent.name:s} contains {len(continent.value):d} tiles")

    continent_to_iso_3166s: dict[Continent, set[str]] = collections.defaultdict(
        set[str]
    )
    for iso_3166, continent in ISO_3166_TO_CONTINENT.items():
        continent_to_iso_3166s[continent].add(iso_3166)
    for continent, iso_3166s in continent_to_iso_3166s.items():
        logger.info(f"{continent.name:s} contains {len(iso_3166s):d} countries")

    dump_boundaries(
        iso_3166_to_continent=ISO_3166_TO_CONTINENT,
        path_to_geojson="boundaries.geojson",
    )
    dump_tiles(
        continent_to_tile_ids={c.name: c.value for c in Continent},
        path_to_geojson="tiles.geojson",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
