from __future__ import annotations

import unittest

from bot.services.automation_coordinator import AutomationCoordinator


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, function, **kwargs):
        self.jobs[kwargs["id"]] = {"function": function, **kwargs}


class FakeCheckins:
    def __init__(self):
        self.calls = []

    def reschedule_user_checkins(self, user_id, bot, timezone):
        self.calls.append((user_id, bot, timezone))


class FakeAssistantLoop:
    def __init__(self):
        self.scheduled = []
        self.rebuilt = []

    def schedule_user(self, scheduler, user_id, timezone):
        self.scheduled.append((scheduler, user_id, timezone))

    async def rebuild_user(self, user_id, reason):
        self.rebuilt.append((user_id, reason))
        if user_id == 2:
            raise RuntimeError("one user must not stop the others")
        return {"saved": user_id, "unallocated": 0}


class AutomationCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedules_all_jobs_and_updates_timezone_without_restart(self):
        scheduler = FakeScheduler()
        checkins = FakeCheckins()
        assistant_loop = FakeAssistantLoop()
        bot = object()
        coordinator = AutomationCoordinator(
            scheduler,
            bot,
            checkins,
            assistant_loop,
        )

        count = coordinator.schedule({1: "Europe/Moscow", 2: "Asia/Tbilisi"})

        self.assertEqual(2, count)
        self.assertIn("assistant:plan-reconcile", scheduler.jobs)
        self.assertEqual(4, scheduler.jobs["assistant:plan-reconcile"]["hours"])
        self.assertEqual(2, len(checkins.calls))
        self.assertEqual(2, len(assistant_loop.scheduled))

        await coordinator.profile_updated(1, "Asia/Almaty")

        self.assertEqual((1, bot, "Asia/Almaty"), checkins.calls[-1])
        self.assertEqual(
            (scheduler, 1, "Asia/Almaty"),
            assistant_loop.scheduled[-1],
        )

    async def test_startup_reconcile_isolates_a_single_user_failure(self):
        coordinator = AutomationCoordinator(
            FakeScheduler(),
            object(),
            FakeCheckins(),
            FakeAssistantLoop(),
        )
        coordinator.schedule({1: "UTC", 2: "UTC", 3: "UTC"})

        result = await coordinator.rebuild_all("startup")

        self.assertEqual({1, 3}, set(result))
        self.assertEqual(1, result[1]["saved"])
        self.assertEqual(3, result[3]["saved"])
