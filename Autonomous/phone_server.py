# -*- coding: utf-8 -*-
"""휴대폰에서 쓰는 사물 인지 — PC 가 추론하고 폰은 카메라와 화면을 맡는다.

    python phone_server.py

띄우면 주소가 나온다. 폰 브라우저에서 그 주소를 열고 **홈 화면에 추가**하면
앱처럼 쓸 수 있다(PWA).

왜 이런 구조인가
    폰에서 직접 추론하려면 모델을 변환해 얹어야 하고 성능도 크게 떨어진다.
    이 PC 에 RTX 5070 Ti 와 이미 만들어 둔 검출기가 있으므로, 폰은 카메라
    프레임만 올려보내고 결과 상자를 받아 그리게 했다. YOLO-World 프롬프트도
    폰에서 그대로 바꿀 수 있다.

왜 HTTPS 인가
    브라우저는 안전한 출처(HTTPS)가 아니면 카메라를 열어주지 않는다. 그래서
    자체 서명 인증서를 만들어 TLS 로 띄운다. 폰에서 처음 열 때 "안전하지 않음"
    경고가 한 번 나오는데, 고급 → 계속을 누르면 된다. 사설 IP 로 나가는 자체
    서명이라 그런 것이고, 통신은 같은 망 안에서만 오간다.

의존성은 표준 라이브러리뿐이다(인증서 생성에만 openssl 을 쓴다).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from coco_ko import to_ko
from detect import WorldBackend, make_backend

CERT = os.path.join(HERE, "certs", "cert.pem")
KEY = os.path.join(HERE, "certs", "key.pem")
CERT_IPS = os.path.join(HERE, "certs", "ips.txt")   # 인증서에 넣은 주소 목록
PAGE = os.path.join(HERE, "phone.html")

MANIFEST = {
    "name": "사물 인지",
    "short_name": "사물인지",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#111418",
    "theme_color": "#111418",
    "icons": [],
}


# ---------------------------------------------------------------------------
def default_route_ip() -> str:
    """기본 경로로 나갈 때 쓰는 주소(보통 유선). 실제로 패킷을 보내지는 않는다."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def all_ipv4() -> list:
    """이 PC 가 가진 IPv4 주소 전부.

    유선과 WiFi 를 동시에 쓰면 주소가 여럿이다. 폰이 붙을 수 있는 주소는
    폰과 같은 망에 있는 쪽뿐이므로, 하나만 찍지 말고 전부 보여준다.
    """
    found = set()
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except (socket.gaierror, OSError):
        pass
    d = default_route_ip()
    if d:
        found.add(d)
    return sorted(a for a in found
                  if a and not a.startswith("127.") and not a.startswith("169.254."))


def describe_ip(ip: str) -> str:
    """주소만 보고 어떤 망인지 대충 알려준다. 어느 걸 골라야 할지 힌트."""
    if ip.startswith("172.20.10."):
        return "아이폰 핫스팟"
    if ip.startswith(("192.168.", "192.168.43.")):
        return "핫스팟 또는 가정용 공유기"
    if ip.startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.")):
        return "기관/사내망 (유선일 가능성)"
    return ""


