"""Config integrity tests (Phase 5A/5B).

Regression guard for the duplicated-config bug: config.py used to assign
CADENCE_MIN, MAX_HISTORY, SL_BUFFER_ATR, DAILY_LOSS_R and others twice, and
Python silently kept the last copy. The effective CADENCE_MIN (20) disagreed
with the 10-minute cron, doubling every loss-streak pause.
"""

import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENGINE_DIR = os.path.join(REPO, "engine")
sys.path.insert(0, ENGINE_DIR)

import config  # noqa: E402

CONFIG_PATH = os.path.join(ENGINE_DIR, "config.py")
WORKFLOW_PATH = os.path.join(REPO, ".github", "workflows", "engine.yml")


def top_level_assignments():
    """Names assigned at module level, in source order (tuple targets split)."""
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=CONFIG_PATH)
    names = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Tuple):
                names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
    return names


class TestNoDuplicateSettings(unittest.TestCase):
    def test_no_setting_is_assigned_twice(self):
        names = top_level_assignments()
        seen, dupes = set(), []
        for n in names:
            if n in seen:
                dupes.append(n)
            seen.add(n)
        self.assertEqual(dupes, [], f"config.py assigns these more than once: {dupes}")

    def test_config_still_defines_its_core_settings(self):
        required = [
            "CONFIG_VERSION", "SYMBOLS", "CLUSTERS", "TG_API",
            "MIN_MATCHES", "TAKE_MIN_MATCHES", "MIN_TP", "MAX_TP",
            "SL_BUFFER_ATR", "MAX_LOSS_STREAK", "PAUSE_SCANS", "DAILY_LOSS_R",
            "CADENCE_MIN", "MAX_HISTORY", "COOLDOWN_H", "REPLAY_TTL_H",
        ]
        names = set(top_level_assignments())
        missing = [r for r in required if r not in names]
        self.assertEqual(missing, [], f"config.py lost these settings: {missing}")


class TestSchedulingContract(unittest.TestCase):
    def test_cadence_matches_workflow_cron(self):
        with open(WORKFLOW_PATH, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r'cron:\s*"\*/(\d+)', text)
        self.assertIsNotNone(m, "engine.yml has no */N cron")
        self.assertEqual(
            int(m.group(1)), config.CADENCE_MIN,
            f"CADENCE_MIN={config.CADENCE_MIN} but the workflow runs every {m.group(1)} min",
        )


class TestSanity(unittest.TestCase):
    def test_target_envelope_is_ordered(self):
        self.assertLess(config.MIN_TP, config.MAX_TP)

    def test_symbols_are_complete(self):
        for sym, cfg in config.SYMBOLS.items():
            self.assertIn("yahoo", cfg, sym)
            self.assertIn("name", cfg, sym)

    def test_clusters_reference_known_symbols(self):
        for cluster in config.CLUSTERS:
            for sym in cluster:
                self.assertIn(sym, config.SYMBOLS)

    def test_risk_constants_are_usable(self):
        self.assertGreater(config.MAX_LOSS_STREAK, 0)
        self.assertGreater(config.PAUSE_SCANS, 0)
        self.assertLess(config.DAILY_LOSS_R, 0)
        self.assertGreater(config.MAX_HISTORY, 0)
        self.assertGreater(config.CADENCE_MIN, 0)


if __name__ == "__main__":
    unittest.main()
