"""Contract checks for KeyBridge's native and fallback depictions."""
import json
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'depictions' / 'com.wm0104.keybridge'
CHANGES = [
    '- 新增苹果原生候选习惯学习。',
    '- 修复学习记录保存与恢复。',
    '- 优化词库编辑及设置界面。',
]
COMPATIBILITY = [
    '兼容测试范围：iOS 15.0–16.x，苹果中文键盘26键全拼与九宫格。',
    '目前仅在 iOS 16.3（roothide）真机验证，其他环境需测试。',
]


class PageText(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())


class KeyBridgeDepictionTests(unittest.TestCase):
    def setUp(self):
        self.native_source = (PAGE / 'depiction.json').read_text(encoding='utf-8')
        self.native = json.loads(self.native_source)
        self.html_source = (PAGE / 'index.html').read_text(encoding='utf-8')
        self.html = PageText(self.html_source)

    def test_native_structure(self):
        self.assertEqual(self.native['class'], 'DepictionTabView')
        self.assertEqual(self.native['minVersion'], '0.4')
        self.assertEqual([tab['tabname'] for tab in self.native['tabs']], ['详情', '更新日志'])
        required = {
            'DepictionHeaderView': ['title'],
            'DepictionSubheaderView': ['title'],
            'DepictionLabelView': ['text'],
            'DepictionTableTextView': ['title', 'text'],
        }
        for tab in self.native['tabs']:
            self.assertEqual(tab['class'], 'DepictionStackView')
            for view in tab['views']:
                self.assertIn(view['class'], required)
                for field in required[view['class']]:
                    self.assertIsInstance(view[field], str)
                    self.assertTrue(view[field])

    def test_exact_changelog_and_literal_hyphens(self):
        views = self.native['tabs'][1]['views']
        self.assertEqual(views[0], {'class': 'DepictionHeaderView', 'title': '0.0.2'})
        self.assertEqual(views[1:], [
            {'class': 'DepictionLabelView', 'text': text} for text in CHANGES
        ])
        self.assertEqual([text for text in self.html.text if text.startswith('- ')], CHANGES)
        for text in CHANGES:
            self.assertIn(f'<p>{text}</p>', self.html_source)

    def test_compatibility_and_category_match(self):
        views = self.native['tabs'][0]['views']
        for text in COMPATIBILITY:
            self.assertIn({'class': 'DepictionLabelView', 'text': text}, views)
            self.assertIn(text, self.html.text)
        self.assertIn({'class': 'DepictionTableTextView', 'title': '分类', 'text': 'Ai插件'}, views)
        self.assertIn('分类：Ai插件', self.html.text)

    def test_no_legacy_changelog_dates_or_list_rendering(self):
        for source in (self.native_source, self.html_source):
            self.assertNotIn('0.0.1', source)
            self.assertNotIn('日期', source)
            self.assertNotRegex(source, r'\b\d{4}[-/.年]\d{1,2}')
            self.assertNotIn('•', source)
        self.assertNotIn('DepictionMarkdownView', self.native_source)
        self.assertTrue({'ul', 'ol', 'li', 'time', 'script'}.isdisjoint(self.html.tags))
        self.assertIn('0.0.2', self.html.text)
        self.assertIn('更新日志', self.html.text)


if __name__ == '__main__':
    unittest.main()
