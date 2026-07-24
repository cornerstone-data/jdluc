"""Mosaic ingested source tiles onto a common grid and harmonize them into one dataset.

For a tile set, each raster dataset's tiles are stitched into a per-band GDAL VRT, warped to
a shared grid, and returned as an xarray.Dataset with one variable per source band. The result is cached.

Example invocation:
  uv run python jdluc/harmonize.py NORTH_AMERICA
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
import rasterio.errors
import rasterio.shutil
import rasterio.transform
import rasterio.vrt
import rioxarray
import xarray

from jdluc import config, continents, geo, ingest, storage, tiling
from jdluc.datasets import NAME_TO_CLS, DatasetName, base

logger = logging.getLogger(__name__)


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
    band_type: base.BandType
    dtype: str
    no_data: float | int | None
    resolution: tiling.XY
    uri: str

    @classmethod
    def from_dataset_tile_id(
        cls, dataset: base.RasterDataset, root: str, tile_id: str
    ) -> typing.Self:
        uri = storage.join_uri(root=root, prefix=dataset.get_prefix(tile_id=tile_id))
        with rasterio.open(fp=uri) as ds:
            rio_dtype = next(iter(ds.dtypes))
            return cls(
                band_type=dataset.band_type,
                dtype=RIO_TO_GDAL_DTYPE[rio_dtype],
                no_data=ds.nodata,
                resolution=tiling.XY(x=ds.width, y=ds.height).validated(),
                uri=uri,
            )

    @property
    def gdal_path(self) -> str:
        return storage.to_gdal_path(uri=self.uri)


def iter_vrt_band_header(
    band_name: str, dtype: str, no_data: int | float | None
) -> collections.abc.Iterator[str]:
    yield f'  <VRTRasterBand dataType="{dtype:s}" band="1">'
    yield f"    <Description>{band_name:s}</Description>"
    yield f"    <NoDataValue>{no_data}</NoDataValue>"


def iter_vrt_band_content(
    band_idx: int,
    dest_offset: tiling.XY,
    dest_resolution: tiling.XY,
    path_to_tile: str,
    resampling: rasterio.enums.Resampling,
    src_offset: tiling.XY,
    src_resolution: tiling.XY,
) -> collections.abc.Iterator[str]:
    yield f'    <SimpleSource resampling="{resampling.name:}">'
    yield f'      <SourceFilename relativeToVRT="0">{path_to_tile:s}</SourceFilename>'
    yield f"      <SourceBand>{band_idx:d}</SourceBand>"
    yield f'      <SrcRect xOff="{src_offset.x:d}" yOff="{src_offset.y:d}" xSize="{src_resolution.x:d}" ySize="{src_resolution.y:d}"/>'
    yield f'      <DstRect xOff="{dest_offset.x:d}" yOff="{dest_offset.y:d}" xSize="{dest_resolution.x:d}" ySize="{dest_resolution.y:d}"/>'
    yield "    </SimpleSource>"


@dataclasses.dataclass
class Grid:
    origin: tiling.XY
    tiles: tiling.XY
    tile_resolution: tiling.XY

    @property
    def epsg(self) -> int:
        return 4326

    @classmethod
    def from_tile_ids_resolution(
        cls, tile_ids: collections.abc.Sequence[str], tile_resolution: tiling.XY
    ) -> typing.Self:
        lats, lons = zip(*map(tiling.get_lat_lon_for_tile_id, tile_ids), strict=True)
        return cls(
            origin=tiling.XY(x=min(lons), y=max(lats)),
            tiles=tiling.XY(
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
    def resolution(self) -> tiling.XY:
        return tiling.XY(
            x=self.tiles.x * self.tile_resolution.x,
            y=self.tiles.y * self.tile_resolution.y,
        ).validated()

    @property
    def iter_preamble(self) -> collections.abc.Iterator[str]:
        yield f'<VRTDataset rasterXSize="{self.resolution.x:d}" rasterYSize="{self.resolution.y:d}">'
        yield f"  <SRS>EPSG:{self.epsg:d}</SRS>"
        yield f"  <GeoTransform>{', '.join(map(str, self.transform))}</GeoTransform>"

    def get_offset_for_tile(self, tile_id: str) -> tiling.XY:
        lat, lon = tiling.get_lat_lon_for_tile_id(tile_id=tile_id)
        return tiling.XY(
            x=(lon - self.origin.x) // 10 * self.tile_resolution.x,
            # Y index increases downward
            y=(self.origin.y - lat) // 10 * self.tile_resolution.y,
        ).validated()

    def get_offset_for_world(self, resolution: tiling.XY, span: tiling.XY) -> tiling.XY:
        pixels_per_degree = tiling.XY(
            x=resolution.x // span.x,
            y=resolution.y // span.y,
        ).validated()
        return tiling.XY(
            x=(self.origin.x + span.x // 2) * pixels_per_degree.x,
            y=(span.y // 2 - self.origin.y) * pixels_per_degree.y,
        ).validated()

    def get_resolution_for_world(
        self, resolution: tiling.XY, span: tiling.XY
    ) -> tiling.XY:
        return tiling.XY(
            x=resolution.x * 10 // span.x * self.tiles.x,
            y=resolution.y * 10 // span.y * self.tiles.y,
        ).validated()

    @staticmethod
    def get_downsampling_for_band_type(
        band_type: base.BandType,
    ) -> rasterio.enums.Resampling:
        match band_type:
            case base.BandType.CATEGORICAL:
                return rasterio.enums.Resampling.mode
            case base.BandType.EXTENSIVE:
                raise NotImplementedError("GDAL doesn't implement sum resampling")
            case base.BandType.INTENSIVE:
                return rasterio.enums.Resampling.average
            case _:
                raise ValueError(band_type)

    @staticmethod
    def get_upsampling_for_band_type(
        band_type: base.BandType,
    ) -> rasterio.enums.Resampling:
        match band_type:
            case base.BandType.CATEGORICAL:
                return rasterio.enums.Resampling.nearest
            case base.BandType.EXTENSIVE:
                raise NotImplementedError(
                    "GDAL doesn't implement distribution resampling"
                )
            case base.BandType.INTENSIVE:
                return rasterio.enums.Resampling.bilinear
            case _:
                raise ValueError(band_type)

    def get_resampling_for_band_type(
        self,
        band_type: base.BandType,
        dest_resolution: tiling.XY,
        src_resolution: tiling.XY,
    ) -> rasterio.enums.Resampling:
        if src_resolution == dest_resolution:
            return rasterio.enums.Resampling.nearest
        elif (
            src_resolution.x > dest_resolution.x
            and src_resolution.y > dest_resolution.y
        ):
            return self.get_downsampling_for_band_type(band_type=band_type)
        else:
            return self.get_upsampling_for_band_type(band_type=band_type)


def get_vrt_for_dataset_band_tile_ids(
    band_idx: int,
    band_name: str,
    dataset: base.RasterDataset,
    grid: Grid,
    ignore_missing_tiles: bool,
    root: str,
    tile_ids: collections.abc.Sequence[str],
) -> str:
    lines = list(grid.iter_preamble)

    logger.info(f"Processing {dataset=} and {band_name=:s}")
    if dataset.partitioning == tiling.Partitioning.TEN_DEGREE_TILE:
        includes_band_header = False
        for tile_id in tile_ids:
            try:
                tile = Tile.from_dataset_tile_id(
                    root=root,
                    dataset=dataset,
                    tile_id=tile_id,
                )
            except rasterio.errors.RasterioIOError:
                if ignore_missing_tiles:
                    logger.warning(
                        f"{tile_id=:s} is missing for {dataset=} but due to {ignore_missing_tiles=} we are continuing"
                    )
                    continue
                else:
                    raise
            else:
                if not includes_band_header:
                    lines.extend(
                        iter_vrt_band_header(
                            band_name=band_name,
                            dtype=tile.dtype,
                            no_data=tile.no_data,
                        )
                    )
                    includes_band_header = True
                lines.extend(
                    iter_vrt_band_content(
                        band_idx=band_idx,
                        dest_offset=grid.get_offset_for_tile(tile_id=tile_id),
                        dest_resolution=grid.tile_resolution,
                        path_to_tile=tile.gdal_path,
                        resampling=grid.get_resampling_for_band_type(
                            band_type=tile.band_type,
                            dest_resolution=grid.tile_resolution,
                            src_resolution=tile.resolution,
                        ),
                        src_offset=tiling.XY(x=0, y=0),
                        src_resolution=tile.resolution,
                    )
                )
    elif dataset.partitioning == tiling.Partitioning.WHOLE_WORLD:
        tile = Tile.from_dataset_tile_id(
            root=root,
            dataset=dataset,
            tile_id=tiling.WHOLE_WORLD_TILE_ID,
        )
        lines.extend(
            iter_vrt_band_header(
                band_name=band_name,
                dtype=tile.dtype,
                no_data=tile.no_data,
            )
        )
        world_span = tiling.XY(x=360, y=180)
        src_offset = grid.get_offset_for_world(
            resolution=tile.resolution, span=world_span
        )
        src_resolution = grid.get_resolution_for_world(
            resolution=tile.resolution, span=world_span
        )
        lines.extend(
            iter_vrt_band_content(
                band_idx=band_idx,
                dest_offset=tiling.XY(x=0, y=0),
                dest_resolution=grid.resolution,
                path_to_tile=tile.gdal_path,
                resampling=grid.get_resampling_for_band_type(
                    band_type=tile.band_type,
                    src_resolution=src_resolution,
                    dest_resolution=grid.resolution,
                ),
                src_offset=src_offset,
                src_resolution=src_resolution,
            )
        )
    else:
        raise ValueError(dataset.partitioning)
    lines.append("  </VRTRasterBand>")
    lines.append("</VRTDataset>")

    with tempfile.NamedTemporaryFile(
        delete=False, mode="w", suffix=".mosaic.vrt"
    ) as fp:
        logger.debug(f"Writing mosaic to {fp.name=:s}")
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
        tempfile.NamedTemporaryFile(delete=False, suffix=".warped.vrt") as warped_fp,
    ):
        logger.debug(f"Writing warped VRT to {warped_fp.name=:s}")
        rasterio.shutil.copy(warped_vrt, warped_fp.name, driver="VRT")
    return warped_fp.name


def get_dset_for_output(path_to_vrts: collections.abc.Sequence[str]) -> xarray.Dataset:
    darrays: list[xarray.DataArray] = []
    chunk_size = geo.get_chunk_size(dtypes=[numpy.dtype("float32")] * len(path_to_vrts))
    for path_to_vrt in path_to_vrts:
        logger.debug(f"Opening {path_to_vrt=:s} with {chunk_size=:d}")
        darray = rioxarray.open_rasterio(
            filename=path_to_vrt,
            chunks=chunk_size,
            # Remove the serialization lock because this is read-only
            lock=False,
        )
        assert isinstance(darray, xarray.DataArray)
        darrays.append(
            geo.unify_dtype_and_no_data(
                darray=darray.isel(band=0, drop=True).rename(
                    darray.attrs.pop("long_name")
                )
            )
        )

    return xarray.Dataset({darray.name: darray for darray in darrays})


@storage.cache_to_zarr(version=0, ignored_args=["ignore_missing_tiles", "skip_ingest"])
def workflow(
    dataset_names: tuple[DatasetName, ...],
    ignore_missing_tiles: bool,
    skip_ingest: bool,
    tile_ids: tuple[str, ...],
    tile_resolution: tiling.XY,
) -> xarray.Dataset:
    logger.info(
        f"Running the harmonize workflow for {dataset_names=:} and {tile_ids=:}"
    )
    datasets = list(map(NAME_TO_CLS.__getitem__, dataset_names))
    cfg = config.Config.from_dot_env()

    if not skip_ingest:
        for dataset in datasets:
            ingest.workflow(
                concurrency=ingest.DEFAULT_CONCURRENCY,
                dataset=dataset,
                overwrite=False,
                root=cfg.ingest_root,
                tile_ids=tile_ids,
            )

    logger.info(f"Constructing common grid for {tile_resolution=:}")
    grid = Grid.from_tile_ids_resolution(
        tile_ids=tile_ids, tile_resolution=tile_resolution
    )
    path_to_vrts = [
        get_vrt_for_dataset_band_tile_ids(
            band_idx=band_idx,
            band_name=band_name,
            root=cfg.ingest_root,
            dataset=dataset,
            grid=grid,
            ignore_missing_tiles=ignore_missing_tiles,
            tile_ids=tile_ids,
        )
        for dataset in datasets
        if isinstance(dataset, base.RasterDataset)
        for band_idx, band_name in enumerate(
            dataset.fully_qualified_band_names, start=1
        )
    ]
    return get_dset_for_output(path_to_vrts=path_to_vrts)


LUC_AND_EMISSIONS_DATASET_NAMES = (
    DatasetName.GFW_GLOBAL_PEATLANDS,
    DatasetName.GFW_HARRIS_AGB,
    DatasetName.GLAD_GLCLUC,
    DatasetName.HUANG_BGB,
    DatasetName.IPCC_CLIMATE_ZONES,
    DatasetName.SOILGRIDS_OCS,
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "continent_names",
        choices=sorted(e.name for e in continents.Continent),
        nargs=argparse.ONE_OR_MORE,
    )
    parser.add_argument(
        "--grid-name",
        choices=sorted(e.name for e in tiling.TileResolution),
        default=tiling.TileResolution.GLAD.name,
        type=str,
    )
    parser.add_argument("--ignore-missing-tiles", action="store_true")
    parser.add_argument("--skip-ingest", action="store_true")
    args = parser.parse_args()

    for continent_name in map(str, args.continent_names):
        workflow(
            dataset_names=LUC_AND_EMISSIONS_DATASET_NAMES,
            ignore_missing_tiles=args.ignore_missing_tiles,
            skip_ingest=args.skip_ingest,
            tile_ids=continents.Continent[continent_name].value,
            tile_resolution=tiling.TileResolution[str(args.grid_name)].value,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
