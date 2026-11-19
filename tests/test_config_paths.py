"""search_terms_root works on another PC (different Windows user name)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import config_loader, terms_loader

LISTS = {
    "list1": {"label": "List 1", "folder": "List 1 - Entire Coverage", "master_file": "m1.csv"},
    "list2": {"label": "List 2", "folder": "List 2 - Top Search Terms", "master_file": "m2.csv"},
}


def _settings(root: Path, lists: dict | None = None):
    data: dict = {"search_terms_root": str(root)}
    if lists is not None:
        data["search_term_lists"] = lists
    return mock.patch.object(config_loader, "settings", return_value=data)


def _home(home: Path):
    return mock.patch.object(config_loader.Path, "home", return_value=home)


def _make_terms_folder(folder: Path) -> Path:
    (folder / "List 2 - Top Search Terms").mkdir(parents=True)
    return folder


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
            with _settings(configured), _home(home):
                self.assertEqual(
                    config_loader.search_terms_root(), home / "Agro_Search_Terms"
                )

    def test_keeps_configured_path_when_folder_is_nowhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = Path(tmp) / "aman" / "Terms_Missing_For_Test"
            with _settings(configured), _home(Path(tmp) / "otheruser"):
                self.assertEqual(config_loader.search_terms_root(), configured)

    def test_finds_folder_left_in_downloads_with_extra_unzip_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "otheruser"
            inner = _make_terms_folder(
                home / "Downloads" / "Agro_Search_Terms" / "Agro_Search_Terms"
            )
            configured = Path(tmp) / "aman" / "Agro_Search_Terms"
            with _settings(configured, LISTS), _home(home):
                self.assertEqual(config_loader.search_terms_root(), inner)

    def test_skips_empty_folder_and_uses_one_with_the_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "otheruser"
            (home / "Agro_Search_Terms").mkdir(parents=True)  # empty copy
            good = _make_terms_folder(home / "Desktop" / "Agro_Search_Terms")
            configured = Path(tmp) / "aman" / "Agro_Search_Terms"
            with _settings(configured, LISTS), _home(home):
                self.assertEqual(config_loader.search_terms_root(), good)

    def test_finds_folder_on_company_onedrive_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "otheruser"
            good = _make_terms_folder(
                home / "OneDrive - Some Company" / "Desktop" / "Agro_Search_Terms"
            )
            configured = Path(tmp) / "aman" / "Agro_Search_Terms"
            with _settings(configured, LISTS), _home(home):
                self.assertEqual(config_loader.search_terms_root(), good)


class MissingTermsMessageTests(unittest.TestCase):
    def test_error_says_where_to_put_the_folder_and_where_it_looked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "otheruser"
            (home / "Agro_Search_Terms").mkdir(parents=True)  # exists but empty
            configured = Path(tmp) / "aman" / "Agro_Search_Terms"
            with _settings(configured, LISTS), _home(home):
                with self.assertRaises(FileNotFoundError) as ctx:
                    terms_loader.load_term_list("list2")
        message = str(ctx.exception)
        self.assertIn(str(home / "Agro_Search_Terms"), message)
        self.assertIn("List 1 - Entire Coverage", message)
        self.assertIn("List 2 - Top Search Terms", message)
        self.assertIn("exists, but", message)  # the empty copy is pointed out
        self.assertIn(str(home / "Downloads" / "Agro_Search_Terms"), message)


if __name__ == "__main__":
    unittest.main()
