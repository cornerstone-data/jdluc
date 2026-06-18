"""Mosaic ingested source tiles onto a common grid and harmonize them into one dataset.

For a tile set, each raster dataset's tiles are stitched into a per-band GDAL VRT, warped to
the shared GLAD 30m / 0.00025° grid (4,000 px/degree), and returned as an xarray.Dataset with
one variable per source band. The result is cached to GCS (`@gcs.cache`).

Example invocation:
  uv run python jdluc/harmonize.py CONUS
"""

import argparse
import collections.abc
import dataclasses
import logging
import tempfile
import typing

import numpy
import rasterio
import rasterio.enums
import rasterio.shutil
import rasterio.transform
import rasterio.vrt
import rioxarray
import xarray

from jdluc import config, gcs, geo, ingest, tiling
from jdluc.datasets import NAME_TO_CLS, DatasetName, base

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class XY:
    x: int
    y: int

    def validated(self) -> typing.Self:
        assert self.x >= 0 and self.y >= 0
        return self


RIO_TO_GDAL_DTYPE = {
    "uint8": "Byte",
    "uint16": "UInt16",
    "int16": "Int16",
    "uint32": "UInt32",
    "int32": "Int32",
    "float32": "Float32",
    "float64": "Float64",
}


@dataclasses.dataclass
class Tile:
    dtype: str
    gcs_uri: str
    no_data: float | int | None
    resampling: rasterio.enums.Resampling
    resolution: XY

    @classmethod
    def from_dataset_tile_id(
        cls, bucket_name: str, dataset: base.RasterDataset, tile_id: str
    ) -> typing.Self:
        gcs_uri = gcs.get_uri_from_bucket_name_prefix(
            bucket_name=bucket_name, prefix=dataset.get_gcs_prefix(tile_id=tile_id)
        )
        with rasterio.open(fp=gcs_uri) as ds:
            rio_dtype = next(iter(ds.dtypes))
            return cls(
                dtype=RIO_TO_GDAL_DTYPE[rio_dtype],
                gcs_uri=gcs_uri,
                no_data=ds.nodata,
                resampling=dataset.resampling,
                resolution=XY(x=ds.width, y=ds.height).validated(),
            )

    @property
    def gdal_gcs_uri(self) -> str:
        return "/vsigs/" + self.gcs_uri.removeprefix("gs://")


def iter_vrt_band_header(
    band_name: str, dtype: str, no_data: int | float | None
) -> collections.abc.Iterator[str]:
    yield f'  <VRTRasterBand dataType="{dtype:s}" band="1">'
    yield f"    <Description>{band_name:s}</Description>"
    yield f"    <NoDataValue>{no_data}</NoDataValue>"


def iter_vrt_band_content(
    band_idx: int,
    dest_offset: XY,
    dest_resolution: XY,
    path_to_tile: str,
    resampling: rasterio.enums.Resampling,
    src_offset: XY,
    src_resolution: XY,
) -> collections.abc.Iterator[str]:
    yield f'    <SimpleSource resampling="{resampling.name:}">'
    yield f'      <SourceFilename relativeToVRT="0">{path_to_tile:s}</SourceFilename>'
    yield f"      <SourceBand>{band_idx:d}</SourceBand>"
    yield f'      <SrcRect xOff="{src_offset.x:d}" yOff="{src_offset.y:d}" xSize="{src_resolution.x:d}" ySize="{src_resolution.y:d}"/>'
    yield f'      <DstRect xOff="{dest_offset.x:d}" yOff="{dest_offset.y:d}" xSize="{dest_resolution.x:d}" ySize="{dest_resolution.y:d}"/>'
    yield "    </SimpleSource>"


