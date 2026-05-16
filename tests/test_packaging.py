"""Tests for the portable-zip install layout — system YAML loader, data-dir
resolution, first-launch defaults, and three-tier profile precedence.

These tests cover the v0.2.0 packaging restructure. They live in their own
file because they touch app-level bootstrap (``ensure_user_editable_files``,
``resolve_data_dir``) which is otherwise only exercised at process start.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from romulus import app as romulus_app
from romulus.core import exporter
from romulus.models import system as system_module
from romulus.models.system import (
    _FALLBACK_REGISTRY,
    _DuplicateSystemIdError,
    load_systems_from_yaml,
)

# ---------------------------------------------------------------------------
# System YAML loader
# ---------------------------------------------------------------------------


class TestSystemYamlRoundTrip:
    """Bundled systems/builtin.yaml must reproduce ``_FALLBACK_REGISTRY`` exactly.

    Once the hardcoded list is removed in a future release this test
    becomes a snapshot freeze — until then it pins the YAML to be byte-
    equivalent to the in-code source of truth, so a typo in the YAML
    can never silently change which platforms ship.
    """

    def test_builtin_yaml_round_trip_matches_fallback(self) -> None:
        bundled = Path(__file__).resolve().parent.parent / "systems"
        assert bundled.is_dir(), (
            "expected systems/ at the repo root next to pyproject.toml"
        )
        loaded = load_systems_from_yaml([bundled])
        assert len(loaded) == len(_FALLBACK_REGISTRY), (
            f"YAML has {len(loaded)} entries, fallback has "
            f"{len(_FALLBACK_REGISTRY)}"
        )
        for live, fb in zip(loaded, _FALLBACK_REGISTRY, strict=True):
            assert live == fb, (
                f"YAML diverges from _FALLBACK_REGISTRY at id={fb.id!r}: "
                f"live={live.model_dump()}\nfallback={fb.model_dump()}"
            )

    def test_module_level_registry_matches_yaml(self) -> None:
        """SYSTEM_REGISTRY is populated at import time from the YAML."""
        assert len(system_module.SYSTEM_REGISTRY) == len(_FALLBACK_REGISTRY)


class TestSystemYamlValidation:
    def test_duplicate_id_across_files_raises(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            yaml.dump({
                "systems": [{
                    "id": "snes",
                    "display_name": "Super Nintendo",
                    "short_name": "SNES",
                }]
            }),
            encoding="utf-8",
        )
        (tmp_path / "b.yaml").write_text(
            yaml.dump({
                "systems": [{
                    "id": "snes",
                    "display_name": "Super Nintendo (duplicate)",
                    "short_name": "SNES",
                }]
            }),
            encoding="utf-8",
        )
        with pytest.raises(_DuplicateSystemIdError, match="snes"):
            load_systems_from_yaml([tmp_path])

    def test_top_level_not_mapping_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just a list", encoding="utf-8")
        with pytest.raises(ValueError, match="top-level"):
            load_systems_from_yaml([bad])

    def test_systems_not_list_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.dump({"systems": {"id": "snes"}}), encoding="utf-8")
        with pytest.raises(ValueError, match="must be a list"):
            load_systems_from_yaml([bad])

    def test_invalid_system_entry_raises_with_id(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        # ``extensions`` must start with a dot per SystemDef's validator.
        bad.write_text(
            yaml.dump({
                "systems": [{
                    "id": "weirdo",
                    "display_name": "Weirdo",
                    "short_name": "W",
                    "extensions": ["nope"],
                }]
            }),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="weirdo"):
            load_systems_from_yaml([bad])

    def test_load_skips_missing_paths(self, tmp_path: Path) -> None:
        # Nonexistent paths are silently skipped — the loader can be called
        # against optional user-supplied directories without pre-checking.
        result = load_systems_from_yaml([tmp_path / "does-not-exist"])
        assert result == []

    def test_load_handles_empty_yaml(self, tmp_path: Path) -> None:
        # Empty file should be skipped, not raise.
        (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
        result = load_systems_from_yaml([tmp_path])
        assert result == []

    def test_fallback_used_when_yaml_load_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken YAML during module init must not crash the app."""
        bad_dir = tmp_path / "systems"
        bad_dir.mkdir()
        (bad_dir / "broken.yaml").write_text("- :\n  - not yaml", encoding="utf-8")

        monkeypatch.setattr(
            system_module, "_resolve_bundled_systems_dir", lambda: bad_dir
        )
        result = system_module._initial_registry()
        # Falls back to the hardcoded list rather than raising.
        assert result == _FALLBACK_REGISTRY

    def test_reload_registry_updates_module_attr(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            yaml.dump({
                "systems": [{
                    "id": "test_only",
                    "display_name": "Test Only",
                    "short_name": "TST",
                    "extensions": [".tst"],
                }]
            }),
            encoding="utf-8",
        )
        try:
            count = system_module.reload_registry([custom])
            assert count == 1
            assert system_module.SYSTEM_REGISTRY[0].id == "test_only"
        finally:
            # Restore the real registry so other tests aren't poisoned.
            system_module.reload_registry()
            assert len(system_module.SYSTEM_REGISTRY) == len(_FALLBACK_REGISTRY)


