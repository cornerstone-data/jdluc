import collections.abc
import logging
import math
import typing

import geopandas
import numpy
import rasterio
import rio_cogeo.cogeo
import rio_cogeo.profiles
import shapely
import xarray

logger = logging.getLogger(__name__)


def set_band_names_for_geotiff(
    band_names: collections.abc.Sequence[str], path_to_geotiff: str
) -> None:
    logger.info(f"Setting {band_names=:} for {path_to_geotiff=:s}")
    with rasterio.open(fp=path_to_geotiff, mode="r+") as dataset:
        for idx, band_name in enumerate(band_names, start=1):
            dataset.set_band_description(idx, band_name)


def get_overview_level(height: int, width: int, minimum_pixels: int = 64) -> int:
    if (pixel_ratio := min(height, width) // minimum_pixels) > 0:
        return math.floor(math.log2(pixel_ratio))
    else:
        return 0


def convert_geotiff_to_cog(
    metadata: dict[str, str],
    no_data: float | int | None,
    path_to_cog: str,
    path_to_geotiff: str,
) -> None:
    logger.info(f"Getting overview level from {path_to_geotiff=:s}")
    with rasterio.open(fp=path_to_geotiff) as dataset:
        overview_level = get_overview_level(
            height=dataset.height,
            width=dataset.width,
        )
    logger.info(f"Converting from {path_to_geotiff=:s} to {path_to_cog=:s}")
    rio_cogeo.cogeo.cog_translate(
        additional_cog_metadata=metadata,
        config={
            "GDAL_NUM_THREADS": "ALL_CPUS",
            "GDAL_TIFF_INTERNAL_MASK": True,
        },
        dst_kwargs=dict(rio_cogeo.profiles.DEFLATEProfile()) | {"BIGTIFF": "IF_SAFER"},
        dst_path=path_to_cog,
        in_memory=False,
        nodata=no_data,
        overview_level=overview_level,
        quiet=False,
        source=path_to_geotiff,
    )


def convert_vector_to_flatgeobuf(
    id_column_names: tuple[str, ...],
    name_column_names: tuple[str, ...],
    path_to_flatgeobuf: str,
    path_to_vector: str,
) -> None:
    logger.info(
        f"Opening {path_to_vector=:s} for {id_column_names=:} and {name_column_names=:}"
    )
    gdf: geopandas.GeoDataFrame = geopandas.read_file(
        filename=path_to_vector, columns=id_column_names + name_column_names
    )
    logger.info(
        f"Forming {id_column_names=:} and {name_column_names=:}, dissolving, and saving to {path_to_flatgeobuf=:s}"
    )
    (
        geopandas.GeoDataFrame(
            crs=gdf.crs,
            data={
                "id": gdf[list(id_column_names)].astype(str).agg(" | ".join, axis=1),
                "name": gdf[list(name_column_names)]
                .astype(str)
                .agg(" | ".join, axis=1),
            },
            geometry=gdf.geometry,
        )
        .dissolve(by="id")
        .reset_index(drop=False)
        .to_file(path_to_flatgeobuf, driver="FlatGeobuf", SPATIAL_INDEX=True)
    )


def validate_geotiff(path_to_geotiff: str) -> None:
    logger.info(f"Validating integrity of {path_to_geotiff=:s}")
    with rasterio.open(path_to_geotiff) as dataset:
        assert isinstance(dataset, rasterio.DatasetReader)
        assert dataset.crs is not None
        assert dataset.crs.to_epsg() == 4326
        assert dataset.height > 0
        assert dataset.width > 0
        assert dataset.transform.a > 0
        assert dataset.transform.e < 0
        assert dataset.width == dataset.height


def get_chunk_size(
    dtypes: collections.abc.Iterable[numpy.dtype],
    number_of_dimensions: int,
    # 4 GiB
    max_bytes_per_chunk: int = 1 << 32,
) -> int:
    # Sum over all the variables
    bytes_per_pixel = sum(dtype.itemsize for dtype in dtypes)
    pixels_per_chunk = max_bytes_per_chunk / bytes_per_pixel
    # Round down to nearest power of two
    return 1 << int(math.log2(pixels_per_chunk) / number_of_dimensions)


ATTRS_TO_MOVE = ("_FillValue", "scale_factor", "add_offset", "dtype", "missing_value")


def unify_dtype_and_no_data(darray: xarray.DataArray) -> xarray.DataArray:
    import rioxarray  # noqa

    if (no_data := darray.rio.nodata) is not None:
        darray = darray.where(darray != no_data, other=numpy.nan)
    darray = darray.astype(numpy.float32).rio.write_nodata(numpy.nan)
    assert isinstance(darray, xarray.DataArray)
    for key in ATTRS_TO_MOVE:
        if key in darray.attrs:
            darray.encoding[key] = darray.attrs.pop(key)
    return darray.transpose(..., "y", "x")


def clip_dset(dset: xarray.Dataset, geometry: shapely.Geometry) -> xarray.Dataset:
    assert isinstance(geometry, shapely.Polygon | shapely.MultiPolygon)
    import rioxarray  # noqa

    ret = dset.rio.clip_box(*geometry.bounds).rio.clip([geometry], drop=True)
    return typing.cast(xarray.Dataset, ret)
