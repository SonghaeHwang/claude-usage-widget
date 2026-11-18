# claude-usage-widget

Windows 작업 표시줄 트레이에서 Claude 사용량을 보여 주는 위젯.
트레이 아이콘을 누르면 등록된 모든 Claude 계정의 사용량이 팝업으로 뜬다.

- 5시간 / 7일 / **모델별 7일 한도**, 리셋 시각, 7일 사용처 분해
- 트레이 아이콘에 가장 높은 사용률을 숫자와 색으로 표시
- 한도 임박(기본 80%·95%)과 리셋 시 Windows 알림
- 지금 속도면 리셋 전에 소진될 때 예상 시각 표시, 5시간 한도의 24시간 추이
- 밝게/어둡게 테마, 설정 창, 여러 계정 관리

구조와 설계 판단은 [DESIGN.md](DESIGN.md) 참고.

## 실행

```
uv sync
start.bat
```

이미 실행 중이면 두 번째 실행은 조용히 종료된다.

## 트레이 메뉴

| 메뉴 | 설명 |
|---|---|
| 사용량 보기 | 팝업 열기 (아이콘 클릭과 같음) |
| 계정 관리 | 계정 추가, 이름 변경, 순서 변경, 목록에서 삭제 |
| 설정… | 테마, 아이콘 모양, 갱신 주기, 알림 기준, 토큰 자동 갱신 |
| Windows 시작 시 자동 실행 | 로그인할 때 자동으로 켜기 |
| 로그 파일 열기 | `~/.claude-usage-widget/widget.log` |

## 재부팅 후 자동 실행

트레이 메뉴 또는 설정 창에서 켠다. 명령으로도 가능하다.

```
uv run python widget.py --autostart on
uv run python widget.py --autostart off
```

현재 사용자의 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 에
`ClaudeUsageWidget` 값으로 등록된다. 프로젝트 폴더를 옮기면 다시 켜야 한다.

## exe 로 묶기

```
build.bat
```

`dist\ClaudeUsageWidget.exe` 하나가 만들어진다. 원하는 곳에 두고 실행한 뒤
트레이 메뉴에서 자동 실행을 켜면 exe 경로로 등록된다.

## 계정과 토큰

계정 하나 = Claude Code 설정 폴더(`CLAUDE_CONFIG_DIR`) 하나.
목록은 `~/.claude-usage-widget/accounts.json` 에 저장되며, 토큰은 각 폴더의
`.credentials.json` 을 읽어 쓴다(저장소에는 포함되지 않는다).

토큰이 만료됐을 때만 위젯이 직접 갱신한다. Claude Code 와 정확히 같은 순간에
갱신하면 한쪽이 재로그인해야 할 수 있다. 걱정되면 설정에서
"만료된 토큰을 위젯이 직접 갱신" 을 끈다(그러면 만료 시 Claude Code 를 한 번 실행해야 한다).

사용량은 비공식 API 로 조회하므로 응답 형식이 바뀌면 일부 항목이 보이지 않을 수 있다.

## 테스트

```
uv run pytest -q
```