# ---------------------------------------------------------------------------
# resolve_data_dir
# ---------------------------------------------------------------------------


class TestResolveDataDir:
    def test_env_var_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "envdata"
        monkeypatch.setenv(romulus_app.DATA_DIR_ENV_VAR, str(target))
        resolved = romulus_app.resolve_data_dir()
        assert resolved.resolve() == target.resolve()
        assert resolved.is_dir()

    def test_env_var_expands_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env var should expand ``~`` so users can write portable scripts
        # without hard-coding their home path.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv(romulus_app.DATA_DIR_ENV_VAR, "~/custom-data")
        resolved = romulus_app.resolve_data_dir()
        assert resolved == (tmp_path / "custom-data").resolve() or resolved == (
            tmp_path / "custom-data"
        )

    def test_falls_back_to_legacy_when_install_dir_not_writable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``<install_dir>/data`` can't be written, fall back to ~/.romulus."""
        legacy_home = tmp_path / "fakehome"
        legacy_home.mkdir()
        legacy = legacy_home / ".romulus"

        monkeypatch.delenv(romulus_app.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(romulus_app, "LEGACY_DATA_DIR", legacy)
        # Pretend the install dir is unwritable.
        monkeypatch.setattr(romulus_app, "_is_writable_dir", lambda p: False)

        resolved = romulus_app.resolve_data_dir()
        assert resolved == legacy
        assert legacy.is_dir()

    def test_uses_install_dir_when_writable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_install = tmp_path / "install"
        fake_install.mkdir()
        monkeypatch.delenv(romulus_app.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(romulus_app, "INSTALL_DIR", fake_install)
        resolved = romulus_app.resolve_data_dir()
        assert resolved == fake_install / "data"
        assert resolved.is_dir()


# ---------------------------------------------------------------------------
# ensure_user_editable_files
# ---------------------------------------------------------------------------


class TestEnsureUserEditableFiles:
    def test_creates_expected_folders(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_install = tmp_path / "install"
        fake_install.mkdir()
        monkeypatch.delenv(romulus_app.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(romulus_app, "INSTALL_DIR", fake_install)
        monkeypatch.setattr(
            romulus_app, "DEFAULT_LOG_DIR", fake_install / "logs"
        )

        romulus_app.ensure_user_editable_files()

        for sub in ("profiles", "systems", "data", "logs"):
            assert (fake_install / sub).is_dir(), f"{sub} not created"

    def test_does_not_overwrite_existing_user_edits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user's edited profile YAML must survive first-launch re-seeding."""
        fake_install = tmp_path / "install"
        fake_install.mkdir()
        profiles_dir = fake_install / "profiles"
        profiles_dir.mkdir()
        edited = profiles_dir / "snes.yaml"
        edited.write_text("user-edited-content-do-not-overwrite", encoding="utf-8")

        # Fake a frozen-bundle payload sitting beside the exe.
        bundle = fake_install / "_internal" / "profiles"
        bundle.mkdir(parents=True)
        (bundle / "snes.yaml").write_text(
            "bundled-default-should-not-clobber", encoding="utf-8"
        )
        (bundle / "newprofile.yaml").write_text("a new one", encoding="utf-8")

        monkeypatch.delenv(romulus_app.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(romulus_app, "INSTALL_DIR", fake_install)
        monkeypatch.setattr(
            romulus_app, "DEFAULT_LOG_DIR", fake_install / "logs"
        )

        romulus_app.ensure_user_editable_files()

        # The user's edit must still be there.
        assert (
            edited.read_text(encoding="utf-8")
            == "user-edited-content-do-not-overwrite"
        )

    def test_seeds_profiles_into_empty_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``<install_dir>/profiles`` is empty AND a bundle is present,
        copy the bundled defaults in.
        """
        fake_install = tmp_path / "install"
        fake_install.mkdir()
        bundle = fake_install / "_internal" / "profiles"
        bundle.mkdir(parents=True)
        (bundle / "x.yaml").write_text("id: x", encoding="utf-8")

        monkeypatch.delenv(romulus_app.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(romulus_app, "INSTALL_DIR", fake_install)
        monkeypatch.setattr(
            romulus_app, "DEFAULT_LOG_DIR", fake_install / "logs"
        )

        romulus_app.ensure_user_editable_files()

        assert (fake_install / "profiles" / "x.yaml").is_file()


# ---------------------------------------------------------------------------
# Profile loader three-tier precedence
# ---------------------------------------------------------------------------


_MINIMAL_PROFILE_YAML = """\
id: {pid}
name: {name}
target_os: emulationstation
base_path: roms
artwork_subdir: ""
artwork_filename_template: "{{stem}}-image{{ext}}"
gamelist_format: emulationstation_xml
multi_disc: m3u
systems:
  snes:
    folder: snes
    supported: true
"""


class TestThreeTierProfileLoading:
    """user_dir > install_dir > package_builtin."""

    def _write_profile(self, path: Path, pid: str, name: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _MINIMAL_PROFILE_YAML.format(pid=pid, name=name), encoding="utf-8"
        )

    def test_user_dir_overrides_install_dir(self, tmp_path: Path) -> None:
        builtin = tmp_path / "builtin"
        install = tmp_path / "install"
        user = tmp_path / "user"
        self._write_profile(builtin / "common.yaml", "common", "Builtin")
        self._write_profile(install / "common.yaml", "common", "Install")
        self._write_profile(user / "common.yaml", "common", "User")

        profiles = exporter.load_all_profiles(
            builtin_dir=builtin, install_dir=install, user_dir=user
        )
        assert profiles["common"].name == "User"

    def test_install_dir_overrides_builtin(self, tmp_path: Path) -> None:
        builtin = tmp_path / "builtin"
        install = tmp_path / "install"
        self._write_profile(builtin / "common.yaml", "common", "Builtin")
        self._write_profile(install / "common.yaml", "common", "Install")

        profiles = exporter.load_all_profiles(
            builtin_dir=builtin, install_dir=install, user_dir=None
        )
        assert profiles["common"].name == "Install"

    def test_builtin_used_when_no_overrides(self, tmp_path: Path) -> None:
        builtin = tmp_path / "builtin"
        self._write_profile(builtin / "common.yaml", "common", "Builtin")
        profiles = exporter.load_all_profiles(
            builtin_dir=builtin, install_dir=None, user_dir=None
        )
        assert profiles["common"].name == "Builtin"

    def test_disjoint_ids_merge(self, tmp_path: Path) -> None:
        builtin = tmp_path / "builtin"
        install = tmp_path / "install"
        user = tmp_path / "user"
        self._write_profile(builtin / "a.yaml", "a", "A")
        self._write_profile(install / "b.yaml", "b", "B")
        self._write_profile(user / "c.yaml", "c", "C")

        profiles = exporter.load_all_profiles(
            builtin_dir=builtin, install_dir=install, user_dir=user
        )
        assert set(profiles) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# resolve_db_path / DEFAULT_DB_PATH lazy resolution
# ---------------------------------------------------------------------------


class TestResolveDbPath:
    def test_db_path_lives_under_resolved_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(romulus_app.DATA_DIR_ENV_VAR, str(tmp_path / "d"))
        path = romulus_app.resolve_db_path()
        assert path == tmp_path / "d" / "romulus.db"

    def test_default_db_path_lazy_attr_honors_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``from romulus.db.connection import DEFAULT_DB_PATH`` is resolved
        each access via PEP 562 ``__getattr__`` so env-var overrides set
        after import still work.
        """
        from romulus.db import connection

        monkeypatch.setenv(romulus_app.DATA_DIR_ENV_VAR, str(tmp_path / "lazy"))
        # Force the harness to do an attribute lookup; PEP 562 __getattr__
        # makes this re-evaluate each call.
        importlib.reload(connection)
        # Compare via resolve() to handle Windows short/long path canonicalization.
        assert (tmp_path / "lazy" / "romulus.db") == connection.DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Settings dialog surfacing of install + data dirs
# ---------------------------------------------------------------------------


class TestSettingsDialogDiagnostics:
    def test_diagnostics_tab_shows_install_and_data_dirs(
        self, qapp, db
    ) -> None:
        """The Diagnostics tab in the Settings dialog must surface the
        resolved install_dir and data_dir so a user filing a bug report can
        copy them out without poking around the filesystem.
        """
        from PySide6.QtWidgets import QLabel

        from romulus.ui.settings_dialog import _DiagnosticsTab

        tab = _DiagnosticsTab(db)
        texts = [lbl.text() for lbl in tab.findChildren(QLabel)]
        joined = "\n".join(texts)
        assert str(romulus_app.INSTALL_DIR) in joined, (
            f"install dir {romulus_app.INSTALL_DIR} not surfaced; got:\n{joined}"
        )
        assert str(romulus_app.resolve_data_dir()) in joined, (
            f"data dir not surfaced; got:\n{joined}"
        )


# ---------------------------------------------------------------------------
# Install-dir consistency check (exporter vs app must match)
# ---------------------------------------------------------------------------


def test_exporter_and_app_install_dir_match() -> None:
    """exporter._resolve_install_dir() and app._resolve_install_dir() must
    return the same path so the three-tier profile search and the data-dir
    resolver agree on what ``install_dir`` means.
    """
    assert (
        exporter._resolve_install_dir().resolve()
        == romulus_app._resolve_install_dir().resolve()
    )
