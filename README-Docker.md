# recordWEB v1.2.9 Docker 실행

이 폴더는 전달받은 `recordWEB_v1.2.9.zip` 원본에 Docker 실행 설정을 더한 버전입니다.
Linux 컨테이너 안에서 `ffmpeg`, `ffprobe`, `streamlink`, `yt-dlp`, `aria2c`, `deno`를 사용할 수 있게 구성했습니다.

## 가장 간단한 실행

Docker Desktop을 실행한 뒤, PowerShell에서 이 폴더로 이동해 실행합니다. 기본 `compose.yaml`은 GHCR의 공개 이미지를 받습니다.

```powershell
docker compose up -d
```

브라우저에서 <http://localhost:5000>으로 접속합니다.

로그 확인과 종료 명령은 다음과 같습니다.

```powershell
docker compose logs -f
docker compose down
```

현재 폴더의 소스로 직접 빌드하려면 별도 로컬 Compose를 사용합니다.

```powershell
docker compose -f compose.local.yaml up -d --build
```

## 저장 위치

- 설정, 채널, 쿠키: `./data/json`
- 기본 녹화 결과: `./recordings/chzzk`
- 웹 화면에서 새 출력 경로를 지정할 때: `/recordings/원하는폴더`

최초 실행 시 ZIP에 들어 있던 `json` 설정이 `./data/json`으로 복사됩니다. 이후 컨테이너를 삭제하거나 다시 만들어도 이 폴더의 설정은 유지됩니다.

ZIP에 개인 채널 및 쿠키 설정이 들어 있었다면 그대로 초기 데이터에 포함됩니다. 이미지를 외부 레지스트리에 공개할 때는 개인 정보가 포함되지 않도록 `json/channels.json`, `json/cookie.json`, `json/ycookie.txt` 등을 먼저 정리하세요.

## 포트 변경

호스트에서 8080 포트로 열려면 다음처럼 실행합니다. 컨테이너 내부 포트는 `json/config.json`의 기본값인 5000을 유지해야 합니다.

```powershell
$env:RECORDWEB_PORT=8080
docker compose up -d
```

이때 접속 주소는 <http://localhost:8080>입니다.

## 개별 Docker 명령으로 실행

Compose를 쓰지 않는 경우:

```powershell
docker build -t recordweb:1.2.9 .
docker run -d --name recordweb --restart unless-stopped -p 5000:5000 `
  -v "${PWD}/data/json:/app/json" `
  -v "${PWD}/recordings:/recordings" `
  -e TZ=Asia/Seoul recordweb:1.2.9
```

## 참고

- Docker 컨테이너에서는 Windows 트레이 아이콘 및 GUI 버전이 아니라 `recordWEB.py` 웹 서버를 실행합니다.
- 하드웨어 인코딩(NVIDIA/Intel/AMD)은 별도의 장치 전달과 드라이버 구성이 필요합니다. 현재 설정은 CPU 및 stream copy 사용을 기준으로 합니다.
- `json/config.json`의 `port`를 5000이 아닌 값으로 바꾸면 Dockerfile의 health check와 Compose의 컨테이너 포트도 같은 값으로 맞춰야 합니다.
