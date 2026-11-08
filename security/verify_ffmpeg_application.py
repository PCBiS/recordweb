"""Actual application FFmpeg commands; generated local media and loopback only."""
import ast
import asyncio
import contextlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import subprocess
import sys
from unittest.mock import patch
import tempfile
import threading
import traceback
from typing import List

SOURCE=Path(os.environ['RUNTIME_SOURCE']).resolve()
FFMPEG=shutil.which('ffmpeg'); FFPROBE=shutil.which('ffprobe'); STREAMLINK=shutil.which('streamlink')
sys.path.insert(0,str(SOURCE))
ROWS=[]
FAILURES=[]
def attempt(label, function):
    try:function()
    except Exception as error:
        FAILURES.append({'check':label,'exception':type(error).__name__,'error':str(error)})
        traceback.print_exc()


def run(command, *, expect_success=True, **kwargs):
    result=subprocess.run(command,capture_output=True,timeout=60,**kwargs)
    if expect_success and result.returncode:
        print('FFMPEG_APPLICATION_STDERR='+result.stderr.decode(errors='replace'),flush=True)
        result.check_returncode()
    return result

def load(path,names,ns):
    tree=ast.parse((SOURCE/path).read_text())
    body=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
    assert {n.name for n in body}==names
    exec(compile(ast.Module(body=body,type_ignores=[]),str(path),'exec'),ns)
    return ns

def probe(path):
    value=json.loads(run([FFPROBE,'-v','error','-show_entries',
        'format=duration:stream=codec_type,codec_name,width,height','-of','json',str(path)]).stdout)
    assert float(value['format']['duration'])>=0.4,(path,value)
    assert {'video','audio'} <= {s['codec_type'] for s in value['streams']},(path,value)
    return value

