TVMON Download Manager 원본 소스

시작하기와 빌드 방법은 저장소 루트의 README.md를 참고하세요.
Windows용 Python 3.11 64비트를 기준으로 검증했습니다.

Python 파일 9개
 tvmon_downloader.py: 프로그램 화면과 다운로드 작업 관리
 tvmon_browser.py: 브라우저 연동과 인증 처리
 tvmon_catalog.py: 작품 검색과 회차 목록
 tvmon_crypto.py: 영상 복호화
 tvmon_domains.py: 사이트 주소 확인과 변환
 tvmon_http.py: HTTP 통신
 tvmon_images.py: 표지 이미지 처리
 tvmon_p2p.py: P2P 수신과 피어 상태
 tvmon_resume.py: 이어받기 기록 관리

화면 아이콘과 고지
 assets/download-menu.png
 assets/fluent-icons-LICENSE.txt

소스 실행
저장소 루트에서 가상환경을 만들고 source/requirements.txt의 의존성을 설치합니다.
  py -3.11 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r .\source\requirements.txt
  .\.venv\Scripts\python.exe .\source\tvmon_downloader.py

빌드
  .\.venv\Scripts\python.exe -m pip install pyinstaller==6.11.1
  .\build-manager.ps1 -Python .\.venv\Scripts\python.exe
빌드 설정: manager-build/TVMON-Download-Manager.spec
빌드 결과: manager-search-build/TVMON-Download-Manager
빌드 임시 파일: manager-search-work
위 경로는 모두 저장소 루트 기준입니다. 빌드는 source 폴더를 직접 읽습니다.

테스트
 manager-build/backups/current-version-tests-20261011/test_tvmon_*.py
실행 명령은 루트 README.md의 테스트 항목을 참고하세요.

소스 실행 시 작업 목록과 설정은 source/data에 생성됩니다.
출력·사용자 데이터·다운로드·브라우저 인증 기록은 Git에 포함하지 않습니다.
