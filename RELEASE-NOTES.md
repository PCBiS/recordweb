# RecordWEB v1.2.9-deps-20261108

Linux AMD64·ARM64 Docker 런타임의 녹화·다시보기 의존성을 갱신합니다. FFmpeg와 ffprobe는 공식 FFmpeg 9.0.2의 고정 커밋에서 각각의 아키텍처로 빌드합니다. 기존 JSON·녹화 볼륨과 Compose 설정을 그대로 사용하는 이미지 교체입니다.

| 의존성 | 새 버전 |
|---|---|
| Brotli Python / Bookworm libbrotli1 | 1.2.1 / 1.0.9-2+deb12u1 |
| urllib3 / Streamlink | 2.8.0 / 8.6.3 |
| aiohttp / yarl / multidict | 3.14.5 / 1.25.1 / 6.9.1 |
| Pillow / yt-dlp | 13.0.0 / 2026.10.26 |
| cryptography / 시스템 OpenSSL | 50.0.2 / Debian 3.0.22-1~deb12u2 |
| certifi | 2026.10.23 |
| pip (빌드 도구) | 26.2 |
| FFmpeg / ffprobe | upstream 9.0.2 |

Brotli 압축 응답, 과도한 HTTP chunk-size 줄, Streamlink의 파일 redirect·DASH Location, FFmpeg의 HLS 하위 playlist 검증에 대한 수정이 포함됩니다. AMD64 후보의 회귀 163개와 실제 공개 방송 세 건을 검증했습니다. CHZZK 60.033초·CIME 60.034초·YouTube 60.000초에서 H.264 영상과 AAC 음성의 전체 디코딩이 성공했습니다. YouTube는 실제 웹 작업자에서 기본 1080p 화질을 사용했고, 시험 시간만 제한해 MP4 정상 완료를 확인했습니다. 계정 인증과 장기 녹화는 별도 검증 범위입니다. CIME의 기존 인증서 실패 후 재시도 정책은 별도 후속입니다.

YouTube의 빈 기본 쿠키 파일은 익명 접속으로 처리합니다. 음성이 포함된 라이브 화질은 그대로 받고, 영상 전용 화질에는 별도 음성 트랙을 결합합니다. 라이브 HLS는 완료 시 앱이 재생 가능한 MP4로 검증하도록 MPEG-TS 출력을 해제합니다.

서명된 Naver HLS에서 확장자가 없는 세그먼트는 검증한 주소와 데이터 형식만 허용해 처리합니다. FFmpeg 9의 네이티브 HTTPS 입력은 인증서 검증을 기본으로 켭니다. 자체 CA를 사용하는 제공자는 신뢰할 수 있는 발급 체인과 일치하는 호스트명이 필요합니다.

yt-dlp 2026.10.26과 certifi 2026.10.23을 사용합니다. 검토한 시스템 CA는 140개, Python 소비자별 CA는 118개이며 기존 제거 대상은 0개입니다.

FFmpeg 선택 기능 중 Vulkan·libplacebo는 Bookworm 버전 제약으로 제외합니다. headless 런타임에서 ffplay·Chromaprint를 제공하지 않습니다. AMD64에서 QSV, 양쪽에서 NVENC 지원 코드를 빌드하며 실제 하드웨어 인코딩에는 호환 장치·드라이버가 필요합니다.

설치 이미지는 `ghcr.io/pcbis/recordweb:v1.2.9-deps-20261108`입니다. 기존 자료를 보관하고 `compose.runtime.yaml`의 버전 태그를 확인한 뒤, 녹화하지 않는 시간에 서비스를 재생성하세요. 이전 r2 사용자는 동일한 볼륨을 유지하고 이전 이미지 태그로 복구할 수 있습니다.
