"""Exercise source routes with FastAPI/ASGI and synthetic state only.

The application's recorder/bootstrap is deliberately not imported. Selected
unchanged route definitions are compiled into a fresh in-memory FastAPI app.
No network connections, real cookies or production paths are used.
"""
import ast
import asyncio
import contextlib
import copy
import io
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import traceback
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import unittest

import httpx
import psutil
from fastapi import FastAPI, Request, HTTPException, Depends, Body, Query, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from starlette.middleware.sessions import SessionMiddleware
from werkzeug.security import generate_password_hash, check_password_hash

from module.storage_metrics import normalizeMonitorPaths


SOURCE = Path(__file__).resolve().parents[1]


def load_functions(filename, names, namespace):
    tree = ast.parse((SOURCE / filename).read_bytes(), filename=filename)
    nodes = [
        copy.deepcopy(n) for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    found = {n.name for n in nodes}
    if found != names:
        raise AssertionError(f"Missing test source functions: {names - found}")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)


class WebSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'recordings'
        self.root.mkdir()
        self.outside = Path(self.tmp.name) / 'outside'
        self.outside.mkdir()
        self.account_file = Path(self.tmp.name) / 'account.json'
        self.account_file.write_text('synthetic account marker')
        self.current_password = 'synthetic-current-password'
        self.new_password = 'synthetic-new-password'
        self.account = {
            'username': 'synthetic-admin',
            'password': generate_password_hash(self.current_password, method='pbkdf2:sha256:1000'),
        }
        self.saved_accounts = []
        self.saved_cookies = []
        self.saved_configs = []
        self.saved_notifications = []
        self.metrics_calls = []
        self.config_reads = 0
        self.template_calls = []
        self.config = {
            'loginMode': True,
            'fileManagerEnabled': True,
            'fileManagerMode': 'whitelist',
            'fileManagerRoots': [str(self.root)],
            'fileManagerReadOnly': False,
            'trashEnabled': False,
        }
        self.cookies = {'chzzk': {'NID_AUT': 'synthetic-cookie-only'}}
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key='synthetic-test-secret')
        self.app.state.config = self.config
        self.app.state.channels = []
        self.app.state.channels_lock = asyncio.Lock()

        def load_config():
            self.config_reads += 1
            return self.config.copy()

        def render_template(name, context, **kwargs):
            self.template_calls.append(name)
            return JSONResponse({
                'template': name,
                'cookies': context.get('cookies'),
                'config': context.get('config'),
            }, status_code=kwargs.get('status_code', 200))

        def save_account(account):
            self.saved_accounts.append(account.copy())
            self.account = account.copy()

        def save_config(config):
            self.saved_configs.append(copy.deepcopy(config))
            self.config.clear()
            self.config.update(copy.deepcopy(config))

        def collect_metrics(*args):
            self.metrics_calls.append(copy.deepcopy(args))
            return {'disks': []}

        async def run_inline(function, *args, **kwargs):
            # Endpoint orchestration is tested here; collector/cache tests cover
            # the synchronous work without starting background recorder threads.
            return function(*args, **kwargs)

        ns = {
            '__name__': 'security_test_routes', 'app': self.app,
            'Any': Any, 'Dict': Dict, 'List': List, 'Optional': Optional,
            'FastAPI': FastAPI, 'Request': Request, 'HTTPException': HTTPException,
            'Depends': Depends, 'Body': Body, 'Query': Query, 'Form': Form,
            'JSONResponse': JSONResponse, 'HTMLResponse': HTMLResponse,
            'RedirectResponse': RedirectResponse, 'FileResponse': FileResponse,
            'os': os, 'ntpath': ntpath, 'shutil': shutil, 'time': time, 're': re,
            'traceback': traceback,
            'asyncio': SimpleNamespace(to_thread=run_inline), 'psutil': psutil,
            'loadConfig': load_config,
            'saveConfig': save_config,
            'saveNotification': lambda data: self.saved_notifications.append(copy.deepcopy(data)),
            'loadCookies': lambda: copy.deepcopy(self.cookies),
            'saveCookies': lambda data: self.saved_cookies.append(copy.deepcopy(data)),
            'loadAccount': lambda: self.account.copy(), 'saveAccount': save_account,
            'loadNotification': lambda: {'telegram_bot_token': 'synthetic-notification-only'},
            'generate_password_hash': generate_password_hash,
            'check_password_hash': check_password_hash,
            'LOGIN_PATH': str(self.account_file),
            'templates': SimpleNamespace(TemplateResponse=render_template),
            'PROGRAM_VERSION': 'security-test',
            'PROGRAM_NAME': 'recordWEB security test',
            'recorder_manager': SimpleNamespace(),
            'busyFilePaths': lambda *args: [], 'isLocked': lambda *args: False,
            'listDir': lambda *args, **kwargs: [],
            '_collectSysMetrics': collect_metrics,
            'normalizeMonitorPaths': normalizeMonitorPaths,
            'TRUE_SET': {'1', 'true', 't', 'yes', 'y', 'on'},
            'FALSE_SET': {'0', 'false', 'f', 'no', 'n', 'off', ''},
        }
        file_functions = {
            'normPath', 'buildAllowedRoots', 'ensureInRoots', 'isDeniedPath',
            'validateEntryName', 'childDestination', 'mkdirPath', 'renamePath',
            'diskUsageFor', 'listDisks', 'listMountRoots',
        }
        load_functions('module/file_manager.py', file_functions, ns)
        load_functions('module/data_manager.py', {'toBool'}, ns)
        web_functions = {
            '_wantsJsonResponse', 'http_exception_handler', 'requireLogin',
            'requireFileManager', 'requireWritableFileManager',
            'getCookies', 'updateCookies', 'configPage', 'get_config_api',
            'get_config', 'updateConfig', 'api_sys_metrics', 'updateAccount', 'login',
            'loginPage', 'index',
            'api_files_usage', 'api_files_list', 'api_files_roots', 'api_files_ls',
            'api_files_disk_usage', 'api_files_download', 'api_files_mkdir',
            'api_files_rename', 'api_files_move', 'api_files_delete',
            'api_files_streamcopy',
            '_normalizeAllowedRoots',
        }
        load_functions('recordWEB.py', web_functions, ns)
        self.ns = ns
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url='http://testserver', follow_redirects=False,
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def login(self):
        response = await self.client.post('/login', data={
            'username': self.account['username'], 'password': self.current_password,
        })
        self.assertEqual(response.status_code, 200)

    def account_form(self, **overrides):
        form = {
            'username': 'synthetic-admin', 'current_password': self.current_password,
            'new_password': self.new_password, 'new_password_confirm': self.new_password,
            'action': 'update',
        }
        form.update(overrides)
        return form

    async def test_anonymous_browser_reads_redirect_before_handlers(self):
        for path in ['/cookies', '/config']:
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers['location'], '/login')
                self.assertNotIn('synthetic-cookie-only', response.text)
        self.assertEqual(self.template_calls, [])
        self.assertEqual(self.config_reads, 0)

    async def test_anonymous_browser_config_write_cannot_run(self):
        response = await self.client.post('/config', data={'loginMode': 'off'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.config_reads, 0)
        self.assertTrue(self.config['loginMode'])

    async def test_login_landing_and_protected_redirects_reach_root_without_authenticating(self):
        for path in ['/login', '/config', '/cookies']:
            with self.subTest(path=path):
                response = await self.client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.url.path, '/')
                self.assertEqual(
                    [redirect.headers['location'] for redirect in response.history],
                    ['/'] if path == '/login' else ['/login', '/'],
                )
                self.assertEqual(response.json()['template'], 'index.html')
                self.assertTrue(response.json()['config']['loginMode'])
                self.assertNotIn('synthetic-cookie-only', response.text)
                self.assertEqual((await self.client.get('/api/config')).status_code, 401)
        self.assertEqual(self.template_calls, ['index.html'] * 3)
        self.assertEqual(self.config_reads, 0)

    async def test_anonymous_json_cookie_write_is_401_and_does_not_save(self):
        response = await self.client.post('/cookies', json={'chzzk': {'NID_AUT': 'synthetic'}})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'detail': 'Login required'})
        self.assertEqual(self.saved_cookies, [])

    async def test_api_and_legacy_json_reads_require_login(self):
        for path in ['/api/config', '/get_config', '/api/sys_metrics']:
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {'detail': 'Login required'})
                self.assertNotIn('location', response.headers)
        self.assertEqual(self.config_reads, 0)

    async def test_explicit_json_accept_on_browser_route_returns_401(self):
        response = await self.client.get('/cookies', headers={'accept': 'application/json'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.template_calls, [])

    async def test_authenticated_cookie_and_config_reads_and_cookie_write(self):
        await self.login()
        response = await self.client.get('/cookies')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cookies'], self.cookies)
        response = await self.client.get('/api/config')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['loginMode'])
        response = await self.client.post('/cookies', json=self.cookies)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved_cookies, [self.cookies])

    async def test_authenticated_metrics_passes_normalized_storage_snapshot(self):
        await self.login()
        self.config['systemMonitorPaths'] = ['  /recordings  ', '/recordings', '']
        self.app.state.channels = [{'id': 'synthetic-channel', 'output_dir': 'chzzk'}]
        response = await self.client.get('/api/sys_metrics')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'disks': []})
        self.assertEqual(self.metrics_calls, [(
            {'systemMonitorPaths': ['/recordings']}, [{'output_dir': 'chzzk'}],
        )])

    def config_form(self, **overrides):
        form = {
            'loginMode': 'on', 'fileManagerEnabled': 'on',
            'fileManagerMode': 'whitelist', 'fileManagerRoots': [str(self.root)],
        }
        form.update(overrides)
        return form

    async def test_settings_form_saves_multiline_monitor_paths(self):
        await self.login()
        response = await self.client.post('/config', data=self.config_form(
            systemMonitorPaths=' /recordings \n/archive\n/recordings\n\n'))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.saved_configs[-1]['systemMonitorPaths'], ['/recordings', '/archive'])
        self.assertEqual(self.app.state.config['systemMonitorPaths'], ['/recordings', '/archive'])
        self.assertEqual(len(self.saved_notifications), 1)

    async def test_settings_form_explicit_empty_monitor_paths_clears_selection(self):
        await self.login()
        self.config['systemMonitorPaths'] = ['/recordings']
        response = await self.client.post('/config', data=self.config_form(systemMonitorPaths=''))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.saved_configs[-1]['systemMonitorPaths'], [])

    async def test_settings_form_omitted_monitor_paths_preserves_selection(self):
        await self.login()
        self.config['systemMonitorPaths'] = ['/recordings', '/archive']
        response = await self.client.post('/config', data=self.config_form())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.saved_configs[-1]['systemMonitorPaths'], ['/recordings', '/archive'])

    async def test_login_mode_off_preserves_optional_auth_behavior(self):
        self.config['loginMode'] = False
        self.assertEqual((await self.client.get('/cookies')).status_code, 200)

    async def test_filemanager_disabled_blocks_all_file_apis(self):
        await self.login()
        self.config['fileManagerEnabled'] = False
        for path in ['usage', 'list', 'roots', 'ls', 'disk-usage', 'download']:
            with self.subTest(path=path):
                response = await self.client.get('/api/files/' + path, params={'path': str(self.root)})
                self.assertEqual(response.status_code, 403)
        for path in ['mkdir', 'rename', 'move', 'delete', 'streamcopy']:
            with self.subTest(path=path):
                response = await self.client.post('/api/files/' + path, json={})
                self.assertEqual(response.status_code, 403)

    async def test_filemanager_requires_login_mode_even_if_enabled_flag_is_true(self):
        self.config['loginMode'] = False
        self.config['fileManagerEnabled'] = True
        for path in ['usage', 'list', 'roots', 'ls', 'disk-usage', 'download']:
            with self.subTest(path=path):
                response = await self.client.get('/api/files/' + path, params={'path': str(self.root)})
                self.assertEqual(response.status_code, 403)
        for path in ['mkdir', 'rename', 'move', 'delete', 'streamcopy']:
            with self.subTest(path=path):
                response = await self.client.post('/api/files/' + path, json={})
                self.assertEqual(response.status_code, 403)

    async def test_readonly_blocks_every_write_api(self):
        await self.login()
        self.config['fileManagerReadOnly'] = True
        for path in ['mkdir', 'rename', 'move', 'delete', 'streamcopy']:
            with self.subTest(path=path):
                response = await self.client.post('/api/files/' + path, json={})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['detail'], 'Read-only mode')

    async def test_invalid_names_rejected_before_filesystem_changes(self):
        await self.login()
        source = self.root / 'original.ts'
        source.write_bytes(b'synthetic recording')
        invalid = ['', '.', '..', '../escape', str(self.outside / 'escape'),
                   r'..\escape', r'C:\escape', 'C:escape', 'bad\x00name', 'bad\nname', 123, ['name']]
        for name in invalid:
            for endpoint, path in [('mkdir', self.root), ('rename', source)]:
                with self.subTest(name=repr(name), endpoint=endpoint):
                    response = await self.client.post('/api/files/' + endpoint, json={
                        'path': str(path), 'new_name': name,
                    })
                    self.assertEqual(response.status_code, 400)
                    self.assertTrue(source.exists())
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertEqual(list(self.root.iterdir()), [source])

    async def test_destination_symlink_outside_whitelist_rejected(self):
        await self.login()
        source = self.root / 'original.ts'
        source.write_bytes(b'synthetic recording')
        destination = self.outside / 'outside.ts'
        destination.write_bytes(b'outside data')
        (self.root / 'redirect.ts').symlink_to(destination)
        response = await self.client.post('/api/files/rename', json={
            'path': str(source), 'new_name': 'redirect.ts',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(source.read_bytes(), b'synthetic recording')
        self.assertEqual(destination.read_bytes(), b'outside data')
        (self.root / 'redirect-dir').symlink_to(self.outside, target_is_directory=True)
        response = await self.client.post('/api/files/mkdir', json={
            'path': str(self.root), 'new_name': 'redirect-dir',
        })
        self.assertEqual(response.status_code, 403)

    async def test_normal_korean_names_spaces_and_rename_work(self):
        await self.login()
        response = await self.client.post('/api/files/mkdir', json={
            'path': str(self.root), 'new_name': '녹화 폴더',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.root / '녹화 폴더').is_dir())
        source = self.root / 'original.ts'
        source.write_bytes(b'synthetic recording')
        response = await self.client.post('/api/files/rename', json={
            'path': str(source), 'new_name': '새 녹화.ts',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(source.exists())
        self.assertEqual((self.root / '새 녹화.ts').read_bytes(), b'synthetic recording')

    async def test_windows_alias_and_alternate_stream_names_rejected(self):
        original_os = self.ns['os']
        self.ns['os'] = SimpleNamespace(name='nt')
        try:
            for name in ['file:stream', 'CON', 'con.txt', 'NUL.ts', 'COM1', 'LPT9.log', 'name.', 'name ']:
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        self.ns['validateEntryName'](name)
            self.assertEqual(self.ns['validateEntryName']('녹화 파일.ts'), '녹화 파일.ts')
        finally:
            self.ns['os'] = original_os

    async def test_account_change_requires_session_and_does_not_log_inputs(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = await self.client.post('/updateAccount', data=self.account_form())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.saved_accounts, [])
        self.assertNotIn(self.current_password, output.getvalue())

    async def test_account_update_verifies_password_hashes_new_one_and_clears_session(self):
        await self.login()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            wrong = await self.client.post('/updateAccount', data=self.account_form(current_password='synthetic-wrong'))
            self.assertEqual(wrong.status_code, 400)
            self.assertEqual(self.saved_accounts, [])
            response = await self.client.post('/updateAccount', data=self.account_form())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.saved_accounts), 1)
        self.assertTrue(check_password_hash(self.saved_accounts[0]['password'], self.new_password))
        self.assertNotEqual(self.saved_accounts[0]['password'], self.new_password)
        self.assertEqual((await self.client.get('/api/config')).status_code, 401)
        for value in [self.current_password, self.new_password, 'synthetic-wrong']:
            self.assertNotIn(value, output.getvalue())

    async def test_account_delete_requires_current_password_and_clears_session(self):
        await self.login()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = await self.client.post('/updateAccount', data=self.account_form(
                action='delete', current_password='synthetic-wrong'))
            self.assertEqual(response.status_code, 400)
            self.assertTrue(self.account_file.exists())
            response = await self.client.post('/updateAccount', data=self.account_form(action='delete'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.account_file.exists())
        self.assertEqual((await self.client.get('/api/config')).status_code, 401)
        self.assertNotIn(self.current_password, output.getvalue())

    async def test_account_unknown_action_empty_password_and_exception_do_not_leak(self):
        await self.login()
        response = await self.client.post('/updateAccount', data=self.account_form(action='unknown'))
        self.assertEqual(response.status_code, 400)
        response = await self.client.post('/updateAccount', data=self.account_form(
            new_password='', new_password_confirm=''))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.saved_accounts, [])
        def fail_save(account):
            raise RuntimeError(self.current_password)
        self.ns['saveAccount'] = fail_save
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = await self.client.post('/updateAccount', data=self.account_form())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(self.current_password, response.text)
        self.assertNotIn(self.current_password, output.getvalue())


if __name__ == '__main__':
    unittest.main()
