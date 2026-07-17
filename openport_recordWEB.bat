@echo off
SETLOCAL

:: 관리자 권한 확인
openfiles >nul 2>&1
if '%errorlevel%' NEQ '0' (
    echo 관리자 권한이 필요합니다.
    echo 이 스크립트를 관리자 권한으로 실행해주세요.
    pause
    exit /b
)

:: 사용자로부터 새로운 포트 번호를 입력받음
set /p port="사용자가 원하는 포트번호를 입력해주세요(49152-65535번 범위 권장): "

:: 포트 번호가 입력되지 않았는지 확인
if not defined port (
    echo 포트 번호가 입력되지 않았습니다.
    pause
    exit /b
)

:: 포트 번호가 숫자인지 확인 (set /a 사용)
set /a testPort=%port% >nul 2>&1
if %errorlevel% neq 0 (
    echo 포트 번호는 숫자여야 합니다.
    pause
    exit /b
)

:: config.json의 경로를 배치파일이 있는 위치를 기준으로 설정
set "config_path=%~dp0json\config.json"

:: config.json 파일이 존재하는지 확인
if not exist "%config_path%" (
    echo config.json 파일을 찾을 수 없습니다: %config_path%
    pause
    exit /b
)

:: config.json 파일 백업
copy "%config_path%" "%config_path%.bak" >nul 2>&1
if %errorlevel% neq 0 (
    echo config.json 파일의 백업 생성에 실패했습니다.
    pause
    exit /b
)

:: PowerShell 명령을 한 줄로 처리
powershell -Command "try { $configPath = '%config_path%'; $config = Get-Content $configPath -Raw | ConvertFrom-Json; if (-not $config.PSObject.Properties['port']) { $config | Add-Member -NotePropertyName 'port' -NotePropertyValue %port% -Force; } else { $config.port = %port%; }; $config | ConvertTo-Json -Depth 10 | Set-Content $configPath; } catch { Write-Host '오류 발생: ' $_.Exception.Message; exit 1 }"

if %errorlevel% neq 0 (
    echo config.json 파일을 업데이트하는 동안 오류가 발생했습니다.
    pause
    exit /b
)

echo 포트 번호 %port%가 config.json에 성공적으로 저장되었습니다.

:: 방화벽 규칙이 이미 존재하는지 확인
netsh advfirewall firewall show rule name="Allow recordWEB port" >nul 2>&1
if '%errorlevel%'=='0' (
    echo "Allow recordWEB port" 규칙이 이미 존재합니다. 기존 규칙을 수정합니다...
    
    :: 기존 규칙 삭제
    netsh advfirewall firewall delete rule name="Allow recordWEB port"
    echo 기존 규칙이 삭제되었습니다.
)

:: 새로운 방화벽 규칙 추가
netsh advfirewall firewall add rule name="Allow recordWEB port" dir=in action=allow protocol=TCP localport=%port%

if %errorlevel% neq 0 (
    echo 포트 %port%에 대한 방화벽 규칙 추가에 실패했습니다.
    pause
    exit /b
)

echo 포트 %port%에 대한 인바운드 규칙이 성공적으로 추가되었습니다.
pause
ENDLOCAL
