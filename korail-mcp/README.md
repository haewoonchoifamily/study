# 코레일 열차 예매 MCP 서버

Claude 같은 AI 클라이언트가 코레일 열차를 조회하고 예약할 수 있게 해 주는 MCP(Model Context Protocol) 서버입니다.
비공식 라이브러리 [`korail2`](https://github.com/carpedm20/korail2)를 감싸서 만들었습니다.

> **주의**
> - 코레일 공식 API가 아니므로 코레일 웹/앱 정책이나 서비스 변경에 따라 동작하지 않을 수 있습니다.
> - 자동화된 예매는 코레일 이용약관에 저촉될 수 있습니다. 개인 용도로, 과도한 조회를 피해서 사용하세요.
> - 결제는 이 서버가 하지 않습니다. 예약 후 발급되는 **구입기한 안에 코레일 앱/홈페이지에서 직접 결제**해야 합니다.

## 제공 도구

| 도구 | 설명 | 안전장치 |
|---|---|---|
| `search_trains` | 출발/도착역, 날짜, 시간으로 열차 조회 | - |
| `reserve_train` | 조회 결과의 열차 하나를 예약 | `confirm=true` 필수 |
| `list_reservations` | 예약 내역 조회 | - |
| `cancel_reservation` | 예약번호로 예약 취소 | `confirm=true` 필수 |
| `list_tickets` | 결제 완료된 승차권 조회 | - |

예약은 `search_trains`가 돌려준 `search_id`와 `index`로 진행합니다.
`confirm`을 생략하면 아무것도 예약되지 않으므로, AI가 사용자 확인 없이 실수로 예매하는 것을 막습니다.

## 설치

Python 3.10 이상이 필요합니다.

```bash
cd korail-mcp
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 실행 (Claude Code에 등록)

로그인 정보는 환경변수로만 전달합니다. 파일이나 코드에 비밀번호를 적지 마세요.

```bash
claude mcp add korail \
  -e KORAIL_ID=회원번호_또는_전화번호 \
  -e KORAIL_PW=비밀번호 \
  -- python /절대경로/korail-mcp/server.py
```

Claude Desktop 등 `mcpServers` 설정을 쓰는 클라이언트라면:

```json
{
  "mcpServers": {
    "korail": {
      "command": "/절대경로/korail-mcp/.venv/bin/python",
      "args": ["/절대경로/korail-mcp/server.py"],
      "env": {
        "KORAIL_ID": "회원번호_또는_전화번호",
        "KORAIL_PW": "비밀번호"
      }
    }
  }
}
```

## 사용 예시

> "2026년 12월 25일 서울에서 부산 가는 KTX, 오전 9시 이후 것 찾아줘"

1. AI가 `search_trains(dep="서울", arr="부산", date="2026-12-25", time="09:00", train_type="ktx")` 호출
2. 결과 목록을 보여주고 사용자에게 고르게 함
3. 사용자가 승인하면 `reserve_train(search_id=..., index=..., confirm=true)` 호출
4. 반환된 예약번호와 구입기한을 확인하고, 구입기한 안에 코레일 앱에서 결제

## 테스트

실제 코레일에 접속하지 않고 가짜 클라이언트로 동작을 검증합니다.

```bash
pip install pytest
pytest -q test_server.py
```

## 한계

- 결제, 회원가입, 카드 할인 입력은 지원하지 않습니다. 할인 카드 옵션은 `korail2`의 Passenger 구조를 확장해야 합니다.
- 예약 대기(waiting list)는 아직 예약 도구에서 사용하지 않습니다(`try_waiting=False`).
- 검색 결과는 메모리에만 저장되며(최근 20건), 서버를 재시작하면 `search_id`가 무효가 됩니다.
- 역 이름은 코레일이 쓰는 한글 명칭을 사용합니다(예: 서울, 용산, 부산, 동대구).
