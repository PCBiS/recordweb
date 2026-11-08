"""Checked CIME execution transform; keep preserved upstream inputs intact."""
import ast

OLD = '''    async def _request_with_ssl(ssl_value):
        async with aiohttp.ClientSession(timeout=client_timeout, headers=req_headers) as session:
            async with session.get(url, allow_redirects=True, ssl=ssl_value) as resp:
                return resp.status, str(resp.url), await resp.text(errors="replace")

    try:
        return await _request_with_ssl(ssl_context if ssl_context is not None else True)

    except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError, ssl.SSLError) as e:
        print(
            f"[CIME][SSL][WARN] 인증서 검증 실패. 해당 요청에 한해 ssl=False로 재시도: {url} / {e}",
            flush=True,
        )
        return await _request_with_ssl(False)'''
NEW = '''    async with aiohttp.ClientSession(timeout=client_timeout, headers=req_headers) as session:
        async with session.get(url, allow_redirects=True,
                               ssl=ssl_context if ssl_context is not None else True) as resp:
            return resp.status, str(resp.url), await resp.text(errors="replace")'''


def transform(text):
    nodes = [node for node in ast.parse(text).body
             if isinstance(node, ast.AsyncFunctionDef) and node.name == '_fetchText']
    if len(nodes) != 1:
        raise ValueError('unknown_cime_fetch_layout')
    node = nodes[0]
    lines = text.splitlines(keepends=True)
    function = ''.join(lines[node.lineno - 1:node.end_lineno])
    if function.count(OLD) != 1:
        raise ValueError('unknown_cime_tls_retry_layout')
    lines[node.lineno - 1:node.end_lineno] = [function.replace(OLD, NEW, 1)]
    return ''.join(lines)
