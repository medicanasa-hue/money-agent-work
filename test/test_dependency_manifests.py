import re
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _pinned_dependencies(values: list[str]) -> dict[str, str]:
    dependencies = {}
    for value in values:
        requirement = Requirement(value)
        specifiers = list(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
        ):
            raise AssertionError(f"dependency must be exactly pinned: {value!r}")
        normalized = str(requirement.specifier)
        if requirement.marker is not None:
            normalized = f"{normalized}; {requirement.marker}"
        dependencies[_canonical_name(requirement.name)] = normalized
    return dependencies


class DependencyManifestTest(unittest.TestCase):
    def test_legacy_requirements_match_runtime_dependencies(self):
        """Keep Docker's legacy pip install list aligned with uv's runtime list."""
        pyproject = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project_dependencies = _pinned_dependencies(
            pyproject["project"]["dependencies"]
        )
        requirement_lines = (
            (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        )
        requirements = _pinned_dependencies(
            [
                line
                for line in requirement_lines
                if line.strip() and not line.startswith("#")
            ]
        )

        self.assertEqual(requirements, project_dependencies)
