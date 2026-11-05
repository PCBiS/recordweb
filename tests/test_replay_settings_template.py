import importlib.util
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parents[1]
CANDIDATE = HERE / 'upstream' / 'templates'
spec = importlib.util.spec_from_file_location('patch_config_ui', HERE / 'docker_support/replay/config_ui_patch.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.form = None
        self.forms = []
        self.fields = []
        self.scripts = []
        self.in_script = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.form = attrs.get('id', '')
            self.forms.append(self.form)
        if tag in ('input', 'select', 'textarea') and 'name' in attrs:
            self.fields.append((self.form, tag, attrs))
        if tag == 'script':
            self.in_script = True
            self.scripts.append([attrs, ''])

    def handle_data(self, data):
        if self.in_script:
            self.scripts[-1][1] += data

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None
        if tag == 'script':
            self.in_script = False


class ReplaySettingsTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (CANDIDATE / 'config.html').read_text()
        cls.updated = patch.transform(cls.original)
        cls.env = Environment(loader=ChoiceLoader([
            DictLoader({'original.html': cls.original, 'updated.html': cls.updated,
                        'partials/replay_settings.html': (HERE / 'templates/partials/replay_settings.html').read_text()}),
            FileSystemLoader(str(CANDIDATE)),
        ]), autoescape=select_autoescape(['html']))

    def render(self, template, logged_in=True, installed=True, config=None):
        request = SimpleNamespace(session={'logged_in': logged_in}, url_for=lambda name: '/' + name)
        return self.env.get_template(template).render(
            request=request, url_for=lambda name, path: '/' + path,
            config=config or {}, notification={}, account=None, channels=[],
            replay_settings_installed=installed, loginMode=True, replay_enabled=True,
            web_ui_title='RecordWEB', program_version='test')

    def test_transform_preserves_original_and_rejects_ambiguous_input(self):
        self.assertEqual(self.updated.replace(patch.INCLUDE, '', 1), self.original)
        self.assertEqual(patch.transform(self.original), self.updated)
        for source in (self.updated, self.original.replace(patch.MARKER, ''),
                       self.original + patch.MARKER):
            with self.assertRaises(ValueError):
                patch.transform(source)

    def test_original_forms_fields_scripts_and_navigation_preserved(self):
        old = Elements(self.render('original.html'))
        new_html = self.render('updated.html')
        new = Elements(new_html)
        self.assertEqual(old.forms, new.forms)
        old_fields = Counter((form, tag, attrs['name']) for form, tag, attrs in old.fields)
        new_fields = Counter((form, tag, attrs['name']) for form, tag, attrs in new.fields)
        replay = Counter(('configForm', tag, name) for tag, name in [
            ('select', 'replayEnabled'), ('input', 'replayDownloadRoot'), ('input', 'replayMinFreeGiB')])
        self.assertEqual(new_fields - old_fields, replay)
        self.assertEqual(old_fields - new_fields, Counter())
        self.assertEqual(old.scripts, new.scripts)
        self.assertIn('class="navbar-container"', new_html)
        self.assertEqual(new_html.count('class="navbar-container"'), 1)

    def test_render_values_login_and_installation(self):
        html = self.render('updated.html', config={'replayEnabled': True,
                           'replayDownloadRoot': '/recordings/test & more', 'replayMinFreeGiB': 2.125})
        fields = {a['name']: a for _, _, a in Elements(html).fields if a['name'].startswith('replay')}
        self.assertEqual(fields['replayDownloadRoot']['value'], '/recordings/test & more')
        self.assertEqual(fields['replayMinFreeGiB']['value'], '2.125')
        self.assertEqual((fields['replayMinFreeGiB']['min'], fields['replayMinFreeGiB']['max'],
                          fields['replayMinFreeGiB']['step']), ('0.001', '65536', '0.001'))
        self.assertTrue(all('disabled' not in field for field in fields.values()))
        self.assertIn('<option value="true" selected>ON</option>', html)
        defaults = self.render('updated.html', logged_in=False)
        disabled = {a['name']: a for _, _, a in Elements(defaults).fields if a['name'].startswith('replay')}
        self.assertEqual(len(disabled), 3)
        self.assertTrue(all('disabled' in field for field in disabled.values()))
        self.assertEqual(disabled['replayDownloadRoot']['value'], '/recordings/replay')
        self.assertEqual(disabled['replayMinFreeGiB']['value'], '5')
        self.assertIn('<option value="false" selected>OFF</option>', defaults)
        self.assertIn('다시보기 설정을 변경하려면 로그인해야 합니다', defaults)
        self.assertNotIn('name="replayEnabled"', self.render('updated.html', installed=False))


if __name__ == '__main__':
    unittest.main()
