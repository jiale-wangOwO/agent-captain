"""First confirmed terminal-write times, using only temporary SQLite state."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_captain.infrastructure.store import Store


CLOCK = "agent_captain.infrastructure.store.time.time"


class DeliveryTimestampTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.sqlite"

    def open(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        return store

    def queued(self, store, key="fixture-message"):
        with patch(CLOCK, return_value=100.25):
            return store.enqueue("remote", "worker", "fixture text", origin="plugin", request_key=key, kind="prompt")

    def test_new_database_has_one_nullable_real_timestamp_and_keeps_creation_time(self):
        store = self.open()
        columns = [dict(row) for row in store.db.execute("pragma table_info(outbox)")]
        matches = [row for row in columns if row["name"] == "submitted_at"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["type"].upper(), "REAL")
        self.assertEqual(matches[0]["notnull"], 0)
        self.assertIsNone(matches[0]["dflt_value"])
        request = self.queued(store)
        row = store.delivery(request)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["time"], 100.25)
        self.assertIsNone(row["submitted_at"])
        self.assertEqual(store.outbox()[0], row)

    def test_legacy_schema_migration_is_idempotent_and_never_backfills_old_confirmations(self):
        with closing(sqlite3.connect(self.path)) as database, database:
            database.row_factory = sqlite3.Row
            database.execute("create table outbox(id text primary key,host text,thread_id text,text text,state text,turn_id text,error text,time real)")
            for state in ("submitted", "delivered", "queued", "unknown"):
                database.execute("insert into outbox values(?,?,?,?,?,?,?,?)",
                    (state, "old-host", "old-worker", "original text", state, "original-turn", "original receipt", 25.5))
            before = {row["id"]: dict(row) for row in database.execute("select * from outbox")}
        # A schema migration has no basis for choosing a historical write time.
        with patch(CLOCK, side_effect=AssertionError("migration cannot invent a time")):
            for _ in range(2):
                store = Store(self.path)
                try:
                    self.assertEqual(sum(row[1] == "submitted_at" for row in store.db.execute("pragma table_info(outbox)")), 1)
                    for request, old in before.items():
                        row = store.delivery(request)
                        self.assertIsNone(row["submitted_at"])
                        self.assertEqual({key: row[key] for key in old}, old)
                finally:
                    store.close()

    def test_first_submitted_or_delivered_confirmation_records_current_time_once(self):
        for confirmed_state in ("submitted", "delivered"):
            with self.subTest(state=confirmed_state):
                store = self.open()
                request = self.queued(store, key=confirmed_state)
                with patch(CLOCK, return_value=125.75):
                    store.update_outbox(request, confirmed_state, turn_id="confirmed-turn")
                row = store.delivery(request)
                self.assertEqual(row["submitted_at"], 125.75)
                self.assertEqual(row["time"], 100.25)
                self.assertEqual(row["state"], confirmed_state)
                self.assertEqual(row["turn_id"], "confirmed-turn")

    def test_queued_submitting_unknown_and_failed_states_are_not_confirmation_times(self):
        store = self.open()
        request = self.queued(store)
        with patch(CLOCK, side_effect=AssertionError("a non-ack state must not record time")):
            for state in ("queued", "submitting", "unknown", "failed", "not_sent", "waiting_for_agent", "scope_revoked", "completed"):
                with self.subTest(state=state):
                    store.update_outbox(request, state, error="fixture state")
                    row = store.delivery(request)
                    self.assertIsNone(row["submitted_at"])
                    self.assertEqual(row["time"], 100.25)
                    self.assertEqual(row["state"], state)

    def test_unknown_then_explicit_confirmation_gets_the_first_confirmed_time(self):
        store = self.open()
        request = self.queued(store)
        store.update_outbox(request, "unknown", error="write result was uncertain")
        with patch(CLOCK, return_value=200.5):
            store.update_outbox(request, "submitted")
        self.assertEqual(store.delivery(request)["submitted_at"], 200.5)

    def test_later_confirmation_and_other_state_changes_preserve_first_write_time(self):
        store = self.open()
        request = self.queued(store)
        with patch(CLOCK, return_value=125.75):
            store.update_outbox(request, "submitted", turn_id="first-turn")
        with patch(CLOCK, return_value=9999.0):
            for state in ("submitted", "delivered", "unknown", "failed", "submitting", "submitted"):
                with self.subTest(state=state):
                    store.update_outbox(request, state, error="later receipt")
                    row = store.delivery(request)
                    self.assertEqual(row["submitted_at"], 125.75)
                    self.assertEqual(row["time"], 100.25)
                    self.assertEqual(row["turn_id"], "first-turn")
                    self.assertEqual(row["error"], "later receipt")
        reopened = self.open()
        self.assertEqual(reopened.delivery(request)["submitted_at"], 125.75)

    def test_old_acknowledged_rows_with_unknown_time_stay_unknown_on_duplicate_ack(self):
        store = self.open()
        for state in ("submitted", "delivered"):
            with self.subTest(state=state), store.db:
                store.db.execute("insert into outbox(id,host,thread_id,text,state,time) values(?,?,?,?,?,?)",
                                 (state, "remote", "worker", "old confirmed text", state, 25.5))
            with patch(CLOCK, return_value=9999.0):
                store.update_outbox(state, "submitted")
                store.update_outbox(state, "delivered")
            self.assertIsNone(store.delivery(state)["submitted_at"])
            self.assertEqual(store.delivery(state)["time"], 25.5)

    def test_same_request_key_reuses_original_row_without_resetting_either_time(self):
        store = self.open()
        request = self.queued(store)
        with patch(CLOCK, return_value=125.75):
            store.update_outbox(request, "submitted")
        before = store.delivery(request)
        with patch(CLOCK, side_effect=AssertionError("key reuse must not allocate a new time")):
            reused = store.enqueue("remote", "worker", "fixture text", origin="plugin", request_key="fixture-message", kind="prompt")
        self.assertEqual(reused, request)
        self.assertEqual(store.delivery(reused), before)
        self.assertEqual(len(store.outbox()), 1)
        with self.assertRaises(ValueError):
            store.enqueue("remote", "worker", "different text", origin="plugin", request_key="fixture-message", kind="prompt")
        self.assertEqual(store.delivery(request), before)


if __name__ == "__main__":
    unittest.main()
