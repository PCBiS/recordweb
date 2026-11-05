import re
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docker_support.replay.navigation import NAMES, transform

BASE = Path(__file__).resolve().parents[1] / 'upstream' / 'templates'

class NavigationTests(unittest.TestCase):
    def test_preserves_body_forms_and_scripts(self):
        for name in NAMES:
            with self.subTest(name=name):
                source = (BASE / f'{name}.html').read_text()
                result = transform(name, source)
                for pattern in (r'<body\b[^>]*>', r'<form\b[^>]*>.*?</form>', r'<script\b[^>]*>.*?</script>'):
                    self.assertEqual(re.findall(pattern, source, re.S | re.I), re.findall(pattern, result, re.S | re.I))
                self.assertEqual(result.count("{% include 'partials/navigation.html' %}"), 1)
        self.assertIn('var savedPreset', transform('config', (BASE / 'config.html').read_text()))

if __name__ == '__main__':
    unittest.main()
