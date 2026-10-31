# Docker 실행 지원·로그·진단

이 버전은 개발자 원본을 `upstream/`에 그대로 보존하고 `patches/public.json`을 검증해 이미지 안의 `/app` 실행 사본을 만듭니다. 원본 파일 해시, 패치 입력/결과 해시가 맞지 않으면 빌드를 중단합니다. `Dockerfile.runtime`이 기존 의존성·CA 이미지 위에 Docker 지원 기능만 추가합니다. 기존 일반 빌드와 구분하며 Linux AMD64를 대상으로 합니다.

보안·파일관리·디스크 표시 수정은 공개 Docker판 기능입니다. 종료 시 취소된 metadata task가 나머지 정리를 중단시키지 않도록 취소 예외 처리도 포함합니다. 개인용 확장 코드·설정은 이 저장소와 공개 이미지에 포함하지 않습니다. 루트의 기존 앱 파일은 이전 패치의 비교·회귀 자료이며 새 이미지의 앱은 `upstream/`과 `patches/`로 생성됩니다.

## 실행

Compose에서 새 버전 태그를 선택하고 기존 JSON·녹화 볼륨에 로그 볼륨을 더합니다.

```yaml
services:
  recordweb:
    image: ghcr.io/pcbis/recordweb:v1.2.9-runtime-20261031
    init: true
    restart: unless-stopped
    stop_grace_period: 60s
    environment:
      TZ: Asia/Seoul
    ports:
      - "5000:5000"
    volumes:
      - ./data/json:/app/json
      - ./recordings:/recordings
      - ./logs:/var/log/recordweb
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "3"
```

SELinux/rootless 환경에서는 해당 환경의 UID·라벨 규칙에 맞게 bind mount를 준비합니다. 전체 앱 디렉터리를 mount하면 이미지의 새 코드를 가릴 수 있습니다. 기존 사용자 데이터는 그대로 보존하며 이미지가 설정·영상 전체를 덮어쓰지 않습니다.

## 로그

원래 `python recordWEB.py` 실행의 stdout/stderr를 Docker 콘솔과 `logs/console.jsonl`에 복제합니다. 파일에는 수집 시각·stream·message·partial 여부가 들어갑니다. stderr가 곧 ERROR는 아닙니다. 원본이 출력하지 않은 traceback이나 내부 상태를 복구하는 기능은 아닙니다.

`console.jsonl`은 원문 출력이므로 계정·채널·제목·파일명·URL 등이 포함될 수 있습니다. 로컬 접근을 제한하고 공개 첨부 전에 검토하세요. 기본 권한은 디렉터리0700·파일0600입니다. `events.jsonl`은 허용된 오류 분류와 알려진 예외 유형·errno·종료 코드만 담습니다. 원본의 `/app/logs/recordFSM_web.log`와 별개입니다.

파일 쓰기 실패·느린 저장·큐 초과 시 녹화 출력을 계속 읽되 일부 로그가 누락될 수 있습니다. `collector-state.json`과 진단팩에 누락/실패 횟수가 기록됩니다. 수집기는 앱 종료 코드와 신호를 전달하며 자식이 상속한 FD 때문에 무한 대기하지 않습니다.

## 수동 진단

```sh
docker exec recordweb python /opt/recordweb-docker/docker_support/diagnostics.py --symptom recording
```

증상은 `start`, `recording`, `postprocessing`, `ui`, `storage`, `network`, `other` 중 고릅니다. 생성 위치는 `logs/diagnostics/diagnostic-*.zip`입니다. 네트워크 전송은 하지 않습니다.

진단팩은 앱/패치/도구 버전, 허용한 설정, 현재 프로세스·저장소 상태, 최근 오류 분류와 수집 결과를 담습니다. 원문 로그·쿠키·계정·채널 ID·제목·파일명·명령행·전체 환경·메모리·브라우저 저장소는 담지 않습니다. 압축 전후 각각4MiB를 제한하고 최근3개를 보관합니다. 이 정보로 모든 실방송·인증·순간적 경쟁 상태가 재현되지는 않습니다. 발생시각과 직전 조작을 별도로 적어 주세요. 스냅샷은 수집 당시의 관찰값이며 상태를 완전히 동시 동결하지 않습니다.

