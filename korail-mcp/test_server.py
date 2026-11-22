"""오프라인 테스트: 실제 코레일에 접속하지 않고 가짜 클라이언트로 동작을 검증합니다."""

from types import SimpleNamespace

import pytest

import server
from korail2 import NoResultsError, ReserveOption


def make_train(no: str, dep_time: str = "093000", general: str = "11", special: str = "00"):
    return SimpleNamespace(
        train_type_name="KTX",
        train_no=no,
        dep_name="서울",
        dep_date="20261225",
        dep_time=dep_time,
        arr_name="부산",
        arr_time="115000",
        general_seat=general,
        special_seat=special,
        has_seat=lambda: general == "11" or special == "11",
        has_waiting_list=lambda: False,
    )


class FakeKorail:
    def __init__(self, trains=None, raise_no_results=False):
        self.trains = trains if trains is not None else [make_train("101"), make_train("103", general="13")]
        self.raise_no_results = raise_no_results
        self.search_calls = []
        self.reserve_calls = []
        self.cancel_calls = []
        self.reservations_list = []

    def search_train(self, dep, arr, **kwargs):
        self.search_calls.append((dep, arr, kwargs))
        if self.raise_no_results:
            raise NoResultsError()
        return list(self.trains)

    def reserve(self, train, passengers=None, option=None, try_waiting=False):
        self.reserve_calls.append((train, passengers, option))
        return SimpleNamespace(
            rsv_id="PNR123",
            price=59800,
            seat_no_count=1,
            buy_limit_date="20261220",
            buy_limit_time="235900",
            **{k: v for k, v in vars(train).items() if k != "has_seat" and k != "has_waiting_list"},
            has_seat=train.has_seat,
            has_waiting_list=train.has_waiting_list,
        )

    def reservations(self):
        return self.reservations_list

    def cancel(self, rsv):
        self.cancel_calls.append(rsv)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    server._searches.clear()
    server._client = None
    monkeypatch.setenv("KORAIL_ID", "test-id")
    monkeypatch.setenv("KORAIL_PW", "test-pw")
    yield
    server._searches.clear()
    server._client = None


def use_fake(monkeypatch, fake):
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    return fake


# ---------------------------------------------------------------- parsing

def test_parse_date_accepts_dashes_and_plain():
    assert server._parse_date("2026-12-25") == "20261225"
    assert server._parse_date("20261225") == "20261225"


def test_parse_date_rejects_garbage():
    with pytest.raises(ValueError):
        server._parse_date("2026-02-30")
    with pytest.raises(ValueError):
        server._parse_date("내일")


def test_parse_time_pads_seconds():
    assert server._parse_time("09:30") == "093000"
    assert server._parse_time("093000") == "093000"
    with pytest.raises(ValueError):
        server._parse_time("25:99")


# ---------------------------------------------------------------- search

def test_search_returns_indexed_trains_and_caches(monkeypatch):
    fake = use_fake(monkeypatch, FakeKorail())
    result = server.search_trains("서울", "부산", "2026-12-25", "09:00")

    assert result["count"] == 2
    assert [t["index"] for t in result["trains"]] == [0, 1]
    assert result["trains"][0]["dep_time"] == "09:30"
    assert result["trains"][0]["general_seat"] == "available"
    assert result["trains"][1]["general_seat"] == "sold_out"
    assert result["search_id"] in server._searches

    dep, arr, kwargs = fake.search_calls[0]
    assert (dep, arr) == ("서울", "부산")
    assert kwargs["date"] == "20261225"
    assert kwargs["time"] == "090000"


def test_search_no_results_returns_empty(monkeypatch):
    use_fake(monkeypatch, FakeKorail(raise_no_results=True))
    result = server.search_trains("서울", "부산", "2026-12-25")
    assert result["count"] == 0
    assert result["trains"] == []


def test_search_bad_date_returns_error_not_exception(monkeypatch):
    use_fake(monkeypatch, FakeKorail())
    result = server.search_trains("서울", "부산", "not-a-date")
    assert result["error"] == "ValueError"


def test_missing_credentials_reports_error(monkeypatch):
    monkeypatch.delenv("KORAIL_ID")
    monkeypatch.delenv("KORAIL_PW")
    result = server.search_trains("서울", "부산", "2026-12-25")
    assert "KORAIL_ID" in result["error"] or "KORAIL_ID" in result["message"]


# ---------------------------------------------------------------- reserve

def test_reserve_without_confirm_does_not_book(monkeypatch):
    fake = use_fake(monkeypatch, FakeKorail())
    search = server.search_trains("서울", "부산", "2026-12-25")

    result = server.reserve_train(search["search_id"], 0)

    assert result["status"] == "not_booked"
    assert fake.reserve_calls == []


def test_reserve_with_confirm_books_selected_train(monkeypatch):
    fake = use_fake(monkeypatch, FakeKorail())
    search = server.search_trains("서울", "부산", "2026-12-25", adults=2)

    result = server.reserve_train(search["search_id"], 0, confirm=True, seat_preference="general_only")

    assert result["status"] == "reserved"
    assert result["rsv_id"] == "PNR123"
    assert result["buy_limit"] == "20261220 23:59"
    train, passengers, option = fake.reserve_calls[0]
    assert train.train_no == "101"
    assert option == ReserveOption.GENERAL_ONLY
    assert passengers[0].count == 2


def test_reserve_rejects_bad_index_and_unknown_search(monkeypatch):
    use_fake(monkeypatch, FakeKorail())
    search = server.search_trains("서울", "부산", "2026-12-25")

    assert server.reserve_train(search["search_id"], 99, confirm=True)["error"] == "ValueError"
    assert server.reserve_train("nope", 0, confirm=True)["error"] == "ValueError"


def test_reserve_rejects_unknown_seat_preference(monkeypatch):
    use_fake(monkeypatch, FakeKorail())
    search = server.search_trains("서울", "부산", "2026-12-25")
    result = server.reserve_train(search["search_id"], 0, confirm=True, seat_preference="bogus")
    assert result["error"] == "ValueError"


# ---------------------------------------------------------------- cancel

def test_cancel_without_confirm_does_nothing(monkeypatch):
    fake = FakeKorail()
    fake.reservations_list = [SimpleNamespace(rsv_id="PNR123")]
    use_fake(monkeypatch, fake)

    result = server.cancel_reservation("PNR123")

    assert result["status"] == "not_cancelled"
    assert fake.cancel_calls == []


def test_cancel_with_confirm_cancels_matching_reservation(monkeypatch):
    fake = FakeKorail()
    target = SimpleNamespace(rsv_id="PNR123")
    fake.reservations_list = [SimpleNamespace(rsv_id="OTHER"), target]
    use_fake(monkeypatch, fake)

    result = server.cancel_reservation("PNR123", confirm=True)

    assert result == {"status": "cancelled", "rsv_id": "PNR123"}
    assert fake.cancel_calls == [target]


def test_cancel_unknown_rsv_id_is_error(monkeypatch):
    fake = use_fake(monkeypatch, FakeKorail())
    result = server.cancel_reservation("missing", confirm=True)
    assert result["error"] == "ValueError"
    assert fake.cancel_calls == []


# ---------------------------------------------------------------- search cache

def test_search_cache_is_bounded(monkeypatch):
    use_fake(monkeypatch, FakeKorail())
    ids = [server.search_trains("서울", "부산", "2026-12-25")["search_id"] for _ in range(server._MAX_CACHED_SEARCHES + 3)]
    assert len(server._searches) == server._MAX_CACHED_SEARCHES
    assert ids[0] not in server._searches
    assert ids[-1] in server._searches
