"""Persistent owner-isolated queues with one analysis and one download slot."""
import asyncio
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import time
import uuid

from .engine import MAX_ITEMS, GENERIC_ERROR, clean_text, parse_url, validate_options, media_extension
from .store import Store, canonical, directory, safe_path
from .errors import ERROR_MESSAGES, ReplayFailure, safe_code, classify_exception

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
SAFE_STAGES = {'analyzing', 'downloading', 'validating', 'finalizing'}
FAILURE = '작업을 완료하지 못했습니다. 주소·인증·도구·저장 공간을 확인해 주세요.'


def now():
    return datetime.now(timezone.utc).isoformat()


class ReplayError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ReplayManager:
    def __init__(self, state_dir, download_root, app_dir, min_free_bytes=5 * 1024 ** 3,
                 worker_command=None, analysis_timeout=120, job_timeout=86400, download_base=None):
        self.root = safe_path(download_root)
        self.download_base = safe_path(download_base) if download_base is not None else self.root.parent
        if not self.root.is_relative_to(self.download_base):
            raise ValueError('outside_download_base')
        self.root = directory(self.root)
        self.work_root = directory(self.root / '.work')
        self.completed_root = directory(self.root / 'completed')
        self.app_dir = Path(app_dir).absolute()
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.analysis_timeout = max(.05, float(analysis_timeout))
        self.job_timeout = max(.05, float(job_timeout))
        self.command = list(worker_command) if worker_command is not None else [sys.executable, str(Path(__file__).with_name('worker.py').absolute())]
        if not self.command or any(not isinstance(x, str) for x in self.command):
            raise ValueError('invalid_worker_command')
        self.store = Store(state_dir)
        self.db = self.store.db
        self.wake = {'analysis': asyncio.Event(), 'job': asyncio.Event(), 'retention': asyncio.Event()}
        self.tasks = []
        self.processes = {}
        self.done = {}
        self.cancel_requested = set()
        self.close_task = None
        self.stopping = False
        self.closed = False
        with self.db:
            # Only legacy completed jobs need a path snapshot. Existing snapshots
            # remain unchanged when a later manager uses a different root.
            for row in self.db.execute("SELECT owner,payload FROM records WHERE kind='job' AND status='completed'").fetchall():
                data = json.loads(row['payload'])
                if '_download_root' not in data:
                    data['_download_root'] = str(self.root)
                    self.store.save('job', row['owner'], data)
            for row in self.db.execute("SELECT kind,owner,payload FROM records WHERE status NOT IN ('completed','failed','cancelled','interrupted')").fetchall():
                data = json.loads(row['payload'])
                data.update(status='interrupted', updated_at=now(), error='서버가 중단되었습니다. 다시 시도해 주세요.')
                if row['kind'] == 'job':
                    data.update(stage='interrupted', can_download=False, speed_bps=0)
                self.store.save(row['kind'], row['owner'], data)
                self._cleanup(self.work_root / data['id'])

    @staticmethod
    def public(value):
        return {key: val for key, val in value.items() if not key.startswith('_')}

    def _get(self, kind, owner, ident):
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
            raise ReplayError(404, '항목을 찾을 수 없습니다.')
        value = self.store.get(kind, owner, ident)
        if value is None:
            raise ReplayError(404, '항목을 찾을 수 없습니다.')
        return value

    def _save(self, kind, owner, value):
        with self.db:
            self.store.save(kind, owner, value)

    def _log(self, ident, message, level='info'):
        # Callers supply fixed messages, never worker/core exception text.
        with self.db:
            self.db.execute('INSERT INTO logs(job,at,level,message) VALUES(?,?,?,?)', (ident, now(), level, message))
            self.db.execute('DELETE FROM logs WHERE job=? AND seq NOT IN (SELECT seq FROM logs WHERE job=? ORDER BY seq DESC LIMIT 200)', (ident, ident))

    def has_active_work(self):
        return self.db.execute("SELECT 1 FROM records WHERE status NOT IN ('completed','failed','cancelled','interrupted') LIMIT 1").fetchone() is not None

    def _available(self):
        if self.stopping or self.closed:
            raise ReplayError(503, '작업 관리자가 종료 중입니다.')

    async def start(self):
        self._available()
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._loop(kind)) for kind in ('analysis', 'job')]
            self.tasks.append(asyncio.create_task(self._retention_loop()))
            for event in self.wake.values():
                event.set()

    def prune_completed(self, current_time=None):
        cutoff = (current_time or datetime.now(timezone.utc)) - timedelta(hours=1)
        with self.db:
            expired = set()
            for row in self.db.execute("SELECT id,payload FROM records WHERE kind='job' AND status='completed'").fetchall():
                job = json.loads(row['payload'])
                try:
                    completed = datetime.fromisoformat(job.get('completed_at') or job['updated_at'])
                    if completed.tzinfo is None:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                if completed <= cutoff and row['id'] not in self.done:
                    expired.add(row['id'])
            if not expired:
                return
            # Retain live siblings of a multi-item idempotent request.
            for row in self.db.execute('SELECT owner,key,ids FROM requests').fetchall():
                ids = json.loads(row['ids'])
                retained = [ident for ident in ids if ident not in expired]
                if retained == ids:
                    continue
                if retained:
                    self.db.execute('UPDATE requests SET ids=? WHERE owner=? AND key=?', (canonical(retained), row['owner'], row['key']))
                else:
                    self.db.execute('DELETE FROM requests WHERE owner=? AND key=?', (row['owner'], row['key']))
            for ident in expired:
                self.db.execute('DELETE FROM logs WHERE job=?', (ident,))
                self.db.execute('DELETE FROM records WHERE id=?', (ident,))

    async def _retention_loop(self):
        while not self.stopping:
            self.prune_completed()
            try:
                await asyncio.wait_for(self.wake['retention'].wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            self.wake['retention'].clear()

    async def analyze(self, owner, urls):
        self._available()
        if not isinstance(urls, list) or not 1 <= len(urls) <= 10:
            raise ReplayError(400, 'URL은 한 번에 1~10개 입력해 주세요.')
        normalized = []
        for url in urls:
            try:
                normalized.append(parse_url(url)['url'])
            except (TypeError, ValueError):
                raise ReplayError(400, '지원하는 HTTPS 영상 또는 YouTube 재생목록 주소를 입력해 주세요.') from None
        if self.db.execute("SELECT count(*) FROM records WHERE kind='analysis' AND status IN ('queued','running')").fetchone()[0] >= 20:
            raise ReplayError(429, '분석 대기열이 가득 찼습니다.')
        if self.db.execute("SELECT count(*) FROM records WHERE kind='analysis' AND owner=?", (owner,)).fetchone()[0] >= 200:
            raise ReplayError(429, '분석 이력 한도에 도달했습니다.')
        data = {'id': uuid.uuid4().hex, 'status': 'queued', 'items': [], '_urls': list(dict.fromkeys(normalized)),
                'created_at': now(), 'updated_at': now(), 'error': None,
                'warnings': ['재생목록을 포함한 분석 결과는 요청당 처음 50개 영상까지입니다.'] if any(parse_url(u).get('playlist') for u in normalized) else []}
        self._save('analysis', owner, data)
        self.wake['analysis'].set()
        return self.public(data)

    def get_analysis(self, owner, ident):
        return self.public(self._get('analysis', owner, ident))

    def _new_job(self, spec):
        return dict(spec, id=uuid.uuid4().hex, created_at=now(), attempt=1, **self._queued_fields())

    def _queued_fields(self):
        return dict(status='queued', stage='queued', progress=None, speed_bps=0, downloaded_bytes=0,
                    total_bytes=None, updated_at=now(), error=None, error_code=None, filename=None,
                    can_download=False, _download_root=str(self.root))

    def _capacity(self, owner, amount, *, new_records=True):
        if self.db.execute("SELECT count(*) FROM records WHERE kind='job' AND owner=? AND status NOT IN ('completed','failed','cancelled','interrupted')", (owner,)).fetchone()[0] + amount > 100:
            raise ReplayError(429, '대기·진행 작업은 최대 100개입니다.')
        if self.db.execute("SELECT count(*) FROM records WHERE kind='job' AND status NOT IN ('completed','failed','cancelled','interrupted')").fetchone()[0] + amount > 100:
            raise ReplayError(429, '전체 작업 대기열이 가득 찼습니다.')
        if new_records and self.db.execute("SELECT count(*) FROM records WHERE kind='job' AND owner=?", (owner,)).fetchone()[0] + amount > 1000:
            raise ReplayError(429, '작업 이력 한도에 도달했습니다.')

    async def create_jobs(self, owner, analysis_id, items, idempotency_key):
        self._available()
        if not isinstance(idempotency_key, str) or not re.fullmatch('[A-Za-z0-9_-]{8,100}', idempotency_key):
            raise ReplayError(400, '올바른 요청 키가 필요합니다.')
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise ReplayError(400, '작업은 한 번에 1~50개 등록해 주세요.')
        normalized = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {'url', 'quality', 'section', 'split', 'speed_limit'}:
                raise ReplayError(400, '작업 옵션이 올바르지 않습니다.')
            try:
                url = parse_url(item.get('url'), allow_playlist=False)['url']
                normalized.append({'url': url, 'quality': item.get('quality'), 'section': item.get('section') or None,
                                   'split': item.get('split', 1), 'speed_limit': item.get('speed_limit') or ''})
            except (ValueError, TypeError):
                raise ReplayError(400, '분석한 영상만 등록할 수 있습니다.') from None
        try:
            body = canonical({'analysis_id': analysis_id, 'items': normalized})
        except (ValueError, TypeError):
            raise ReplayError(400, '작업 옵션이 올바르지 않습니다.') from None
        with self.db:
            previous = self.db.execute('SELECT body,ids FROM requests WHERE owner=? AND key=?', (owner, idempotency_key)).fetchone()
            if previous:
                if previous['body'] != body:
                    raise ReplayError(409, '동일한 요청 키의 내용이 변경되었습니다.')
                return {'jobs': [self.get_job(owner, ident) for ident in json.loads(previous['ids'])]}
            analysis = self._get('analysis', owner, analysis_id)
            if analysis['status'] != 'completed':
                raise ReplayError(409, '분석이 완료된 뒤 등록해 주세요.')
            known = {item['url']: item for item in analysis['items'] if not item.get('error')}
            specs = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get('url'), str):
                    raise ReplayError(400, '분석한 영상만 등록할 수 있습니다.')
                try:
                    url = parse_url(item['url'], allow_playlist=False)['url']
                except ValueError:
                    raise ReplayError(400, '분석한 영상만 등록할 수 있습니다.') from None
                if url not in known:
                    raise ReplayError(400, '분석한 영상만 등록할 수 있습니다.')
                try:
                    specs.append(validate_options(item, known[url]))
                except (ValueError, TypeError):
                    raise ReplayError(400, '품질·구간·분할·속도 옵션을 확인해 주세요.') from None
            self._capacity(owner, len(specs))
            jobs = [self._new_job(spec) for spec in specs]
            for job in jobs:
                self.store.save('job', owner, job)
            self.db.execute('INSERT INTO requests VALUES(?,?,?,?)', (owner, idempotency_key, body, canonical([job['id'] for job in jobs])))
        for job in jobs:
            self._log(job['id'], '작업을 대기열에 등록했습니다.')
        self.wake['job'].set()
        return {'jobs': [self.public(job) for job in jobs]}

    def list_jobs(self, owner):
        rows = self.db.execute("SELECT payload FROM records WHERE kind='job' AND owner=? ORDER BY rowid DESC LIMIT 200", (owner,))
        return {'jobs': [self.public(json.loads(row[0])) for row in rows]}

    def get_job(self, owner, ident):
        job = self._get('job', owner, ident)
        if job['status'] == 'completed':
            try:
                self.resolve_file(owner, ident)
            except ReplayError:
                job['can_download'] = False
        return self.public(job)

    def logs(self, owner, ident, after=0):
        self._get('job', owner, ident)
        if type(after) is not int or after < 0:
            raise ReplayError(400, '로그 커서가 올바르지 않습니다.')
        entries = [dict(row) for row in self.db.execute('SELECT seq,at,level,message FROM logs WHERE job=? AND seq>? ORDER BY seq LIMIT 200', (ident, after))]
        return {'logs': entries, 'next_cursor': entries[-1]['seq'] if entries else after}

    async def cancel(self, owner, ident):
        self._available()
        job = self._get('job', owner, ident)
        if job['status'] in TERMINAL:
            return self.get_job(owner, ident)
        finished = self.done.get(ident)
        active = finished is not None
        if active:
            self.cancel_requested.add(ident)
        job.update(status='cancelling' if active else 'cancelled', stage='cancelling' if active else 'cancelled', updated_at=now())
        self._save('job', owner, job)
        self._log(ident, '취소를 요청했습니다.')
        await self._terminate(self.processes.get(ident))
        if active:
            try:
                await asyncio.wait_for(finished.wait(), 8)
            except asyncio.TimeoutError:
                pass
        return self.get_job(owner, ident)

    async def retry(self, owner, ident):
        self._available()
        previous = self._get('job', owner, ident)
        if previous['status'] not in ('failed', 'cancelled', 'interrupted'):
            raise ReplayError(409, '실패·취소·중단한 작업만 재시도할 수 있습니다.')
        if ident in self.done:
            raise ReplayError(409, '이전 시도를 정리하고 있습니다.')
        with self.db:
            self._capacity(owner, 1, new_records=False)
            job = previous
            job.update(self._queued_fields(), attempt=previous.get('attempt', 1) + 1)
            self.store.save('job', owner, job)
            self._log(ident, f"{job['attempt']}번째 시도를 대기열에 등록했습니다.")
        self.wake['job'].set()
        return {'jobs': [self.public(job)]}

    def resolve_file(self, owner, ident):
        job = self._get('job', owner, ident)
        name = job.get('filename')
        allowed_stems = {ident, f"{ident}-attempt-{job.get('attempt', 1)}"}
        if job['status'] != 'completed' or not job.get('can_download') or not isinstance(name, str) or name not in {stem + ext for stem in allowed_stems for ext in ('.mp4', '.mkv', '.webm')}:
            raise ReplayError(404, '완료 파일을 찾을 수 없습니다.')
        try:
            root_value = job.get('_download_root')
            if not isinstance(root_value, str):
                raise ValueError('missing_download_root')
            root = Path(root_value)
            if not root.is_absolute() or '..' in root.parts:
                raise ValueError('unsafe_download_root')
            root = safe_path(root)
            if not root.is_relative_to(self.download_base):
                raise ValueError('outside_download_base')
            path = safe_path(root / 'completed' / name)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError('invalid_file')
        except (OSError, ValueError):
            raise ReplayError(404, '완료 파일을 찾을 수 없습니다.') from None
        return path

    @staticmethod
    async def _terminate(proc):
        if proc is None:
            return
        # The process group is only the PID captured from our own live spawn.
        # Descendants may outlive the leader; signal its group even after wait().
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 1.5)
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 1.5)
        except asyncio.TimeoutError:
            pass

    async def _spawn(self, ident, command, **kwargs):
        launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, start_new_session=True, **kwargs))
        try:
            proc = await asyncio.shield(launch)
        except asyncio.CancelledError:
            proc = await launch
            await self._terminate(proc)
            raise
        self.processes[ident] = proc
        # A cancel/close can arrive while create_subprocess_exec is suspended.
        # Register first, then stop the newly created process before handing it
        # to either the downloader or the validation probe.
        if self.stopping or ident in self.cancel_requested:
            await self._terminate(proc)
            raise ValueError('cancelled_during_spawn')
        return proc

    def _space(self):
        if shutil.disk_usage(safe_path(self.root)).free < self.min_free_bytes:
            raise ReplayFailure('insufficient_space')

    @staticmethod
    def _bytes(work):
        amount = 0
        with os.scandir(work) as entries:
            for count, item in enumerate(entries):
                if count >= 200 or item.is_symlink():
                    raise ValueError('unsafe_work_contents')
                if item.is_file(follow_symlinks=False):
                    amount += item.stat(follow_symlinks=False).st_size
        return amount

    async def _operation(self, kind, owner, data, work):
        ident = data['id']
        task = {'urls': data['_urls']} if kind == 'analysis' else {key: data[key] for key in ('url', 'platform', 'quality', 'section', 'split', 'speed_limit')}
        proc = await self._spawn(ident, self.command + ['--operation', 'analyze' if kind == 'analysis' else 'download', '--app-dir', str(self.app_dir), '--work-dir', str(work)],
                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536)
        if self.stopping or self._get(kind, owner, ident)['status'] == 'cancelling':
            await self._terminate(proc)
            raise ValueError('cancelled_before_start')
        proc.stdin.write(canonical(task).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        result = None
        worker_error = None

        async def reader():
            nonlocal result, worker_error
            async for raw in proc.stdout:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError('invalid_worker_event')
                name = event.get('event')
                if name == 'result':
                    if result is not None:
                        raise ValueError('duplicate_result')
                    result = event.get('data')
                elif name == 'error':
                    worker_error = safe_code(event.get('code'))
                elif name in ('stage', 'progress') and kind == 'job':
                    job = self._get('job', owner, ident)
                    if job['status'] == 'cancelling' or self.stopping:
                        continue
                    stage = event.get('stage')
                    if stage in SAFE_STAGES:
                        job['stage'] = stage
                        if stage in ('validating', 'finalizing'):
                            job['status'] = 'finalizing'
                    progress = event.get('progress')
                    if progress is None or type(progress) in (int, float) and math.isfinite(progress) and 0 <= progress < 100:
                        job['progress'] = progress
                    job['updated_at'] = now()
                    self._save('job', owner, job)
            return await proc.wait()

        async def monitor():
            last_bytes, last_at = 0, time.monotonic()
            while True:
                await asyncio.sleep(.5)
                if kind == 'job':
                    self._space()
                    amount = self._bytes(work)
                    job = self._get('job', owner, ident)
                    if job['status'] != 'cancelling' and not self.stopping:
                        moment = time.monotonic()
                        job.update(downloaded_bytes=amount, speed_bps=max(0, amount - last_bytes) / max(.001, moment - last_at), updated_at=now())
                        self._save('job', owner, job)
                        last_bytes, last_at = amount, moment

        reading = asyncio.create_task(reader())
        monitoring = asyncio.create_task(monitor())
        try:
            finished, _ = await asyncio.wait((reading, monitoring), timeout=self.analysis_timeout if kind == 'analysis' else self.job_timeout, return_when=asyncio.FIRST_COMPLETED)
            if not finished:
                raise ValueError('worker_timeout')
            for finished_task in finished:
                finished_task.result()
            if worker_error is not None:
                raise ReplayFailure(worker_error)
            if reading not in finished or reading.result() != 0 or not isinstance(result, dict):
                raise ReplayFailure('engine_operation_failed')
            return result
        finally:
            await self._terminate(proc)
            for pending in (reading, monitoring):
                pending.cancel()
            await asyncio.gather(reading, monitoring, return_exceptions=True)

    @staticmethod
    def _analysis_items(result):
        items = result.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise ValueError('invalid_analysis_result')
        safe = []
        for item in items:
            p = parse_url(item['url'], allow_playlist=bool(item.get('error')))
            error = GENERIC_ERROR if item.get('error') else None
            qs = item.get('qualities')
            if not isinstance(qs, list) or len(qs) > 200:
                raise ValueError('invalid_qualities')
            qualities = []
            for q in qs:
                ident = q.get('id')
                if not isinstance(ident, str) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}', ident):
                    raise ValueError('invalid_quality')
                qualities.append({'id': ident, 'label': clean_text(q.get('label'), 200)})
            if not error and not qualities:
                raise ValueError('missing_quality')
            supports = item.get('supports', {})
            duration = item.get('duration_seconds')
            if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
                duration = None
            safe.append({key: p[key] for key in ('url', 'platform', 'video_id')} | {
                'title': clean_text(item.get('title')), 'duration_seconds': duration, 'qualities': qualities,
                'supports': {key: bool(supports.get(key)) for key in ('section', 'split', 'speed')}, 'error': error})
        return safe

    async def _validate_output(self, ident, result, work):
        name = result.get('name')
        if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe_result_name')
        source = safe_path(work / name)
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_nlink != 1:
            raise ValueError('invalid_media_file')
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            raise ReplayFailure('tool_missing')
        proc = await self._spawn(ident, [ffprobe, '-v', 'error', '-show_entries', 'format=duration,format_name:stream=codec_type', '-of', 'json', str(source)],
                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 45)
            if proc.returncode != 0 or len(output) > 65536:
                raise ValueError('ffprobe_failed')
            media = json.loads(output)
            duration = float(media.get('format', {}).get('duration', 0))
            if not math.isfinite(duration) or duration <= 0 or not any(s.get('codec_type') == 'video' for s in media.get('streams', [])):
                raise ValueError('invalid_media')
            containers = media.get('format', {}).get('format_name', '').split(',')
            extension = media_extension(containers, source)
            if extension is None:
                raise ValueError('unsupported_media_container')
            return source, extension
        finally:
            await self._terminate(proc)

    def _cleanup(self, work):
        try:
            if work.parent == self.work_root and re.fullmatch('[0-9a-f]{32}', work.name):
                if work.is_symlink():
                    work.unlink()
                elif work.exists():
                    shutil.rmtree(safe_path(work))
        except OSError:
            pass

    async def _loop(self, kind):
        while not self.stopping:
            self.wake[kind].clear()
            row = self.db.execute("SELECT owner,payload FROM records WHERE kind=? AND status='queued' ORDER BY json_extract(payload,'$.updated_at'), rowid LIMIT 1", (kind,)).fetchone()
            if row is None:
                await self.wake[kind].wait()
                continue
            owner, data = row['owner'], json.loads(row['payload'])
            ident = data['id']
            done = self.done[ident] = asyncio.Event()
            work = self.work_root / ident
            try:
                # Refuse to adopt any stale partial output from an earlier attempt.
                safe_path(work).mkdir(mode=0o700)
                data.update(status='running', updated_at=now())
                if kind == 'job':
                    self._space()
                    data.update(stage='analyzing')
                    self._log(ident, '전용 작업 프로세스를 시작했습니다.')
                self._save(kind, owner, data)
                result = await self._operation(kind, owner, data, work)
                data = self._get(kind, owner, ident)
                if self.stopping or data['status'] == 'cancelling':
                    raise ValueError('cancelled')
                if kind == 'analysis':
                    data.update(status='completed', items=self._analysis_items(result))
                else:
                    data.update(status='finalizing', stage='validating', updated_at=now())
                    self._save(kind, owner, data)
                    source, extension = await self._validate_output(ident, result, work)
                    data = self._get(kind, owner, ident)
                    if self.stopping or data['status'] == 'cancelling':
                        raise ValueError('cancelled')
                    self._space()
                    target = safe_path(self.completed_root / (ident + extension))
                    # Same filesystem hard-link publication is atomic and refuses
                    # overwrite. The source is unlinked only after publication.
                    try:
                        os.link(source, target, follow_symlinks=False)
                    except FileExistsError:
                        # An interrupted attempt may have published before its DB commit.
                        if data.get('attempt', 1) == 1:
                            raise
                        target = safe_path(self.completed_root / f"{ident}-attempt-{data['attempt']}{extension}")
                        os.link(source, target, follow_symlinks=False)
                    source.unlink()
                    data.update(status='completed', stage='completed', completed_at=now(), filename=target.name, can_download=True,
                                progress=100, downloaded_bytes=target.stat().st_size, speed_bps=0)
                    self._log(ident, '검증을 마친 영상 파일을 저장했습니다.')
                data['updated_at'] = now()
                self._save(kind, owner, data)
            except (Exception, asyncio.CancelledError) as failure:
                await self._terminate(self.processes.get(ident))
                data = self._get(kind, owner, ident)
                status = 'interrupted' if self.stopping else 'cancelled' if data['status'] == 'cancelling' else 'failed'
                code = safe_code(classify_exception(failure))
                message = '서버 종료로 작업이 중단되었습니다.' if status == 'interrupted' else ERROR_MESSAGES[code]
                data.update(status=status, updated_at=now(), error=None if status == 'cancelled' else message,
                            error_code=code if status == 'failed' else None)
                if kind == 'job':
                    data.update(stage=status, speed_bps=0, can_download=False)
                    self._log(ident, {'interrupted': '서버 종료로 작업이 중단되었습니다.', 'cancelled': '작업을 취소했습니다.'}.get(status, message), 'info' if status == 'cancelled' else 'error')
                self._save(kind, owner, data)
            finally:
                self.processes.pop(ident, None)
                self._cleanup(work)
                done.set()
                self.done.pop(ident, None)
                self.cancel_requested.discard(ident)

    async def close(self):
        if self.close_task is None:
            self.stopping = True
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        if self.closed:
            return
        self.stopping = True
        for event in self.wake.values():
            event.set()
        await asyncio.gather(*(self._terminate(proc) for proc in list(self.processes.values())), return_exceptions=True)
        if self.tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self.tasks), 10)
            except asyncio.TimeoutError:
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
        with self.db:
            for row in self.db.execute("SELECT kind,owner,payload FROM records WHERE status NOT IN ('completed','failed','cancelled','interrupted')").fetchall():
                data = json.loads(row['payload'])
                data.update(status='interrupted', updated_at=now(), error='서버 종료로 작업이 중단되었습니다.')
                if row['kind'] == 'job':
                    data.update(stage='interrupted', speed_bps=0, can_download=False)
                self.store.save(row['kind'], row['owner'], data)
        self.store.close()
        self.closed = True