with tempfile.TemporaryDirectory(prefix='candidate-ffmpeg-app-') as temporary:
    root=Path(temporary);(root/'hls').mkdir();(root/'segments').mkdir()
    source=root/'source.ts'
    run([FFMPEG,'-hide_banner','-v','error','-f','lavfi','-i','color=c=blue:s=960x540:r=10',
         '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','4','-threads','1',
         '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-g','10',
         '-c:a','aac','-b:a','96k','-f','mpegts',str(source)])
    run([FFMPEG,'-v','error','-i',str(source),'-c','copy','-f','hls','-hls_time','1',
         '-hls_list_size','0',str(root/'hls/master.m3u8')])
    hits=[];faults=[];received_cookies=[]
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path);received_cookies.append(self.headers.get('Cookie'))
            if self.path.startswith('/hls/opaque/'):
                self.path='/hls/'+self.path.split('/hls/opaque/',1)[1].split('?',1)[0]+'.ts'
            if self.path=='/reconnect/master.m3u8':
                if not faults:
                    faults.append('503');self.send_response(503);self.end_headers();return
                self.path='/hls/master.m3u8'
            elif self.path.startswith('/reconnect/'):
                self.path=self.path.replace('/reconnect/','/hls/',1)
            super().do_GET()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(root)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        def check_cime():
            cime=load('module/cime_recorder.py',{'_buildCimeFfmpegHeaders','buildCimeFfmpegCommand','remuxCimeTempToMp4','_readStream'},
                {'List':List,'getFFmpeg':lambda:FFMPEG,'CIME_USER_AGENT':'synthetic-fixture-agent',
                 'getCimeCookieHeader':lambda:'SYNTHETIC=fixture','os':os,'asyncio':asyncio,
                 'subprocess':subprocess,'contextlib':contextlib})
            output=root/'cime.part.ts'
            command=cime['buildCimeFfmpegCommand'](base+'/reconnect/master.m3u8',str(output))
            assert command[command.index('-extension_picky')+1]=='0'
            assert command[command.index('-reconnect_max_retries')+1]=='10'
            run(command)
            value=probe(output);assert float(value['format']['duration'])>=3.8
            assert hits.count('/reconnect/master.m3u8')>=2 and faults==['503']
            final=root/'cime-final.mp4'
            assert asyncio.run(cime['remuxCimeTempToMp4'](str(output),str(final)))
            probe(final);assert not output.exists()
            ROWS.append({'check':'actual_cime_hls_command_and_503_reconnect_remux',
                         'duration':float(value['format']['duration']),'manifest_attempts':hits.count('/reconnect/master.m3u8'),
                         'extension_picky':0,'synthetic_auth_only':True})
        attempt('actual_cime_hls_command_and_reconnect',check_cime)

        live=load('module/live_recorder.py',{'probeVcodec','probeVideoSize','pickHwaccel','copySpecificFile','buildPipeCmds'},
            {'os':os,'re':re,'shlex':shlex,'subprocess':subprocess,'asyncio':asyncio,
             'getFFmpeg':lambda:FFMPEG,'getFFprobe':lambda:FFPROBE,'getStreamlink':lambda:STREAMLINK,
             'loadConfig':lambda:{'plugin_type':'basic'},
             'buildCommonPluginArgs':lambda *a,**kw:[],
             'buildChannelUrl':lambda channel:'https://chzzk.naver.com/live/'+'0'*32})
        # For buildPipeCmds use the unchanged application helpers, not fixture replacements.
        load('module/live_recorder.py',{'buildCommonPluginArgs','buildChannelUrl','buildChzzkCookieHeader','getChzzkCookieData'},live)
        live['base_directory']=str(SOURCE)
        def check_postprocess():
            for label,stream_copy,resolution in [('copy',True,'source'),('cpu',False,'source'),('cpu-scaled',False,'480p')]:
                output=root/('post-'+label+'.mp4')
                input_path = root/'post-copy.mp4' if label=='cpu-scaled' else source
                asyncio.run(live['copySpecificFile'](str(input_path),str(output),stream_copy,'ultrafast',False,
                    '500k','libx264',26,'aac','96k',postprocess_resolution=resolution,
                    extra_ffmpeg_options='-threads 1',ffmpeg_console_mode='inherit',job_id='synthetic-'+label))
                value=probe(output);assert float(value['format']['duration'])>=3.8
                videos=[s for s in value['streams'] if s['codec_type']=='video']
                assert videos[0]['codec_name']=='h264'
                assert any(s['codec_type']=='audio' and s['codec_name']=='aac' for s in value['streams'])
                if label=='cpu-scaled':assert videos[0]['height']==480
                ROWS.append({'check':'actual_postprocess_'+label,'duration':float(value['format']['duration']),
                             'video':videos[0]['codec_name'],'height':videos[0]['height'],'audio':'aac'})
        attempt('actual_cpu_postprocess_and_copy',check_postprocess)
        def check_segments():
            pattern=str(root/'segments/video_%03d.ts')
            sl_cmd,ff_cmd=live['buildPipeCmds']({'id':'0'*32,'quality':'best'}, {}, {'resolved_quality':'best'},
                                             segmentPattern=pattern,segmentSec=1)
            index=next(i for i,v in enumerate(sl_cmd) if v.startswith('https://chzzk.naver.com/live/'))
            sl_cmd[index]='hls://'+base+'/hls/master.m3u8'
            with (root/'streamlink.stderr').open('wb') as err:
                producer=subprocess.Popen(sl_cmd,stdout=subprocess.PIPE,stderr=err)
                consumer=run(ff_cmd,stdin=producer.stdout)
                producer.stdout.close();rc=producer.wait(timeout=30)
            if rc:
                print((root/'streamlink.stderr').read_text());raise AssertionError('segmented Streamlink producer failed')
            segments=sorted((root/'segments').glob('*.ts'))
            values=[probe(p) for p in segments]
            duration=sum(float(v['format']['duration']) for v in values)
            assert len(segments)>=3 and duration>=3.8
            ROWS.append({'check':'actual_chzzk_buildPipeCmds_regular_segmented','segments':len(segments),'total_duration':duration})
        attempt('actual_chzzk_regular_segmented',check_segments)
    finally:server.shutdown();server.server_close();thread.join(timeout=3)

    def check_tls():
        # Explicit TLS verification exercises the compiled native TLS backend. The
        # application's existing command has no explicit tls_verify option; report
        # that policy separately from toolchain certificate validation.
        run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','1',
             '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost',
             '-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem')])
        tls=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(root)))
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(root/'cert.pem'),str(root/'key.pem'))
        tls.socket=context.wrap_socket(tls.socket,server_side=True)
        thread=threading.Thread(target=tls.serve_forever,daemon=True);thread.start()
        try:
            good=f'https://localhost:{tls.server_port}/source.ts'
            output=root/'tls.mp4'
            run([FFMPEG,'-v','error','-tls_verify','1','-ca_file',str(root/'cert.pem'),'-i',good,'-c','copy',str(output)])
            probe(output)
            for label,url,ca in [('unknown_ca',good,[]),('hostname',f'https://127.0.0.1:{tls.server_port}/source.ts',['-ca_file',str(root/'cert.pem')])]:
                result=run([FFMPEG,'-v','error','-tls_verify','1',*ca,'-i',url,'-f','null','-'],expect_success=False)
                assert result.returncode!=0,'FFmpeg accepted invalid TLS '+label
            from docker_support.replay import hls_transport
            from docker_support.replay.engine import probe_file
            real_rewrite=hls_transport.rewrite_media_playlist
            manifest_url='https://vod.pstatic.net/hls/master.m3u8?_lsu_sa_=SYNTHETIC%2BQUERY'
            # Keep actual query rewrite and native CLI; route only the resulting
            # fixture URLs to localhost instead of contacting a real provider.
            def local_route(content,url):
                return real_rewrite(content,url).replace(b'https://vod.pstatic.net/',
                    f'https://localhost:{tls.server_port}/'.encode())
            replay=root/'replay-signed.mp4'
            cookie_start=len(received_cookies)
            registered=[]
            version=run([FFMPEG,'-version']).stdout.decode(errors='replace')
            strict_default=int(re.search(r'ffmpeg version (\d+)',version).group(1))>=9
            default_args=[FFMPEG,'-v','error','-i',good,'-f','null','-']
            if strict_default:
                rejected=run(default_args,expect_success=False)
                assert rejected.returncode!=0 and b'certificate' in rejected.stderr.lower()
            # Native GnuTLS nested HLS loads use the system CA bundle. ca_file
            # authenticates the outer manifest only; SSL_CERT_FILE is ignored by
            # this backend. Add only our one-day fixture CA inside this disposable
            # build/test container and restore the exact original bytes afterward.
            bundle=Path('/etc/ssl/certs/ca-certificates.crt')
            bundle_before=bundle.read_bytes()
            try:
                bundle.write_bytes(bundle_before+b'\n'+(root/'cert.pem').read_bytes())
                with patch.object(hls_transport,'_fetch_manifest',return_value=(root/'hls/master.m3u8').read_bytes()), \
                     patch.object(hls_transport,'rewrite_media_playlist',side_effect=local_route):
                    assert hls_transport.run_signed_hls(manifest_url,str(replay),None,FFMPEG,
                        {'Cookie':'SYNTHETIC=fixture'},subprocess,
                        lambda processes:registered.extend(processes),lambda:registered.clear())
                opaque_manifest=re.sub(rb'(?m)^([^#\r\n]+)\.ts$',rb'opaque/\1',(root/'hls/master.m3u8').read_bytes())
                assert opaque_manifest!=(root/'hls/master.m3u8').read_bytes()
                opaque_replay=root/'replay-signed-opaque.mp4'
                with patch.object(hls_transport,'_fetch_manifest',return_value=opaque_manifest), \
                     patch.object(hls_transport,'rewrite_media_playlist',side_effect=local_route):
                    assert hls_transport.run_signed_hls(manifest_url,str(opaque_replay),None,FFMPEG,
                        {'Cookie':'SYNTHETIC=fixture'},subprocess,
                        lambda processes:registered.extend(processes),lambda:registered.clear())
                assert not registered
                opaque_probe=probe_file(opaque_replay);assert opaque_probe['duration_seconds']>=3.8
                assert any('/hls/opaque/' in path and '_lsu_sa_=SYNTHETIC%2BQUERY' in path for path in hits)
                ROWS.append({'check':'actual_replay_signed_hls_opaque_native_pipe_and_probe',
                             **opaque_probe,'unchanged_protocol_whitelist':True,'default_tls_verification':True})
                if strict_default:
                    bad_hostname=run([FFMPEG,'-v','error','-i',f'https://127.0.0.1:{tls.server_port}/source.ts','-f','null','-'],expect_success=False)
                    assert bad_hostname.returncode!=0 and b'hostname' in bad_hostname.stderr.lower()
            finally:
                bundle.write_bytes(bundle_before)
            assert bundle.read_bytes()==bundle_before
            if strict_default:
                rejected=run(default_args,expect_success=False)
                assert rejected.returncode!=0 and b'certificate' in rejected.stderr.lower()
            assert not registered
            replay_probe=probe_file(replay);assert replay_probe['duration_seconds']>=3.8
            assert not any(received_cookies[cookie_start:]),'Cookie must not reach native replay segment requests'
            assert any('_lsu_sa_=SYNTHETIC%2BQUERY' in path for path in hits)
            ROWS.append({'check':'actual_replay_signed_hls_native_pipe_and_probe',
                         **replay_probe,'cookie_not_forwarded_to_native_segments':True,
                         'synthetic_origin_remapped_to_localhost':True,'fixture_ca_bundle_restored':True,
                         'fixture_ca_bundle_sha256':hashlib.sha256(bundle_before).hexdigest()})
            protocol_help=run([FFMPEG,'-hide_banner','-h','protocol=tls']).stdout.decode(errors='replace')
            version=run([FFMPEG,'-version']).stdout.decode(errors='replace')
            ROWS.append({'check':'native_ffmpeg_explicit_tls_verify','valid_ca':True,'unknown_ca_rejected':True,
                         'hostname_rejected':True,'version':version.splitlines()[0],'gnutls_enabled':'--enable-gnutls' in version,
                         'openssl_enabled':'--enable-openssl' in version,'tls_verify_option_present':'tls_verify' in protocol_help,
                         'default_verification_strict':strict_default})
            print('NATIVE_TLS_POLICY='+json.dumps({'explicit_validation_passed':True,'default_verification_strict':strict_default,'fixture_ca_bundle_restored':True}))
        finally:tls.shutdown();tls.server_close();thread.join(timeout=3)
    attempt('native_ffmpeg_explicit_tls_verify',check_tls)
print('FFMPEG_APPLICATION_FIXTURE_RESULT='+json.dumps(ROWS))
print('FFMPEG_APPLICATION_FIXTURE_FAILURES='+json.dumps(FAILURES))
raise SystemExit(bool(FAILURES))
