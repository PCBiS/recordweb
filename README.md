# recordWEB Docker

recordWEB v1.2.9를 Linux Docker 컨테이너에서 실행하기 위한 프로젝트입니다.
https://gall.dcinside.com/mgallery/board/view/?id=stellive&no=2427022
게시글의 ㅇㅇ(poestar)님께서 개발하셨으며 docker에서 배포 가능한 형식으로 사용하기 위해
ChatGPT Codex를 이용하여 변환작업하였습니다.

## 빠른 실행

이 저장소의 `compose.yaml`이 있는 폴더에서 다음 명령을 실행합니다.

```bash
docker compose up -d
```

로컬에 이미지가 없으면 `ghcr.io/pcbis/recordweb:latest`를 자동으로 다운로드합니다.
웹 화면은 <http://localhost:5000>에서 열립니다.

```bash
docker compose ps
docker compose logs -f
docker compose down
```

## 업데이트

```bash
docker compose pull
docker compose up -d
```

`compose.yaml`에 `pull_policy: always`가 설정되어 있어 기동할 때 새 이미지도 확인합니다.

## 데이터

- 설정, 채널, 쿠키: `./data/json`
- 녹화 결과: `./recordings`
- 웹에서 출력 경로를 직접 지정할 때: `/recordings/원하는폴더`

> [!IMPORTANT]
> `./recordings:/recordings` 마운트는 녹화 파일을 보존하기 위해 반드시 필요합니다.
> 이 마운트를 제거하면 녹화물이 컨테이너 내부에 저장되어 컨테이너 재생성, 업데이트 또는 삭제 시 함께 사라질 수 있습니다.

기본 `compose.yaml`에는 다음 필수 마운트가 이미 포함되어 있습니다.

```yaml
volumes:
  - ./data/json:/app/json
  - ./recordings:/recordings
```

`./recordings`는 컨테이너 경로가 아니라 `compose.yaml`이 있는 호스트 폴더의 `recordings` 디렉터리입니다. 서버의 다른 디스크에 저장하려면 왼쪽 경로만 절대 경로로 변경합니다.

```yaml
volumes:
  - /mnt/recordings:/recordings
```

오른쪽 컨테이너 경로 `/recordings`는 변경하지 마세요. 웹에서 녹화 출력 경로를 설정할 때도 `/recordings` 아래를 사용해야 호스트에 보존됩니다.

최초 실행 때 이미지의 기본 JSON 파일이 `./data/json`에 생성됩니다. 컨테이너를 삭제하거나 업데이트해도 두 호스트 폴더는 유지되지만, 서버 자체 장애에 대비해 `./data/json`과 `./recordings`를 함께 백업하는 것을 권장합니다.

## 포트 변경

PowerShell:

```powershell
$env:RECORDWEB_PORT=8080
docker compose up -d
```

Linux/macOS:

```bash
RECORDWEB_PORT=8080 docker compose up -d
```

## 로컬 소스에서 빌드

GHCR 이미지 대신 현재 소스로 직접 빌드하려면:

```bash
docker compose -f compose.local.yaml up -d --build
```

## 이미지 배포

`main` 브랜치에 푸시하거나 `v`로 시작하는 태그를 푸시하면 GitHub Actions가 Linux AMD64 이미지를 빌드해 GHCR에 게시합니다.

```bash
git tag v1.2.9
git push origin v1.2.9
```

게시되는 태그 예시는 다음과 같습니다.

- `ghcr.io/pcbis/recordweb:latest`
- `ghcr.io/pcbis/recordweb:v1.2.9`
- `ghcr.io/pcbis/recordweb:sha-...`

GHCR 패키지가 비공개라면 다른 사용자는 이미지를 받을 수 없습니다. 공개 배포하려면 GitHub의 package settings에서 패키지 visibility를 Public으로 설정합니다.

## 보안

기본 웹 설정은 인터넷 공개를 전제로 하지 않습니다. 공유기에서 5000번 포트를 직접 포워딩하지 말고, 외부 접속에는 Tailscale 또는 인증이 적용된 HTTPS 리버스 프록시를 사용하세요.
