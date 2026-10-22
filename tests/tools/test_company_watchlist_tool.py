"""Deterministic state and dedup guarantees for the company watchlist tool."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import company_watchlist_tool as tool


class FakeDedup:
    stores = {}

    def __init__(self, path):
        self.path = str(path)
        self.entries = list(self.stores.get(self.path, []))

    def should_include(self, company, article_type, headline):
        return not any(
            row['company'].lower() == company.lower()
            and row['type'] == article_type
            and row['headline'].lower() == headline.lower()
            for row in self.entries
        )

    def add_entry(self, company, article_type, headline, url):
        self.entries.append({
            'company': company, 'type': article_type,
            'headline': headline, 'url': url,
            'date': '2026-10-22', 'included_in_brief': '2026-10-22',
        })

    def save(self):
        self.stores[self.path] = list(self.entries)

    def get_stats(self):
        return {'total_entries': len(self.entries)}


class CompanyWatchlistToolTests(unittest.TestCase):
    def setUp(self):
        FakeDedup.stores.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.watchlist = self.root / 'watchlist.json'
        self.log = self.root / 'dedup.md'
        self.watchlist.write_text(json.dumps({
            'updatedAt': '2026-10-20',
            'companies': [{
                'name': 'Acme', 'tickers': ['ACME'],
                'aliases': ['Widget'], 'notes': '',
            }],
        }))
        self.dedup_patch = patch.object(tool, '_dedup', lambda path: FakeDedup(path))
        self.dedup_patch.start()

    def tearDown(self):
        self.dedup_patch.stop()
        self.tmp.cleanup()

    def call(self, action, **kwargs):
        return json.loads(tool.company_watchlist(
            action,
            watchlist_path=str(self.watchlist),
            dedup_log_path=str(self.log),
            **kwargs,
        ))

    def test_validate_and_dry_run(self):
        self.assertTrue(self.call('validate')['success'])
        result = self.call('dry_run')
        self.assertTrue(result['success'])
        self.assertEqual([row['name'] for row in result['companies']], ['Acme'])

    def test_add_rejects_existing_alias_or_ticker(self):
        for collision in ('Widget', 'ACME'):
            with self.subTest(collision=collision):
                result = self.call('add_company', name='Other', aliases=[collision])
                self.assertFalse(result['success'])
                self.assertEqual(result['conflicting_identifier'], collision)
        self.assertEqual(len(json.loads(self.watchlist.read_text())['companies']), 1)

    def test_add_and_remove_round_trip(self):
        added = self.call('add_company', name='Beta', tickers=['BETA'], aliases=['Beta Inc'])
        self.assertTrue(added['success'])
        self.assertEqual(len(json.loads(self.watchlist.read_text())['companies']), 2)
        removed = self.call('remove_company', name='BETA')
        self.assertTrue(removed['success'])
        self.assertEqual(len(json.loads(self.watchlist.read_text())['companies']), 1)

    def test_record_enforces_duplicate_check_atomically(self):
        args = dict(name='Acme', article_type='earnings', headline='Acme reports results', url='https://example.test/a')
        self.assertTrue(self.call('record_included', **args)['success'])
        duplicate = self.call('record_included', **args)
        self.assertFalse(duplicate['success'])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(len(FakeDedup.stores[str(self.log)]), 1)

    def test_schema_and_registration_contract(self):
        self.assertEqual(tool.COMPANY_WATCHLIST_SCHEMA['name'], 'company_watchlist')
        self.assertIn('record_included', tool.COMPANY_WATCHLIST_SCHEMA['parameters']['properties']['action']['enum'])


if __name__ == '__main__':
    unittest.main()