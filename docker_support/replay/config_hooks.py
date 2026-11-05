"""Checked source transforms extending the existing config save owner."""
import ast


def _replace_function(text,name,transform):
    node=next(n for n in ast.parse(text).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    lines=text.splitlines(keepends=True)
    source=''.join(lines[node.lineno-1:node.end_lineno])
    result=transform(source)
    lines[node.lineno-1:node.end_lineno]=[result]
    return ''.join(lines)


def transform_web(text):
    def form(source):
        marker='    try:\n'
        assert marker in source
        source=source.replace(marker,'    replay_tx = None\n'+marker,1)
        marker='        posted_form = await request.form()\n'
        assert marker in source
        source=source.replace(marker,marker+'''        replay_controller = getattr(request.app.state, 'replay_settings', None)
        if replay_controller is not None:
            try:
                replay_tx = await replay_controller.begin(request, posted_form, current_config)
            except Exception as replay_error:
                raise HTTPException(getattr(replay_error, 'status', 500), getattr(replay_error, 'detail', '다시보기 설정 검증에 실패했습니다.'))
''',1)
        marker='        saveConfig(new_config)\n'
        assert marker in source
        source=source.replace(marker,'''        if replay_tx is not None:
            new_config.update(replay_tx.values)
'''+marker,1)
        marker='        request.app.state.config = new_config\n'
        assert marker in source
        source=source.replace(marker,marker+'''        if replay_tx is not None:
            try:
                await replay_tx.commit(new_config)
            except Exception as replay_error:
                raise HTTPException(getattr(replay_error, 'status', 500), getattr(replay_error, 'detail', '다시보기 설정 적용에 실패했습니다.'))
''',1)
        marker='    except Exception as e:\n        print(f"[ERROR] 설정 저장 중 오류 발생: {e}")'
        assert marker in source
        source=source.replace(marker,'    except HTTPException:\n        raise\n\n'+marker,1)
        return source.rstrip()+'''
    finally:
        if replay_tx is not None:
            await replay_tx.finish()
'''
    text=_replace_function(text,'updateConfig',form)
    def api(source):
        header=source.splitlines(keepends=True)[0]
        return header+'''    replay_tx = None
    try:
        current = loadConfig() or {}
        controller = getattr(request.app.state, 'replay_settings', None)
        if controller is not None:
            replay_tx = await controller.begin(request, body, current)
        values = {**body, **(replay_tx.values if replay_tx is not None else {})}
        merged = saveConfig(values)
        request.app.state.config = merged
        if replay_tx is not None:
            await replay_tx.commit(merged)
        return {"status": "ok", "config": merged}
    except HTTPException:
        raise
    except Exception as error:
        if hasattr(error, 'status') and hasattr(error, 'detail'):
            raise HTTPException(error.status, error.detail)
        raise
    finally:
        if replay_tx is not None:
            await replay_tx.finish()
'''
    return _replace_function(text,'save_config_api',api)


def transform_data_manager(text):
    marker='def loadConfig():\n'
    assert text.count(marker)==1
    initialization = """def loadConfig():
    import os, math
    try:
        replay_free_default = float(os.environ.get('RECORDWEB_REPLAY_MIN_FREE_BYTES', 5 * 1024 ** 3)) / 1024 ** 3
        replay_free_default = max(.001, replay_free_default) if math.isfinite(replay_free_default) else 5
    except (ValueError, TypeError):
        replay_free_default = 5
"""
    text=text.replace(marker, initialization, 1)
    def defaults(source):
        marker='    defaults = {\n'
        assert source.count(marker)==1
        return source.replace(marker,marker+'''        "replayEnabled": os.environ.get('RECORDWEB_REPLAY_ENABLED', '1') == '1',
        "replayDownloadRoot": os.environ.get('RECORDWEB_REPLAY_DOWNLOAD_ROOT', '/recordings/replay'),
        "replayMinFreeGiB": replay_free_default,
''',1)
    return _replace_function(text,'loadConfig',defaults)
