import dataclasses
import functools
import hashlib
import inspect
import logging
import os
import typing
import urllib.parse

import git
import google.cloud.storage
import pandas
import xarray

logger = logging.getLogger(__name__)


def get_bucket_name_prefix_from_uri(uri: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(uri)
    bucket_name = parsed.netloc
    prefix = parsed.path.lstrip("/")
    return bucket_name, prefix


def get_uri_from_bucket_name_prefix(bucket_name: str, prefix: str) -> str:
    return f"gs://{bucket_name:s}/{prefix:s}"


@functools.cache
def get_gcs_client(gcp_project: str) -> google.cloud.storage.Client:
    return google.cloud.storage.Client(project=gcp_project)


def gcs_blob_exists(gcp_project: str, remote_path: str) -> bool:
    bucket_name, prefix = get_bucket_name_prefix_from_uri(uri=remote_path)
    return (
        get_gcs_client(gcp_project=gcp_project)
        .bucket(bucket_name=bucket_name)
        .blob(blob_name=prefix)
        .exists()
    )


def upload_local_path_to_gcs(
    gcp_project: str, local_path: str, remote_path: str
) -> None:
    logger.info(
        f"Uploading from {local_path=:s} to {remote_path=:s} within {gcp_project=:s}"
    )
    bucket_name, prefix = get_bucket_name_prefix_from_uri(uri=remote_path)
    (
        get_gcs_client(gcp_project=gcp_project)
        .bucket(bucket_name=bucket_name)
        .blob(blob_name=prefix)
        .upload_from_filename(filename=local_path)
    )


def write_dask_dataset_to_zarr(dset: xarray.Dataset, path_to_zarr: str) -> None:
    assert dset.chunks is not None, f"{dset=:} is not a chunked dask array"

    import dask.config
    import dask.diagnostics
    import rasterio

    from jdluc.config import Config

    num_workers = Config.from_dot_env().number_of_dask_workers

    logger.info(
        f"Saving to {path_to_zarr=:s} with {num_workers=:d} and {dset.chunksizes=:}"
    )
    with (
        rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.vrt",
        ),
        dask.config.set(scheduler="threads", num_workers=num_workers),
        dask.diagnostics.ProgressBar(dt=5, minimum=1),
    ):
        dset.drop_vars("spatial_ref").to_zarr(
            compute=False, consolidated=False, group=None, store=path_to_zarr
        ).compute()


def open_zarr_to_dask_dataset(path_to_zarr: str) -> xarray.Dataset:
    import rioxarray  # noqa

    logger.info(f"Loading {path_to_zarr=:s}")
    dset = xarray.open_zarr(store=path_to_zarr, consolidated=False)
    assert dset.chunks is not None
    assert isinstance(dset, xarray.Dataset)
    assert dset.encoding["source"] == path_to_zarr
    return dset.rio.write_crs(4326)


P = typing.ParamSpec("P")
R = typing.TypeVar("R")


class CacherProtocol(typing.Protocol[R]):
    def __init__(self, *, bucket_name: str, hash_key: str) -> None: ...
    @property
    def exists_uri(self) -> str: ...
    def deserialize(self) -> R: ...
    def serialize(self, value: R) -> None: ...


@dataclasses.dataclass
class ParquetCacher:
    bucket_name: str
    hash_key: str

    @property
    def gcs_uri(self) -> str:
        return get_uri_from_bucket_name_prefix(
            bucket_name=self.bucket_name, prefix=f"{self.hash_key:s}.parquet"
        )

    exists_uri = gcs_uri

    def deserialize(self) -> pandas.DataFrame:
        logger.info(f"Loading from {self.gcs_uri=:s}")
        return pandas.read_parquet(path=self.gcs_uri)

    def serialize(self, df: pandas.DataFrame) -> None:
        logger.info(f"Saving to {self.gcs_uri=:s}")
        df.to_parquet(path=self.gcs_uri)


@dataclasses.dataclass
class ZarrCacher:
    bucket_name: str
    hash_key: str

    @property
    def gcs_uri(self) -> str:
        return get_uri_from_bucket_name_prefix(
            bucket_name=self.bucket_name, prefix=f"{self.hash_key:s}.zarr"
        )

    @property
    def exists_uri(self) -> str:
        return f"{self.gcs_uri:s}/zarr.json"

    def deserialize(self) -> xarray.Dataset:
        return open_zarr_to_dask_dataset(path_to_zarr=self.gcs_uri)

    def serialize(self, dset: xarray.Dataset) -> None:
        write_dask_dataset_to_zarr(dset=dset, path_to_zarr=self.gcs_uri)


RETURN_TYPE_TO_CACHE_CLS: dict[typing.Any, type[ParquetCacher] | type[ZarrCacher]] = {
    pandas.DataFrame: ParquetCacher,
    xarray.Dataset: ZarrCacher,
}


class CacherDecoratorProtocol(typing.Protocol):
    def __call__(
        self, func: typing.Callable[P, R]
    ) -> functools._lru_cache_wrapper[R]: ...


def cache(
    version: int, ignored_args: list[str] | None = None
) -> CacherDecoratorProtocol:
    def get_module_for_func(func: typing.Callable[..., object]) -> str:
        relative_path = os.path.relpath(
            path=inspect.getfile(func),
            start=git.Repo(search_parent_directories=True).working_dir,
        )
        return relative_path.removesuffix(".py").replace(os.sep, ".")

    def decorator(
        func: typing.Callable[P, R],
    ) -> functools._lru_cache_wrapper[R]:
        cache_cls = RETURN_TYPE_TO_CACHE_CLS[inspect.signature(func).return_annotation]

        from jdluc.config import Config

        @functools.cache
        @functools.wraps(func)
        def inner(*args: P.args, **kwargs: P.kwargs) -> R:
            hash_tokens = (
                get_module_for_func(func=func),
                func.__qualname__,
                version,
                *args,
                *(
                    (arg, value)
                    for arg, value in sorted(kwargs.items())
                    if (ignored_args is None) or (arg not in ignored_args)
                ),
            )
            data = "|".join(map(str, hash_tokens)).encode()
            hash_key = hashlib.sha1(data=data).hexdigest()[:12]
            config = Config.from_dot_env()
            cacher = typing.cast(
                CacherProtocol[R],
                cache_cls(bucket_name=config.scratch_bucket_name, hash_key=hash_key),
            )
            if gcs_blob_exists(
                gcp_project=config.gcp_project,
                remote_path=cacher.exists_uri,
            ):
                ret = cacher.deserialize()
            else:
                ret = func(*args, **kwargs)
                cacher.serialize(ret)
            return ret

        return inner

    return decorator
