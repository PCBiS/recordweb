"""Insert the replay settings include into the existing configuration form."""

MARKER = "            <!-- 알림 폼 -->"
INCLUDE = "            {% include 'partials/replay_settings.html' %}\n\n"


def transform_style(text: str) -> str:
    """Use the shared light/dark palette for the account management panel."""
    start = text.index('.account-management {')
    end = text.index('}', start) + 1
    block = text[start:end]
    if block.count('background-color: #fff;') != 1 or block.count('border: 1px solid #ddd;') != 1:
        raise ValueError('unknown_account_management_style')
    updated = block.replace('background-color: #fff;', 'background-color: var(--surface);')
    updated = updated.replace('border: 1px solid #ddd;', 'border: 1px solid var(--border);')
    return text[:start] + updated + text[end:]


def transform(text: str) -> str:
    """Return a deterministic patch, rejecting unexpected or ambiguous templates."""
    if INCLUDE in text:
        raise ValueError("Replay settings include already present")
    if text.count(MARKER) != 1:
        raise ValueError("Expected exactly one notification section marker")
    form_start = text.find('<form id="configForm"')
    form_end = text.find('</form>', form_start)
    marker_at = text.index(MARKER)
    if form_start < 0 or form_end < 0 or not form_start < marker_at < form_end:
        raise ValueError("Notification marker is not inside configForm")
    return text.replace(MARKER, INCLUDE + MARKER, 1)


def transform_script(text):
    old = """        const text = await response.text();
        console.error('POST /config failed:', response.status, text);
        alert('설정 저장 중 오류가 발생했습니다.');"""
    new = """        let detail = '설정 저장 중 오류가 발생했습니다.';
        if ((response.headers.get('content-type') || '').includes('application/json')) {
          const body = await response.json();
          if (typeof body.detail === 'string') detail = body.detail;
          else if (typeof body.message === 'string') detail = body.message;
        }
        console.error('POST /config failed:', response.status);
        alert(detail);"""
    if text.count(old) != 1:
        raise ValueError('unknown_config_error_handler')
    return text.replace(old, new, 1)


def transform_file_move_errors(text: str) -> str:
    old = '    try { await onChoose(movePickerPath); } finally { close(); }'
    new = '''    btnOk.disabled = true;
    try {
      await onChoose(movePickerPath);
      close();
    } catch (e) {
      alert(formatFileApiError(e, "이동", movePickerPath));
    } finally {
      btnOk.disabled = false;
    }'''
    if text.count(old) != 1:
        raise ValueError('unknown_move_picker_handler')
    text = text.replace(old, new, 1)
    marker = '  const securityHit ='
    if text.count(marker) != 1:
        raise ValueError('unknown_file_error_formatter')
    return text.replace(marker, '''  if (/read-only mode/i.test(t)) {
    return "파일관리가 읽기전용 모드입니다. 설정관리에서 읽기전용 모드를 OFF로 변경하고 저장해 주세요.";
  }

'''+marker, 1)
