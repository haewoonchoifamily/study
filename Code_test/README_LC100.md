# Thorlabs LC100 라인 카메라 데이터 읽기·표시

라인스캔은 한 번에 한 줄(LC100 은 2048화소)만 들어온다. 그래서 화면은 두 가지를
같이 본다.

- **① 라인 프로파일** — 지금 들어온 한 줄. 신호 모양과 **포화 여부**를 본다.
- **② 워터폴** — 최근 400줄을 쌓은 2차원 이미지. 시간에 따른 변화를 본다.

## 현재 상태 (2026-09-09)

드라이버 설치가 끝났고, **64비트 파이썬에서 실제 카메라가 돌아간다.**

```
USB\VID_1313&PID_80A0\M00274403     ← Thorlabs LC100, S/N M00274403
Status    : OK
VISA      : visa64.dll, visa32.dll
수집 경로 : VISA 직결 (기본)   ← 32비트 파이썬이 필요 없다
```

```bash
python lc100_cam.py
```

### 왜 VISA 로 직접 말하는가

`LC100_Drv` 는 **32비트로만 배포된다.** 설치본에 64비트 바이너리가 아예 없다
(`Win64\Bin` 이 없고 `.dll`·`.lib` 모두 `_32`, PE Machine `0x014C`).
그래서 64비트 파이썬은 이 DLL 을 부를 수 없다.

그런데 그 DLL 이 하는 일은 **VISA USB RAW 위의 얇은 껍데기**다. 설치본에
드라이버 원본 소스(`Thorlabs LC100\LC100_Drv.c`)가 통째로 들어 있어서,
같은 USB 제어 전송을 PyVISA 로 그대로 보내면 된다. 그렇게 만든 것이
`LC100VisaSource` 이고, 비트수와 무관하게 동작한다.

필요한 것은 PyVISA 하나뿐이다 (NI-VISA 는 드라이버와 같이 이미 깔려 있다).

```bash
python -m pip install pyvisa
```

### 두 경로 비교

| | `LC100VisaSource` (기본) | `TLLC100Source` |
|---|---|---|
| 방식 | VISA 로 USB 제어 전송 직접 | `LC100_Drv_32.dll` 호출 |
| 파이썬 | 32·64비트 아무거나 | **32비트만** |
| 필요한 것 | pyvisa | 없음 (DLL 만) |
| `--source` | `lc100` | `lc100dll` |

`auto` 는 VISA 경로를 먼저 고르고, 안 되면 DLL, 그것도 안 되면 시뮬레이션이다.

## 실행

```bash
python lc100_app.py                  # 카메라가 준비되면 카메라, 아니면 시뮬레이션
python lc100_app.py --source sim     # 시뮬레이션 (하드웨어 없이 화면 확인)
python lc100_app.py --source webcam  # 웹캠 한 줄 (실시간 표시 경로 점검)
python lc100_app.py --source lc100   # 실제 LC100 (VISA 직결)
python lc100_app.py --source lc100dll # 실제 LC100 (32비트 DLL 경유)
python lc100_app.py --check          # 연결·드라이버 점검만
```

화면 기능: 노출시간 변경, y축 자동/고정, N줄 평균화, 일시정지, 지우기,
소스 전환·재연결, **저장**(`.npz` 원시배열 + `.csv` + `.png` 화면 캡처).

## 구성

| 파일 | 역할 |
|---|---|
| `lc100_cam.py` | 데이터 소스 계층 + 설치 진단 + DLL 조사 |
| `lc100_app.py` | 실시간 표시 GUI (tkinter + matplotlib) |

`lc100_cam.py` 의 소스 3종은 모두 같은 인터페이스(`LineSource`)를 쓴다.
그래서 카메라가 준비되면 `--source lc100` 만 바꾸면 되고, 표시 코드는 그대로다.

| 소스 | 설명 |
|---|---|
| `LC100VisaSource` | 실제 LC100 — VISA 직결. 기본값 |
| `TLLC100Source` | 실제 LC100 — 32비트 `LC100_Drv` DLL 경유 |
| `WebcamSource` | USB 웹캠의 가운데 한 행을 라인으로 사용 |
| `SimulatedSource` | 배경 + 피크 5개 + 고정패턴잡음 + 샷노이즈 + 드리프트 |

시뮬레이터는 **노출시간에 비례해 신호가 커지고 라인레이트도 그만큼 느려진다**
(10 ms → 약 98줄/초). 실제 카메라와 같은 흐름으로 화면을 검증할 수 있다.

