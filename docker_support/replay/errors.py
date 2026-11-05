"""Fixed public failure codes. Native diagnostics never leave process memory."""
import errno
import re
import ssl
import subprocess

ERROR_MESSAGES = {
    'http_400': '영상 서버가 요청을 거부했습니다. 영상을 다시 분석한 뒤 재시도해 주세요.',
    'http_401': '영상 서버 인증에 실패했습니다. 로그인·쿠키 상태를 확인해 주세요.',
    'http_403': '영상 서버에서 접근을 거부했습니다. 영상 공개 범위와 인증 상태를 확인해 주세요.',
    'network_timeout': '영상 서버 연결 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.',
    'tls_error': '영상 서버의 보안 연결을 확인하지 못했습니다.',
    'protocol_denied': '지원하지 않는 전송 주소 또는 프로토콜입니다.',
    'tool_missing': '다운로드에 필요한 실행 도구를 찾지 못했습니다.',
    'storage_unavailable': '저장 공간 또는 저장 경로에 문제가 있습니다.',
    'insufficient_space': '다운로드를 시작하기에 저장 공간이 부족합니다.',
    'invalid_media': '다운로드한 파일에서 유효한 영상을 확인하지 못했습니다.',
    'quality_changed': '선택한 품질이 변경되었습니다. 영상을 다시 분석해 주세요.',
    'engine_operation_failed': '영상 처리에 실패했습니다. 다시 시도해 주세요.',
}


def safe_code(code):
    return code if isinstance(code, str) and code in ERROR_MESSAGES else 'engine_operation_failed'


class ReplayFailure(ValueError):
    def __init__(self, code):
        self.code = safe_code(code)
        super().__init__(self.code)


def classify_diagnostic(value):
    """Inspect transient text, returning only a code; never retain the text."""
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    text = str(value or '').lower()
    for status in (401, 403, 400):
        if re.search(r'\b(?:http(?:/\d(?:\.\d)?)?(?:\s+error)?|server returned|status(?:\s+code)?|error)\s*[:=]?\s*' + str(status) + r'\b', text):
            return 'http_' + str(status)
    if any(marker in text for marker in ('connection timed out', 'operation timed out', 'read timed out', 'connection timeout')):
        return 'network_timeout'
    if any(marker in text for marker in ('certificate verify failed', 'certificate verification failed', 'tls handshake', 'ssl handshake')):
        return 'tls_error'
    if any(marker in text for marker in ('not on whitelist', 'protocol not found', 'protocol not allowed')):
        return 'protocol_denied'
    if any(marker in text for marker in ('no space left on device', 'read-only file system', 'disk quota exceeded')):
        return 'storage_unavailable'
    if 'invalid data found when processing input' in text:
        return 'invalid_media'
    return 'engine_operation_failed'


def record_diagnostic(diagnostics, value):
    code = classify_diagnostic(value)
    if code != 'engine_operation_failed':
        diagnostics['code'] = code


def classify_exception(exc, diagnostics=None):
    if isinstance(exc, ReplayFailure):
        return exc.code
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    if type(status) is int and status in (400, 401, 403):
        return 'http_' + str(status)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or type(exc).__name__ in ('Timeout', 'ConnectTimeout', 'ReadTimeout'):
        return 'network_timeout'
    if isinstance(exc, ssl.SSLError) or type(exc).__name__ == 'SSLError':
        return 'tls_error'
    if isinstance(exc, FileNotFoundError):
        return 'tool_missing'
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES):
        return 'storage_unavailable'
    if isinstance(exc, ValueError):
        code = {
            'quality_changed': 'quality_changed', 'invalid_media': 'invalid_media',
            'unsupported_media_container': 'invalid_media', 'single_valid_media_required': 'invalid_media',
            'ffprobe_missing': 'tool_missing', 'engine_missing': 'tool_missing',
            'unsafe_provider_url': 'protocol_denied', 'unsafe_provider_host': 'protocol_denied',
            'unsafe_command': 'protocol_denied', 'unexpected_tool': 'protocol_denied',
            'dedicated_empty_work_required': 'storage_unavailable', 'output_conflict': 'storage_unavailable',
        }.get(str(exc))
        if code:
            return code
    code = safe_code((diagnostics or {}).get('code'))
    if code != 'engine_operation_failed':
        return code
    # Exceptions can include signed URLs/headers; classify in memory only.
    return classify_diagnostic(str(exc))
