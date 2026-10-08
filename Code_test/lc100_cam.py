# -*- coding: utf-8 -*-
"""Thorlabs LC100 라인 카메라 데이터 소스 계층.

표시 프로그램(lc100_app.py)이 쓰는 공통 인터페이스(LineSource)와 그 구현들:

    LC100VisaSource 실제 LC100 (VISA 직결 — 64비트에서도 동작, 기본)
    TLLC100Source   실제 LC100 (32비트 Thorlabs LC100 드라이버 DLL 경유)
    WebcamSource    USB 웹캠 한 줄을 읽어 라인스캔처럼 취급 (배선 점검용)
    SimulatedSource 하드웨어 없이 표시부를 검증하기 위한 모의 신호

진단 명령:
    python lc100_cam.py            # 장치/드라이버 설치 상태 점검
    python lc100_cam.py --probe    # LC100 DLL 을 찾아 export 함수 전부 출력
    python lc100_cam.py --demo     # 시뮬레이션 소스로 몇 줄 읽어보기

LC100_Drv 는 32비트로만 배포되므로 64비트 파이썬에서는 DLL 을 못 부른다.
LC100VisaSource 는 그 DLL 이 하는 USB 제어 전송을 VISA 로 직접 보내서
같은 일을 한다 (프로토콜은 설치본의 LC100_Drv.c 에서 옮겨왔다).
"""
from __future__ import annotations

import ctypes as C
import os
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

THORLABS_VID = 0x1313
LC100_PID = 0x80A0
LC100_PIXELS = 2048          # LC100 CCD 화소 수 (사양)

# SPLICCO(LC100 용 소프트웨어) 설치 시 DLL 이 놓이는 경로.
# 매뉴얼 "5.2 LC100 Smart Line Camera" 기준. SPLICCO 드라이버는 32비트다.
DLL_CANDIDATES = [
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\Bin\LC100_Drv_32.dll",
    r"C:\Program Files\IVI Foundation\VISA\WinNT\Bin\LC100_Drv_32.dll",
]
# 설치 후 API 를 확정할 때 볼 파일들 (헤더 + 드라이버 원본 + C 예제)
DOC_CANDIDATES = [
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\include\LC100_Drv.h",
    r"C:\Program Files\IVI Foundation\VISA\WinNT\include\LC100_Drv.h",
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\Thorlabs LC100\LC100_Drv.c",
    r"C:\Program Files\IVI Foundation\VISA\WinNT\Thorlabs LC100\LC100_Drv.c",
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\Thorlabs LC100\Examples\C\sample.c",
]


class CameraError(RuntimeError):
    """장치/드라이버 문제."""


# ---------------------------------------------------------------------------
# PE export 리더 — DLL 이 실제로 내보내는 함수 이름을 그대로 읽는다.
# (pefile 같은 외부 패키지 없이 순수 파이썬으로 처리)
# ---------------------------------------------------------------------------
def pe_exports(path) -> list:
    """DLL(PE 파일)의 export 함수 이름 목록."""
    data = Path(path).read_bytes()
    if data[:2] != b"MZ":
        raise ValueError("PE 파일이 아닙니다: %s" % path)
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("PE 서명을 찾지 못했습니다.")
    coff = e_lfanew + 4
    n_sec = struct.unpack_from("<H", data, coff + 2)[0]
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    # 데이터 디렉터리 위치: PE32 는 +96, PE32+(64비트) 는 +112
    dd = opt + (96 if magic == 0x10B else 112)
    exp_rva = struct.unpack_from("<I", data, dd)[0]
    if not exp_rva:
        return []

    sections = []
    sec = opt + size_opt
    for i in range(n_sec):
        o = sec + i * 40
        vsz, va, rawsz, raw = struct.unpack_from("<IIII", data, o + 8)
        sections.append((va, max(vsz, rawsz), raw))

    def rva2off(rva):
        for va, sz, raw in sections:
            if va <= rva < va + sz:
                return raw + (rva - va)
        return None

    o = rva2off(exp_rva)
    if o is None:
        return []
    n_names = struct.unpack_from("<I", data, o + 24)[0]
    names_rva = struct.unpack_from("<I", data, o + 32)[0]
    no = rva2off(names_rva)
    out = []
    for i in range(n_names):
        so = rva2off(struct.unpack_from("<I", data, no + 4 * i)[0])
        if so is None:
            continue
        end = data.index(b"\0", so)
        out.append(data[so:end].decode("ascii", "replace"))
    return sorted(out)