## 드라이버 API

`--probe` 로 DLL 의 export 52개를 확인하고, 설치본의 `LC100_Drv.h` 와
`Examples/C/sample.c` 로 `TLLC100Source` 를 확정했다. 헤더를 보고 바로잡은 곳:

- `getScanData` 의 버퍼는 `ViUInt16` 이다. `c_double` 로 잡으면 값이 쓰레기가 된다.
- `getIntegrationTime` 은 `attribute` 인자를 하나 더 받는다 (`ACT/MIN/MAX/DEF`).
- `visatype.h` 에서 `_VI_FUNC` 가 Win32 에서 `__stdcall` 이다 → `CDLL` 이 아니라 `WinDLL`.

### VISA 직결이 쓰는 USB 프로토콜

`LC100_Drv.c` 에서 그대로 옮겼다. 제어 전송은 `bmRequestType` `0x40`(out) /
`0xC0`(in), 스캔 데이터만 벌크 read 다.

| 동작 | 전송 |
|---|---|
| 적분시간 쓰기 | out `0x46`, wIndex `12`, uint32 마이크로초 |
| 적분시간 읽기 | in `0x46`, wIndex `12`(ACT) `13`(MIN) `14`(MAX) `15`(DEF) |
| 동작 모드 | out `0x47`, wIndex = 모드 (`2` = SW_LOOP) |
| 스캔 시작 | out `0x49`, wIndex `0`(단발) `1`(연속) |
| 상태 | in `0x4A`, 4바이트 |
| 라인 데이터 | 벌크 read 4096바이트 = uint16 × 2048 |

여는 순서도 `LC100_init` 을 따른다: `VI_ATTR_USB_END_IN` 을 `VI_USB_END_NONE`
으로 끄고 → `viFlush` → `viClear`.

**엔디안 주의.** `LC100_Drv.c` 의 `conv32_t` 는 필드 이름이 `hhb..llb` 라
빅엔디안처럼 보이지만, x86 에서 `ViUInt32` 에 겹쳐지므로 **첫 바이트가 최하위**다.
상태·적분시간 모두 리틀엔디안(`<I`)으로 읽어야 한다. 빅엔디안으로 읽으면
상태 `1`(TRANSFER) 이 `0x01000000` 으로 보여서 영원히 기다리게 된다.

### 검증

같은 조건에서 벤더 예제 `Examples\C\sample.exe`(32비트, 순정 DLL 사용)와
`LC100VisaSource` 의 결과가 일치한다. 적분시간 되읽기, 장치가 알려주는
최소/최대(1.054 ms / 50 s), 라인당 대기시간까지 모두 맞는다.

## 참고 사양 (LC100)

- 2048화소 선형 Si CCD, 350 – 1100 nm
- USB 2.0, 단종일 2022-11-23

## 알아둘 점

- 수집은 카메라 속도로 하고 **화면은 10 fps 로 제한**한다. 워터폴(400×2048)을
  매 줄마다 다시 그리면 이벤트 루프가 밀려 GUI 가 멈춘 것처럼 보인다.
- 수집은 별도 스레드에서 하고 tkinter 는 절대 건드리지 않는다. GUI 는 큐만 비운다.
  큐가 가득 차면 **오래된 줄부터 버린다**(최신 데이터 우선).
- 워터폴에서 아직 채워지지 않은 구간은 흰색(NaN)으로 나온다.
- **VISA 세션은 스레드 하나씩만 써야 한다.** 수집 스레드가 `read_line()` 을 도는
  동안 메인 스레드가 10 fps 로 `get_exposure_ms()` 를 부르면, 같은 세션에 동시
  접근이 일어나 NI-VISA 가 access violation 으로 **프로세스째 죽는다**(파이썬
  예외가 아니라 세그폴트라 로그도 안 남는다). 그래서 `LC100VisaSource` 는 모든
  장치 접근을 `RLock` 으로 직렬화한다. 상태 확인과 벌크 읽기는 한 임계구역으로
  묶고, 폴링 사이에는 락을 놓아 노출시간 변경이 끼어들 수 있게 한다.
- 카메라는 한 번에 한 프로그램만 연다. Thorlabs 순정 프로그램(Splicco)이 떠
  있으면 `VI_ERROR_RSRC_BUSY` 가 난다. 둘 중 하나를 닫아야 한다.
