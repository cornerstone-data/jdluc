"""World Bank | Official Boundaries

license: CC BY 4.0

year: 2026

https://datacatalog.worldbank.org/search/dataset/0038272/world-bank-official-boundaries
"""

import collections.abc
import enum
import functools
import logging

import geopandas
import shapely

from jdluc import config, gcs, tiling, utils
from jdluc.datasets import base

logger = logging.getLogger(__name__)


class AdminLevel(enum.Enum):
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


def get_ten_degree_tile_ids_for_admin_id(admin_id: str, admin_level: int) -> list[str]:
    dataset = ADMIN_LEVEL_TO_DATASET[AdminLevel(admin_level)]
    path_to_fgb = gcs.get_uri_from_bucket_name_prefix(
        bucket_name=config.Config.from_dot_env().ingest_bucket_name,
        prefix=dataset.get_gcs_prefix(tile_id="world"),
    )
    logger.info(
        f"Loading {admin_level=:d}'s geometry for {admin_id=:s}'s from {path_to_fgb=:s}"
    )
    geometry = (
        geopandas.read_file(filename=path_to_fgb)
        .set_index(keys="id")
        .loc[admin_id]
        .geometry
    )
    assert isinstance(geometry, shapely.Polygon | shapely.MultiPolygon)
    return sorted(tiling.iter_ten_degree_tile_id_for_geometry(geometry=geometry))


@functools.cache
def get_jurisdiction_for_admin_level(
    admin_level: AdminLevel,
) -> geopandas.GeoDataFrame:
    logger.info(f"Loading geometry for {admin_level.name=:s}")
    dataset = ADMIN_LEVEL_TO_DATASET[admin_level]
    return geopandas.read_file(
        filename=gcs.get_uri_from_bucket_name_prefix(
            bucket_name=config.Config.from_dot_env().ingest_bucket_name,
            prefix=dataset.get_gcs_prefix(tile_id="world"),
        )
    ).set_index("id")


def iter_province_for_iso_a3(
    iso_a3: str,
) -> collections.abc.Generator[tuple[str, str, shapely.Geometry]]:
    provincial = get_jurisdiction_for_admin_level(admin_level=AdminLevel.PROVINCIAL)
    for admin_id, row in sorted(provincial.iterrows()):
        if str(admin_id).startswith(iso_a3):
            yield str(admin_id), row["name"], row["geometry"]
