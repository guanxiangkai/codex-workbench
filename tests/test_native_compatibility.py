"""更新检测的负向回归：未知版本、缺验收、删字段和新增必填必须挡住。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.native_compatibility import NATIVE_CASES, PROTOCOL_CASES, evaluate_upgrade, protocol_contract
from codex_workbench.native_compatibility import _shape


class NativeCompatibilityTests(unittest.TestCase):
    artifacts = {'cli_sha256': 'a' * 64, 'desktop_sha256': 'b' * 64}
    def test_protocol_success_without_native_acceptance_does_not_enable_routing(self):
        result = evaluate_upgrade({'a': 1}, {'a': 1}, {case: True for case in PROTOCOL_CASES})
        self.assertTrue(result['protocol_compatible'])
        self.assertTrue(result['isolated_behavior_passed'])
        self.assertFalse(result['native_routing_ready'])

    def test_missing_behavior_check_does_not_count_as_pass(self):
        accepted = {'artifacts': self.artifacts, 'cases': {case: True for case in NATIVE_CASES}}
        self.assertFalse(evaluate_upgrade({}, {}, {'first_turn_completed': True}, accepted, artifacts=self.artifacts)['native_routing_ready'])

    def test_new_native_version_invalidates_old_acceptance(self):
        accepted = {'artifacts': self.artifacts, 'cases': {case: True for case in NATIVE_CASES}}
        updated = {**self.artifacts, 'desktop_sha256': 'c' * 64}
        self.assertFalse(evaluate_upgrade({}, {}, {case: True for case in PROTOCOL_CASES}, accepted, artifacts=updated)['native_routing_ready'])

    def test_protocol_change_is_blocked_even_with_native_evidence(self):
        accepted = {'artifacts': self.artifacts, 'cases': {case: True for case in NATIVE_CASES}}
        result = evaluate_upgrade({'request': 'old'}, {'request': 'new'}, {case: True for case in PROTOCOL_CASES}, accepted, artifacts=self.artifacts)
        self.assertEqual(['request'], result['changed_contracts'])
        self.assertFalse(result['native_routing_ready'])

    def test_exact_artifacts_and_all_evidence_are_required(self):
        accepted = {'artifacts': self.artifacts, 'cases': {case: True for case in NATIVE_CASES}}
        checks = {case: True for case in PROTOCOL_CASES}
        self.assertTrue(evaluate_upgrade({}, {}, checks, accepted, artifacts=self.artifacts)['native_routing_ready'])
        del accepted['cases']['prewarm_default_change']
        self.assertFalse(evaluate_upgrade({}, {}, checks, accepted, artifacts=self.artifacts)['native_routing_ready'])

    def test_irrelevant_optional_field_is_compatible_but_required_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / 'SampleParams.json'
            schema = {'properties': {'id': {'type': 'string'}}, 'required': ['id']}
            (root / 'ClientRequest.json').write_text(json.dumps({'oneOf': [{'properties': {'method': {'enum': ['sample']}}}]}))
            with patch('codex_workbench.native_compatibility.FIELDS', {'SampleParams': ['id']}), patch('codex_workbench.native_compatibility.METHODS', {'sample'}):
                request.write_text(json.dumps(schema)); before = protocol_contract(root)
                schema['description'] = 'New documentation'
                schema['properties']['optional'] = {'type': 'string'}
                request.write_text(json.dumps(schema)); self.assertEqual(before, protocol_contract(root))
                schema['required'].append('optional')
                request.write_text(json.dumps(schema)); self.assertNotEqual(before, protocol_contract(root))
                del schema['properties']['id']
                request.write_text(json.dumps(schema))
                with self.assertRaisesRegex(ValueError, 'Missing protocol field'):
                    protocol_contract(root)

    def test_removal_of_method_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'ClientRequest.json').write_text('{"oneOf": []}')
            with patch('codex_workbench.native_compatibility.FIELDS', {}):
                with self.assertRaisesRegex(ValueError, 'Missing protocol methods'):
                    protocol_contract(root)

    def test_description_property_is_data_not_schema_documentation(self):
        before = {'type': 'object', 'properties': {'description': {'type': 'string'}}}
        after = {'type': 'object', 'properties': {'description': {'type': 'integer'}}}
        self.assertNotEqual(_shape({}, before), _shape({}, after))
