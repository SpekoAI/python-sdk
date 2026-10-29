"""Regressions for the published Python 3.10 prerelease dependency gap."""

import importlib.util
import unittest
from email.message import Message
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_distribution_metadata", ROOT / "scripts/check_distribution_metadata.py"
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
PROJECT = CHECK.tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def metadata(requirements):
    result = Message()
    for key, value in (
        ("Name", PROJECT["name"]),
        ("Version", PROJECT["version"]),
        ("Requires-Python", PROJECT["requires-python"]),
    ):
        result[key] = value
    for requirement in requirements:
        result["Requires-Dist"] = requirement
    return result


class DistributionMetadataTests(unittest.TestCase):
    def test_original_supported_dependency_ranges(self):
        self.assertEqual(PROJECT["requires-python"], ">=3.9")
        self.assertFalse(SpecifierSet(PROJECT["requires-python"]).contains("3.8.99"))
        for version in CHECK.PYTHON_VERSIONS:
            with self.subTest(python=version):
                selected = CHECK.selected_requirements(PROJECT["dependencies"], version)
                if Version(version).release[:2] == (3, 9):
                    expected = {"aiortc<1.14.0,>=1.13.0", "av<14.3.0,>=14.0.0"}
                else:
                    expected = {"aiortc<2.0.0,>=1.15.0", "av<18.0.0,>=14.0.0"}
                actual = {key for key in selected if key.startswith(("aiortc", "av"))}
                self.assertEqual(actual, expected)
                self.assertTrue(all(selected[key] == 1 for key in expected))

    def test_accepts_normalized_partition(self):
        requirements = [
            r.replace("python_version", "python_full_version").replace("'3.9'", "'3.9.*'")
            for r in PROJECT["dependencies"]
        ]
        CHECK.check_metadata(PROJECT, metadata(requirements))

    def test_rejects_published_prerelease_gap(self):
        requirements = [
            r.replace("python_version == '3.9'", "python_full_version < '3.10'").replace(
                "python_version != '3.9'", "python_full_version >= '3.10'"
            )
            for r in PROJECT["dependencies"]
        ]
        with self.assertRaisesRegex(ValueError, "Python 3.10.0.dev0"):
            CHECK.check_metadata(PROJECT, metadata(requirements))

    def test_rejects_missing_or_duplicate_dependency(self):
        for requirements in (PROJECT["dependencies"][:-1], PROJECT["dependencies"] * 2):
            with self.subTest(requirements=requirements):
                with self.assertRaisesRegex(ValueError, "dependency selection differs"):
                    CHECK.check_metadata(PROJECT, metadata(requirements))

    def test_rejects_changed_python_floor(self):
        value = metadata(PROJECT["dependencies"])
        value.replace_header("Requires-Python", ">=3.10")
        with self.assertRaisesRegex(ValueError, "Requires-Python differs"):
            CHECK.check_metadata(PROJECT, value)


if __name__ == "__main__":
    unittest.main()
