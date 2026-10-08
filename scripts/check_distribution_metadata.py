"""Reject dependency marker changes in the archives before publication."""

import argparse
import tarfile
import zipfile
from collections import Counter
from email.parser import BytesParser
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10 test environments include tomli via pytest.
    import tomli as tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

# Include both sides of the supported minor-version boundary and prereleases.
PYTHON_VERSIONS = (
    "3.9.0.dev0",
    "3.9.0a1",
    "3.9.0b1",
    "3.9.0rc1",
    "3.9.0",
    "3.9.99",
    "3.10.0.dev0",
    "3.10.0.dev1",
    "3.10.0a1",
    "3.10.0b1",
    "3.10.0rc1",
    "3.10.0",
    "3.10.0.post1",
    "3.10.1rc1",
    "3.10.1",
    "3.11.0.dev0",
    "3.11.0a1",
    "3.11.0",
    "3.12.0",
    "3.13.0",
    "3.14.0",
    "3.15.0",
    "4.0.0.dev0",
    "4.0.0",
)


def selected_requirements(requirements, python_version):
    environment = default_environment()
    release = Version(python_version).release
    environment.update(
        python_version=f"{release[0]}.{release[1]}",
        python_full_version=python_version,
    )
    selected = []
    for value in requirements:
        requirement = Requirement(value)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            requirement.name = canonicalize_name(requirement.name)
            requirement.marker = None
            selected.append(str(requirement))
    return Counter(selected)


def check_metadata(project, metadata):
    if canonicalize_name(metadata["Name"] or "") != canonicalize_name(project["name"]):
        raise ValueError("distribution name differs from source")
    if metadata["Version"] != project["version"]:
        raise ValueError("distribution version differs from source")
    if metadata["Requires-Python"] != project["requires-python"]:
        raise ValueError("Requires-Python differs from source")
    supported = SpecifierSet(project["requires-python"])
    actual = metadata.get_all("Requires-Dist", [])
    for version in PYTHON_VERSIONS:
        if not supported.contains(version, prereleases=True):
            continue
        expected = selected_requirements(project["dependencies"], version)
        if selected_requirements(actual, version) != expected:
            raise ValueError(f"dependency selection differs for Python {version}")


def read_metadata(archive):
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as wheel:
            paths = [p for p in wheel.namelist() if p.endswith(".dist-info/METADATA")]
            if len(paths) != 1:
                raise ValueError("wheel must contain exactly one METADATA file")
            data = wheel.read(paths[0])
    else:
        with tarfile.open(archive, "r:gz") as sdist:
            paths = [
                p
                for p in sdist.getmembers()
                if p.name.count("/") == 1 and p.name.endswith("/PKG-INFO") and p.isfile()
            ]
            if len(paths) != 1:
                raise ValueError("sdist must contain exactly one root PKG-INFO file")
            with sdist.extractfile(paths[0]) as metadata:
                data = metadata.read()
    return BytesParser().parsebytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    project = tomllib.loads(args.project.read_text())["project"]
    wheels = list(args.dist.glob("*.whl"))
    sdists = list(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one sdist")
    for archive in wheels + sdists:
        check_metadata(project, read_metadata(archive))
        print(f"{archive.name}: dependency metadata matches source")


if __name__ == "__main__":
    main()
