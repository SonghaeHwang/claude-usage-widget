# claude-usage-widget

Windows 작업 표시줄 트레이에서 Claude 사용량(5시간 / 7일)을 보여 주는 위젯.
트레이 아이콘을 누르면 등록된 모든 Claude 계정의 사용량이 팝업으로 뜬다.

## 실행

```
uv sync
start.bat
```

## 재부팅 후 자동 실행

트레이 아이콘 우클릭 → **Windows 시작 시 자동 실행** 을 체크한다.
명령으로 켜고 끌 수도 있다.

```
uv run python widget.py --autostart on
uv run python widget.py --autostart off
```

현재 사용자의 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 에
`ClaudeUsageWidget` 값으로 등록된다. 프로젝트 폴더를 옮기면 다시 켜야 한다.

## 계정

계정 하나 = Claude Code 설정 폴더(`CLAUDE_CONFIG_DIR`) 하나.
목록은 `~/.claude-usage-widget/accounts.json` 에 저장되며, 토큰은 각 폴더의
`.credentials.json` 을 읽어 쓴다(저장소에는 포함되지 않는다).
