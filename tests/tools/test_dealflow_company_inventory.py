"""Regression: discovery must not probe guessed company paths."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import dealflow_kanban_tool as tool


class CompanyInventoryTest(unittest.TestCase):
    def test_missing_companies_are_empty_inventory_not_read_errors(self):
        with tempfile.TemporaryDirectory() as root:
            result = tool._company_inventory(Path(root))
            self.assertTrue(result['success'])
            self.assertEqual(result['companies'], [])
            self.assertEqual(result['company_count'], 0)

    def test_complete_inventory_has_ids_and_literal_paths(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            for i in range(60):
                path = base / f'Companies/Company {i}/Overview.md'
                path.parent.mkdir(parents=True)
                path.write_text(f'---\ncompany: Name {i}\ndealflow:\n  company_id: "id-{i}"\n  deal_id: "deal-{i}"\n---\n# Evidence\n')
            result = tool._company_inventory(base)
            self.assertEqual(result['company_count'], 60)
            self.assertEqual(len(result['companies']), 60)
            self.assertIn({'path': 'Companies/Company 0/Overview.md', 'folder': 'Company 0', 'name': 'Name 0', 'company_id': 'id-0', 'deal_id': 'deal-0'}, result['companies'])

    def test_legacy_without_frontmatter_is_candidate_not_invented_id(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'Companies/Legacy/Overview.md'
            p.parent.mkdir(parents=True)
            p.write_text('# Legacy\n')
            result = tool._company_inventory(Path(root))
            self.assertEqual(result['companies'][0]['company_id'], None)

    def test_symlink_outside_worktree_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            base = Path(root)
            p = base / 'Companies/Escape/Overview.md'
            p.parent.mkdir(parents=True)
            target = Path(outside) / 'Overview.md'
            target.write_text('# Private\n')
            p.symlink_to(target)
            with self.assertRaises(tool.ValidationError):
                tool._company_inventory(base)

    def test_malformed_frontmatter_fails_instead_of_silent_omission(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'Companies/Bad/Overview.md'
            p.parent.mkdir(parents=True)
            p.write_text('---\ncompany: [unterminated\n---\n')
            with self.assertRaises(tool.ValidationError):
                tool._company_inventory(Path(root))

    def test_falsey_non_mapping_dealflow_value_is_rejected(self):
        for value in ("[]", "false", "0", '""'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                p = Path(root) / 'Companies/Bad/Overview.md'
                p.parent.mkdir(parents=True)
                p.write_text(f'---\ncompany: Bad\ndealflow: {value}\n---\n')
                with self.assertRaises(tool.ValidationError):
                    tool._company_inventory(Path(root))

    def test_public_action_wires_inventory_and_schema(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            p = base / 'Companies/Acme/Overview.md'
            p.parent.mkdir(parents=True)
            p.write_text('---\ncompany: Acme\n---\n')
            with patch.object(tool, '_resolve_worktree', return_value=base):
                result = json.loads(tool.dealflow_kanban('company_inventory', str(base)))
            self.assertTrue(result['success'])
            self.assertEqual(result['action'], 'company_inventory')
            self.assertEqual(result['company_count'], 1)
            self.assertIn('company_inventory', tool.DEALFLOW_KANBAN_SCHEMA['parameters']['properties']['action']['enum'])
