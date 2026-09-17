"""任务标题展示边界的合成数据测试。"""

import unittest

from codex_workbench.titles import safe_display_title, suggest_title


class SafeDisplayTitleTests(unittest.TestCase):
    """确保列表标题不会成为任务正文或凭据的旁路。"""

    def test_short_title_is_stable(self) -> None:
        self.assertEqual("修复登录问题", safe_display_title("修复登录问题"))
        self.assertEqual("季度汇报与财务分析", safe_display_title("季度汇报与财务分析"))

    def test_markdown_url_and_absolute_path_are_reduced(self) -> None:
        value = safe_display_title("请修复 [接口文档](https://example.test/api?token=synthetic) 中的 /workspace/project/config.json")
        self.assertEqual("修复 接口文档 中的 文件(config.json)", value)
        self.assertNotIn("https://", value)
        self.assertNotIn("/workspace/", value)

    def test_long_multiline_prompt_extracts_bounded_action(self) -> None:
        value = safe_display_title("请帮我\n修复任务列表标题泄露问题。" + "无关说明" * 40, limit=20)
        self.assertEqual("修复任务列表标题泄露问题", value)
        self.assertLessEqual(len(value), 20)

    def test_secret_values_are_not_returned_by_display_or_suggested_title(self) -> None:
        secret = "synthetic_long_value"
        prompt = f"请修复发布流程，APP\\_KEY {secret} 密码：{secret} Authorization: Bearer {secret}"
        for title in (safe_display_title(prompt), suggest_title(prompt)):
            self.assertNotIn(secret, title)
            self.assertIn("修复发布流程", title)

    def test_path_next_to_chinese_text_uses_only_its_basename(self) -> None:
        value = safe_display_title("分析一下/workspace/project/config.json，确认结果")
        self.assertIn("文件(config.json)", value)
        self.assertNotIn("/workspace/project", value)

    def test_invalid_url_is_replaced_without_raising(self) -> None:
        self.assertEqual("链接", safe_display_title("https://[invalid"))

    def test_html_is_plain_text_not_markup(self) -> None:
        self.assertEqual("修复 <img src=x> 标题", safe_display_title("修复 <img src=x> 标题"))


if __name__ == "__main__":
    unittest.main()
