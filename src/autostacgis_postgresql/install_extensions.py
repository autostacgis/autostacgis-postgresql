"""Install only the pgSTAC-required contrib modules from a verified PG 16 archive."""

import argparse
import io
import os
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def install_extensions(archive, destination):
    root = Path(destination).resolve()
    with zipfile.ZipFile(archive) as jar:
        payloads = [name for name in jar.namelist() if name.endswith(".txz")]
        if len(payloads) != 1:
            raise ValueError("Expected one native PostgreSQL archive")
        with tarfile.open(fileobj=io.BytesIO(jar.read(payloads[0]))) as bundle:
            count = 0
            for entry in bundle:
                relative = PurePosixPath(entry.name)
                name = relative.name
                required = name.startswith(("btree_gist.", "btree_gist--", "unaccent.", "unaccent--"))
                dependency = os.name == "nt" and relative.parts[0] == "bin" and name.endswith(".dll")
                if not (required or dependency) or not entry.isfile():
                    continue
                target = (root / entry.name).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("Extension archive path escapes the runtime")
                if dependency and target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(entry)
                if stream is None:
                    raise ValueError("Missing extension payload")
                target.write_bytes(stream.read())
                target.chmod(entry.mode & 0o777)
                count += int(required)
            if count < 4:
                raise ValueError("Archive does not include the required btree_gist and unaccent files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("destination")
    args = parser.parse_args()
    install_extensions(args.archive, args.destination)


if __name__ == "__main__":
    main()
