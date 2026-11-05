import os
import subprocess
import threading

# ffmpeg 경로 설정
def get_ffmpeg_path():
    current_dir = os.path.dirname(__file__)  # 현재 스크립트의 위치
    ffmpeg_path = os.path.join(current_dir, '..', 'dependent', 'ffmpeg', 'bin', 'ffmpeg.exe')  # ffmpeg 실행 파일 상대 위치
    return os.path.abspath(ffmpeg_path)  # 절대 경로 반환

def copy_stream(input_file, output_file):
    ffmpeg_path = get_ffmpeg_path()
    cmd = [
        ffmpeg_path,
        "-i", input_file,
        "-c", "copy",  # 단순 스트림 복사 옵션
        output_file
    ]
    try:
        subprocess.run(cmd, check=True)
        print(f"Finished copying {input_file}")
    except subprocess.CalledProcessError as e:
        print(f"Failed to copy {input_file}: {e}")

def process_files_in_directory(directory):
    threads = []
    # 디렉토리 내의 모든 파일을 탐색
    for filename in os.listdir(directory):
        # TS 또는 MP4 파일만 처리
        if filename.endswith('.ts') or filename.endswith('.mp4'):
            input_file = os.path.join(directory, filename)
            output_file = os.path.join(directory, f"fixed_{filename}")
            thread = threading.Thread(target=copy_stream, args=(input_file, output_file))
            thread.start()
            threads.append(thread)

    # 모든 스레드가 완료될 때까지 대기
    for thread in threads:
        thread.join()

if __name__ == "__main__":
    current_directory = os.path.dirname(os.path.realpath(__file__))
    process_files_in_directory(current_directory)
