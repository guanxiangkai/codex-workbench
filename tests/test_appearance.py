"""常见矢量图标导入的呈现属性与主动内容边界。"""
import unittest

from codex_workbench.appearance import AppearanceError, icon_value, validate_icon


class SvgImportTests(unittest.TestCase):
    def test_standard_fill_rule_and_stroke_attributes_survive_import(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><title>合成图标</title><path fill-rule="evenodd" clip-rule="evenodd" stroke-dasharray="2 3" stroke-miterlimit="10" vector-effect="non-scaling-stroke" d="M2 2H22V22H2Z"/></svg>'
        self.assertEqual(svg, icon_value(validate_icon({"kind": "svg", "value": svg}))["value"])

    def test_active_or_external_content_and_non_svg_root_are_rejected(self):
        for svg in ('<path d="M0 0"/>', '<svg onload="alert(1)"></svg>', '<svg><image href="https://example.invalid/a.png"/></svg>', '<svg><path fill="url(https://example.invalid/a)"/></svg>'):
            with self.subTest(svg=svg), self.assertRaises(AppearanceError):
                validate_icon({"kind": "svg", "value": svg})


if __name__ == "__main__":
    unittest.main()
