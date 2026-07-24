import argparse
import concurrent.futures
import dataclasses
import logging
import random
import subprocess
import sys
import typing

from jdluc import continents

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Trace:
    iso_3166: str
    returncode: int
    stdout: str

    @classmethod
    def from_iso_3166(cls, iso_3166: str) -> typing.Self:
        result = subprocess.run(
            [sys.executable, "-m", "jdluc.trace", "--skip-display", iso_3166],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return cls(
            iso_3166=iso_3166, returncode=result.returncode, stdout=result.stdout
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    iso_3166s = [
        iso_3166
        for iso_3166, continent in continents.ISO_3166_TO_CONTINENT.items()
        if continent != continents.Continent.UNCLASSIFIED
    ]
    random.shuffle(iso_3166s)

    failed: list[Trace] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[concurrent.futures.Future[Trace], str] = {
            pool.submit(Trace.from_iso_3166, iso_3166): iso_3166
            for iso_3166 in iso_3166s
        }
        for completed, future in enumerate(
            concurrent.futures.as_completed(futures), start=1
        ):
            trace = future.result()
            logger.log(
                logging.INFO if trace.returncode == 0 else logging.ERROR,
                f"< {trace.iso_3166:s} > exited {trace.returncode:d} ({completed:d}/{len(iso_3166s):d})",
            )
            if trace.returncode != 0:
                failed.append(trace)

    for trace in failed:
        print(trace.iso_3166)
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
