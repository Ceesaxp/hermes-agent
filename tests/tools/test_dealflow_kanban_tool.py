from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import dealflow_kanban_tool as tool


def run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def kanban_text(checkpoint: str, stage: str = "Screening") -> str:
    return f"""---
title: "DSV Deals Kanban"
created: 2026-10-01
updated: 2026-10-19
last_sync_with_dealflow: {checkpoint}
type: deals-kanban
tags: [deals-kanban]
---

# DSV Deals Kanban

## Columns

- **Captured**
- **Screening**
- **Passed**

## Current cards

| Card | Company | Stage | Recommendation | Research_Bench path |
|---|---|---|---|---|
| TBD | Acme | {stage} | — | `Companies/Acme/Overview.md` |
"""


def audit_text(checkpoint: str) -> str:
    return f"""run_id: 20261019T000042Z
status: validated_pending_publication
window:
  verified_max_updated_at: {checkpoint}
  checkpoint_written: {checkpoint}
pagination:
  collected_count: 1
  deduplicated_count: 1
  inclusive_boundary_duplicate_count: 0
  complete: true
kanban_result:
  company_count_after_sync: 1
  changed_existing_company_count: 0
  stage_change_count: 0
  metadata_only_change_count: 0
  additions_count: 1
  disappearance_count: 0
  stage_changes: []
  metadata_only_changes: []
  additions:
    - "Acme: Screening"
  disappearances: []
files_changed:
  - DealFlow/DSV_Deals_Kanban.md
  - DealFlow/sync_logs/20261019_nightly_sync.log
validation:
  yaml_frontmatter: pending
  unique_company_placement: pending
  valid_stage_labels: pending
  markdown_structure: pending
  git_diff_check: pending
  changed_company_rows_read_back: pending
  addition_rows_read_back: pending
  checkpoint_equals_verified_max_updated_at: pending
publication:
  automation_branch: automation/dealflow-test
  automation_branch_push: pending
  content_commit: pending
  merge_commit: pending
  master_push: pending
  remote_master_commit_at_final_verification: pending
  kanban_blob_at_final_verification: pending
  audit_log_blob_at_final_verification: pending
  final_audit_commit: self_referential_hash_reported_by_cron_read_back
  remote_blob_verified: false
  audit_log_verified: false
"""


