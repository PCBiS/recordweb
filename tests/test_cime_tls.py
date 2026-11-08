"""Actual local TLS behavior of composed CIME source; synthetic Cookie only."""
import ast
import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import types
from typing import Dict, Optional, Tuple
import unittest
from unittest.mock import patch

import aiohttp

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('CIME_TEST_SOURCE',str(ROOT)))
spec=importlib.util.spec_from_file_location('cime_fixture_transform',SOURCE/'docker_support/replay/cime_tls_patch.py')
transform_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(transform_module)
ORIGINAL=(SOURCE/'upstream/module/cime_recorder.py').read_text()
TRANSFORMED=transform_module.transform(ORIGINAL)


def functions(text,names,namespace):
    tree=ast.parse(text)
    tree.body=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
    if {node.name for node in tree.body}!=names:raise AssertionError('missing_cime_fixture_function')
    exec(compile(tree,'cime_tls_fixture_source','exec'),namespace)
    return namespace


class CimeTlsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='cime-tls-fixture-')
        cls.root=Path(cls.temporary.name);cls.hits=[]
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','1',
            '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost',
            '-keyout',str(cls.root/'key.pem'),'-out',str(cls.root/'cert.pem')],check=True,capture_output=True)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits.append({'path':self.path,'cookie':self.headers.get('Cookie'),'user_agent':self.headers.get('User-Agent')})
                if self.path.startswith('/redirect'):
                    self.send_response(302)
                    target='/public' if self.path=='/redirect' else f'https://127.0.0.1:{self.server.server_port}/login'
                    self.send_header('Location',target);self.end_headers();return
                status=200
                if self.path=='/login':
                    status=200 if self.headers.get('Cookie')=='SYNTHETIC=auth' else 401
                    body=b'synthetic authenticated body' if status==200 else b'synthetic login required'
                elif self.path=='/hls':body=b'#EXTM3U\n#EXTINF:2,\nsegment.ts\n'
                elif self.path=='/invalid-utf8':body=b'\xfffixture'
                elif self.path=='/forbidden':status=403;body=b'synthetic unavailable'
                else:body='synthetic public body 합성'.encode()
                self.send_response(status);self.send_header('Content-Type','text/plain; charset=utf-8');self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.root/'cert.pem'),str(cls.root/'key.pem'))
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'https://localhost:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=3);cls.temporary.cleanup()

    def setUp(self):
        self.hits.clear()
        self.ns=functions(TRANSFORMED,{'_createCimeSslContext','_fetchText','_isLivePlaylistText','isLivePlaylist'},
            {'ssl':ssl,'aiohttp':aiohttp,'Optional':Optional,'Dict':Dict,'Tuple':Tuple,
             'certifi':types.SimpleNamespace(where=lambda:str(self.root/'cert.pem')),
             '_headers':lambda:{'Cookie':'SYNTHETIC=default'}})

    async def test_valid_public_body_and_default_headers_are_preserved(self):
        status,url,body=await self.ns['_fetchText'](self.base+'/public',headers={})
        self.assertEqual((status,url,body),(200,self.base+'/public','synthetic public body 합성'))
        self.assertEqual(self.hits,[{'path':'/public','cookie':'SYNTHETIC=default','user_agent':self.hits[0]['user_agent']}])

    async def test_valid_cookie_authenticated_body_and_explicit_headers_are_preserved(self):
        status,url,body=await self.ns['_fetchText'](self.base+'/login',headers={'Cookie':'SYNTHETIC=auth','User-Agent':'synthetic-client'})
        self.assertEqual((status,body),(200,'synthetic authenticated body'))
        self.assertEqual(self.hits,[{'path':'/login','cookie':'SYNTHETIC=auth','user_agent':'synthetic-client'}])

    async def test_valid_tls_login_required_and_http_status_are_not_hidden(self):
        status,_,body=await self.ns['_fetchText'](self.base+'/login',headers={'Accept':'text/plain'})
        self.assertEqual((status,body),(401,'synthetic login required'))
        status,_,body=await self.ns['_fetchText'](self.base+'/forbidden')
        self.assertEqual((status,body),(403,'synthetic unavailable'))

    async def test_valid_redirect_and_replacement_text_decoding_are_preserved(self):
        status,url,body=await self.ns['_fetchText'](self.base+'/redirect')
        self.assertEqual((status,url,body),(200,self.base+'/public','synthetic public body 합성'))
        self.assertEqual([hit['path'] for hit in self.hits],['/redirect','/public'])
        status,_,body=await self.ns['_fetchText'](self.base+'/invalid-utf8')
        self.assertEqual((status,body),(200,'\ufffdfixture'))

    async def assert_rejected_once_without_http_cookie(self,url):
        output=io.StringIO()
        real_session=aiohttp.ClientSession
        with patch.object(aiohttp,'ClientSession',wraps=real_session) as sessions, \
             contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            with self.assertRaises((aiohttp.ClientConnectorCertificateError,aiohttp.ClientConnectorSSLError,ssl.SSLError)):
                await self.ns['_fetchText'](url,timeout=3,headers={'Cookie':'SYNTHETIC=auth'})
        self.assertEqual(sessions.call_count,1,'certificate failure must not create an insecure retry session')
        self.assertEqual(self.hits,[],'no HTTP request or Cookie may follow an untrusted TLS handshake')
        self.assertEqual(output.getvalue(),'')

    async def test_untrusted_certificate_is_not_retried_without_verification(self):
        self.ns['certifi']=None
        await self.assert_rejected_once_without_http_cookie(self.base+'/login')

    async def test_hostname_mismatch_is_not_retried_without_verification(self):
        await self.assert_rejected_once_without_http_cookie(f'https://127.0.0.1:{self.server.server_port}/login')

    async def test_missing_custom_ssl_context_uses_strict_aiohttp_default(self):
        self.ns['_createCimeSslContext']=lambda:None
        await self.assert_rejected_once_without_http_cookie(self.base+'/login')

    async def test_redirect_to_invalid_hostname_is_rejected_before_second_cookie_request(self):
        with self.assertRaises((aiohttp.ClientConnectorCertificateError,aiohttp.ClientConnectorSSLError,ssl.SSLError)):
            await self.ns['_fetchText'](self.base+'/redirect-bad-host',headers={'Cookie':'SYNTHETIC=auth'})
        self.assertEqual([hit['path'] for hit in self.hits],['/redirect-bad-host'])

    async def test_live_playlist_gate_closes_on_tls_failure_and_accepts_valid_playlist(self):
        self.assertTrue(await self.ns['isLivePlaylist'](self.base+'/hls'))
        self.hits.clear();self.ns['certifi']=None
        self.assertFalse(await self.ns['isLivePlaylist'](self.base+'/hls'))
        self.assertEqual(self.hits,[])


