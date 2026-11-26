# -*- coding: utf-8 -*-
"""계명대학교 수시모집 경쟁률 수집·파싱 모듈.

유웨이(uway)의 실시간 경쟁률 페이지에서 전형별·학과별 경쟁률을 읽어온다.

    from kmu_ratio import load
    snap = load()                     # 네트워크에서 가져오기 (캐시 사용)
    snap.departments()                # 모집단위 목록
    snap.by_department('의예과')       # 그 학과의 전형별 경쟁률

원본 페이지
    목록  https://m.uway.com/power/   (여기서 대학별 경쟁률 링크를 찾는다)
    상세  http://ratio.uwayapply.com/<토큰>

상세 페이지 토큰은 모집 연도마다 바뀌므로, 기본적으로 목록 페이지에서
자동으로 찾아낸다(discover_ratio_url). 찾지 못하면 RATIO_URL_FALLBACK 을 쓴다.

CLI:
    python kmu_ratio.py                     # 주요학과 경쟁률 요약
    python kmu_ratio.py 의예과 간호학과       # 특정 학과
    python kmu_ratio.py --summary           # 전형별 총괄
    python kmu_ratio.py --csv out.csv       # 전체 데이터 CSV 저장
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import lxml.html
import requests

UNIVERSITY = "계명대학교"

# 대구·경북 4년제 수시 목록 (o_loc=12:11). 여기서 대학별 경쟁률 링크를 찾는다.
LIST_URL = ("https://m.uway.com/power/?v_mode=case_view&o_gb2=4&o_gb=4&R_SearchText="
            "&term=on&o_term=2&o_loc=12%3A11&status=open%3Atoday_close%3Aschedule%3Aclose")

# 목록 페이지에서 링크를 찾지 못했을 때 쓰는 주소 (2027학년도 수시).
RATIO_URL_FALLBACK = "http://ratio.uwayapply.com/Sl5KOk05SmYlJjomSjdmVGY="

CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_TTL = timedelta(minutes=10)      # 원본이 10분 단위로 갱신된다
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 항상 '주요학과'에 넣을 학과 (데이터에 있을 때만).
ALWAYS_MAJOR = ["의예과", "약학부(6년제)", "혁신신약학과", "간호학과"]


class FetchError(RuntimeError):
    """네트워크 또는 페이지 구조 문제."""


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------
@dataclass
class Row:
    """전형 × 모집단위 하나의 경쟁률."""
    track: str          # 전형 (예: '정원내 학생부교과(일반전형)')
    quota_type: str     # 정원내 / 정원외
    college: str        # 대학 (예: '공과대학')
    dept: str           # 모집단위 (예: '컴퓨터공학과 [교직]')
    quota: int          # 모집인원
    applicants: int     # 지원인원
    ratio: float        # 경쟁률

    @property
    def dept_key(self) -> str:
        """'[교직]' 같은 꼬리표를 뗀 학과명 (묶음용)."""
        return normalize_dept(self.dept)

    @property
    def qualifier(self) -> str:
        """대괄호 안의 꼬리표.

        같은 학과가 여러 줄로 나뉘는 경우가 있다.
        예: '의예과[경상북도 포항권]' -> '경상북도 포항권'. '[교직]' 표시도 여기로.
        """
        m = re.findall(r"\[([^\]]*)\]", self.dept)
        # '[교직]'은 교직과정 표시일 뿐 모집단위를 나누는 값이 아니므로 뺀다.
        parts = [x.strip() for x in m if x.strip() and x.strip() != "교직"]
        return " / ".join(parts)

    @property
    def label(self) -> str:
        """표에 쓸 전형 이름 (권역 등으로 나뉘면 그 꼬리표를 덧붙인다)."""
        q = self.qualifier
        return "%s · %s" % (self.track, q) if q else self.track


@dataclass
class Group:
    """표시·집계용 '한 묶음'. 여러 행이 모집인원을 공유하면 하나로 합친 것.

    원본은 일부 전형에서 모집인원을 묶음 단위로 적고 그 값을 각 행에 되풀이한다.
    예) 실기/실적(예체능전형)의 관현악과는 악기별로 행이 나뉘지만
        '모집 13명 / 경쟁률 0.08'은 관현악과 전체 값이라 행마다 같다.
    이런 행을 그대로 더하면 모집인원이 부풀려지므로 묶어서 한 번만 센다.
    (지원인원은 행마다 실제값이라 언제나 합산한다.)
    """
    track: str
    quota_type: str
    college: str
    dept: str            # 정규화된 학과명
    detail: str          # 합쳐진 꼬리표 (예: '바이올린 / 더블베이스 / 하프')
    quota: int
    applicants: int
    ratio: float
    n_rows: int = 1
    merged: bool = False

    @property
    def label(self) -> str:
        return "%s · %s" % (self.track, self.detail) if self.detail else self.track


@dataclass
class SummaryRow:
    """전형별 총괄 한 줄."""
    quota_type: str
    track: str
    quota: int
    applicants: int
    ratio: float


@dataclass
class Snapshot:
    """한 시점의 경쟁률 전체."""
    university: str
    title: str                      # '2027학년도 수시모집 경쟁률'
    as_of: str                      # '2026년 09월 08일 08시 00분 기준'
    source_url: str
    fetched_at: str
    summary: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    _unreliable: set = None      # unreliable_tracks() 캐시
    _totals: dict = None         # _all_totals() 캐시

    # -- 조회 -------------------------------------------------------------
    def _all_totals(self) -> dict:
        """{학과: (모집, 지원, 경쟁률)} 캐시. 정렬·목록에 쓴다.

        묶음(Group) 기준이라 화면에 보이는 값과 정렬 순서가 어긋나지 않는다.
        """
        if getattr(self, "_totals", None) is None:
            keys = {r.dept_key for r in self.rows}
            self._totals = {d: self.dept_total(d) for d in keys}
        return self._totals

    def departments(self, major_only: bool = False, top_n: int = 30) -> list:
        """모집단위 목록 (모집인원 합계 내림차순).

        major_only=True 면 모집인원이 큰 상위 top_n 개와 ALWAYS_MAJOR 만 남긴다.
        """
        total = self._all_totals()
        ordered = sorted(total, key=lambda d: (-total[d][0], d))
        if not major_only:
            return ordered
        major = set(ordered[:top_n])
        major |= {d for d in ordered if d in ALWAYS_MAJOR}
        return [d for d in ordered if d in major]

    def by_department(self, dept: str) -> list:
        """해당 학과의 전형별 경쟁률 (모집인원 내림차순)."""
        key = normalize_dept(dept)
        hits = [r for r in self.rows if r.dept_key == key]
        return sorted(hits, key=lambda r: (-r.quota, r.track))

    def groups(self, dept: str) -> list:
        """해당 학과의 전형별 묶음 (모집인원 중복 표기를 정리한 것).

        표시와 집계 모두 이 결과를 쓴다. 자세한 판정 근거는 group_rows 참고.
        """
        return group_rows(self.by_department(dept))

    def dept_total(self, dept: str) -> tuple:
        """(모집인원 합, 지원인원 합, 통합 경쟁률).

        모집인원을 공유하는 행은 한 번만 센다 (Group 참고).
        """
        gs = self.groups(dept)
        q = sum(g.quota for g in gs)
        a = sum(g.applicants for g in gs)
        return q, a, (a / q if q else 0.0)

    def reconciliation(self) -> dict:
        """전형별로 '학과행 묶음 합'과 '대학 발표 소계'를 대조한다.

        반환 {전형(접두어 제외): (소계모집, 집계모집)} 중 어긋나는 것만.

        원본이 일부 전형에서 모집인원을 학과별로 배분하지 않고 적기 때문에
        완전히 맞출 수는 없다. 예를 들어 학생부교과(만학도전형)은 학과마다
        7명으로 적혀 있으나 소계는 70명이라 어떤 묶음 규칙으로도 재현되지 않는다.
        이런 전형이 섞인 학과의 '통합 모집인원'은 참고값으로만 봐야 한다.
        """
        agg = {}
        for d in self.departments():
            for g in self.groups(d):
                k = re.sub(r"^(정원내|정원외)\s*", "", g.track).strip()
                agg[k] = agg.get(k, 0) + g.quota
        out = {}
        for s in self.summary:
            k = re.sub(r"^(정원내|정원외)\s*", "", s.track).strip()
            got = agg.get(k, 0)
            if got != s.quota:
                out[k] = (s.quota, got)
        return out

    def unreliable_tracks(self) -> set:
        """모집인원 합계를 믿기 어려운 전형 이름(접두어 제외) 집합."""
        if getattr(self, "_unreliable", None) is None:
            self._unreliable = set(self.reconciliation())
        return self._unreliable

    def dept_has_caveat(self, dept: str) -> bool:
        """이 학과의 통합 모집인원에 위 전형이 섞여 있는가."""
        bad = self.unreliable_tracks()
        for g in self.groups(dept):
            if re.sub(r"^(정원내|정원외)\s*", "", g.track).strip() in bad:
                return True
        return False

    def colleges(self) -> list:
        seen = {}
        for r in self.rows:
            seen.setdefault(r.college, None)
        return list(seen)

    def college_of(self, dept: str) -> str:
        for r in self.by_department(dept):
            if r.college:
                return r.college
        return ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def group_rows(rows: list, tol: float = 0.011) -> list:
    """모집인원을 공유하는 행을 묶는다.

    같은 (전형, 모집인원, 경쟁률)인 행이 둘 이상일 때, 그 행들의 지원인원을
    모두 더한 값이 표기된 경쟁률과 맞으면 '한 묶음을 되풀이 표기한 것'으로 보고
    모집인원을 한 번만 센다.

    이 검사가 필요한 이유: 우연히 (모집인원, 경쟁률)이 같은 별개 모집단위도 있다.
    예) 의예과[경상북도 영주권] 1명/3명/3.00 과 의예과[상주권] 1명/3명/3.00 은
        각각 독립이며(합 6/1 = 6.00 ≠ 3.00) 실제로 둘 다 세어야 한다.
        반면 관현악과[바이올린/더블베이스/하프]는 13명/0.08 이 공통이고
        지원인원 합 1명이 1/13 = 0.08 로 맞으므로 하나로 묶는다.
    """
    buckets: dict = {}
    order: list = []
    for r in rows:
        key = (r.track, r.quota, round(r.ratio, 2))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)

    out = []
    for key in order:
        rs = buckets[key]
        track, quota, ratio = key
        applied = sum(x.applicants for x in rs)
        # (1) 합쳐야 표기된 경쟁률이 설명되고,
        # (2) 각 행 단독으로는 그 경쟁률이 설명되지 않아야 '되풀이 표기'다.
        #     둘 다 지원 0명이라 우연히 맞아떨어지는 별개 모집단위를 지키는 조건.
        sum_ok = len(rs) > 1 and quota > 0 and abs(applied / quota - ratio) <= tol
        each_ok = quota > 0 and all(abs(x.applicants / quota - ratio) <= tol for x in rs)
        shared = sum_ok and not each_ok
        if shared:
            details = [x.qualifier for x in rs if x.qualifier]
            out.append(Group(track=track, quota_type=rs[0].quota_type,
                             college=rs[0].college, dept=rs[0].dept_key,
                             detail=" / ".join(details), quota=quota,
                             applicants=applied, ratio=ratio,
                             n_rows=len(rs), merged=True))
        else:
            for x in rs:
                q = x.qualifier
                out.append(Group(track=x.track, quota_type=x.quota_type,
                                 college=x.college, dept=x.dept_key,
                                 detail=q,
                                 quota=x.quota, applicants=x.applicants,
                                 ratio=x.ratio))
    return sorted(out, key=lambda g: (-g.quota, g.track))


def normalize_dept(name: str) -> str:
    """'컴퓨터공학과 [교직]' -> '컴퓨터공학과'. 공백도 정리."""
    s = re.sub(r"\[[^\]]*\]", "", str(name))
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
def _get(url: str, timeout: float = 20.0) -> str:
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise FetchError("페이지를 가져오지 못했습니다: %s (%s)" % (url, e)) from e
    # 유웨이 페이지는 euc-kr 이지만 헤더가 부정확한 경우가 있다.
    for enc in (r.apparent_encoding, "euc-kr", "cp949", "utf-8"):
        if not enc:
            continue
        try:
            return r.content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return r.content.decode("euc-kr", "replace")


def discover_ratio_url(university: str = UNIVERSITY, list_url: str = LIST_URL) -> str:
    """유웨이 목록 페이지에서 해당 대학의 경쟁률 상세 주소를 찾는다."""
    html = _get(list_url)
    pattern = r"<li>(?:(?!</li>).)*?" + re.escape(university) + r".*?</li>"
    for block in re.findall(pattern, html, re.S):
        m = re.search(r"_checkUrlOpen\('([^']+)'\)", block)
        if m:
            return m.group(1)
    raise FetchError("목록 페이지에서 %s 의 경쟁률 링크를 찾지 못했습니다." % university)


# ---------------------------------------------------------------------------
# 파싱
# ---------------------------------------------------------------------------
def _expand(table) -> list:
    """rowspan/colspan 을 펼쳐 [행][열] 문자열 격자로 만든다.

    이 페이지는 '대학'/'구분' 열에 rowspan 이 걸려 있어 그대로 읽으면 열이 밀린다.
    """
    grid: list = []
    pending: dict = {}          # (row, col) -> 값
    for i, tr in enumerate(table.xpath(".//tr")):
        while len(grid) <= i:
            grid.append({})
        col = 0
        for cell in tr.xpath("./th|./td"):
            while (i, col) in pending:
                grid[i][col] = pending.pop((i, col))
                col += 1
            text = re.sub(r"\s+", " ", cell.text_content() or "").strip()
            rs = int(cell.get("rowspan") or 1)
            cs = int(cell.get("colspan") or 1)
            for dc in range(cs):
                grid[i][col + dc] = text
                for dr in range(1, rs):
                    pending[(i + dr, col + dc)] = text
            col += cs
        while (i, col) in pending:
            grid[i][col] = pending.pop((i, col))
            col += 1
    width = max((max(r) + 1 if r else 0) for r in grid) if grid else 0
    return [[r.get(c, "") for c in range(width)] for r in grid]


def _col_index(header_row: list) -> dict:
    """헤더 문자열 -> 열 번호. colspan 으로 같은 헤더가 반복되면 첫 열을 쓴다."""
    idx = {}
    for i, h in enumerate(header_row):
        h = (h or "").strip()
        if h and h not in idx:
            idx[h] = i
    return idx


def _pick(idx: dict, *names) -> int | None:
    """헤더 이름으로 열 번호 찾기 (정확 일치 우선, 없으면 부분 일치)."""
    for n in names:
        if n in idx:
            return idx[n]
    for n in names:
        for k, v in idx.items():
            if n in k:
                return v
    return None


def _cell(row: list, i) -> str:
    return row[i] if i is not None and i < len(row) else ""


def _int(s: str) -> int:
    s = re.sub(r"[^\d-]", "", str(s))
    return int(s) if s not in ("", "-") else 0


def _ratio(s: str) -> float:
    """'0.72 : 1' -> 0.72 ('-' 등은 0.0)."""
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(s))
    return float(m.group(1)) if m else 0.0


def parse(html: str, source_url: str = "") -> Snapshot:
    """경쟁률 상세 페이지 HTML -> Snapshot."""
    doc = lxml.html.fromstring(html)
    text = re.sub(r"\s+", " ", doc.text_content())

    m = re.search(r"(\d{4}학년도)\s*(\S*모집)?\s*경쟁률", text)
    title = " ".join(x for x in (m.group(1), m.group(2) or "", "경쟁률") if x) if m else "경쟁률"
    m = re.search(r"(\d{4}년\s*\d{2}월\s*\d{2}일\s*\d{2}시\s*\d{2}분\s*기준)", text)
    as_of = m.group(1) if m else ""

    tables = doc.xpath("//table")
    if not tables:
        raise FetchError("경쟁률 표를 찾지 못했습니다. 페이지 구조가 바뀌었을 수 있습니다.")

    # --- 첫 표: 전형별 총괄 (구분, 전형, 총모집인원, 지원인원, 경쟁률) -------
    # 열 위치는 헤더로 찾는다. '모집단위' 열에 colspan=2 가 걸려 있어 고정
    # 인덱스로 읽으면 한 칸씩 밀린다.
    summary = []
    grid = _expand(tables[0])
    if grid:
        ix = _col_index(grid[0])
        c_gb, c_tr = _pick(ix, "구분"), _pick(ix, "전형")
        c_q = _pick(ix, "총모집인원", "모집인원")
        c_a, c_r = _pick(ix, "지원인원"), _pick(ix, "경쟁률")
        for r in grid[1:]:
            if any("소계" in c or "합계" in c for c in r):
                continue
            track = _cell(r, c_tr)
            if not track:
                continue
            summary.append(SummaryRow(quota_type=_cell(r, c_gb), track=track,
                                      quota=_int(_cell(r, c_q)),
                                      applicants=_int(_cell(r, c_a)),
                                      ratio=_ratio(_cell(r, c_r))))

    # --- 나머지 표: 전형마다 (대학, 모집단위, 모집인원, 지원인원, 경쟁률) ----
    rows = []
    for t in tables[1:]:
        cap = t.xpath(".//caption/text()")
        track = re.sub(r"\s+", " ", cap[0]).strip() if cap else ""
        if not track:
            continue
        quota_type = "정원외" if track.startswith("정원외") else "정원내"
        grid = _expand(t)
        if not grid:
            continue
        ix = _col_index(grid[0])
        c_col, c_dept = _pick(ix, "대학"), _pick(ix, "모집단위")
        c_q, c_a = _pick(ix, "모집인원"), _pick(ix, "지원인원")
        c_r = _pick(ix, "경쟁률")
        for r in grid[1:]:
            if any("소계" in c or "합계" in c for c in r):
                continue
            dept = _cell(r, c_dept)
            if not dept or dept.isdigit():
                continue
            rows.append(Row(track=track, quota_type=quota_type,
                            college=_cell(r, c_col), dept=dept,
                            quota=_int(_cell(r, c_q)),
                            applicants=_int(_cell(r, c_a)),
                            ratio=_ratio(_cell(r, c_r))))

    if not rows:
        raise FetchError("학과별 경쟁률 행을 하나도 읽지 못했습니다.")

    return Snapshot(university=UNIVERSITY, title=title, as_of=as_of,
                    source_url=source_url,
                    fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    summary=summary, rows=rows)


# ---------------------------------------------------------------------------
# 캐시 + 공개 API
# ---------------------------------------------------------------------------
def _cache_path() -> Path:
    return CACHE_DIR / "kmu_ratio.html"


def _cache_age() -> timedelta | None:
    p = _cache_path()
    if not p.exists():
        return None
    return timedelta(seconds=time.time() - p.stat().st_mtime)


def load(force: bool = False, url: str = None, use_cache: bool = True) -> Snapshot:
    """경쟁률 스냅샷을 얻는다.

    force=True 면 캐시를 무시하고 새로 받는다. 네트워크가 끊기면 캐시가 있는
    한 캐시를 쓴다(오래되었더라도).
    """
    p = _cache_path()
    age = _cache_age()
    if use_cache and not force and age is not None and age < CACHE_TTL:
        return parse(p.read_text(encoding="utf-8"), source_url="(캐시) " + str(p))

    try:
        ratio_url = url or discover_ratio_url()
    except FetchError:
        ratio_url = url or RATIO_URL_FALLBACK

    try:
        html = _get(ratio_url)
        snap = parse(html, source_url=ratio_url)
    except FetchError:
        if use_cache and p.exists():
            snap = parse(p.read_text(encoding="utf-8"), source_url="(캐시) " + str(p))
            snap.fetched_at += "  ※ 네트워크 실패, 캐시 사용"
            return snap
        raise

    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        p.write_text(html, encoding="utf-8")
    return snap


def to_csv(snap: Snapshot, path) -> Path:
    """전체 행을 CSV(UTF-8 BOM, 엑셀에서 바로 열림)로 저장."""
    import csv
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["구분", "전형", "대학", "모집단위", "모집인원", "지원인원", "경쟁률"])
        for r in snap.rows:
            w.writerow([r.quota_type, r.track, r.college, r.dept,
                        r.quota, r.applicants, "%.2f" % r.ratio])
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _pad(s: str, width: int) -> str:
    """한글(전각)을 2칸으로 세어 폭을 맞춘다. 콘솔 표가 어긋나지 않게."""
    import unicodedata
    s = str(s)
    out, w = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out) + " " * (width - w)


def _print_dept(snap: Snapshot, dept: str) -> None:
    hits = snap.groups(dept)
    if not hits:
        print("  '%s' : 해당 모집단위를 찾지 못했습니다." % dept)
        return
    q, a, ratio = snap.dept_total(dept)
    college = snap.college_of(dept)
    print("\n■ %s  (%s)   통합 %d명 모집 / %d명 지원 / %.2f : 1"
          % (normalize_dept(dept), college, q, a, ratio))
    print("  %s %6s %6s %9s" % (_pad("전형", 54), "모집", "지원", "경쟁률"))
    print("  " + "-" * 78)
    for r in hits:
        print("  %s %6d %6d %7.2f:1"
              % (_pad(r.label, 54), r.quota, r.applicants, r.ratio))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="계명대학교 수시모집 경쟁률 조회")
    ap.add_argument("dept", nargs="*", help="학과명 (생략하면 주요학과 전체)")
    ap.add_argument("--summary", action="store_true", help="전형별 총괄만 출력")
    ap.add_argument("--list", action="store_true", help="모집단위 목록 출력")
    ap.add_argument("--all", action="store_true", help="주요학과 대신 전체 학과")
    ap.add_argument("--top", type=int, default=20, help="주요학과 개수 (기본 20)")
    ap.add_argument("--csv", metavar="FILE", help="전체 데이터를 CSV 로 저장")
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 새로 받기")
    a = ap.parse_args(argv)

    try:
        snap = load(force=a.refresh)
    except FetchError as e:
        print("오류: %s" % e, file=sys.stderr)
        return 1

    print("=" * 62)
    print("  %s %s" % (snap.university, snap.title))
    print("  %s   (조회 %s)" % (snap.as_of, snap.fetched_at))
    print("=" * 62)

    if a.csv:
        print("CSV 저장: %s" % to_csv(snap, a.csv))
        return 0

    if a.list:
        for d in snap.departments(major_only=not a.all, top_n=a.top):
            q, ap_, ratio = snap.dept_total(d)
            print("  %s %4d명 모집 / %5d명 지원 / %5.2f : 1"
                  % (_pad(d, 32), q, ap_, ratio))
        return 0

    if a.summary or (not a.dept and False):
        print("\n[전형별 총괄]")
        print("  %s %s %7s %7s %9s"
              % (_pad("구분", 6), _pad("전형", 40), "모집", "지원", "경쟁률"))
        print("  " + "-" * 72)
        for row in snap.summary:
            print("  %s %s %7d %7d %7.2f:1"
                  % (_pad(row.quota_type, 6), _pad(row.track, 40),
                     row.quota, row.applicants, row.ratio))
        return 0

    targets = a.dept or snap.departments(major_only=not a.all, top_n=a.top)
    shown_caveat = False
    for d in targets:
        _print_dept(snap, d)
        shown_caveat = shown_caveat or snap.dept_has_caveat(d)
    if shown_caveat:
        bad = ", ".join(sorted(snap.unreliable_tracks()))
        print("\n※ 다음 전형은 원본이 모집인원을 학과별로 배분해 적지 않아, 통합 "
              "모집인원이 대학 발표 소계와 다를 수 있습니다:\n   %s" % bad)
        print("   전형별 경쟁률(위 표의 각 줄)은 원본 그대로이며 정확합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