def make_repo(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    state_root = tmp_path / "state"
    worktree = state_root / "worktree-test"
    remote = tmp_path / "remote.git"
    worktree.mkdir(parents=True)
    run(["git", "init", "--bare", str(remote)], cwd=tmp_path)
    run(["git", "init", "-b", "automation/dealflow-test"], cwd=worktree)
    run(["git", "config", "user.name", "Hermes Test"], cwd=worktree)
    run(["git", "config", "user.email", "hermes-test@example.invalid"], cwd=worktree)
    run(["git", "remote", "add", "origin", str(remote)], cwd=worktree)

    (worktree / "Companies/Acme").mkdir(parents=True)
    (worktree / "Companies/Acme/Overview.md").write_text("# Acme\n", encoding="utf-8")
    (worktree / "DealFlow/sync_logs").mkdir(parents=True)
    checkpoint = "2026-10-17T11:42:45.752058Z"
    kanban = worktree / tool.KANBAN_REL
    log = worktree / "DealFlow/sync_logs/20261019_nightly_sync.log"
    kanban.write_text(kanban_text(checkpoint, stage="Captured"), encoding="utf-8")
    log.write_text(audit_text(checkpoint).replace("Acme: Screening", "Acme: Captured"), encoding="utf-8")
    run(["git", "add", "."], cwd=worktree)
    run(["git", "commit", "-m", "baseline"], cwd=worktree)
    run(["git", "push", "origin", "HEAD:master"], cwd=worktree)

    kanban.write_text(kanban_text(checkpoint), encoding="utf-8")
    log.write_text(audit_text(checkpoint), encoding="utf-8")
    return worktree, log, checkpoint, remote


class DealflowKanbanToolTest(unittest.TestCase):
    def test_validate_rejects_invalid_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree, log, _, _ = make_repo(Path(tmp))
            (worktree / tool.KANBAN_REL).write_text(
                kanban_text("2026-10-17T11:42:45.752058Z", stage="Made Up"),
                encoding="utf-8",
            )
            result = tool._validate(worktree, log)
            self.assertFalse(result["success"])
            self.assertTrue(any("invalid stage 'Made Up'" in item for item in result["errors"]))

    def test_validate_and_publish_with_remote_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree, log, checkpoint, _ = make_repo(root)
            with (
                patch.object(tool, "STATE_ROOT", root / "state"),
                patch.object(tool, "RECEIPT_DIR", root / "state" / "receipts"),
            ):
                hook = worktree / ".git/hooks/pre-commit"
                hook.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
                hook.chmod(0o755)
                validation = tool._validate(worktree, log)
                self.assertTrue(validation["success"], validation["errors"])
                self.assertEqual(validation["checkpoint"], checkpoint)
                self.assertEqual(validation["company_count"], 1)

                receipt = tool._publish(
                    worktree,
                    log,
                    validation_receipt=validation["validation_receipt"],
                    commit_message="Dealflow sync: deterministic fixture",
                )
                self.assertTrue(receipt["success"])
                self.assertTrue(receipt["read_back_verified"])
                self.assertEqual(receipt["remote_master"], receipt["final_audit_commit"])
                remote_master = run(["git", "ls-remote", "origin", "refs/heads/master"], cwd=worktree).split()[0]
                self.assertEqual(remote_master, receipt["final_audit_commit"])
                persisted = json.loads(Path(receipt["receipt_path"]).read_text(encoding="utf-8"))
                self.assertEqual(persisted["final_audit_commit"], receipt["final_audit_commit"])
                self.assertIn("status: publication_evidence_in_tool_receipt", log.read_text(encoding="utf-8"))
                self.assertEqual(run(["git", "status", "--porcelain"], cwd=worktree), "")

    def test_publish_recovers_after_remote_push_before_final_receipt_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree, log, _, _ = make_repo(root)
            with (
                patch.object(tool, "STATE_ROOT", root / "state"),
                patch.object(tool, "RECEIPT_DIR", root / "state" / "receipts"),
            ):
                validation = tool._validate(worktree, log)
                real_write = tool._write_receipt_atomic

                def flaky_write(path: Path, payload: dict) -> None:
                    if payload.get("state") == "published":
                        raise OSError("simulated receipt-finalization failure")
                    real_write(path, payload)

                with patch.object(tool, "_write_receipt_atomic", side_effect=flaky_write):
                    with self.assertRaises(OSError):
                        tool._publish(
                            worktree,
                            log,
                            validation_receipt=validation["validation_receipt"],
                            commit_message="Dealflow sync: recovery fixture",
                        )

                recovered = tool._publish(
                    worktree,
                    log,
                    validation_receipt="not-needed-for-prepared-recovery",
                    commit_message="not-needed-for-prepared-recovery",
                )
                self.assertTrue(recovered["success"])
                self.assertEqual(recovered["state"], "published")
                self.assertEqual(recovered["remote_master"], recovered["expected_commit"])

    def test_metadata_change_requires_existing_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree, log, _, _ = make_repo(Path(tmp))
            text = log.read_text(encoding="utf-8")
            text = text.replace("changed_existing_company_count: 0", "changed_existing_company_count: 1")
            text = text.replace("metadata_only_change_count: 0", "metadata_only_change_count: 1")
            text = text.replace("metadata_only_changes: []", 'metadata_only_changes:\n    - "Ghost: refreshed"')
            log.write_text(text, encoding="utf-8")
            result = tool._validate(worktree, log)
            self.assertFalse(result["success"])
            self.assertTrue(any("missing company: Ghost" in item for item in result["errors"]))

    def test_untracked_audit_trailing_whitespace_fails_diff_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree, log, _, _ = make_repo(Path(tmp))
            log.write_text(log.read_text(encoding="utf-8") + "bad: value  \n", encoding="utf-8")
            result = tool._validate(worktree, log)
            self.assertFalse(result["success"])
            self.assertFalse(result["checks"]["git_diff_check"])

    def test_handler_pins_repository_origin_and_audit_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree, log, _, remote = make_repo(root)
            with (
                patch.object(tool, "STATE_ROOT", root / "state"),
                patch.object(tool, "REPO_PATH", worktree),
                patch.object(tool, "EXPECTED_ORIGIN", str(remote)),
            ):
                payload = json.loads(tool.dealflow_kanban("validate", str(worktree), str(log)))
                self.assertTrue(payload["success"], payload)
                with patch.object(tool, "EXPECTED_ORIGIN", "ssh://wrong.invalid/repo.git"):
                    rejected = json.loads(tool.dealflow_kanban("validate", str(worktree), str(log)))
                    self.assertFalse(rejected["success"])
                    self.assertIn("Unexpected origin URL", rejected["error"])
                wrong_name = log.with_name("latest.log")
                wrong_name.write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
                rejected = json.loads(tool.dealflow_kanban("validate", str(worktree), str(wrong_name)))
                self.assertFalse(rejected["success"])
                self.assertIn("YYYYMMDD_nightly_sync.log", rejected["error"])

    def test_receipt_path_rejects_malformed_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tool, "RECEIPT_DIR", Path(tmp) / "receipts"):
                with self.assertRaises(tool.ValidationError):
                    tool._receipt_path({"run_id": "../../escaped"})

    def test_run_spec_is_registered(self) -> None:
        payload = json.loads(tool.dealflow_kanban("run_spec"))
        self.assertTrue(payload["success"])
        self.assertIn("validate", " ".join(payload["workflow"]))
        self.assertEqual(tool.DEALFLOW_KANBAN_SCHEMA["name"], "dealflow_kanban")


if __name__ == "__main__":
    unittest.main()
