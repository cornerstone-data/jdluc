"""World Bank | Official Boundaries

license: CC BY 4.0

year: 2026

https://datacatalog.worldbank.org/search/dataset/0038272/world-bank-official-boundaries
"""

import collections.abc
import dataclasses
import enum
import functools
import logging

import geopandas
import shapely

from jdluc import config, storage, tiling, utils
from jdluc.datasets import base

logger = logging.getLogger(__name__)


class AdminLevel(enum.IntEnum):
    NATIONAL = 0
    PROVINCIAL = 1
    DISTRICT = 2


def _save_tile_id_to_local_path_for_admin_level(
    admin_level: AdminLevel,
) -> base.SaveTileIdToLocalPathType:
    remote_url = (
        "https://datacatalogfiles.worldbank.org/ddh-published/0038272/2/DR0095370/"
        "World Bank Official Boundaries (GeoPackage)/World Bank Official Boundaries - "
        f"Admin {admin_level.value:d}.gpkg"
    )

    def inner(local_path: str, tile_id: str) -> None:
        utils.save_remote_url_to_local_path(
            local_path=local_path,
            params={},
            remote_url=remote_url,
        )

    return inner


ADMIN_0_DATASET = base.VectorDataset(
    id_column_names=("ISO_A3",),
    name_column_names=("NAM_0",),
    product_name="admin-0",
    save_tile_id_to_local_path=_save_tile_id_to_local_path_for_admin_level(
        admin_level=AdminLevel.NATIONAL
    ),
    source_name="world-bank",
    version="v0",
)

ADMIN_1_DATASET = base.VectorDataset(
    id_column_names=("ADM1CD_c",),
    name_column_names=("NAM_0", "NAM_1"),
    product_name="admin-1",
    save_tile_id_to_local_path=_save_tile_id_to_local_path_for_admin_level(
        admin_level=AdminLevel.PROVINCIAL
    ),
    source_name="world-bank",
    version="v0",
)

ADMIN_2_DATASET = base.VectorDataset(
    id_column_names=("ADM2CD_c",),
    name_column_names=("NAM_0", "NAM_1", "NAM_2"),
    product_name="admin-2",
    save_tile_id_to_local_path=_save_tile_id_to_local_path_for_admin_level(
        admin_level=AdminLevel.DISTRICT
    ),
    source_name="world-bank",
    version="v0",
)


ADMIN_LEVEL_TO_DATASET = {
    AdminLevel.NATIONAL: ADMIN_0_DATASET,
    AdminLevel.PROVINCIAL: ADMIN_1_DATASET,
    AdminLevel.DISTRICT: ADMIN_2_DATASET,
}
assert set(AdminLevel) == set(ADMIN_LEVEL_TO_DATASET)


@functools.cache
def load_geodataframe_for_admin_level(admin_level: int) -> geopandas.GeoDataFrame:
    dataset = ADMIN_LEVEL_TO_DATASET[AdminLevel(admin_level)]
    path_to_fgb = storage.join_uri(
        root=config.Config.from_dot_env().ingest_root,
        prefix=dataset.get_prefix(tile_id="world"),
    )
    logger.info(f"Loading {admin_level=:d} geometrys from {path_to_fgb=:s}")
    return geopandas.read_file(filename=path_to_fgb).set_index(keys="id")


def get_ten_degree_tile_ids_for_admin_id(admin_id: str, admin_level: int) -> list[str]:
    gdf = load_geodataframe_for_admin_level(admin_level=admin_level)
    geometry = gdf.loc[admin_id].geometry
    assert isinstance(geometry, shapely.Polygon | shapely.MultiPolygon)
    return sorted(tiling.iter_ten_degree_tile_id_for_geometry(geometry=geometry))


@functools.cache
def get_jurisdiction_for_admin_level(
    admin_level: AdminLevel,
) -> geopandas.GeoDataFrame:
    logger.info(f"Loading geometry for {admin_level.name=:s}")
    dataset = ADMIN_LEVEL_TO_DATASET[admin_level]
    return geopandas.read_file(
        filename=storage.join_uri(
            root=config.Config.from_dot_env().ingest_root,
            prefix=dataset.get_prefix(tile_id="world"),
        )
    ).set_index("id")


@dataclasses.dataclass
class Jurisdiction:
    level: AdminLevel
    id: str
    name: str
    geometry: shapely.Geometry


def iter_jurisdiction_for_iso_3166(
    admin_level: AdminLevel, iso_3166: str
) -> collections.abc.Generator[Jurisdiction]:
    provincial = get_jurisdiction_for_admin_level(admin_level=admin_level)
    for admin_id, row in sorted(provincial.iterrows()):
        if str(admin_id).startswith(iso_3166):
            yield Jurisdiction(
                level=admin_level,
                id=str(admin_id),
                name=row["name"],
                geometry=row["geometry"],
            )