def find_dll() -> str | None:
    """설치된 TLLC100 DLL 경로를 찾는다."""
    for p in DLL_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 알려진 경로에 없으면 Thorlabs/VISA 설치 폴더만 얕게 훑는다.
    # (Program Files 전체 재귀 탐색은 수 초~수십 초가 걸려 쓰지 않는다)
    roots = []
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            roots += [os.path.join(base, "IVI Foundation"), os.path.join(base, "Thorlabs")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            if cur[len(root):].count(os.sep) >= 4:      # 깊이 제한
                dirs[:] = []
                continue
            for f in files:
                if f.lower().startswith("lc100_drv") and f.lower().endswith(".dll"):
                    return os.path.join(cur, f)
    return None


# ---------------------------------------------------------------------------
# 공통 인터페이스
# ---------------------------------------------------------------------------
class LineSource:
    """한 줄(라인) 데이터를 내주는 소스."""

    name = "base"
    n_pixels = LC100_PIXELS
    max_value = 65535.0        # read_line 이 돌려주는 값의 이론적 최대

    def open(self):
        return self

    def close(self):
        pass

    def read_line(self) -> np.ndarray:
        """길이 n_pixels 의 1차원 배열 한 줄. 실패 시 CameraError."""
        raise NotImplementedError

    # 노출시간 [ms] — 지원하지 않는 소스는 그대로 둔다
    def get_exposure_ms(self) -> float:
        return float("nan")

    def set_exposure_ms(self, ms: float):
        pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------------------
class SimulatedSource(LineSource):
    """하드웨어 없이 표시부를 검증하기 위한 모의 라인 신호.

    분광기에 물린 라인 카메라에서 볼 법한 신호를 흉내낸다:
    넓은 배경 + 몇 개의 피크 + 화소 고정패턴 + 샷 노이즈 + 느린 드리프트.
    """

    name = "시뮬레이션"

    def __init__(self, n_pixels: int = LC100_PIXELS, seed: int = 0):
        self.n_pixels = n_pixels
        self.max_value = 4095.0                 # 12 bit 가정
        self._rng = np.random.default_rng(seed)
        self._t0 = time.time()
        self._t_last = 0.0
        self._exposure = 10.0
        x = np.arange(n_pixels)
        self._bg = 120 + 60 * np.exp(-((x - n_pixels * 0.5) / (n_pixels * 0.42)) ** 2)
        # 화소별 고정 패턴 잡음 (실제 CCD 에도 있다)
        self._fpn = self._rng.normal(1.0, 0.012, n_pixels)
        self._peaks = [(0.18, 900, 6), (0.33, 2600, 9), (0.52, 1500, 4),
                       (0.71, 3300, 12), (0.86, 700, 5)]

    def read_line(self):
        # 실제 카메라는 노출시간만큼 걸려서 한 줄을 내준다. 여기서도 같은 속도로
        # 맞춰 준다. (안 그러면 CPU 속도만큼 무한정 생성해 GUI 를 굶긴다.)
        period = max(self._exposure, 0.5) / 1000.0
        wait = period - (time.time() - self._t_last)
        if wait > 0:
            time.sleep(wait)
        self._t_last = time.time()

        x = np.arange(self.n_pixels)
        t = time.time() - self._t0
        y = self._bg.copy()
        for pos, amp, wid in self._peaks:
            c = pos * self.n_pixels + 6.0 * np.sin(t * 0.35 + pos * 9)   # 느린 드리프트
            a = amp * (0.85 + 0.15 * np.sin(t * 0.8 + pos * 3))
            y += a * np.exp(-0.5 * ((x - c) / wid) ** 2)
        y *= self._fpn
        y *= max(self._exposure, 0.01) / 10.0        # 노출시간 비례
        y = self._rng.poisson(np.clip(y, 0, None)).astype(float)   # 샷 노이즈
        return np.clip(y, 0, self.max_value)

    def get_exposure_ms(self):
        return self._exposure

    def set_exposure_ms(self, ms):
        self._exposure = float(np.clip(ms, 0.01, 1000.0))


# ---------------------------------------------------------------------------
class WebcamSource(LineSource):
    """USB 웹캠의 한 행을 라인으로 사용 (표시 경로를 실제 하드웨어로 점검용).

    LC100 대용이 아니라, 카메라가 없을 때 '실시간 데이터가 화면에 제대로
    흐르는지' 확인하기 위한 것이다.
    """

    name = "웹캠(라인 추출)"

    def __init__(self, index: int = 0, row: float = 0.5):
        self.index = index
        self.row = row
        self.cap = None
        self.max_value = 255.0

    def open(self):
        import cv2
        self.cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            raise CameraError("웹캠(index %d)을 열지 못했습니다." % self.index)
        ok, frm = self.cap.read()
        if not ok or frm is None:
            self.cap.release()
            raise CameraError("웹캠에서 프레임을 읽지 못했습니다.")
        self.n_pixels = frm.shape[1]
        return self

    def read_line(self):
        import cv2
        ok, frm = self.cap.read()
        if not ok or frm is None:
            raise CameraError("웹캠 프레임 읽기 실패")
        if frm.ndim == 3:
            frm = cv2.cvtColor(frm, cv2.COLOR_BGR2GRAY)
        r = int(np.clip(self.row, 0, 1) * (frm.shape[0] - 1))
        return frm[r].astype(float)

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None


# ---------------------------------------------------------------------------
class TLLC100Source(LineSource):
    """실제 LC100 — Thorlabs LC100 계측기 드라이버(LC100_Drv) 경유.

    이름·인자·상수는 추측이 아니라 설치본의 원본 문서에서 확정했다.

        헤더 : ...\\IVI Foundation\\VISA\\WinNT\\include\\LC100_Drv.h
        예제 : ...\\VISA\\WinNT\\Thorlabs LC100\\Examples\\C\\sample.c  (스캔 루프)

    확정된 사항:
      * 호출 규약은 __stdcall (visatype.h 의 _VI_FUNC) → CDLL 이 아니라 WinDLL
      * LC100_getScanData 는 ViUInt16[2048] 을 채운다 (화소당 0..65535)
      * LC100_getIntegrationTime 은 attribute 인자를 하나 더 받는다 (초 단위)
      * 자유 연속 수집은 setOperatingMode(SW_LOOP) + startScanCont
      * 데이터는 상태 레지스터가 TRANSFER 가 된 뒤에 읽는다 (sample.c 와 동일)

    `python lc100_cam.py --probe` 로 DLL 의 export 목록을 다시 확인할 수 있다.
    """

    name = "Thorlabs LC100"

    # 장치 상태 레지스터 (LC100_Drv.h)
    STATUS_IDLE = 0x00000000       # 대기
    STATUS_TRANSFER = 0x00000001   # 스캔 완료, PC 로 전송 대기
    STATUS_TRIGGERED = 0x00000002  # 스캔 진행 중
    STATUS_ARMED = 0x00000003      # 외부 트리거 대기

    # 동작 모드 (LC100_Drv.h)
    OPMODE_IDLE = 0
    OPMODE_SW_SINGLE_SHOT = 1
    OPMODE_SW_LOOP = 2             # 내부 트리거로 가능한 한 빠르게

    # getIntegrationTime 의 attribute
    ATTR_ACT, ATTR_MIN, ATTR_MAX, ATTR_DEF = 0, 1, 2, 3

    # 동작 -> 함수 이름 후보 (--probe 로 확인한 실제 이름이 맨 앞)
    _CANDIDATES = {
        "init":        ["LC100_init"],
        "close":       ["LC100_close"],
        "status":      ["LC100_getDeviceStatus"],
        "get_data":    ["LC100_getScanData"],
        "start_cont":  ["LC100_startScanCont"],
        "start_one":   ["LC100_startScan"],
        "set_opmode":  ["LC100_setOperatingMode"],
        "set_exp":     ["LC100_setIntegrationTime"],
        "get_exp":     ["LC100_getIntegrationTime"],
        "error_msg":   ["LC100_errorMessage"],
    }
    # 없으면 열지 않는다. 나머지는 없으면 없는 대로 진행.
    _REQUIRED = {"init", "close", "status", "get_data"}

    # ViStatus=Int32, ViSession=UInt32, ViBoolean=UInt16, ViReal64=double
    _SIG = {
        "init":       (C.c_int32, [C.c_char_p, C.c_uint16, C.c_uint16,
                                   C.POINTER(C.c_uint32)]),
        "close":      (C.c_int32, [C.c_uint32]),
        "status":     (C.c_int32, [C.c_uint32, C.POINTER(C.c_int32)]),
        "get_data":   (C.c_int32, [C.c_uint32, C.POINTER(C.c_uint16)]),
        "start_cont": (C.c_int32, [C.c_uint32]),
        "start_one":  (C.c_int32, [C.c_uint32]),
        "set_opmode": (C.c_int32, [C.c_uint32, C.c_int32]),
        "set_exp":    (C.c_int32, [C.c_uint32, C.c_double]),
        "get_exp":    (C.c_int32, [C.c_uint32, C.c_int16, C.POINTER(C.c_double)]),
        "error_msg":  (C.c_int32, [C.c_uint32, C.c_int32, C.c_char_p]),
    }

    read_timeout_s = 5.0

    def __init__(self, resource: str | None = None, dll_path: str | None = None):
        self.dll_path = dll_path or find_dll()
        self.resource = resource
        self.lib = None
        self.h = C.c_uint32(0)
        self.fn = {}
        self.exports = []
        self.max_value = 65535.0
        self._continuous = False
        self._lock = threading.RLock()           # LC100VisaSource 와 같은 이유

    # -- 내부 -------------------------------------------------------------
    def _bind(self):
        self.exports = pe_exports(self.dll_path)
        missing = []
        for key, names in self._CANDIDATES.items():
            hit = next((n for n in names if n in self.exports), None)
            if hit is None:
                if key in self._REQUIRED:
                    missing.append("%s (후보: %s)" % (key, ", ".join(names)))
                continue
            f = getattr(self.lib, hit)
            f.restype, f.argtypes = self._SIG[key]
            self.fn[key] = f
        if "start_cont" not in self.fn and "start_one" not in self.fn:
            missing.append("start (후보: LC100_startScanCont, LC100_startScan)")
        if missing:
            raise CameraError(
                "드라이버 DLL 에서 필요한 함수를 찾지 못했습니다.\n"
                "  DLL : %s\n  누락 : %s\n\n"
                "이 DLL 이 내보내는 함수 목록:\n    %s\n\n"
                "`python lc100_cam.py --probe` 결과를 보고 _CANDIDATES 를 "
                "실제 이름으로 맞춰 주십시오."
                % (self.dll_path, "; ".join(missing),
                   "\n    ".join(self.exports) or "(없음)"))

    def _chk(self, status, what):
        if status == 0:
            return
        msg = ""
        if "error_msg" in self.fn:
            buf = C.create_string_buffer(512)      # LC100_ERR_DESCR_BUFFER_SIZE
            try:
                self.fn["error_msg"](self.h, C.c_int32(status), buf)
                msg = buf.value.decode("latin-1", "replace").strip()
            except Exception:                    # noqa: BLE001
                pass
        raise CameraError("%s 실패 (status=0x%X) %s" % (what, status & 0xFFFFFFFF, msg))

    def _resource_candidates(self):
        """VISA 리소스 문자열 후보. 드라이버 예제가 두 형태를 다 쓴다."""
        if self.resource:
            return [self.resource]
        base = default_resource_string()
        alt = base.replace("USB0::", "USB::", 1)
        return [base] if alt == base else [base, alt]

    # -- 공개 -------------------------------------------------------------
    def open(self):
        if not self.dll_path:
            raise CameraError(
                "LC100 드라이버 DLL 을 찾지 못했습니다.\n"
                "Thorlabs LC100 소프트웨어(USB 드라이버 + VISA + 계측기 드라이버)를 "
                "먼저 설치해야 합니다. `python lc100_cam.py` 로 상태를 확인하십시오.")
        # LC100_Drv 는 32비트로만 배포된다(설치본에 Win64 바이너리가 없다).
        # 64비트 파이썬은 32비트 DLL 을 불러올 수 없다.
        if struct.calcsize("P") == 8 and "_32" in os.path.basename(self.dll_path):
            raise CameraError(
                "32비트 드라이버를 64비트 파이썬에서 열 수 없습니다.\n"
                "  DLL    : %s (32비트)\n"
                "  파이썬 : %s (64비트)\n\n"
                "해결: 32비트 파이썬을 설치해 그쪽에서 실행하십시오.\n"
                "  1) python.org 에서 Windows installer (32-bit) 설치\n"
                "  2) py -3.12-32 -m pip install numpy matplotlib\n"
                "  3) py -3.12-32 lc100_app.py\n"
                % (self.dll_path, sys.executable))
        # _VI_FUNC 는 Win32 에서 __stdcall 이다(visatype.h). CDLL 로 부르면
        # 32비트에서 스택이 깨진다.
        loader = getattr(C, "WinDLL", C.CDLL)
        try:
            self.lib = loader(self.dll_path)
        except OSError as e:
            raise CameraError("드라이버 DLL 을 불러오지 못했습니다: %s\n  %s"
                              % (self.dll_path, e)) from e
        self._bind()

        # 예제(sample.c / C# 데모)와 동일하게 IDQuery·reset 모두 끈다.
        last = None
        for res in self._resource_candidates():
            st = self.fn["init"](res.encode("ascii"), 0, 0, C.byref(self.h))
            if st == 0:
                self.resource = res
                break
            last = (res, st)
        else:
            self._chk(last[1], "장치 열기(init, %s)" % last[0])

        if "set_opmode" in self.fn and "start_cont" in self.fn:
            self._chk(self.fn["set_opmode"](self.h, self.OPMODE_SW_LOOP),
                      "동작 모드 설정(SW_LOOP)")
        if "start_cont" in self.fn:
            self._chk(self.fn["start_cont"](self.h), "연속 스캔 시작")
            self._continuous = True
        else:
            self._continuous = False             # read_line 이 매번 트리거한다
        return self

    def read_line(self):
        buf = (C.c_uint16 * self.n_pixels)()     # getScanData 는 ViUInt16 배열
        reg = C.c_int32(0)
        deadline = time.monotonic() + self.read_timeout_s
        while True:
            with self._lock:
                self._chk(self.fn["status"](self.h, C.byref(reg)), "상태 읽기")
                s = reg.value
                if s == self.STATUS_TRANSFER:    # 스캔 끝 → 읽을 수 있다
                    self._chk(self.fn["get_data"](self.h, buf), "라인 읽기")
                    return np.frombuffer(buf, dtype=np.uint16).astype(float)
                if not self._continuous and s == self.STATUS_IDLE:
                    self._chk(self.fn["start_one"](self.h), "스캔 시작")
            if time.monotonic() > deadline:
                raise CameraError(
                    "라인을 기다리다 시간이 초과됐습니다 (%.1f초, 상태=0x%08X).\n"
                    "노출시간이 너무 길거나 스캔이 시작되지 않았습니다."
                    % (self.read_timeout_s, s & 0xFFFFFFFF))
            time.sleep(0.0005)

    def get_exposure_ms(self):
        if "get_exp" not in self.fn:
            return float("nan")
        v = C.c_double(0)
        with self._lock:
            # attribute 인자가 있다. ACT = 지금 설정된 값.
            self._chk(self.fn["get_exp"](self.h, C.c_int16(self.ATTR_ACT), C.byref(v)),
                      "노출시간 읽기")
        return v.value * 1000.0                  # 드라이버는 초 단위

    def set_exposure_ms(self, ms):
        if "set_exp" not in self.fn:
            return
        with self._lock:
            self._chk(self.fn["set_exp"](self.h, C.c_double(ms / 1000.0)),
                      "노출시간 설정")

    def close(self):
        if self.lib is not None and self.h.value:
            if "set_opmode" in self.fn:
                try:
                    self.fn["set_opmode"](self.h, self.OPMODE_IDLE)
                except Exception:                # noqa: BLE001
                    pass
            if "close" in self.fn:
                try:
                    self.fn["close"](self.h)
                except Exception:                # noqa: BLE001
                    pass
        self.h = C.c_uint32(0)
        self.lib = None
        self.fn = {}
        self._continuous = False


# ---------------------------------------------------------------------------
class LC100VisaSource(LineSource):
    """실제 LC100 — 계측기 DLL 을 거치지 않고 VISA 로 직접 말한다.

    왜 이게 필요한가
      LC100_Drv 는 32비트로만 배포된다. 64비트 파이썬은 그 DLL 을 못 부른다.
      그런데 DLL 자체가 하는 일은 VISA USB RAW 위의 얇은 껍데기일 뿐이라,
      같은 제어 전송을 직접 보내면 64비트에서도 그대로 동작한다.

    프로토콜 출처
      설치본의 드라이버 원본 `...\\Thorlabs LC100\\LC100_Drv.c` 를 그대로 옮겼다.
      제어 전송은 bmRequestType 0x40(out) / 0xC0(in), 스캔 데이터는 벌크 read.

    검증
      같은 조건에서 벤더 예제(`Examples\\C\\sample.exe`)와 결과가 일치한다.

    주의 — 엔디안
      LC100_Drv.c 의 conv32_t 는 필드 이름이 hhb..llb 라 빅엔디안처럼 보이지만,
      x86 에서 ViUInt32 에 겹쳐지므로 **첫 바이트가 최하위**다. 즉 리틀엔디안이다.
      상태·적분시간 모두 `<I` 로 읽어야 한다.
    """

    name = "Thorlabs LC100 (VISA)"

    # -- LC100_Drv.c 의 USB 명령 ------------------------------------------
    WCMD_CCD = 0x46                # 적분시간 등 CCD 레지스터 쓰기
    RCMD_CCD = 0x46                # 〃 읽기
    WCMD_SET_OPMODE = 0x47
    WCMD_EXEC_SCAN = 0x49
    RCMD_GET_STATUS = 0x4A
    RCMD_GET_ERROR = 0xFF

    REG_INT_TIME = 12              # 적분시간 (마이크로초, uint32)
    REG_INT_TIME_MIN = 13
    REG_INT_TIME_MAX = 14
    REG_INT_TIME_DEF = 15

    EXEC_SCAN_SINGLE = 0
    EXEC_SCAN_CONT = 1

    OPMODE_IDLE = 0
    OPMODE_SW_SINGLE_SHOT = 1
    OPMODE_SW_LOOP = 2

    STATUS_IDLE = 0
    STATUS_TRANSFER = 1            # 스캔 완료 → 이때 읽는다
    STATUS_TRIGGERED = 2
    STATUS_ARMED = 3

    # visa.h
    VI_ATTR_USB_END_IN = 0x3FFF01A9
    VI_USB_END_NONE = 0
    VI_WRITE_BUF_DISCARD = 0x01
    VI_READ_BUF_DISCARD = 0x02

    # LC100_Drv.h 의 LC100_FIND_PATTERN
    FIND_PATTERN = ("USB?*RAW{VI_ATTR_MANF_ID==0x1313 && "
                    "((VI_ATTR_MODEL_CODE>=0x80A0) && (VI_ATTR_MODEL_CODE<=0x80AF))}")

    read_timeout_s = 5.0

    def __init__(self, resource: str | None = None, exposure_ms: float | None = None):
        self.resource = resource
        self.exposure_ms = exposure_ms
        self.rm = None
        self.dev = None
        self.max_value = 65535.0
        # VISA 세션 하나를 수집 스레드와 GUI 스레드가 같이 쓴다.
        # NI-VISA 는 같은 세션에 동시 접근하면 access violation 으로 죽는다.
        # (lc100_app 은 수집 스레드에서 read_line, 메인 스레드에서 10 fps 로
        #  get_exposure_ms 를 부른다.) 모든 장치 접근을 이 락으로 직렬화한다.
        self._lock = threading.RLock()

    # -- 내부 -------------------------------------------------------------
    def _out(self, request, value, index, data=b""):
        with self._lock:
            return self.dev.visalib.usb_control_out(
                self.dev.session, 0x40, request, value, index, data)

    def _in(self, request, value, index, length):
        with self._lock:
            raw, _ = self.dev.visalib.usb_control_in(
                self.dev.session, 0xC0, request, value, index, length)
        return bytes(raw)

    def _read_u32(self, request, index):
        raw = self._in(request, 0, index, 4)
        if len(raw) != 4:
            raise CameraError("레지스터 %d 읽기: 4바이트를 기대했는데 %d바이트"
                              % (index, len(raw)))
        return struct.unpack("<I", raw)[0]

    def _status(self):
        return self._read_u32(self.RCMD_GET_STATUS, 0)

    def _deadline_s(self):
        """적분시간이 길면 그만큼 더 기다린다."""
        exp = self.exposure_ms or 0.0
        return max(self.read_timeout_s, exp / 1000.0 * 3.0 + 1.0)

    def _apply_timeout(self):
        self.dev.timeout = int(max(2000.0, self._deadline_s() * 1000.0))

    # -- 공개 -------------------------------------------------------------
    def open(self):
        try:
            import pyvisa
        except ImportError as e:
            raise CameraError(
                "pyvisa 가 없습니다. 설치하십시오:\n"
                "    python -m pip install pyvisa") from e

        try:
            self.rm = pyvisa.ResourceManager()
        except Exception as e:                   # noqa: BLE001
            raise CameraError(
                "VISA 런타임을 열 수 없습니다: %s\n"
                "NI-VISA 가 설치돼 있어야 합니다 (visa64.dll)." % e) from e

        res = self.resource
        if not res:
            try:
                found = self.rm.list_resources(self.FIND_PATTERN)
            except Exception:                    # noqa: BLE001
                found = ()
            res = found[0] if found else default_resource_string()

        try:
            self.dev = self.rm.open_resource(res)
        except Exception as e:                   # noqa: BLE001
            self.close()
            raise CameraError(
                "LC100 을 열지 못했습니다: %s\n  %s\n"
                "USB 연결과 드라이버 상태를 확인하십시오 (python lc100_cam.py)."
                % (res, e)) from e
        self.resource = res

        try:
            # LC100_init 과 같은 순서: END_IN 끄기 → flush → viClear
            self.dev.set_visa_attribute(self.VI_ATTR_USB_END_IN, self.VI_USB_END_NONE)
            self.dev.timeout = 5000
            self.dev.flush(self.VI_WRITE_BUF_DISCARD | self.VI_READ_BUF_DISCARD)
            self.dev.clear()

            if self.exposure_ms is None:
                self.exposure_ms = self._read_u32(self.RCMD_CCD,
                                                  self.REG_INT_TIME) / 1000.0
            else:
                self.set_exposure_ms(self.exposure_ms)
            self._apply_timeout()

            self._out(self.WCMD_SET_OPMODE, 0, self.OPMODE_SW_LOOP)
            self._out(self.WCMD_EXEC_SCAN, 0, self.EXEC_SCAN_CONT)
        except CameraError:
            self.close()
            raise
        except Exception as e:                   # noqa: BLE001
            self.close()
            raise CameraError("LC100 초기화 실패: %s" % e) from e
        return self

    def read_line(self):
        limit = self._deadline_s()
        nbytes = self.n_pixels * 2
        t0 = time.monotonic()
        raw = st = None
        while raw is None:
            # 상태 확인과 벌크 읽기 사이에 다른 스레드가 끼어들면 안 된다.
            # 폴링 사이사이에는 락을 놓아 노출시간 변경 같은 것이 들어올 수 있게 한다.
            with self._lock:
                st = self._status()
                if st == self.STATUS_TRANSFER:
                    raw = self.dev.read_bytes(nbytes)
                    break
            if time.monotonic() - t0 > limit:
                raise CameraError(
                    "라인을 기다리다 시간이 초과됐습니다 (%.1f초, 상태=0x%08X).\n"
                    "적분시간이 너무 길거나 스캔이 시작되지 않았습니다." % (limit, st))
            time.sleep(0.0005)
        if len(raw) != nbytes:
            raise CameraError("라인 읽기: %d바이트를 기대했는데 %d바이트"
                              % (nbytes, len(raw)))
        return np.frombuffer(raw, dtype="<u2").astype(float)

    def get_exposure_ms(self):
        if self.dev is None:
            return float("nan")
        return self._read_u32(self.RCMD_CCD, self.REG_INT_TIME) / 1000.0

    def set_exposure_ms(self, ms):
        if self.dev is None:
            return
        self._out(self.WCMD_CCD, 0, self.REG_INT_TIME,
                  struct.pack("<I", int(round(ms * 1000.0))))
        self.exposure_ms = ms
        self._apply_timeout()

    def exposure_limits_ms(self):
        """(최소, 최대) 적분시간 [ms]. 장치가 알려주는 값."""
        return (self._read_u32(self.RCMD_CCD, self.REG_INT_TIME_MIN) / 1000.0,
                self._read_u32(self.RCMD_CCD, self.REG_INT_TIME_MAX) / 1000.0)

    def close(self):
        with self._lock:
            if self.dev is not None:
                try:
                    self._out(self.WCMD_SET_OPMODE, 0, self.OPMODE_IDLE)
                except Exception:                # noqa: BLE001
                    pass
                try:
                    self.dev.close()
                except Exception:                # noqa: BLE001
                    pass
                self.dev = None
        if self.rm is not None:
            try:
                self.rm.close()
            except Exception:                    # noqa: BLE001
                pass
            self.rm = None


def default_resource_string(serial: str | None = None) -> str:
    """VISA 리소스 문자열. 예: USB0::0x1313::0x80A0::M00274403::RAW"""
    sn = serial or (detect_lc100() or {}).get("serial", "")
    return "USB0::0x%04X::0x%04X::%s::RAW" % (THORLABS_VID, LC100_PID, sn)


# ---------------------------------------------------------------------------
# 설치 상태 진단
# ---------------------------------------------------------------------------
def detect_lc100() -> dict | None:
    """Windows 장치 목록에서 LC100 을 찾아 상태를 돌려준다."""
    if sys.platform != "win32":
        return None
    import subprocess
    ps = (
        "$d = Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -match 'VID_1313&PID_80A0' } | Select-Object -First 1; "
        "if ($d) { "
        "$p = (Get-PnpDeviceProperty -InstanceId $d.InstanceId "
        "-KeyName DEVPKEY_Device_ProblemCode -ErrorAction SilentlyContinue).Data; "
        "Write-Output ($d.FriendlyName + '|' + $d.Status + '|' + $d.InstanceId + '|' + $p) }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    parts = (out.splitlines()[0]).split("|")
    inst = parts[2] if len(parts) > 2 else ""
    return dict(name=parts[0], status=parts[1] if len(parts) > 1 else "",
                instance_id=inst, problem=parts[3] if len(parts) > 3 else "",
                serial=inst.rsplit("\\", 1)[-1] if "\\" in inst else "")


def have_pyvisa() -> bool:
    try:
        import pyvisa                            # noqa: F401
    except ImportError:
        return False
    return True


def diagnose() -> dict:
    """장치·드라이버·VISA·DLL 설치 상태를 한 번에 점검."""
    dev = detect_lc100()
    dll = find_dll()
    visa = [p for p in (r"C:\Windows\System32\visa64.dll",
                        r"C:\Windows\System32\visa32.dll") if os.path.isfile(p)]
    pyvisa_ok = have_pyvisa()
    bits = struct.calcsize("P") * 8
    # DLL 경로: 32비트 파이썬에서만 쓸 수 있다.
    dll_usable = bool(dll) and not (bits == 64 and "_32" in os.path.basename(dll))
    # VISA 경로: 비트수와 무관하다.
    visa_usable = bool(visa) and pyvisa_ok
    ready = bool(dev and dev.get("status") == "OK" and (visa_usable or dll_usable))
    return dict(device=dev, dll=dll, visa=visa, pyvisa=pyvisa_ok, bits=bits,
                dll_usable=dll_usable, visa_usable=visa_usable, ready=ready)


def print_diagnosis():
    d = diagnose()
    print("=" * 66)
    print("  Thorlabs LC100 연결·드라이버 점검")
    print("=" * 66)
    dev = d["device"]
    if dev:
        print("  장치      : %s  (S/N %s)" % (dev["name"], dev["serial"] or "?"))
        print("  상태      : %s%s" % (dev["status"],
              "  ← 드라이버 미설치(Code 28)" if dev.get("problem") == "28" else ""))
        print("  InstanceId: %s" % dev["instance_id"])
    else:
        print("  장치      : 찾지 못함 (USB 연결/전원 확인)")
    print("  LC100_Drv : %s" % (d["dll"] or "없음 — Thorlabs LC100 소프트웨어 미설치"))
    print("  VISA      : %s" % (", ".join(d["visa"]) if d["visa"] else "없음"))
    print("  파이썬    : %d비트   pyvisa %s" % (d["bits"], "있음" if d["pyvisa"] else "없음"))
    print("-" * 66)
    print("  수집 경로 2가지")
    print("    VISA 직결 (기본)  : %s" % (
        "사용 가능" if d["visa_usable"] else
        ("pyvisa 없음 — python -m pip install pyvisa" if d["visa"]
         else "NI-VISA 없음")))
    print("    LC100_Drv DLL     : %s" % (
        "사용 가능" if d["dll_usable"] else
        ("DLL 이 32비트라 64비트 파이썬에서 못 씀" if d["dll"] else "DLL 없음")))
    print("-" * 66)
    if d["ready"]:
        print("  ▶ 실제 카메라로 실행 가능합니다:  python lc100_app.py")
    else:
        print("  ▶ 지금은 실제 카메라를 열 수 없습니다.")
        print("    SPLICCO 소프트웨어(USB 드라이버 + NI-VISA + LC100_Drv)를 설치하세요.")
        print("    설치 전에도 표시 프로그램은 확인할 수 있습니다:")
        print("      python lc100_app.py --source sim       (시뮬레이션)")
        print("      python lc100_app.py --source webcam    (웹캠 한 줄)")
    print("=" * 66)
    return d


def make_source(kind: str = "auto", **kw) -> LineSource:
    """'auto' | 'lc100' | 'lc100dll' | 'webcam' | 'sim'

    'lc100' 은 VISA 직결(어느 비트수에서나 동작)이고,
    'lc100dll' 은 32비트 LC100_Drv DLL 경유다.
    """
    kind = (kind or "auto").lower()
    if kind == "auto":
        d = diagnose()
        if d["visa_usable"] and d["device"] and d["device"].get("status") == "OK":
            kind = "lc100"
        elif d["dll_usable"] and d["device"] and d["device"].get("status") == "OK":
            kind = "lc100dll"
        else:
            kind = "sim"
    if kind in ("lc100", "visa"):
        return LC100VisaSource(**kw)
    if kind in ("lc100dll", "tllc100", "dll"):
        return TLLC100Source(**kw)
    if kind == "webcam":
        return WebcamSource(**kw)
    if kind in ("sim", "simulated"):
        return SimulatedSource(**kw)
    raise ValueError("알 수 없는 소스: %s" % kind)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe" in argv:
        dll = find_dll()
        if not dll:
            print("LC100_Drv DLL 을 찾지 못했습니다. 드라이버를 먼저 설치하십시오.")
            return 1
        print("DLL:", dll)
        names = pe_exports(dll)
        print("export 함수 %d개:" % len(names))
        for n in names:
            print("   ", n)
        return 0
    if "--demo" in argv:
        with SimulatedSource() as s:
            for i in range(3):
                y = s.read_line()
                print("line %d: n=%d  min=%.0f  max=%.0f  mean=%.1f"
                      % (i, y.size, y.min(), y.max(), y.mean()))
        return 0
    print_diagnosis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