def ensure_cert(ips: list) -> bool:
    """자체 서명 인증서를 만든다.

    인증서에는 접속할 주소가 subjectAltName 으로 들어가 있어야 한다(크롬·사파리는
    CN 을 안 본다). 그래서 주소가 바뀌면 — 유선에서 핫스팟으로 옮기면 —
    인증서도 다시 만든다. 안 그러면 폰에서 주소 불일치 경고가 한 겹 더 붙는다.
    """
    os.makedirs(os.path.dirname(CERT), exist_ok=True)
    have = set()
    if os.path.isfile(CERT_IPS):
        try:
            with open(CERT_IPS, encoding="utf-8") as f:
                have = set(f.read().split())
        except OSError:
            pass
    if (os.path.isfile(CERT) and os.path.isfile(KEY) and set(ips) <= have):
        return True
    if have and not set(ips) <= have:
        print("주소가 바뀌어 인증서를 다시 만듭니다 (%s)" % ", ".join(sorted(ips)))

    san = ",".join(["IP:%s" % ip for ip in ips] + ["IP:127.0.0.1", "DNS:localhost"])
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", KEY, "-out", CERT, "-days", "825", "-nodes",
        "-subj", "/CN=%s" % (ips[0] if ips else "localhost"),
        "-addext", "subjectAltName=%s" % san,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print("[오류] 인증서를 만들지 못했습니다: %s" % e)
        return False
    if r.returncode != 0 or not os.path.isfile(CERT):
        print("[오류] openssl 실패:\n%s" % (r.stderr or r.stdout))
        return False
    try:
        with open(CERT_IPS, "w", encoding="utf-8") as f:
            f.write("\n".join(ips))
    except OSError:
        pass
    print("인증서 생성: %s  (%s)" % (CERT, ", ".join(ips) or "주소 없음"))
    return True


# ---------------------------------------------------------------------------
class Engine:
    """검출기 하나를 여러 요청이 나눠 쓴다. 접근은 전부 직렬화한다."""

    def __init__(self, kind, device, weights, prompt):
        self.lock = threading.Lock()
        self.kind = kind
        self.device = device
        self.weights = weights
        self.backend = make_backend(kind, device, weights, prompt)

    @property
    def is_world(self):
        return isinstance(self.backend, WorldBackend)

    def info(self):
        with self.lock:
            return dict(name=self.backend.name, kind=self.kind,
                        device=getattr(self.backend, "device", "-"),
                        world=self.is_world,
                        classes=list(getattr(self.backend, "classes", [])),
                        suggested_conf=getattr(self.backend, "suggested_conf", 0.5))

    def switch(self, kind, weights=None, prompt=None):
        with self.lock:
            self.backend = make_backend(kind, self.device, weights, prompt)
            self.kind = kind
            self.weights = weights

    def set_classes(self, names):
        with self.lock:
            if not self.is_world:
                raise ValueError("world 검출기일 때만 프롬프트를 바꿀 수 있습니다.")
            return self.backend.set_classes(names)

    def detect(self, frame, conf):
        with self.lock:
            t0 = time.perf_counter()
            dets = self.backend(frame, conf)
            return dets, (time.perf_counter() - t0) * 1000


ENGINE: Engine | None = None


# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ObjectDetect/1.0"

    def log_message(self, fmt, *args):            # 프레임마다 찍히면 시끄럽다
        pass

    # -- 보조 -------------------------------------------------------------
    def _send(self, code, body: bytes, ctype: str, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"phone.html not found", "text/plain")
        elif path == "/manifest.webmanifest":
            self._send(200, json.dumps(MANIFEST).encode("utf-8"),
                       "application/manifest+json")
        elif path == "/config":
            self._json(ENGINE.info())
        else:
            self._send(404, b"not found", "text/plain")

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/detect":
            return self._detect()
        if path == "/classes":
            return self._classes()
        if path == "/backend":
            return self._backend()
        self._send(404, b"not found", "text/plain")

    def _detect(self):
        raw = self._body()
        if not raw:
            return self._json({"error": "빈 요청"}, 400)
        try:
            conf = float(self.headers.get("X-Conf") or 0.25)
        except ValueError:
            conf = 0.25
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return self._json({"error": "이미지를 해석할 수 없습니다"}, 400)
        try:
            dets, ms = ENGINE.detect(frame, conf)
        except Exception as e:                    # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        h, w = frame.shape[:2]
        self._json({
            "w": w, "h": h, "ms": round(ms, 1),
            "objects": [{"label": d.label, "ko": to_ko(d.label),
                         "score": round(d.score, 3),
                         "box": [round(v, 1) for v in d.box]} for d in dets],
        })

    def _classes(self):
        try:
            names = json.loads(self._body() or b"{}").get("classes") or []
        except json.JSONDecodeError:
            return self._json({"error": "잘못된 JSON"}, 400)
        try:
            applied = ENGINE.set_classes(names)
        except Exception as e:                    # noqa: BLE001
            return self._json({"error": str(e)}, 400)
        return self._json({"classes": applied, "info": ENGINE.info()})

    def _backend(self):
        try:
            d = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "잘못된 JSON"}, 400)
        kind = d.get("kind") or "auto"
        try:
            ENGINE.switch(kind, d.get("weights"), d.get("classes"))
        except Exception as e:                    # noqa: BLE001
            return self._json({"error": str(e)}, 400)
        return self._json({"info": ENGINE.info()})

    def handle_one_request(self):
        # 폰이 창을 닫으면 연결이 뚝 끊긴다. 그때마다 역추적을 뱉지 않게 한다.
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, ssl.SSLError,
                TimeoutError):
            self.close_connection = True


