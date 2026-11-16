"""관리 포트 전달기.

RWEB_ADMIN_PORT 로 받은 TCP 연결을 컨테이너 안 loopback 의 recordWEB 포트로 그대로 넘긴다.
recordWEB 은 loopback 접속을 "서버 PC"로 취급하므로, 이 포트로 들어오면 서버 PC 전용 기능을 쓸 수 있다.
이 포트는 호스트의 127.0.0.1 에만 게시해야 한다.
"""
import asyncio
import json
import os
import sys


def app_port():
    try:
        with open('/app/json/config.json', encoding='utf-8') as f:
            return int(json.load(f).get('port', 5000))
    except (OSError, ValueError, TypeError):
        return 5000


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(reader, writer, target):
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', target)
    except OSError:
        writer.close()
        return
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main():
    listen, target = int(os.environ['RWEB_ADMIN_PORT']), app_port()
    if listen == target:
        sys.exit('[recordweb-docker] RWEB_ADMIN_PORT 는 recordWEB 포트와 달라야 합니다.')
    server = await asyncio.start_server(lambda r, w: handle(r, w, target), '0.0.0.0', listen)
    print(f'[recordweb-docker] 관리 포트 {listen} -> 127.0.0.1:{target}', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
