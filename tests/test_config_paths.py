"""search_terms_root works on another PC (different Windows user name)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import config_loader


def _settings(root: Path):
    return mock.patch.object(
        config_loader, "settings", return_value={"search_terms_root": str(root)}
    )


class SearchTermsRootTests(unittest.TestCase):
    def test_uses_configured_folder_when_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Agro_Search_Terms"
            folder.mkdir()
            with _settings(folder):
                self.assertEqual(config_loader.search_terms_root(), folder)

    def test_falls_back_to_home_folder_on_another_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "otheruser"
            (home / "Agro_Search_Terms").mkdir(parents=True)
            configured = Path(tmp) / "aman" / "Agro_Search_Terms"  # does not exist
            with _settings(configured), mock.patch.object(
                config_loader.Path, "home", return_value=home
            ):
                self.assertEqual(
                    config_loader.search_terms_root(), home / "Agro_Search_Terms"
                )

    def test_keeps_configured_path_when_folder_is_nowhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = Path(tmp) / "aman" / "Terms_Missing_For_Test"
            with _settings(configured), mock.patch.object(
                config_loader.Path, "home", return_value=Path(tmp) / "otheruser"
            ):
                self.assertEqual(config_loader.search_terms_root(), configured)


if __name__ == "__main__":
    unittest.main()
