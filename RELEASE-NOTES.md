# RecordWEB v1.2.9-cime-tls-20261109

CIME 방송 정보, 시청 페이지와 HLS 재생목록 조회에서 인증서 검증 실패 후 같은 요청을 검증 없이 보내던 동작을 수정했습니다. 신뢰하지 않는 발급자나 호스트명이 일치하지 않는 서버에는 HTTP 요청과 CIME 쿠키가 전달되지 않습니다. 다음 확인 주기에도 인증서 검증을 사용합니다.

Docker 이미지는 `ghcr.io/pcbis/recordweb:v1.2.9-cime-tls-20261109`입니다. 기존 녹화 파일과 설정 볼륨을 유지한 채 녹화가 없는 시점에 태그를 교체합니다. FFmpeg·ffprobe는 9.0.2를 사용합니다. [이전 의존성 패치노트](https://github.com/PCBiS/recordweb/releases/tag/deps-20261108)
