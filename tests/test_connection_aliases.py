"""Explicit private legacy aliases; synthetic files, no real tokens or clients."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_captain.server import app
class ConnectionAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.runtime').mkdir()
        self.fixture = patch.object(app, 'ROOT', self.root)
        self.fixture.start()
        self.old = self.root / 'legacy-context' / 'CEOHub' / 'service.json'
        self.old.parent.mkdir(parents=True)
        self.prior = {'url': 'http://127.0.0.1:39001', 'token': 'old-fixture-capability', 'pid': 123, 'version': '0.3.1'}
        self.next = {**self.prior, 'token': 'new-fixture-capability', 'pid': 456, 'version': '0.5.0'}
        self.old.write_text(json.dumps(self.prior), encoding='utf-8')
        self.ref = self.root / '.runtime' / 'service-compatibility-references.json'

    def tearDown(self):
        self.fixture.stop()
        self.temp.cleanup()

    def register(self, paths):
        self.ref.write_text(json.dumps({'service_files': [str(p) for p in paths]}), encoding='utf-8')

    def test_only_explicit_same_endpoint_old_service_alias_is_refreshed(self):
        self.assertEqual(app.sync_connection_aliases(self.next), 0)
        self.register([self.old])
        self.assertEqual(app.sync_connection_aliases(self.next), 1)
        self.assertEqual(json.loads(self.old.read_text()), self.next)

    def test_other_ports_auth_files_and_missing_targets_are_preserved(self):
        auth = self.root / 'auth.json'
        auth.write_text(json.dumps(self.prior), encoding='utf-8')
        self.old.write_text(json.dumps({**self.prior, 'url': 'http://127.0.0.1:39002'}), encoding='utf-8')
        self.register([self.old, auth, self.old.parent / 'missing.json'])
        before = self.old.read_bytes(), auth.read_bytes()
        self.assertEqual(app.sync_connection_aliases(self.next), 0)
        self.assertEqual((self.old.read_bytes(), auth.read_bytes()), before)

    def test_malformed_reference_or_non_hub_descriptor_is_not_replaced(self):
        self.ref.write_text('{invalid', encoding='utf-8')
        self.assertEqual(app.sync_connection_aliases(self.next), 0)
        self.register([self.old])
        self.old.write_text(json.dumps({'url': self.prior['url'], 'account': 'synthetic'}), encoding='utf-8')
        before = self.old.read_bytes()
        self.assertEqual(app.sync_connection_aliases(self.next), 0)
        self.assertEqual(self.old.read_bytes(), before)
