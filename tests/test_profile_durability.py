"""Profile durability: batched checkpoint cadence."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.storage.run_durability import RunDurability


class TestProfileDurabilityFlushCadence(unittest.TestCase):
    @patch("src.storage.run_durability.append_entry")
    @patch("src.storage.run_durability.settings")
    def test_profile_heavy_ops_checkpoints_every_n(self, mock_settings, mock_append) -> None:
        mock_settings.return_value = {
            "profile_backup_every": 100,
            "profile_export_every": 100,
            "profile_flush_every": 3,
            "profile_export_on_run_end_only": True,
        }
        db = MagicMock()
        db.continent_code = "EUROPE"
        db.country_name = "United Kingdom"
        durability = RunDurability(db, lambda _msg: None, mode="profile")

        for i in range(5):
            durability.append_profile_ledger(f"id-{i}", "completed", f"Co {i}")
            durability.run_profile_heavy_ops()

        self.assertEqual(db.flush_profile_checkpoint.call_count, 1)

    @patch("src.storage.run_durability.append_entry")
    @patch("src.storage.run_durability.settings")
    def test_profile_run_end_always_checkpoints(self, mock_settings, mock_append) -> None:
        mock_settings.return_value = {
            "profile_backup_every": 100,
            "profile_export_every": 100,
            "profile_flush_every": 10,
        }
        db = MagicMock()
        db.continent_code = "EUROPE"
        db.country_name = "United Kingdom"
        durability = RunDurability(db, lambda _msg: None, mode="profile")
        durability.append_profile_ledger("id-1", "completed", "Co")
        durability.run_profile_heavy_ops()
        with patch.object(durability, "_maybe_backup"), patch.object(
            durability, "_maybe_export_profile"
        ), patch("src.storage.run_durability.create_backup"), patch(
            "src.storage.run_durability.export_profile_csv", return_value=[]
        ):
            durability.on_run_end()
        db.flush_profile_checkpoint_full.assert_called()


if __name__ == "__main__":
    unittest.main()
