"""ProfileRunCoordinator: limit budget, cookie version, VPN mutex."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.profile_coordinator import ProfileRunCoordinator


class TestProfileCoordinator(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.run_lock = self.data_dir / "testland.profile.run.lock"
        self._patch = patch("src.profile_coordinator.DATA_DIR", self.data_dir)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.temp_dir.cleanup()

    async def test_limit_budget(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        coord.set_limit(3)
        taken = 0
        for _ in range(5):
            if await coord.take_limit_slot():
                taken += 1
        self.assertEqual(taken, 3)

    async def test_wait_for_fresh_cookies_detects_change(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        store = MagicMock()
        store.get_cookie_updated_at.side_effect = ["v1", "v2"]
        changed = await coord.wait_for_fresh_cookies(store, "w0")
        self.assertFalse(changed)
        changed2 = await coord.wait_for_fresh_cookies(store, "w0")
        self.assertTrue(changed2)

    async def test_vpn_exclusive_runs_one_at_a_time(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        order: list[int] = []
        lock = asyncio.Lock()

        async def job(n: int) -> None:
            async def _fn() -> None:
                async with lock:
                    if order:
                        self.assertGreater(n, order[-1] - 1)
                    order.append(n)
                await asyncio.sleep(0.05)

            await coord.run_vpn_exclusive(_fn)

        await asyncio.gather(job(1), job(2), job(3))
        self.assertEqual(sorted(order), [1, 2, 3])

    async def test_cookie_refresh_exclusive_runs_one_at_a_time(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        active = 0
        peak = 0
        gate = asyncio.Event()

        async def job() -> None:
            nonlocal active, peak

            async def _fn() -> None:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await gate.wait()
                active -= 1

            await coord.run_cookie_refresh_exclusive(_fn)

        tasks = [asyncio.create_task(job()) for _ in range(4)]
        await asyncio.sleep(0.05)
        self.assertEqual(active, 1)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(peak, 1)
        self.assertEqual(active, 0)

    async def test_shared_recovery_runs_once(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        calls = 0

        async def recover() -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)

        results = await asyncio.gather(
            coord.maybe_run_shared_recovery(recover),
            coord.maybe_run_shared_recovery(recover),
            coord.maybe_run_shared_recovery(recover),
        )
        self.assertEqual(calls, 1)
        self.assertEqual(sum(1 for r in results if r), 1)
        self.assertEqual(sum(1 for r in results if not r), 2)

    async def test_shared_browser_reused_across_ensures(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        fake_browser = MagicMock()
        fake_browser.is_connected.return_value = True
        launch_count = 0

        async def fake_launch(headless: bool = True):
            nonlocal launch_count
            launch_count += 1
            coord._shared_browser = fake_browser
            coord._shared_playwright = MagicMock()
            return fake_browser

        with patch.object(coord, "_launch_shared_browser_unlocked", side_effect=fake_launch):
            b1 = await coord.ensure_shared_browser()
            b2 = await coord.ensure_shared_browser()
        self.assertIs(b1, b2)
        self.assertEqual(launch_count, 1)

    async def test_shared_recovery_suspends_browsers_before_recover(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        closed: list[str] = []

        class FakeSession:
            async def close_profile_session(self, force_cookie_save: bool = False) -> None:
                closed.append("closed")

        coord.register_browser(FakeSession())
        coord.register_browser(FakeSession())
        order: list[str] = []

        async def recover() -> None:
            order.append("recover")
            self.assertEqual(closed, ["closed", "closed"])

        await coord.maybe_run_shared_recovery(recover)
        self.assertEqual(order, ["recover"])
        self.assertEqual(closed, ["closed", "closed"])

    async def test_browser_restart_exclusive_serializes(self) -> None:
        coord = ProfileRunCoordinator(self.run_lock)
        active = 0
        peak = 0

        async def job() -> None:
            nonlocal active, peak

            async def _fn() -> None:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

            await coord.run_browser_restart_exclusive(_fn)

        await asyncio.gather(job(), job(), job())
        self.assertEqual(peak, 1)

    async def test_recovery_done_gates_workers_during_recovery(self) -> None:
        """wait_recovery_done() must block until recover_fn finishes."""
        coord = ProfileRunCoordinator(self.run_lock)
        gate = asyncio.Event()
        recovery_reached_end = False

        async def recover() -> None:
            nonlocal recovery_reached_end
            await gate.wait()
            recovery_reached_end = True

        recovery_task = asyncio.create_task(coord.maybe_run_shared_recovery(recover))
        await asyncio.sleep(0.05)

        # wait_recovery_done() should block while gate is not set.
        wait_task = asyncio.create_task(coord.wait_recovery_done())
        await asyncio.sleep(0.05)
        self.assertFalse(wait_task.done(), "wait_recovery_done() returned before recovery finished")

        gate.set()
        await recovery_task
        await asyncio.wait_for(wait_task, timeout=2.0)
        self.assertTrue(recovery_reached_end)

    async def test_cooldown_set_after_recovery_not_before(self) -> None:
        """Cooldown window must start after recover_fn returns, not before."""
        coord = ProfileRunCoordinator(self.run_lock)
        gate = asyncio.Event()

        async def recover() -> None:
            await gate.wait()

        recovery_task = asyncio.create_task(coord.maybe_run_shared_recovery(recover))
        await asyncio.sleep(0.05)

        # Before recovery finishes, cooldown timer is still zero → no wait.
        # Workers should use wait_recovery_done(), not wait_rate_limit_cooldown().
        cooldown_before = asyncio.create_task(coord.wait_rate_limit_cooldown())
        await asyncio.sleep(0.05)
        self.assertTrue(cooldown_before.done(), "Cooldown should not be set before recovery ends")
        await cooldown_before

        gate.set()
        await recovery_task

        # After recovery, cooldown is now active.
        cooldown_after = asyncio.create_task(coord.wait_rate_limit_cooldown())
        await asyncio.sleep(0.05)
        self.assertFalse(cooldown_after.done(), "Cooldown should block after recovery ends")
        cooldown_after.cancel()
        try:
            await cooldown_after
        except asyncio.CancelledError:
            pass

    async def test_restart_shared_browser_idempotent_under_concurrent_callers(self) -> None:
        """N workers all calling restart_shared_browser at once must launch exactly 1 browser."""
        coord = ProfileRunCoordinator(self.run_lock)
        launch_count = 0

        async def fake_launch(headless: bool = True):
            nonlocal launch_count
            launch_count += 1
            fake = MagicMock()
            fake.is_connected.return_value = True
            coord._shared_browser = fake
            coord._shared_playwright = MagicMock()
            return fake

        with patch.object(coord, "_launch_shared_browser_unlocked", side_effect=fake_launch):
            with patch.object(coord, "_stop_shared_browser_unlocked", new_callable=AsyncMock):
                browsers = await asyncio.gather(
                    coord.restart_shared_browser(),
                    coord.restart_shared_browser(),
                    coord.restart_shared_browser(),
                )

        # All callers should get the same browser object.
        self.assertEqual(len(set(id(b) for b in browsers)), 1)
        # Browser should have been launched exactly once.
        self.assertEqual(launch_count, 1)

    async def test_with_shared_browser_stopped_holds_lock_during_fn(self) -> None:
        """with_shared_browser_stopped must hold _browser_restart_lock while fn runs."""
        coord = ProfileRunCoordinator(self.run_lock)
        fn_running = asyncio.Event()
        lock_acquired_during_fn = False

        async def long_fn():
            fn_running.set()
            await asyncio.sleep(0.1)
            return "done"

        async def try_acquire_lock() -> bool:
            await fn_running.wait()
            return coord._browser_restart_lock.locked()

        with patch.object(coord, "_stop_shared_browser_unlocked", new_callable=AsyncMock):
            with patch.object(coord, "_launch_shared_browser_unlocked", new_callable=AsyncMock) as mock_launch:
                fake = MagicMock()
                fake.is_connected.return_value = True
                mock_launch.return_value = fake
                coord._shared_browser = fake

                result, lock_state = await asyncio.gather(
                    coord.with_shared_browser_stopped(long_fn, headless=True),
                    try_acquire_lock(),
                )

        self.assertEqual(result, "done")
        self.assertTrue(lock_state, "_browser_restart_lock must be held while fn runs")

    def test_run_lock_blocks_second_acquire(self) -> None:
        coord1 = ProfileRunCoordinator(self.run_lock)
        coord1.acquire_run_lock()
        coord2 = ProfileRunCoordinator(self.run_lock)
        with patch(
            "src.profile_coordinator.settings",
            lambda: {"profile_run_lock_timeout_seconds": 0.1},
        ):
            with self.assertRaises(RuntimeError):
                coord2.acquire_run_lock()
        coord1.release_run_lock()


if __name__ == "__main__":
    unittest.main()