class CimeSourceTransformTests(unittest.TestCase):
    def test_transform_only_changes_fetch_and_preserves_ca_resolution_and_other_functions(self):
        def others(text):
            tree=ast.parse(text)
            return {node.name:ast.get_source_segment(text,node) for node in tree.body
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name!='_fetchText'}
        self.assertEqual(others(ORIGINAL),others(TRANSFORMED))
        self.assertNotIn('_request_with_ssl(False)',TRANSFORMED)

    def test_transform_rejects_unknown_or_already_patched_source(self):
        with self.assertRaises(ValueError):transform_module.transform('pass\n')
        with self.assertRaises(ValueError):transform_module.transform(TRANSFORMED)

    def test_execution_manifest_produces_checked_fail_closed_source(self):
        spec=importlib.util.spec_from_file_location('cime_fixture_composer',SOURCE/'docker_support/compose_source.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import hashlib
        original_hash=hashlib.sha256((SOURCE/'upstream/module/cime_recorder.py').read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix='cime-composed-fixture-') as temporary:
            out=Path(temporary)/'app'
            receipt=module.compose(SOURCE/'upstream',out,SOURCE/'patches/upstream.json',SOURCE/'patches/public.json')
            self.assertEqual((out/'module/cime_recorder.py').read_text(),TRANSFORMED)
            self.assertEqual(receipt['files']['module/cime_recorder.py'],hashlib.sha256(TRANSFORMED.encode()).hexdigest())
        self.assertEqual(hashlib.sha256((SOURCE/'upstream/module/cime_recorder.py').read_bytes()).hexdigest(),original_hash)


if __name__=='__main__':unittest.main()
