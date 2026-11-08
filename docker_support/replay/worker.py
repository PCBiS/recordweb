"""Private one-operation subprocess. Its only public output is safe JSON events."""
import os
import stat
import sys


def readonly_cookie_path(path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISREG(info.st_mode) and info.st_size > 0:
        return path
    return None


def main():
    # Preserve a dedicated, non-inherited emitter before importing the engine or
    # any upstream modules. Even native child output goes to /dev/null.
    output = os.fdopen(os.dup(1), 'w', encoding='utf-8', buffering=1)
    sink = os.open(os.devnull, os.O_WRONLY)
    os.dup2(sink, 1)
    os.dup2(sink, 2)
    os.close(sink)
    import argparse
    import importlib
    import json
    from pathlib import Path
    import threading

    lock = threading.Lock()
    diagnostics = {}
    # Safe fallback also covers failures before the engine import succeeds.
    classify_error = lambda exc, context: 'engine_operation_failed'

    def emit(event):
        with lock:
            output.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
            output.flush()

    try:
        parser = argparse.ArgumentParser()
        parser.add_argument('--operation', choices=('analyze', 'download'), required=True)
        parser.add_argument('--app-dir', required=True)
        parser.add_argument('--work-dir', required=True)
        args = parser.parse_args()
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('spec_too_large')
        task = json.loads(raw)
        app_dir = Path(args.app_dir).resolve(strict=True)
        if not (app_dir / 'module' / 'replay_download.py').is_file():
            raise ValueError('engine_missing')
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from docker_support.replay.engine import analyze_urls, guarded_core, parse_url, run_download
        from docker_support.replay.errors import classify_exception
        classify_error = classify_exception
        urls = task.get('urls') if args.operation == 'analyze' else [task.get('url')]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 50:
            raise ValueError('invalid_urls')
        parsed = [parse_url(url, allow_playlist=args.operation == 'analyze') for url in urls]
        sys.path.insert(0, str(app_dir))
        core = importlib.import_module('module.replay_download')
        dm = core.data_manager
        config_dir = Path(os.environ.get('RECORDWEB_REPLAY_CONFIG_DIR', str(app_dir / 'json')))
        # Retain the original schema/normalization loaders. Their shared helper
        # reads originals or backups without performing upstream recovery writes.
        for attr in ('CONFIG_PATH', 'COOKIE_PATH', 'yCOOKIE_PATH', 'LOGIN_PATH'):
            if hasattr(dm, attr):
                setattr(dm, attr, str(config_dir / Path(getattr(dm, attr)).name))

        def readonly_json(path, default):
            for candidate in (Path(path), Path(str(path) + '.bak')):
                try:
                    if candidate.is_symlink() or candidate.stat().st_size > 8 * 1024 * 1024:
                        continue
                    return json.loads(candidate.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
            return default

        dm.readJsonSafe = readonly_json
        dm.yloadCookies = lambda: readonly_cookie_path(dm.yCOOKIE_PATH)
        emit({'event': 'stage', 'stage': 'analyzing'})
        platform = parsed[0]['platform'] if args.operation == 'download' else None
        with guarded_core(core, platform, emit, diagnostics=diagnostics):
            result = {'items': analyze_urls(core, [p['url'] for p in parsed])} if args.operation == 'analyze' else run_download(core, task, args.work_dir, emit)
        emit({'event': 'result', 'data': result})
        return 0
    except (Exception, SystemExit) as exc:
        emit({'event': 'error', 'code': classify_error(exc, diagnostics)})
        return 1
    finally:
        output.close()


if __name__ == '__main__':
    raise SystemExit(main())
