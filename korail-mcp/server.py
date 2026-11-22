"""코레일(Korail) 열차 예매 MCP 서버.

비공식 라이브러리 `korail2`를 감싸서 MCP 도구로 노출합니다.

- search_trains     : 열차 조회 (search_id 와 열차 index 반환)
- reserve_train     : 조회 결과 중 하나를 예약 (confirm=true 필수)
- list_reservations : 예약 내역 조회
- cancel_reservation: 예약 취소 (confirm=true 필수)
- list_tickets      : 결제 완료된 승차권 조회

로그인 정보는 환경변수 KORAIL_ID, KORAIL_PW 에서만 읽습니다.
결제는 이 서버가 하지 않습니다. 예약 후 발급되는 구입기한 안에
코레일 앱/홈페이지에서 직접 결제해야 합니다.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from korail2 import (
    AdultPassenger,
    ChildPassenger,
    Korail,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    ReserveOption,
    TrainType,
)

mcp = FastMCP("korail")

_lock = threading.Lock()
_client: Korail | None = None
# search_id -> {"trains": [...], "adults": int, "children": int}
_searches: dict[str, dict[str, Any]] = {}
_MAX_CACHED_SEARCHES = 20

_TRAIN_TYPES = {
    "all": TrainType.ALL,
    "ktx": TrainType.KTX,
    "saemaeul": TrainType.SAEMAEUL,
    "mugunghwa": TrainType.MUGUNGHWA,
}

_OPTIONS = {
    "general_first": ReserveOption.GENERAL_FIRST,
    "general_only": ReserveOption.GENERAL_ONLY,
    "special_first": ReserveOption.SPECIAL_FIRST,
    "special_only": ReserveOption.SPECIAL_ONLY,
}

_SEAT_STATUS = {"11": "available", "13": "sold_out", "00": "none"}


# ---------------------------------------------------------------- helpers

def _get_client() -> Korail:
    """로그인된 Korail 클라이언트를 한 번만 만들어 재사용합니다."""
    global _client
    with _lock:
        if _client is None:
            korail_id = os.environ.get("KORAIL_ID")
            korail_pw = os.environ.get("KORAIL_PW")
            if not korail_id or not korail_pw:
                raise RuntimeError(
                    "KORAIL_ID 와 KORAIL_PW 환경변수를 설정해야 합니다."
                )
            try:
                _client = Korail(korail_id, korail_pw)
            except KorailError:
                _client = None
                raise
        return _client


def _reset_client() -> None:
    global _client
    with _lock:
        _client = None


def _parse_date(value: str) -> str:
    """'2026-12-25' 또는 '20261225' -> '20261225'"""
    digits = re.sub(r"[-./\s]", "", value)
    if not re.fullmatch(r"\d{8}", digits):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    datetime.strptime(digits, "%Y%m%d")  # 잘못된 날짜면 ValueError
    return digits


def _parse_time(value: str) -> str:
    """'09:30', '0930', '093000' -> '093000'"""
    digits = value.replace(":", "").strip()
    if not re.fullmatch(r"\d{4}(\d{2})?", digits):
        raise ValueError("시간은 HH:MM 형식이어야 합니다.")
    if len(digits) == 4:
        digits += "00"
    datetime.strptime(digits, "%H%M%S")
    return digits


def _fmt_time(hhmmss: str | None) -> str | None:
    if not hhmmss or len(hhmmss) < 4:
        return hhmmss
    return f"{hhmmss[:2]}:{hhmmss[2:4]}"


def _passengers(adults: int, children: int) -> list[Any]:
    if adults < 0 or children < 0 or adults + children < 1:
        raise ValueError("승객은 1명 이상이어야 합니다.")
    people: list[Any] = []
    if adults:
        people.append(AdultPassenger(adults))
    if children:
        people.append(ChildPassenger(children))
    return people


def _train_dict(train: Any, index: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "train_type": train.train_type_name,
        "train_no": train.train_no,
        "dep_station": train.dep_name,
        "dep_date": train.dep_date,
        "dep_time": _fmt_time(train.dep_time),
        "arr_station": train.arr_name,
        "arr_time": _fmt_time(train.arr_time),
        "general_seat": _SEAT_STATUS.get(train.general_seat, "unknown"),
        "special_seat": _SEAT_STATUS.get(train.special_seat, "unknown"),
        "has_seat": train.has_seat(),
        "waiting_list_available": train.has_waiting_list(),
    }
    if index is not None:
        data = {"index": index, **data}
    return data


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, NeedToLoginError):
        _reset_client()  # 다음 호출에서 다시 로그인
    return {"error": type(exc).__name__, "message": str(exc)}


def _find_search(search_id: str) -> dict[str, Any]:
    entry = _searches.get(search_id)
    if entry is None:
        raise ValueError(
            "search_id 를 찾을 수 없습니다. search_trains 를 다시 호출하세요."
        )
    return entry


# ---------------------------------------------------------------- tools

@mcp.tool()
def search_trains(
    dep: str,
    arr: str,
    date: str,
    time: str = "00:00",
    adults: int = 1,
    children: int = 0,
    train_type: str = "all",
    include_sold_out: bool = False,
) -> dict[str, Any]:
    """출발역/도착역/날짜/시간으로 코레일 열차를 조회합니다.

    Args:
        dep: 출발역 이름 (한글, 예: 서울, 부산, 대전)
        arr: 도착역 이름 (한글)
        date: 출발 날짜 (YYYY-MM-DD)
        time: 이 시각 이후 열차부터 조회 (HH:MM, 기본 00:00)
        adults: 성인 인원 수
        children: 어린이 인원 수
        train_type: all | ktx | saemaeul | mugunghwa
        include_sold_out: True면 매진 열차도 포함

    Returns:
        search_id 와 열차 목록. 예약하려면 search_id 와 index 를 reserve_train 에 넘기세요.
    """
    try:
        day = _parse_date(date)
        hhmmss = _parse_time(time)
        if train_type not in _TRAIN_TYPES:
            raise ValueError(f"train_type 은 {', '.join(_TRAIN_TYPES)} 중 하나여야 합니다.")
        people = _passengers(adults, children)

        client = _get_client()
        try:
            trains = client.search_train(
                dep,
                arr,
                date=day,
                time=hhmmss,
                train_type=_TRAIN_TYPES[train_type],
                passengers=people,
                include_no_seats=include_sold_out,
            )
        except NoResultsError:
            trains = []

        search_id = uuid.uuid4().hex[:12]
        with _lock:
            if len(_searches) >= _MAX_CACHED_SEARCHES:
                _searches.pop(next(iter(_searches)))
            _searches[search_id] = {
                "trains": list(trains),
                "adults": adults,
                "children": children,
            }

        return {
            "search_id": search_id,
            "count": len(trains),
            "trains": [_train_dict(t, i) for i, t in enumerate(trains)],
        }
    except (KorailError, ValueError, RuntimeError) as exc:
        return _error(exc)


@mcp.tool()
def reserve_train(
    search_id: str,
    index: int,
    confirm: bool = False,
    seat_preference: str = "general_first",
) -> dict[str, Any]:
    """search_trains 결과 중 한 열차를 예약합니다. 결제는 별도로 해야 합니다.

    Args:
        search_id: search_trains 가 반환한 search_id
        index: search_trains 결과의 열차 index
        confirm: 반드시 true 여야 예약됩니다. 사용자가 열차 정보를 확인하고 승인한 뒤에만 true 로 설정하세요.
        seat_preference: general_first | general_only | special_first | special_only

    Returns:
        예약번호(rsv_id), 금액, 구입기한. 구입기한 안에 코레일 앱/홈페이지에서 결제해야 합니다.
    """
    if not confirm:
        return {
            "status": "not_booked",
            "message": "confirm=true 가 없어 예약하지 않았습니다. 열차 정보를 사용자에게 확인받은 뒤 다시 호출하세요.",
        }
    try:
        if seat_preference not in _OPTIONS:
            raise ValueError(f"seat_preference 는 {', '.join(_OPTIONS)} 중 하나여야 합니다.")
        entry = _find_search(search_id)
        trains = entry["trains"]
        if not 0 <= index < len(trains):
            raise ValueError(f"index 는 0 ~ {len(trains) - 1} 사이여야 합니다.")

        client = _get_client()
        reservation = client.reserve(
            trains[index],
            passengers=_passengers(entry["adults"], entry["children"]),
            option=_OPTIONS[seat_preference],
            try_waiting=False,
        )
        buy_date = reservation.buy_limit_date
        buy_time = _fmt_time(reservation.buy_limit_time)
        return {
            "status": "reserved",
            "rsv_id": reservation.rsv_id,
            "price": reservation.price,
            "seat_count": reservation.seat_no_count,
            "buy_limit": f"{buy_date} {buy_time}" if buy_date else None,
            "message": "예약되었습니다. 구입기한 안에 코레일 앱/홈페이지에서 결제하세요.",
            **_train_dict(reservation),
        }
    except (KorailError, ValueError, RuntimeError) as exc:
        return _error(exc)


@mcp.tool()
def list_reservations() -> dict[str, Any]:
    """현재 계정의 예약 내역(미결제 포함)을 조회합니다."""
    try:
        client = _get_client()
        try:
            reservations = client.reservations()
        except NoResultsError:
            reservations = []
        return {
            "count": len(reservations),
            "reservations": [
                {
                    "rsv_id": r.rsv_id,
                    "price": r.price,
                    "buy_limit": f"{r.buy_limit_date} {_fmt_time(r.buy_limit_time)}",
                    **_train_dict(r),
                }
                for r in reservations
            ],
        }
    except (KorailError, RuntimeError) as exc:
        return _error(exc)


@mcp.tool()
def cancel_reservation(rsv_id: str, confirm: bool = False) -> dict[str, Any]:
    """예약번호로 예약을 취소합니다.

    Args:
        rsv_id: list_reservations 에서 확인한 예약번호
        confirm: 반드시 true 여야 취소됩니다. 사용자 승인 후에만 true 로 설정하세요.
    """
    if not confirm:
        return {
            "status": "not_cancelled",
            "message": "confirm=true 가 없어 취소하지 않았습니다.",
        }
    try:
        client = _get_client()
        try:
            reservations = client.reservations()
        except NoResultsError:
            reservations = []
        target = next((r for r in reservations if r.rsv_id == rsv_id), None)
        if target is None:
            raise ValueError(f"예약번호 {rsv_id} 를 찾을 수 없습니다.")
        client.cancel(target)
        return {"status": "cancelled", "rsv_id": rsv_id}
    except (KorailError, ValueError, RuntimeError) as exc:
        return _error(exc)


@mcp.tool()
def list_tickets() -> dict[str, Any]:
    """결제가 완료된 승차권 목록을 조회합니다."""
    try:
        client = _get_client()
        try:
            tickets = client.tickets()
        except NoResultsError:
            tickets = []
        return {
            "count": len(tickets),
            "tickets": [
                {"ticket_no": t.get_ticket_no(), **_train_dict(t)} for t in tickets
            ],
        }
    except (KorailError, RuntimeError) as exc:
        return _error(exc)


if __name__ == "__main__":
    mcp.run()
