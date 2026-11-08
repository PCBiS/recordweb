"""Checked Docker execution transforms; preserve upstream and public app inputs."""
import ast


def _replace_function(text, name, before, after):
    nodes = [node for node in ast.parse(text).body
             if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(nodes) != 1:
        raise ValueError('unknown_youtube_function_layout:' + name)
    node = nodes[0]
    lines = text.splitlines(keepends=True)
    function = ''.join(lines[node.lineno - 1:node.end_lineno])
    if function.count(before) != 1:
        raise ValueError('unknown_youtube_patch_layout:' + name)
    lines[node.lineno - 1:node.end_lineno] = [function.replace(before, after, 1)]
    return ''.join(lines)


def transform_data_manager(text):
    return _replace_function(text, 'yloadCookies',
        '    # 파일이 존재하면, 경로만 반환\n    return yCOOKIE_PATH',
        '    # Empty existing jars mean anonymous access; leave their bytes intact.\n'
        '    if os.path.getsize(yCOOKIE_PATH) == 0:\n'
        '        return None\n\n'
        '    # Nonempty jars retain the existing yt-dlp validation/error behavior.\n'
        '    return yCOOKIE_PATH')


def transform_hls_mux_output(text):
    return _replace_function(text, 'downloadYoutube',
        "        '--merge-output-format', 'mkv',\n"
        "        '--ffmpeg-location', os.path.dirname(ffmpeg_path),",
        "        '--merge-output-format', 'mkv',\n"
        "        '--no-hls-use-mpegts',\n"
        "        '--ffmpeg-location', os.path.dirname(ffmpeg_path),")


def transform_replay(text):
    text = _replace_function(text, 'downloadYoutube',
        "        '-f', f'{format_id}+bestaudio',",
        "        '-f', f'{format_id}[acodec!=none]/{format_id}+bestaudio',")
    return transform_hls_mux_output(text)
