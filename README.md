# recordWEB Docker

recordWEB v1.2.9를 Linux Docker 컨테이너에서 실행하기 위한 프로젝트입니다.

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

최초 실행 때 이미지의 기본 JSON 파일이 `./data/json`에 생성됩니다. 컨테이너를 삭제하거나 업데이트해도 이 두 호스트 폴더는 유지됩니다.

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
