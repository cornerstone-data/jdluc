import pytest

from jdluc.gcs import (
    get_bucket_name_prefix_from_uri,
    get_uri_from_bucket_name_prefix,
)


@pytest.mark.parametrize(
    "bucket_name", ("bucket_name", "with-dash", "with space and .")
)
@pytest.mark.parametrize("prefix", ("file-at-root.txt", "path/to/nested/file.txt"))
def test_bucket_name_prefix_uri_roundtrip(bucket_name: str, prefix: str) -> None:
    assert get_bucket_name_prefix_from_uri(
        uri=get_uri_from_bucket_name_prefix(bucket_name=bucket_name, prefix=prefix)
    ) == (bucket_name, prefix)
