"""专业模型服务仅保存受管引用，不能把认证值写入普通配置。"""
import unittest
from codex_workbench.professional_services import validate_services


class ProfessionalServiceTests(unittest.TestCase):
    def test_local_and_online_service_references(self):
        values = validate_services([{"name": "本地服务", "base_url": "http://127.0.0.1:8000/v1"},
                                    {"name": "在线服务", "base_url": "https://example.invalid/v1", "credential_ref": "vault:service-model"}])
        self.assertEqual("openai-compatible", values[0]["protocol"])
        self.assertEqual("vault:service-model", values[1]["credential_ref"])

    def test_credentials_and_url_credentials_are_rejected(self):
        for changes in ({"credential_ref": "synthetic-invalid-reference"}, {"base_url": "https://example.invalid/v1?api_key=example"},
                        {"base_url": "https://user:example@example.invalid/v1"}, {"api_key": "synthetic-key"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_services([{"name": "示例", "base_url": "https://example.invalid/v1", **changes}])
