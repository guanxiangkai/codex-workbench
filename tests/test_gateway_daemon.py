"""网关持久化授权白名单的无账户回归。"""

import unittest

from codex_workbench.gateway_daemon import _authorized_accounts


class GatewayAuthorizationTests(unittest.TestCase):
    """守护进程只接受设置中显式且一致的账户声明。"""

    def settings(self):
        return {
            "authorized_account_ids": ["current", "synthetic-account"],
            "accounts": [
                {"id": "current", "home": "/tmp/current", "subject_id": "synthetic-current"},
                {"id": "synthetic-account", "home": "/tmp/other", "subject_id": "synthetic-other"},
            ],
        }

    def test_returns_only_explicitly_authorized_accounts_in_whitelist_order(self):
        accounts = _authorized_accounts(self.settings())
        self.assertEqual(["current", "synthetic-account"], [account["id"] for account in accounts])

    def test_rejects_missing_current_duplicates_and_declaration_mismatch(self):
        cases = []
        missing_current = self.settings(); missing_current["authorized_account_ids"] = ["synthetic-account"]
        cases.append(missing_current)
        duplicate = self.settings(); duplicate["authorized_account_ids"] = ["current", "current"]
        cases.append(duplicate)
        mismatch = self.settings(); mismatch["accounts"] = mismatch["accounts"][:1]
        cases.append(mismatch)
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(ValueError, "授权账户配置无效"):
                    _authorized_accounts(settings)


if __name__ == "__main__":
    unittest.main()
