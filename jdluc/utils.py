import datetime
import functools
import logging
import threading
import typing

import git
import requests

logger = logging.getLogger(__name__)


@functools.cache
def get_requests_session() -> requests.Session:
    logger.info("Creating a fresh session for requests")
    return requests.Session()


def save_remote_url_to_local_path(
    local_path: str,
    params: dict[str, str] | str,
    remote_url: str,
    # 4 MiB
    chunk_size: int = 1 << 22,
) -> None:
    logger.info(f"GET'ing from {remote_url=:s} with {params=:}")
    with get_requests_session().request(
        method="GET", params=params, stream=True, url=remote_url
    ) as response:
        response.raise_for_status()
        logger.info(f"Streaming data from {remote_url=:s} to {local_path=:s}")
        with open(file=local_path, mode="wb") as fp:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    fp.write(chunk)


@functools.cache
def get_git_version(default_branch_name: str = "main") -> str:
    repo = git.Repo(__file__, search_parent_directories=True)
    if (branch_name := repo.active_branch.name) == default_branch_name:
        return f"{branch_name:s}-{repo.head.commit.hexsha[:8]:s}"
    else:
        return branch_name


@functools.cache
def get_git_remote_url(default_remote_name: str = "origin") -> str:
    repo = git.Repo(__file__, search_parent_directories=True)
    (remote,) = (
        remote for remote in repo.remotes if remote.name == default_remote_name
    )
    return next(iter(remote.urls))


def get_utc_timestamp() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def threadsafe_cache[**P, R](func: typing.Callable[P, R]) -> typing.Callable[P, R]:
    cached = functools.cache(func)

    # each cached function has its own lock
    lock = threading.Lock()

    @functools.wraps(cached)
    def inner(*args: P.args, **kwargs: P.kwargs) -> R:
        with lock:
            return cached(*args, **kwargs)  # type: ignore

    inner.cache_clear = cached.cache_clear  # type: ignore
    inner.cache_info = cached.cache_info  # type: ignore
    inner.cache_parameters = cached.cache_parameters  # type: ignore
    return inner
