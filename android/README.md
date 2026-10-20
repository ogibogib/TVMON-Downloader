# TVMON Android

TVMON/TVWIKI 영상 전용 Android 앱입니다. 영상 검색, 회차 선택, HTTP/HLS 다운로드, 이어받기, 대기열 관리와 저장 영상 보관함을 제공합니다. 뉴토끼 소설·P2P·PC Chrome 인증 기능은 포함하지 않습니다.

## 다운로드

- [Android v0.1.10 릴리스](https://github.com/ogibogib/TVMON-Downloader/releases/tag/android-v0.1.10)
- [APK 다운로드](https://github.com/ogibogib/TVMON-Downloader/releases/download/android-v0.1.10/TVMON-Android-v0.1.10.apk)
- [Android 소스 ZIP](https://github.com/ogibogib/TVMON-Downloader/releases/download/android-v0.1.10/TVMON-Android-v0.1.10-source.zip)
- [SHA-256 체크섬](https://github.com/ogibogib/TVMON-Downloader/releases/download/android-v0.1.10/SHA256SUMS.txt)

Android 10(API 29) 이상의 ARM64 휴대폰과 x86_64 에뮬레이터를 지원합니다. 32비트 Android 기기는 지원하지 않습니다. APK는 개발용 키로 서명한 테스트 배포본이며 Google Play 배포판은 아닙니다. 같은 앱의 업데이트 설치는 기존 대기열·설정·저장 영상을 유지합니다.

## 사용 방법

1. 검색에서 작품 제목을 입력하고 작품을 선택합니다. 작품 또는 회차 링크를 직접 입력할 수도 있습니다.
2. 회차를 선택한 뒤 **대기열 추가** 또는 **다운로드**를 누릅니다. 최근 받은 작품에서는 검색 없이 해당 작품의 회차 목록을 다시 엽니다.
3. 대기열 카드 오른쪽 상단의 **시작 / 일시정지 / 재개**로 개별 회차를 제어합니다. 제어 대상이 2개 이상이면 하단에 전체 제어가 표시됩니다. 회차는 순서대로 처리하고 조각은 작업자 4개로 받습니다.
4. 카드를 누르면 조각 지도·진행률·재생시간 등이 있는 상세 패널이 열립니다. 완료 항목에서는 재생할 수 있습니다.
5. 완료 파일은 공용 **다운로드/TVMON** 폴더에 `.ts`로 저장됩니다. 보관함은 대기열 기록과 별도로 실제 저장 파일을 조회하며, 카드 전체 또는 재생 아이콘을 누르면 외부 플레이어를 엽니다. MPEG-TS를 지원하는 플레이어가 필요합니다.
6. 보관함의 **⋮ → 삭제**는 확인 후 실제 영상 파일과 해당 파일의 완료 작업 기록을 지웁니다. 대기열의 목록 삭제·전체 삭제는 완료 영상 파일을 유지하며, 최근 작품 기록도 보존합니다.
7. 설정에서 접속 주소와 안내 페이지 URL을 편집합니다. **주소 설정 저장**으로 적용하고 **사이트 접속 확인**으로 연결 결과를 확인합니다. 접속 도메인은 입력한 순서대로 시도합니다.

앱 데이터 삭제 또는 앱 제거 시 내부 대기열·미완료 조각은 삭제됩니다. APK 업데이트에는 앱 데이터 삭제가 필요하지 않습니다.

## v0.1.10의 현재 화면

- 개별 제어 버튼은 모든 카드의 오른쪽 상단에 같은 크기로 표시합니다.
- 대기 카드는 제목·대기 중·시작만 표시하며 빈 진행률 바와 0.0 MB는 숨깁니다.
- 다운로드 용량은 왼쪽, 속도·남은 시간은 같은 행의 오른쪽에 표시합니다. 이 행은 13sp이며 양쪽의 '약'을 생략하고 오른쪽 글자 폭을 5% 줄였습니다. 공간이 부족하면 말줄임 처리합니다.
- 진행률·받은 조각·바이트 수를 유지하며 일시정지·재개합니다. 전체 제어는 완료 항목을 제외한 제어 대상이 2개 이상일 때 표시합니다.
- 보관함은 파란 재생 아이콘과 ⋮ 메뉴를 사용합니다. 긴 제목은 2줄로 표시하고 기본 .ts 확장자를 생략합니다. 확장자만 다른 파일은 구분해서 표시합니다.
- 삭제 확인창의 배경은 표시 전에 적용합니다. 보관함 확인창은 영상 제목과 복구 불가 안내를 표시합니다.

## 소스에서 빌드

Windows PowerShell과 다음 도구가 필요합니다. Android Studio에 포함된 JDK·SDK를 사용할 수 있습니다.

- JDK 17 이상
- Android SDK Platform 36, Build Tools 35.0.0, Platform Tools
- NDK 27.0.12077973, CMake 3.22.1
- Gradle 8.13 및 Android Gradle Plugin 8.13.2 — 래퍼와 설정에 고정되어 있습니다.

저장소를 받거나 소스 ZIP을 해제한 뒤, `android` 폴더가 있는 최상위 폴더에서 실행합니다.

```powershell
.\android\build.ps1 -Check
```

결과 APK는 `android/artifacts/TVMON-Android-v0.1.10.apk`에 생성됩니다. `-Check`는 APK, Android Lint와 계측 테스트 APK를 빌드합니다. Android Studio에서는 `android` 폴더를 프로젝트로 엽니다.

빌드 스크립트는 `native-deps.ps1`로 공식 libcurl-impersonate 2.2.2 Android 라이브러리를 내려받아 고정 SHA-256과 대조합니다. 캐시는 `android/build/native-deps`에 저장되며 라이선스 고지는 APK의 `assets/native-licenses`에 포함합니다. Android Studio에서 직접 빌드하기 전에도 해당 스크립트를 실행해야 합니다. JNI 라이브러리는 16KB 페이지 크기를 지원합니다.

SDK/JDK 자동 검색이 맞지 않으면 `ANDROID_HOME`과 `JAVA_HOME`을 지정하세요. 소스 ZIP에는 로컬 SDK 경로, 캐시, 서명 키, 휴대폰 설정·대기열, 받은 영상 파일이 포함되지 않습니다. 다른 환경에서 생성한 개발용 서명 키는 배포 APK의 키와 다를 수 있습니다.

## 검증

- APK 빌드·Lint·v2 서명 검증 통과. 최신 APK는 연결된 Samsung SM-S918N(Android 16)에 설치하고 설정·대기열·저장 파일 보존을 확인했습니다.
- 대기열 카드 개편 시 상태별 27개, 320dp·글꼴 150%의 동일 27개, 실제 다운로드 제어 12개, 기존 품질 41개·삭제 20개·보관함 32개를 통과했습니다(반복 포함 159개).
- 마지막 한 줄 표시·13sp·문구/글자 폭 조정 후 대기열 27개와 320dp·글꼴 150%의 동일 27개를 다시 통과했습니다. 기존 화면 설정은 복원했습니다.
- 삭제 검사는 테스트가 만든 에뮬레이터 파일만 사용했습니다. 사용자의 휴대폰 파일은 테스트로 삭제하지 않았습니다.
- Android 10–15 실제 기기, TalkBack 음성 안내 전체 흐름, 외부 플레이어 지속 재생은 추가 검증이 필요합니다.

계측 검사는 비어 있는 테스트 전용 에뮬레이터에서 실행하세요. 예를 들어 SDK Platform Tools가 PATH에 있다면 다음과 같이 실행합니다.

```powershell
adb -s emulator-5554 install -r android/artifacts/TVMON-Android-v0.1.10.apk
adb -s emulator-5554 install -r android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb -s emulator-5554 shell am instrument -w -e queueControls true com.tvmon.android.test/com.tvmon.android.AndroidChecks
```

`queueControlsLive`, `quality`, `deletion`, `library` 모드도 같은 방식으로 선택할 수 있습니다. 다운로드·삭제 검사는 테스트 작업과 파일을 생성하고 정리합니다.