# ---------------------------------------------------------------------------
def main(argv=None):
    global ENGINE
    ap = argparse.ArgumentParser(description="휴대폰용 사물 인지 서버")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "yolo", "world", "ssdlite", "frcnn", "frcnn50"])
    ap.add_argument("--weights", default=None)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--prompt", default=None,
                    help="world 백엔드에서 찾을 것 (쉼표로, 영어=한글 가능)")
    ap.add_argument("--http", action="store_true",
                    help="TLS 없이 띄운다 (PC 브라우저 localhost 확인용. "
                         "폰에서는 카메라가 안 열린다)")
    a = ap.parse_args(argv)

    ips = all_ipv4()
    prompt = [s for s in (a.prompt or "").split(",") if s.strip()] or None

    print("검출기 준비 중 (%s)…" % a.backend)
    t0 = time.perf_counter()
    try:
        ENGINE = Engine(a.backend, a.device, a.weights, prompt)
    except Exception as e:                        # noqa: BLE001
        print("[오류] 검출기를 만들지 못했습니다: %s" % e)
        return 1
    info = ENGINE.info()
    print("준비 완료: %s / %s (%.1f초)"
          % (info["name"], info["device"], time.perf_counter() - t0))
    if info["world"]:
        print("찾는 것: %s" % ", ".join(info["classes"]))

    httpd = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    scheme = "http"
    if not a.http:
        if not ips:
            print("[오류] 이 PC 에 접속 가능한 주소가 없습니다. 네트워크를 확인하십시오.")
            return 1
        if not ensure_cert(ips):
            print("[오류] 인증서가 없어 HTTPS 로 띄울 수 없습니다.")
            print("       openssl 이 없으면 --http 로 띄우되, 폰에서는 카메라가")
            print("       열리지 않습니다(브라우저가 막습니다).")
            return 1
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"

    print()
    print("=" * 64)
    print("  폰 브라우저에서 열 주소")
    print()
    if not ips:
        print("      (주소를 찾지 못했습니다)")
    for ip in ips:
        note = describe_ip(ip)
        print("      %s://%s:%d/%s"
              % (scheme, ip, a.port, ("        <- " + note) if note else ""))
    print()
    if len(ips) > 1:
        print("  * 주소가 여러 개입니다. **폰과 같은 망에 있는 것**을 쓰십시오.")
        print("    폰 핫스팟에 이 PC 를 연결했다면 핫스팟 쪽 주소입니다.")
    else:
        print("  * 폰과 이 PC 가 같은 망에 있어야 합니다.")
        print("    유선만 쓰는 PC 라면 폰 핫스팟(또는 USB 테더링)에 붙이십시오.")
    if scheme == "https":
        print("  * 처음 열면 '연결이 비공개가 아닙니다' 경고가 뜹니다.")
        print("    고급 → 계속 진행 을 누르십시오 (자체 서명 인증서라 그렇습니다).")
    print("  * 열린 뒤 공유 → 홈 화면에 추가 하면 앱처럼 씁니다.")
    print("  * 끄려면 이 창에서 Ctrl+C.")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
