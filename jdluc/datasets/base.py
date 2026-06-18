import collections.abc
import dataclasses
import enum
import logging
import os
import tempfile
import typing

import pandas
import rasterio.enums

from jdluc import gcs, geo, tiling, utils

logger = logging.getLogger(__name__)


class AssetType(enum.Enum):
    @typing.override
    def __str__(self) -> str:
        return self.name.lower().replace("_", "-")

    RASTER = enum.auto()
    VECTOR = enum.auto()
    TABULAR = enum.auto()


SaveTileIdToLocalPathType = collections.abc.Callable[[str, str], None]


@dataclasses.dataclass
class RasterDataset:
    band_names: list[str]
    no_data: float | int | None
    partitioning: tiling.Partitioning
    product_name: str
    resampling: rasterio.enums.Resampling
    save_tile_id_to_local_path: SaveTileIdToLocalPathType
    source_name: str
    version: str

    def get_gcs_prefix(self, tile_id: str) -> str:
        return os.path.join(
            str(AssetType.RASTER),
            self.source_name,
            self.product_name,
            self.version,
            str(self.partitioning),
            f"{tile_id:s}.tif",
        )

    def ingest_a_tile(
        self, bucket_name: str, gcp_project: str, overwrite: bool, tile_id: str
    ) -> str:
        # Stage a geotiff to a tmpdir, clean up and convert to COG, then upload to a templated GCS prefix
        gcs_uri = gcs.get_uri_from_bucket_name_prefix(
            bucket_name=bucket_name, prefix=self.get_gcs_prefix(tile_id=tile_id)
        )
        if overwrite or not gcs.gcs_blob_exists(
            gcp_project=gcp_project, remote_path=gcs_uri
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                path_to_geotiff = os.path.join(tmpdir, "geotiff.tif")
                self.save_tile_id_to_local_path(path_to_geotiff, tile_id)
                geo.validate_geotiff(path_to_geotiff=path_to_geotiff)
                geo.set_band_names_for_geotiff(
                    band_names=self.band_names, path_to_geotiff=path_to_geotiff
                )
                path_to_cog = os.path.join(tmpdir, "cog.tif")
                geo.convert_geotiff_to_cog(
                    metadata={
                        "watershed-data-version": self.version,
                        "watershed-processing-time": utils.get_utc_timestamp(),
                        "watershed-processing-version": utils.get_git_version(),
                        "watershed-product-name": self.product_name,
                        "watershed-remote-url": utils.get_git_remote_url(),
                        "watershed-source-name": self.source_name,
                    },
                    no_data=self.no_data,
                    path_to_cog=path_to_cog,
                    path_to_geotiff=path_to_geotiff,
                )
                gcs.upload_local_path_to_gcs(
                    gcp_project=gcp_project, local_path=path_to_cog, remote_path=gcs_uri
                )
        else:
            logger.warning(
                f"Skipping upload of {tile_id=:s} to GCS because {gcs_uri=:s} "
                f"exists and {overwrite=:}"
            )
        return gcs_uri

    @property
    def fully_qualified_band_names(self) -> list[str]:
        return [
            ":".join((self.source_name, self.product_name, band_name))
            for band_name in self.band_names
        ]

    @property
    def fully_qualified_band_name(self) -> str:
        (ret,) = self.fully_qualified_band_names
        return ret


@dataclasses.dataclass
class VectorDataset:
    id_column_names: tuple[str, ...]
    name_column_names: tuple[str, ...]
    product_name: str
    save_tile_id_to_local_path: SaveTileIdToLocalPathType
    source_name: str
    version: str

    @property
    def partitioning(self) -> tiling.Partitioning:
        return tiling.Partitioning.WHOLE_WORLD

    def get_gcs_prefix(self, tile_id: str) -> str:
        return os.path.join(
            str(AssetType.VECTOR),
            self.source_name,
            self.product_name,
            self.version,
            str(self.partitioning),
            f"{tile_id:s}.fgb",
        )

    def ingest_a_tile(
        self, bucket_name: str, gcp_project: str, overwrite: bool, tile_id: str
    ) -> str:
        # Stage a vector file to a tmpdir, clean up and convert to flatgeobuf, then upload to a templated GCS prefix
        gcs_uri = gcs.get_uri_from_bucket_name_prefix(
            bucket_name=bucket_name, prefix=self.get_gcs_prefix(tile_id=tile_id)
        )
        if overwrite or not gcs.gcs_blob_exists(
            gcp_project=gcp_project, remote_path=gcs_uri
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                path_to_vector = os.path.join(tmpdir, "vector.dat")
                self.save_tile_id_to_local_path(path_to_vector, tile_id)
                path_to_flatgeobuf = os.path.join(tmpdir, "vector.fgb")
                geo.convert_vector_to_flatgeobuf(
                    id_column_names=self.id_column_names,
                    name_column_names=self.name_column_names,
                    path_to_flatgeobuf=path_to_flatgeobuf,
                    path_to_vector=path_to_vector,
                )
                gcs.upload_local_path_to_gcs(
                    gcp_project=gcp_project,
                    local_path=path_to_flatgeobuf,
                    remote_path=gcs_uri,
                )
        else:
            logger.warning(
                f"Skipping upload of {tile_id=:s} to GCS because {gcs_uri=:s} "
                f"exists and {overwrite=:}"
            )
        return gcs_uri


GetRecordsForTileIdType = typing.Callable[[str], list[dict[str, str | float]]]


@dataclasses.dataclass
class TabularDataset:
    get_records_for_tile_id: GetRecordsForTileIdType
    idx_column_names: list[str]
    product_name: str
    source_name: str
    version: str

    @property
    def partitioning(self) -> tiling.Partitioning:
        return tiling.Partitioning.WHOLE_WORLD

    def get_gcs_prefix(self, tile_id: str) -> str:
        return os.path.join(
            str(AssetType.TABULAR),
            self.source_name,
            self.product_name,
            self.version,
            str(self.partitioning),
            f"{tile_id:s}.parquet",
        )

    def ingest_a_tile(
        self, bucket_name: str, gcp_project: str, overwrite: bool, tile_id: str
    ) -> str:
        gcs_uri = gcs.get_uri_from_bucket_name_prefix(
            bucket_name=bucket_name, prefix=self.get_gcs_prefix(tile_id=tile_id)
        )
        if overwrite or not gcs.gcs_blob_exists(
            gcp_project=gcp_project, remote_path=gcs_uri
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                local_path = os.path.join(tmpdir, "data.parquet")
                records = self.get_records_for_tile_id(tile_id)
                df = (
                    pandas.DataFrame.from_records(data=records)
                    .set_index(self.idx_column_names)
                    .sort_index()
                )
                df.to_parquet(path=local_path)
                gcs.upload_local_path_to_gcs(
                    gcp_project=gcp_project, local_path=local_path, remote_path=gcs_uri
                )
        else:
            logger.warning(
                f"Skipping upload of {tile_id=:s} to GCS because {gcs_uri=:s} "
                f"exists and {overwrite=:}"
            )
        return gcs_uri
