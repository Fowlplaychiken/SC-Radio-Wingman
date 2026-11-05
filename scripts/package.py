"""Build the source-only SC Radio install archive from this standalone checkout."""

import argparse
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
TINYTAG_HASHES = {
    "dependencies/sc_radio_tinytag/__init__.py": (
        "b901456763a1da35824e25ffbebec3123e3e37539a58120dfd5fbd2c8ddb626b"
    ),
    "dependencies/sc_radio_tinytag/tinytag.py": (
        "f3ffb48ddc305cda53b574cde9c314c08defa6a7b9705fe655ba2d5f52bf27a8"
    ),
    "dependencies/sc_radio_tinytag/LICENSE": (
        "497f61aac3d877f48539dfc38d3c35215ba53648d0f9c4ac7fec467a0dc0cb4d"
    ),
}
FILES = (
    "LICENSE",
    "NOTICE.md",
    "README.md",
    "assets/sc-radio.svg",
    "default_config.yaml",
    "main.py",
    *TINYTAG_HASHES,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Path to the new sc_radio ZIP")
    out = parser.parse_args().output
    members = {name: (ROOT / name).read_bytes() for name in FILES}
    for name, digest in TINYTAG_HASHES.items():
        if sha256(members[name]).hexdigest() != digest:
            parser.error(f"bundled TinyTag bytes differ from the pinned upstream wheel: {name}")
    with ZipFile(out, "x") as archive:
        for name, raw in members.items():
            info = ZipInfo(f"sc_radio/{name}", (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw, compress_type=ZIP_DEFLATED)
    print(out)


if __name__ == "__main__":
    main()
