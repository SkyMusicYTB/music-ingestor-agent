from __future__ import annotations

import subprocess
import sys
import textwrap
import zipfile
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from hatchling import build as hatchling_build
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pinned_requirements(path: Path) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-r", "--", "\\")):
            continue
        requirement = Requirement(line.removesuffix(" \\"))
        exact_versions = [
            Version(specifier.version)
            for specifier in requirement.specifier
            if specifier.operator == "=="
        ]
        if len(exact_versions) == 1:
            pins[canonicalize_name(requirement.name)] = exact_versions[0]
    return pins


def test_editable_backend_requirements_are_explicitly_locked() -> None:
    inputs = _pinned_requirements(REPO_ROOT / "requirements" / "development.in")
    lock = _pinned_requirements(REPO_ROOT / "requirements" / "development.lock")

    for raw_requirement in hatchling_build.get_requires_for_build_editable({}):
        requirement = Requirement(raw_requirement)
        name = canonicalize_name(requirement.name)
        assert name in inputs
        assert name in lock
        assert inputs[name] == lock[name]
        assert inputs[name] in requirement.specifier


def test_development_lock_covers_supported_linux_dependency_markers() -> None:
    lock = _pinned_requirements(REPO_ROOT / "requirements" / "development.lock")

    for python_version in ("3.12", "3.13", "3.14"):
        environment = default_environment()
        environment.update(
            {
                "extra": "",
                "os_name": "posix",
                "platform_machine": "x86_64",
                "platform_system": "Linux",
                "python_full_version": f"{python_version}.0",
                "python_version": python_version,
                "sys_platform": "linux",
            }
        )
        for package_name in lock:
            try:
                installed = distribution(package_name)
            except PackageNotFoundError:
                continue
            for raw_requirement in installed.requires or ():
                requirement = Requirement(raw_requirement)
                if requirement.marker and not requirement.marker.evaluate(environment):
                    continue
                dependency_name = canonicalize_name(requirement.name)
                assert dependency_name in lock, (
                    f"{package_name} requires {raw_requirement!r} on Linux/Python "
                    f"{python_version}, but it is absent from the development lock"
                )
                assert lock[dependency_name] in requirement.specifier, (
                    f"{package_name} requires {raw_requirement!r} on Linux/Python "
                    f"{python_version}, but the lock pins {lock[dependency_name]}"
                )


def test_installed_wheel_contains_runtime_data_and_can_migrate(tmp_path: Path) -> None:
    wheel_directory = tmp_path / "wheel"
    wheel_directory.mkdir()
    subprocess.run(  # noqa: S603 - fixed interpreter and repository under test
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--quiet",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_directory),
            ".",
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    wheels = list(wheel_directory.glob("music_agent-*.whl"))
    assert len(wheels) == 1

    installed = tmp_path / "site-packages"
    installed.mkdir()
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        archive.extractall(installed)

    required = {
        "alembic.ini",
        "migrations/env.py",
        "migrations/script.py.mako",
        "migrations/versions/0001_initial_schema.py",
        "migrations/versions/0002_source_decisions_and_hardening.py",
        "app/templates/base.html",
        "app/static/app.css",
        "app/static/app.js",
        "app/prompts/orchestrator_v1.txt",
        "app/prompts/source_selector_v1.txt",
        "app/prompts/source_matcher_v2.txt",
        "app/prompts/canonical_matcher_v2.txt",
    }
    assert not required.difference(names)

    runtime = tmp_path / "runtime"
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        sys.path.insert(0, sys.argv[1])
        from app.cli import migrate
        from app.config import Settings
        from app.db.engine import create_database_engine, current_revision

        root = Path(sys.argv[2])
        settings = Settings(
            environment="test",
            database_path=root / "state" / "music-agent.db",
            artwork_path=root / "state" / "artwork",
            downloads_path=root / "downloads",
            music_path=root / "music",
            backup_path=root / "backups",
        )
        migrate(settings)
        engine = create_database_engine(settings)
        try:
            assert current_revision(engine) == "0002"
        finally:
            engine.dispose()
        """
    )
    subprocess.run(  # noqa: S603 - isolated installed-wheel smoke test
        [sys.executable, "-c", script, str(installed), str(runtime)],
        cwd=tmp_path,
        check=True,
    )
