"""Independently checked subset of real Qwen 4B-generated boundary cases."""
import json
from pathlib import Path
import unittest

from graph_engineering.scheduling_policy import board_status


class ModelGeneratedPolicyTests(unittest.TestCase):
    def test_supervisor_accepted_cases_and_explicit_node_boundaries(self):
        path=Path(__file__).resolve().parents[1]/'evidence/phase2/qwen-case-verification.json'
        cases=json.loads(path.read_text())['cases']
        for case in cases:
            if not case['accepted']: continue
            with self.subTest(case=case):
                self.assertEqual(board_status(case['node'],case['status'],case['paused']),case['expected'])
        self.assertEqual(board_status('test_execution','ready'),'Testing')
        self.assertEqual(board_status('independent_review','running'),'Code Review')
        self.assertEqual(board_status('merge','running'),'Ready for Merge')
        self.assertEqual(board_status('repository_discovery','ready'),'To Do')
        self.assertEqual(board_status('unrelated','ready'),'In Progress')
