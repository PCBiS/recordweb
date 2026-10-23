# v1.2.9-ca-20261023

기존 `v1.2.9` AMD64 이미지에 CA 신뢰 정책만 적용한 버전입니다.

```text
ghcr.io/pcbis/recordweb:v1.2.9-ca-20261023
```

기반 이미지는 `ghcr.io/pcbis/recordweb@sha256:7953c7491829599227275c6811d2773a2c2936656cdda14efc7fd647f7da61db`로 고정합니다. 앱 소스·Python 패키지·OS 패키지는 갱신하지 않습니다. 기존 `v1.2.9`와 멀티아키텍처 `latest` 태그는 이 릴리스에서 변경하지 않습니다.

시스템 CA와 Python certifi에서 다음 DER SHA-256 인증서를 제거하고 Debian의 CA 링크를 다시 생성합니다.

| 인증서 | DER SHA-256 |
| --- | --- |
| emSign ECC Root CA - G3 | `86A1ECBA089C4A8D3BBE2734C612BA341D813E043CF9E8A862CD5C57A36BBE6B` |
| AAA Certificate Services | `D7A7A0FB5D7E2731D771E9484EBCDEF71D5F0C3E0A2948782BC83EE0EA699EF4` |

이는 Java 관련 CA 점검에 이어 적용한 공개 CA 신뢰 변경입니다. Java 런타임을 설치하거나 교체하지 않습니다. 별도 사설 CA나 사용자의 설정·쿠키·녹화 데이터는 이미지에 포함하지 않습니다.

빌드 중 원본 CA 집합에서 해당 지문만 빠졌는지 검사합니다. 게시 전에는 원본 11개 레이어 보존, CA 레이어 1개 추가, 실행 설정·Healthcheck 보존, Python SSL·certifi·Requests·HTTPX의 실제 신뢰 집합, 오프라인 CA 재생성의 멱등성, 소스 컴파일과 ffmpeg 실행을 검증합니다. 검증을 통과한 OCI manifest를 버전 태그로 복사합니다. 실방송 녹화·로그인·외부 플랫폼 API에 대한 통합 시험은 포함하지 않습니다.

이 릴리스는 Linux AMD64 전용입니다. ARM64 장비는 기존 멀티아키텍처 태그를 사용해야 합니다.

기존 Compose에서 이 버전을 선택할 때는 `RECORDWEB_TAG=v1.2.9-ca-20261023`을 지정합니다. 실제 서비스의 이미지 교체는 녹화가 없는 유지보수 시간에 진행하고, 설정과 녹화 볼륨을 보존해야 합니다. 이번 이미지 게시 작업은 실행 중인 서비스의 배포를 수행하지 않습니다.

재현에 사용하는 파일은 `Dockerfile.ca`, `security/ca_policy.py`, `security/verify_image.py`이며, 워크플로는 `.github/workflows/publish-ca-image.yml`입니다. 원본 태그로 돌아가면 이번 CA 신뢰 제거도 해제됩니다.
