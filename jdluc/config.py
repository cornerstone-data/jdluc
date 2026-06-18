import dataclasses
import functools
import logging
import typing

import dotenv

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Config:
    ingest_bucket_name: str
    gcp_project: str
    number_of_dask_workers: int
    scratch_bucket_name: str
    usda_nass_api_key: str

    @classmethod
    @functools.cache
    def from_dot_env(cls) -> typing.Self:
        logger.info("Searching for a .env file")
        path_to_env = dotenv.find_dotenv(raise_error_if_not_found=True)
        logger.info(f"Loading configs from {path_to_env=:s}")
        configs = dotenv.dotenv_values(path_to_env)
        types = typing.get_type_hints(cls)
        return cls(
            **{
                field.name: types[field.name](configs[field.name.upper()])
                for field in dataclasses.fields(cls)
            }
        )
