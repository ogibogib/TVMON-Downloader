# TVMON Download Manager

Windows용 영상·소설 다운로드 매니저입니다. TVMON/TVWIKI 영상과 뉴토끼 소설을 검색하고, 여러 회차를 대기열에 추가해 차례로 저장합니다.

## 실행 파일 받기

[최신 Windows 버전 다운로드](https://github.com/ogibogib/TVMON-Downloader/releases/latest)

1. Releases에서 `TVMON-Download-Manager-v1.0.0-windows-x64.zip`을 받습니다.
2. ZIP 파일의 **전체 내용**을 쓰기 가능한 폴더에 풉니다.
3. `TVMON-Download-Manager.exe`를 실행합니다. `_internal` 폴더를 함께 유지하세요.

Windows 10/11 64비트를 대상으로 합니다. 배포본은 Python 설치 없이 실행할 수 있습니다. 브라우저 인증과 P2P 기능에는 PC에 설치된 Google Chrome을 사용합니다.

## 주요 기능

- 영상·소설 작품 검색, 회차 선택, 대기열 일괄 추가
- 영상 일시정지·이어받기, 조각 검증과 순서대로 병합
- HTTP와 P2P 수신 상태, 작업자·서버·피어 상세 표시
- 소설 TXT 저장과 전체 회차 ZIP 생성
- 사이트 주소 자동 확인, 도메인 우선순위와 안내 페이지 설정
- 우클릭 재생·이어받기·현재 접속 주소 복사

자세한 사용법은 [사용방법.txt](manager/TVMON-Download-Manager/사용방법.txt)를 참고하세요.

## 소스에서 실행

Windows용 Python 3.11 64비트를 기준으로 검증했습니다. 저장소 루트의 PowerShell에서 실행합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\source\requirements.txt
.\.venv\Scripts\python.exe .\source\tvmon_downloader.py
```

## Windows 실행 파일 빌드

위 가상환경을 준비한 뒤 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller==6.11.1
.\build-manager.ps1 -Python .\.venv\Scripts\python.exe
```

결과는 `manager-search-build/TVMON-Download-Manager/`에 생성됩니다. 실행 파일, `_internal` 폴더와 `사용방법.txt`를 함께 배포합니다. 빌드 설정은 `manager-build/TVMON-Download-Manager.spec`입니다.

## 테스트

기존 테스트는 `manager-build/backups/current-version-tests-20261011/`에 있습니다. 경로 구조를 유지해 공개했습니다. 저장소 루트에서 다음 명령으로 실행합니다.

```powershell
$env:PYTHONPATH = (Resolve-Path .\source).Path
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s .\manager-build\backups\current-version-tests-20261011 -p "test_tvmon_*.py" -v
```

대부분의 검사는 로컬 재현 데이터를 사용합니다. 별도 실사이트 표지 이미지가 필요한 검사는 자료가 없으면 건너뜁니다.

## 데이터 위치

- 배포본의 작업 목록과 설정: 실행 파일 옆 `data/`
- 소스 실행의 작업 목록과 설정: `source/data/`
- 기본 다운로드 폴더: 실행 위치의 `다운로드/`

기존 PC에서는 이전 버전의 설정과 인증 프로필을 이어서 사용할 수 있습니다. 저장소와 Releases에는 다운로드 파일, 사용자 설정, 로그, 브라우저 프로필을 포함하지 않습니다. `.gitignore`는 공개할 프로젝트 경로만 허용하도록 구성되어 있습니다.

## 외부 구성 요소

메뉴 아이콘은 Microsoft Fluent System Icons를 사용하며 [MIT 고지](source/assets/fluent-icons-LICENSE.txt)를 포함합니다. 배포본에는 함께 제공되는 라이브러리의 라이선스 고지가 들어 있습니다.

사이트 주소와 응답 형식이 바뀌면 일부 기능에 수정이 필요할 수 있습니다. 실제 다운로드 속도는 서버·피어·네트워크·저장 장치 상태에 따라 달라집니다.
