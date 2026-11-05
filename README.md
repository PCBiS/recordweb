# recordWEB Docker

recordWEB v1.2.9의 웹 녹화 기능과 다시보기 다운로드를 Linux AMD64·ARM64 Docker 이미지로 제공합니다.

이미지 지원 플랫폼은 `linux/amd64`와 `linux/arm64`입니다. Docker가 호스트에 맞는 이미지를 선택합니다.

## 빠른 실행

```sh
docker compose -f compose.runtime.yaml up -d
```

웹 화면은 <http://localhost:5000>에서 열립니다. 다시보기 다운로드는 로그인 모드를 켜고 로그인한 뒤 메뉴에서 이용합니다.

- [최종 기능과 패치노트](RELEASE-NOTES.md)
- [다시보기·클립 다운로드, 옵션과 저장 위치](REPLAY.md)
- [Docker 실행·로그·진단](RUNTIME.md)

## 업데이트

설정·영상 볼륨과 기존 Compose를 백업하고 진행 중인 녹화·다운로드를 마친 뒤 업데이트합니다.

```sh
docker compose -f compose.runtime.yaml pull
docker compose -f compose.runtime.yaml up -d
```

`compose.runtime.yaml`은 `ghcr.io/pcbis/recordweb:v1.2.9-replay-20261106-r2`을 사용합니다. JSON·영상·로그는 각각 `./data/json`, `./recordings`, `./logs`에 보존됩니다. 완료 작업 내역은 완료 후1시간 정리하며 영상은 파일관리에서 계속 확인할 수 있습니다.

## 원본과 Docker 구성

원본 recordWEB은 `ㅇㅇ(poestar)`님이 개발했습니다. [원본 게시글](https://gall.dcinside.com/mgallery/board/view/?id=stellive&no=2427022)에서 프로그램 설명을 확인할 수 있습니다.

원본 입력은 `upstream/`에 보존합니다. Docker 빌드는 `patches/`의 입력·결과 해시를 검사하고 `/app` 실행 사본을 생성합니다. `docker_support/`에는 다시보기 대기열과 Docker 로그·진단 지원을 둡니다. 저장소 루트의 기존 앱 파일은 원본 계열의 참조 소스로 보존하며, 새 배포 이미지는 `Dockerfile.runtime`에서 빌드합니다.

```sh
docker build -f Dockerfile.runtime --target runtime -t recordweb-replay .
```

[기존 Docker 안내](README-Docker.md)와 이전 릴리스 기록은 해당 버전의 구성과 이력으로 보존합니다.
