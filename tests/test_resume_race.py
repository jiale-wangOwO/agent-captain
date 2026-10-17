import tempfile
import unittest
from agent_captain.legacy.legacy_appserver_hub import LegacyAppServerHub as Hub


class SnapshotRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temp.name)
        self.hub.store.remember("local", "worker", "test", owned=True, access="control")

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def test_newer_completion_is_not_overwritten_by_old_active_resume_snapshot(self):
        key = ("local", "worker")
        self.hub.runtime_versions[key] = 1
        owner = type("Owner", (), {"closed": False})()
        self.hub.bind_owner("local", "worker", owner, {"status": {"type": "active"}}, expected_version=0)
        self.assertNotIn(key, self.hub.active)
        self.assertTrue(self.hub.get_agent(*key)["connected"])

    def test_newer_started_turn_survives_old_idle_resume_snapshot(self):
        key = ("local", "worker")
        self.hub.runtime_versions[key] = 1
        self.hub.active[key] = "new-turn"
        owner = type("Owner", (), {"closed": False})()
        self.hub.bind_owner("local", "worker", owner, {"status": {"type": "idle"}}, expected_version=0)
        self.assertEqual(self.hub.active[key], "new-turn")

    def test_editing_default_local_label_does_not_change_agent_route(self):
        result = self.hub.upsert_host({"id": "local", "label": "我的电脑", "transport": "local", "live": False})
        self.assertEqual(result["label"], "我的电脑")
        self.assertEqual(self.hub.get_agent("local", "worker")["thread_id"], "worker")