## 자동 진단

- 명확한 ERROR/CRITICAL/FATAL·traceback·알려진 예외 종료 줄은 수집 후보입니다. 외부 도구의 소문자 level 표기도 인식합니다.
- 녹화/명령/후처리 시작 실패처럼 명시된 실패는 즉시 수집합니다. 일시적 network ERROR는60초 내2회, WARNING은 같은 분류가60초 내3회일 때 수집합니다.
- 같은 분류는120초, 전체는30초의 대기시간을 둡니다. 동시에 대기 가능한 수집 요청도 제한합니다.
- 직전 최대5분·최대2048개의 안전한 사건 기록과 감지 당시 상태를 먼저 확보하고, 이후30초 사건을 덧붙입니다. 잘림·누락과 실제 시각을 기록합니다. 원문 콘솔의 모든 줄을 공유팩에 넣는 것은 아닙니다.
- 앱 비정상 종료도 수집합니다. 정상 종료 요청 중에는 이를 비정상 종료로 분류하지 않습니다. 컨테이너 전체 강제 종료·호스트 장애에서는 최종 팩 생성이 불가능할 수 있습니다. `pending-diagnostic.json`에는 생성 중인 안전한 스냅샷이 남을 수 있습니다.

트리거는 현재 앱/도구의 텍스트 형식을 분류하는 방식입니다. 모든 실패를 감지하거나 실제 실패 여부를 완벽하게 판정하지는 않습니다. 단일 WARNING마다 팩을 만들지 않습니다.

## 설정

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `RECORDWEB_CAPTURE_LOGS` | `1` | `0`이면 수집기를 거치지 않고 원래 명령 실행 |
| `RECORDWEB_LOG_DIR` | `/var/log/recordweb` | 로그·진단팩 폴더 |
| `RECORDWEB_LOG_MAX_BYTES` | `10485760` | 콘솔 파일당10MiB |
| `RECORDWEB_LOG_BACKUPS` | `5` | 콘솔 회전본5개 |
| `RECORDWEB_AUTO_DIAGNOSTICS` | `1` | `0`이면 자동 수집 해제, 수동 CLI는 사용 가능 |
| `RECORDWEB_DIAG_PRE_SECONDS` | `300` | 앞선 사건 기록 요청 범위 |
| `RECORDWEB_DIAG_POST_SECONDS` | `30` | 뒤따르는 사건 대기시간 |
| `RECORDWEB_DIAG_WARNING_COUNT` | `3` | WARNING 반복 임계값 |
| `RECORDWEB_DIAG_WARNING_WINDOW` | `60` | 반복 판정 범위 |
| `RECORDWEB_DIAG_COOLDOWN` | `120` | 같은 분류의 중복 수집 억제 |
| `RECORDWEB_STOP_TIMEOUT` | `45` | 앱 종료 대기시간, Compose grace는 더 길게 지정 |

위 설정은 수집기 설정이며 원본 앱의 INFO/DEBUG 레벨을 바꾸지 않습니다. `PYTHONFAULTHANDLER=1`은 Python 치명적 오류 traceback을 보완하며 요청형 signal dump나 메모리 덤프를 활성화하는 기능은 아닙니다.

## 검증·복구

기존 보안/디스크 회귀, 원본 불변/패치 조합, 회전/개인정보 제외/트리거/저장 실패, signal 전달/마지막 출력, 취소된 metadata task 종료를 시험합니다. 별도 네트워크 없는 후보에서 원래 전체 기동, 합성 메타데이터와 실제 FFmpeg의 두 녹화기 루프, 수동/자동 진단을 추가 검증합니다. 실방송·장기 녹화·외부 플랫폼 인증의 성공까지 의미하지 않습니다.

운영 교체 전 현재 Compose·전체 Env·설정·이미지·기존 override를 보존합니다. 실패 시 일치하는 이전 이미지/정의/override 세트로 복구하되 최신 사용자 데이터와 영상은 보존합니다. 로그 폴더는 이전 이미지로 돌아가도 남겨 둘 수 있습니다.