@dataclasses.dataclass
class Grid:
    origin: XY
    tiles: XY
    tile_resolution: XY

    @property
    def epsg(self) -> int:
        return 4326

    @classmethod
    def from_tile_ids_resolution(
        cls, tile_ids: collections.abc.Sequence[str], tile_resolution: XY
    ) -> typing.Self:
        lats, lons = zip(*map(tiling.get_lat_lon_for_tile_id, tile_ids))
        return cls(
            origin=XY(x=min(lons), y=max(lats)),
            tiles=XY(
                x=(max(lons) - min(lons)) // 10 + 1,
                y=(max(lats) - min(lats)) // 10 + 1,
            ).validated(),
            tile_resolution=tile_resolution,
        )

    @property
    def transform(self) -> tuple[float, float, float, float, float, float]:
        return (
            self.origin.x,
            10 / self.tile_resolution.x,
            0,
            self.origin.y,
            0,
            -10 / self.tile_resolution.y,
        )

    @property
    def resolution(self) -> XY:
        return XY(
            x=self.tiles.x * self.tile_resolution.x,
            y=self.tiles.y * self.tile_resolution.y,
        ).validated()

    @property
    def iter_preamble(self) -> collections.abc.Iterator[str]:
        yield f'<VRTDataset rasterXSize="{self.resolution.x:d}" rasterYSize="{self.resolution.y:d}">'
        yield f"  <SRS>EPSG:{self.epsg:d}</SRS>"
        yield f"  <GeoTransform>{', '.join(map(str, self.transform))}</GeoTransform>"

    def get_offset_for_tile(self, tile_id: str) -> XY:
        lat, lon = tiling.get_lat_lon_for_tile_id(tile_id=tile_id)
        return XY(
            x=(lon - self.origin.x) // 10 * self.tile_resolution.x,
            # Y index increases downward
            y=(self.origin.y - lat) // 10 * self.tile_resolution.y,
        ).validated()

    def get_offset_for_world(self, resolution: XY, span: XY) -> XY:
        pixels_per_degree = XY(
            x=resolution.x // span.x,
            y=resolution.y // span.y,
        ).validated()
        return XY(
            x=(self.origin.x + span.x // 2) * pixels_per_degree.x,
            y=(span.y // 2 - self.origin.y) * pixels_per_degree.y,
        ).validated()

    def get_resolution_for_world(self, resolution: XY, span: XY) -> XY:
        return XY(
            x=resolution.x * 10 // span.x * self.tiles.x,
            y=resolution.y * 10 // span.y * self.tiles.y,
        ).validated()


def get_vrt_for_dataset_band_tile_ids(
    band_idx: int,
    band_name: str,
    bucket_name: str,
    dataset: base.RasterDataset,
    grid: Grid,
    tile_ids: collections.abc.Sequence[str],
) -> str:
    lines = list(grid.iter_preamble)

    logger.info(f"Processing {dataset=} and {band_name=:s}")
    if dataset.partitioning == tiling.Partitioning.TEN_DEGREE_TILE:
        for tile_idx, tile_id in enumerate(tile_ids):
            tile = Tile.from_dataset_tile_id(
                bucket_name=bucket_name,
                dataset=dataset,
                tile_id=tile_id,
            )
            if tile_idx == 0:
                lines.extend(
                    iter_vrt_band_header(
                        band_name=band_name,
                        dtype=tile.dtype,
                        no_data=tile.no_data,
                    )
                )
            lines.extend(
                iter_vrt_band_content(
                    band_idx=band_idx,
                    dest_offset=grid.get_offset_for_tile(tile_id=tile_id),
                    dest_resolution=grid.tile_resolution,
                    path_to_tile=tile.gdal_gcs_uri,
                    resampling=tile.resampling,
                    src_offset=XY(x=0, y=0),
                    src_resolution=tile.resolution,
                )
            )
    elif dataset.partitioning == tiling.Partitioning.WHOLE_WORLD:
        (tile_id,) = tiling.NAME_TO_TILE_SET[tiling.TileSetName.WHOLE_WORLD]
        tile = Tile.from_dataset_tile_id(
            bucket_name=bucket_name,
            dataset=dataset,
            tile_id=tile_id,
        )
        lines.extend(
            iter_vrt_band_header(
                band_name=band_name,
                dtype=tile.dtype,
                no_data=tile.no_data,
            )
        )
        lines.extend(
            iter_vrt_band_content(
                band_idx=band_idx,
                dest_offset=XY(x=0, y=0),
                dest_resolution=grid.resolution,
                path_to_tile=tile.gdal_gcs_uri,
                resampling=tile.resampling,
                src_offset=grid.get_offset_for_world(
                    resolution=tile.resolution,
                    span=XY(x=360, y=180),
                ),
                src_resolution=grid.get_resolution_for_world(
                    resolution=tile.resolution,
                    span=XY(x=360, y=180),
                ),
            )
        )
    else:
        raise ValueError(dataset.partitioning)
    lines.append("  </VRTRasterBand>")
    lines.append("</VRTDataset>")

    with tempfile.NamedTemporaryFile(
        delete=False, mode="w", suffix=".mosaic.vrt"
    ) as fp:
        logger.info(f"Writing mosaic to {fp.name=:s}")
        fp.writelines(line + "\n" for line in lines)
        mosaic_path = fp.name

    with (
        rasterio.open(mosaic_path) as mosaic_fp,
        rasterio.vrt.WarpedVRT(
            mosaic_fp,
            crs="EPSG:4326",
            transform=rasterio.transform.Affine.from_gdal(*grid.transform),
            width=grid.resolution.x,
            height=grid.resolution.y,
            resampling=rasterio.enums.Resampling.nearest,
        ) as warped_vrt,
        tempfile.NamedTemporaryFile(
            delete=False, mode="w", suffix=".warped.vrt"
        ) as warped_fp,
    ):
        logger.info(f"Writing warped VRT to {warped_fp.name=:s}")
        rasterio.shutil.copy(warped_vrt, warped_fp.name, driver="VRT")
    return warped_fp.name


def get_dset_for_output(path_to_vrts: collections.abc.Sequence[str]) -> xarray.Dataset:
    darrays: list[xarray.DataArray] = []
    chunk_size = geo.get_chunk_size(
        dtypes=[numpy.dtype("float32")] * len(path_to_vrts), number_of_dimensions=2
    )
    for path_to_vrt in path_to_vrts:
        logger.info(f"Opening {path_to_vrt=:s} with {chunk_size=:d}")
        darray = rioxarray.open_rasterio(
            filename=path_to_vrt,
            chunks=chunk_size,
            # Remove the serialization lock because this is read-only
            lock=False,
        )
        assert isinstance(darray, xarray.DataArray)
        darrays.append(
            geo.unify_dtype_and_no_data(
                darray=darray.isel(band=0)
                .drop_vars("band")
                .rename(darray.attrs.pop("long_name"))
            )
        )

    return xarray.Dataset({darray.name: darray for darray in darrays})


GLAD_TILE_RESOLUTION = XY(
    x=tiling.PIXELS_PER_TEN_DEGREE_TILE, y=tiling.PIXELS_PER_TEN_DEGREE_TILE
)


@gcs.cache(version=1)
def workflow(
    dataset_names: tuple[DatasetName, ...], tile_ids: tuple[str, ...]
) -> xarray.Dataset:
    logger.info(
        f"Running the harmonize workflow for {dataset_names=:} and {tile_ids=:}"
    )
    datasets = list(map(NAME_TO_CLS.__getitem__, dataset_names))
    cfg = config.Config.from_dot_env()

    for dataset in datasets:
        tile_set = (
            ("world",)
            if dataset.partitioning == tiling.Partitioning.WHOLE_WORLD
            else tile_ids
        )
        ingest.workflow(
            bucket_name=cfg.ingest_bucket_name,
            dataset=dataset,
            concurrency=ingest.DEFAULT_CONCURRENCY,
            gcp_project=cfg.gcp_project,
            overwrite=False,
            tile_set=tile_set,
        )

    logger.info("Constructing common grid")
    grid = Grid.from_tile_ids_resolution(
        tile_ids=tile_ids, tile_resolution=GLAD_TILE_RESOLUTION
    )
    path_to_vrts = [
        get_vrt_for_dataset_band_tile_ids(
            band_idx=band_idx,
            band_name=band_name,
            bucket_name=cfg.ingest_bucket_name,
            dataset=dataset,
            grid=grid,
            tile_ids=tile_ids,
        )
        for dataset in datasets
        if isinstance(dataset, base.RasterDataset)
        for band_idx, band_name in enumerate(
            dataset.fully_qualified_band_names, start=1
        )
    ]
    return get_dset_for_output(path_to_vrts=path_to_vrts)


DATASET_NAMES = (
    DatasetName.GFW_GLOBAL_PEATLANDS,
    DatasetName.GFW_HARRIS_AGB,
    DatasetName.GLAD_GLCLUC,
    DatasetName.HUANG_BGB,
    DatasetName.IPCC_CLIMATE_ZONES,
    DatasetName.SOILGRIDS_OCS,
    DatasetName.USDA_NASS_CDL,
)
assert set(DATASET_NAMES) == {
    dataset_name
    for dataset_name in DatasetName
    if isinstance(NAME_TO_CLS[dataset_name], base.RasterDataset)
}


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
    workflow(
        dataset_names=DATASET_NAMES, tile_ids=tiling.NAME_TO_TILE_SET[tile_set_name]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
