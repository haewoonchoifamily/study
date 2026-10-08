# -*- coding: utf-8 -*-
"""카메라·영상·이미지에서 사물을 찾아 박스를 치고 이름표를 붙인다.

    python detect.py                     # 카메라 0번 실시간
    python detect.py --source 1          # 카메라 1번
    python detect.py --source cat.jpg    # 이미지 한 장
    python detect.py --source clip.mp4   # 동영상 파일
    python detect.py --save out.mp4      # 결과를 파일로 저장

실행 중 단축키
    q / ESC  종료          space  일시정지        s  현재 화면 저장
    [ / ]    신뢰도 -/+    l      한글/영문 전환   h  도움말 표시

검출기 (--backend)
    auto     ultralytics 가 있으면 yolo, 없으면 ssdlite   (기본)
    yolo     ultralytics YOLO — COCO 80종         가장 빠르고 정확
    world    YOLO-World — 찾을 것을 글자로 지정   80종에 없는 것도 찾는다
    frcnn    torchvision Faster R-CNN MobileNetV3 중간
    frcnn50  torchvision Faster R-CNN ResNet50    가장 정확
    ssdlite  torchvision SSDLite320 MobileNetV3   의존성 최소

world 는 클래스 목록이 고정돼 있지 않다. 찾고 싶은 것을 영어로 적으면 된다.

    python detect.py --backend world --prompt "door, stairs, safety helmet, mug"

프롬프트는 CLIP 텍스트 인코더를 거치므로 **영어**가 훨씬 잘 맞는다. 그리고
open-vocabulary 는 신뢰도가 전반적으로 낮게 나오므로 문턱값도 낮게 잡는다
(--conf 를 안 주면 world 는 0.15, 나머지는 0.5 를 쓴다).

RTX 5070 Ti 에서 640x480 한 장 (20회 평균, 워밍업 후):

    yolo11n   5.3 ms  |  frcnn    12.7 ms  |  frcnn50  34.0 ms
    yolo11s   6.2 ms  |  ssdlite  38.2 ms

SSDLite 는 이름과 달리 GPU 에서 가장 느리다. 작은 연산이 많아 GPU 를 못 채운다.
CPU 나 의존성 최소화가 필요할 때만 의미가 있다.

가중치는 처음 한 번 자동으로 내려받는다(yolo11n 5 MB ~ frcnn50 167 MB).
클래스는 COCO 80종(사람·자동차·의자·노트북 …)이다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coco_ko import add_alias, to_ko

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
KO_FONTS = [                                     # 한글 라벨용 (윈도우 기본 폰트)
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\malgunsl.ttf",
    r"C:\Windows\Fonts\gulim.ttc",
    r"C:\Windows\Fonts\batang.ttc",
]


class Detection:
    """찾아낸 물체 하나."""

    __slots__ = ("x1", "y1", "x2", "y2", "score", "label")

    def __init__(self, x1, y1, x2, y2, score, label):
        self.x1, self.y1, self.x2, self.y2 = float(x1), float(y1), float(x2), float(y2)
        self.score = float(score)
        self.label = str(label)

    @property
    def box(self):
        return (self.x1, self.y1, self.x2, self.y2)

    def as_dict(self):
        return dict(label=self.label, score=round(self.score, 4),
                    box=[round(v, 1) for v in self.box])


# ---------------------------------------------------------------------------
# 검출기
# ---------------------------------------------------------------------------
class TorchvisionBackend:
    """torchvision 의 사전학습 COCO 검출 모델."""

    MODELS = {
        "ssdlite": ("ssdlite320_mobilenet_v3_large",
                    "SSDLite320_MobileNet_V3_Large_Weights"),
        "frcnn":   ("fasterrcnn_mobilenet_v3_large_320_fpn",
                    "FasterRCNN_MobileNet_V3_Large_320_FPN_Weights"),
        "frcnn50": ("fasterrcnn_resnet50_fpn_v2",
                    "FasterRCNN_ResNet50_FPN_V2_Weights"),
    }

    def __init__(self, kind="ssdlite", device="auto"):
        import torch
        from torchvision.models import detection as det

        if kind not in self.MODELS:
            raise ValueError("알 수 없는 모델: %s (%s 중 하나)"
                             % (kind, ", ".join(self.MODELS)))
        fn_name, w_name = self.MODELS[kind]

        self.torch = torch
        self.device = self._pick_device(device)
        weights = getattr(det, w_name).COCO_V1
        self.categories = weights.meta["categories"]
        self.model = getattr(det, fn_name)(weights=weights).eval().to(self.device)
        self.name = "torchvision/%s" % kind

    def _pick_device(self, device):
        if device == "auto":
            return "cuda" if self.torch.cuda.is_available() else "cpu"
        return device

    def __call__(self, frame_bgr, conf):
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        x = self.torch.from_numpy(rgb).permute(2, 0, 1).to(self.device)
        x = x.float().div_(255.0)
        with self.torch.inference_mode():
            out = self.model([x])[0]

        keep = out["scores"] >= conf
        boxes = out["boxes"][keep].cpu().numpy()
        scores = out["scores"][keep].cpu().numpy()
        labels = out["labels"][keep].cpu().numpy()

        res = []
        for (x1, y1, x2, y2), s, li in zip(boxes, scores, labels):
            name = self.categories[li] if li < len(self.categories) else str(li)
            if name in ("N/A", "__background__"):
                continue
            res.append(Detection(x1, y1, x2, y2, s, name))
        return res


class YoloBackend:
    """ultralytics YOLO. 설치돼 있을 때만 쓴다."""

    def __init__(self, weights="yolo11n.pt", device="auto"):
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise RuntimeError(
                "ultralytics 가 없습니다. 설치하거나 다른 백엔드를 쓰십시오:\n"
                "    python -m pip install ultralytics\n"
                "    python detect.py --backend ssdlite") from e
        self.model = YOLO(weights)
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        self.device = device
        self.name = "yolo/%s" % weights

    def __call__(self, frame_bgr, conf):
        r = self.model(frame_bgr, conf=conf, device=self.device, verbose=False)[0]
        names = r.names
        res = []
        for b in r.boxes:
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            res.append(Detection(x1, y1, x2, y2, float(b.conf[0]),
                                 names[int(b.cls[0])]))
        return res


class WorldBackend:
    """YOLO-World — 찾을 것을 글자로 지정하는 open-vocabulary 검출기.

    COCO 80종에 매여 있지 않다. `set_classes(["door", "mug", ...])` 로 언제든
    찾을 대상을 바꿀 수 있고, 모델을 다시 만들 필요가 없다.

    주의
      * 프롬프트는 CLIP 텍스트 인코더를 타므로 영어가 훨씬 잘 맞는다.
      * 신뢰도가 고정 어휘 모델보다 전반적으로 낮게 나온다. 0.1~0.25 로 본다.
      * 첫 set_classes 는 CLIP 을 올리느라 1~2초 걸리고, 이후는 0.1초 안쪽이다.
    """

    DEFAULT_PROMPT = [
        "person", "door", "window", "stairs", "desk", "chair", "monitor",
        "keyboard", "mug", "bottle", "box", "trash can", "backpack",
        "potted plant", "lamp",
    ]
    suggested_conf = 0.15                        # open-vocabulary 는 낮게 본다

    def __init__(self, weights="yolov8s-worldv2.pt", device="auto", classes=None):
        try:
            from ultralytics import YOLOWorld
        except ImportError as e:
            raise RuntimeError(
                "ultralytics 가 없습니다:\n"
                "    python -m pip install ultralytics") from e
        self.model = YOLOWorld(weights)
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        self.device = device
        self.weights = weights
        self.classes = []
        self.set_classes(classes or self.DEFAULT_PROMPT)

    @property
    def name(self):
        return "world/%s (%d종)" % (self.weights, len(self.classes))

    def set_classes(self, names):
        """찾을 대상을 바꾼다. 모델을 다시 만들지 않아도 된다.

        "door=문" 처럼 적으면 CLIP 에는 영어(door)를 주고 화면에는 한글(문)을
        보여준다. 프롬프트는 영어여야 잘 맞지만 라벨은 한글로 보고 싶을 때 쓴다.
        """
        prompts, aliases = [], {}
        for raw in names:
            s = (raw or "").strip()
            if not s:
                continue
            if "=" in s:
                en, ko = (p.strip() for p in s.split("=", 1))
                if en and ko:
                    aliases[en] = ko
                    s = en
                else:
                    s = en or s
            prompts.append(s)
        if not prompts:
            raise ValueError("찾을 것을 하나 이상 적어야 합니다.")
        self.model.set_classes(prompts)
        for en, ko in aliases.items():
            add_alias(en, ko)
        self.classes = prompts
        return prompts

    def __call__(self, frame_bgr, conf):
        r = self.model(frame_bgr, conf=conf, device=self.device, verbose=False)[0]
        names = r.names
        res = []
        for b in r.boxes:
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            res.append(Detection(x1, y1, x2, y2, float(b.conf[0]),
                                 names[int(b.cls[0])]))
        return res


def make_backend(kind, device="auto", weights=None, classes=None):
    if kind == "auto":
        # ultralytics 가 있으면 그게 제일 빠르고 정확하다. 없으면 torchvision.
        try:
            return YoloBackend(weights or "yolo11n.pt", device)
        except RuntimeError:
            print("[알림] ultralytics 가 없어 ssdlite 로 실행합니다."
                  " (pip install ultralytics 하면 훨씬 빨라집니다)")
            return TorchvisionBackend("ssdlite", device)
    if kind == "yolo":
        return YoloBackend(weights or "yolo11n.pt", device)
    if kind == "world":
        return WorldBackend(weights or "yolov8s-worldv2.pt", device, classes)
    return TorchvisionBackend(kind, device)


# ---------------------------------------------------------------------------
# 그리기
# ---------------------------------------------------------------------------
def color_for(name: str):
    """클래스마다 항상 같은 색. 어두운 배경 위에서도 보이게 밝기를 올린다."""
    h = 0
    for ch in name:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    hsv = np.uint8([[[h % 180, 200, 255]]])
    b, g, r = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
    return int(b), int(g), int(r)


class Labeler:
    """이름표 그리기. 한글이 필요하면 PIL 로, 아니면 OpenCV 로 그린다.

    OpenCV 의 putText 는 한글 글리프가 없어 네모로 나온다. 그래서 한글일 때만
    프레임을 PIL 로 한 번 넘겨 라벨을 모두 그리고 되돌린다(프레임당 왕복 1회).
    """

    def __init__(self, lang="ko", font_size=18):
        self.lang = lang
        self.font_size = font_size
        self.font = None
        self.ImageDraw = None
        self.Image = None
        if lang == "ko":
            self._load_font()
            if self.font is None:
                print("[알림] 한글 폰트를 찾지 못해 영문 이름으로 표시합니다.")
                self.lang = "en"

    def _load_font(self):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return
        for path in KO_FONTS:
            if os.path.isfile(path):
                try:
                    self.font = ImageFont.truetype(path, self.font_size)
                    self.Image, self.ImageDraw = Image, ImageDraw
                    return
                except OSError:
                    continue

    def text(self, name: str) -> str:
        return to_ko(name) if self.lang == "ko" else name

    def measure(self, s: str):
        if self.font is not None:
            l, t, r, b = self.font.getbbox(s)
            return r - l, b - t
        (w, h), _ = cv2.getTextSize(s, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        return w, h

    def draw(self, frame, items):
        """items: [(x, y, 글자, 색)] — (x, y) 는 글자 왼쪽 위 모서리."""
        if not items:
            return frame
        if self.font is None:
            for x, y, s, _ in items:
                cv2.putText(frame, s, (int(x), int(y) + 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(frame, s, (int(x), int(y) + 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
                            cv2.LINE_AA)
            return frame
        img = self.Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        d = self.ImageDraw.Draw(img)
        for x, y, s, _ in items:
            d.text((x, y), s, font=self.font, fill=(255, 255, 255),
                   stroke_width=2, stroke_fill=(0, 0, 0))
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def draw_detections(frame, dets, labeler, show_score=True):
    """박스를 그리고 이름표 목록을 만들어 한 번에 올린다."""
    h, w = frame.shape[:2]
    thick = max(2, int(round(min(h, w) / 400)))
    pad = 4
    items = []
    for d in dets:
        color = color_for(d.label)
        x1, y1 = int(d.x1), int(d.y1)
        x2, y2 = int(d.x2), int(d.y2)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, thick)

        s = labeler.text(d.label)
        if show_score:
            s = "%s %.0f%%" % (s, d.score * 100)
        tw, th = labeler.measure(s)
        # 박스 위에 붙이되, 화면 밖으로 나가면 박스 안쪽으로 내린다
        ty = y1 - th - 2 * pad
        if ty < 0:
            ty = y1 + pad
        tx = min(x1, max(0, w - tw - 2 * pad))
        cv2.rectangle(frame, (tx, ty), (tx + tw + 2 * pad, ty + th + 2 * pad),
                      color, -1)
        items.append((tx + pad, ty + pad, s, color))
    return labeler.draw(frame, items)


def draw_hud(frame, lines):
    """왼쪽 위 상태 표시. 영문·숫자만 쓰므로 OpenCV 로 충분하다."""
    y = 8
    for line in lines:
        (tw, th), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (8, y), (8 + tw + 10, y + th + 10), (0, 0, 0), -1)
        cv2.putText(frame, line, (13, y + th + 3), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1, cv2.LINE_AA)
        y += th + 14
    return frame


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------
def open_camera(index):
    """윈도우에서는 백엔드마다 되고 안 되고가 갈려서 순서대로 시도한다."""
    tries = [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("기본", None)]
    for name, api in tries:
        cap = cv2.VideoCapture(index) if api is None else cv2.VideoCapture(index, api)
        if cap.isOpened():
            ok, _ = cap.read()
            if ok:
                print("카메라 %d번 열림 (%s)" % (index, name))
                return cap
        cap.release()
    return None


def list_cameras(n=5):
    found = []
    for i in range(n):
        cap = open_camera(i)
        if cap is not None:
            found.append(i)
            cap.release()
    return found


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def summarize(dets):
    c = Counter(d.label for d in dets)
    return ", ".join("%s x%d" % (to_ko(k), v) for k, v in c.most_common()) or "없음"


def run_image(path, backend, labeler, args):
    frame = cv2.imread(path)
    if frame is None:
        print("[오류] 이미지를 읽을 수 없습니다: %s" % path)
        return 1
    t0 = time.perf_counter()
    dets = backend(frame, args.conf)
    dt = (time.perf_counter() - t0) * 1000

    print("\n%s  (%dx%d, 추론 %.0f ms)" % (path, frame.shape[1], frame.shape[0], dt))
    print("-" * 64)
    if dets:
        print("%-16s %-8s %s" % ("물체", "신뢰도", "박스 (x1, y1, x2, y2)"))
        for d in sorted(dets, key=lambda d: -d.score):
            print("%-16s %6.1f%%   %5.0f %5.0f %5.0f %5.0f"
                  % (to_ko(d.label), d.score * 100, d.x1, d.y1, d.x2, d.y2))
    else:
        print("찾은 물체가 없습니다. --conf 를 낮춰 보십시오 (지금 %.2f)." % args.conf)
    print("-" * 64)
    print("요약:", summarize(dets))

    out = draw_detections(frame.copy(), dets, labeler, not args.no_score)
    dst = args.save or str(Path(path).with_name(Path(path).stem + "_det.jpg"))
    cv2.imwrite(dst, out)
    print("저장: %s" % dst)
    if args.json:
        write_json(args.json, [d.as_dict() for d in dets], source=path)
    if not args.no_window:
        cv2.imshow("detect  |  q: quit", out)
        while cv2.waitKey(50) not in (ord("q"), 27):
            if cv2.getWindowProperty("detect  |  q: quit", cv2.WND_PROP_VISIBLE) < 1:
                break
        cv2.destroyAllWindows()
    return 0


def run_stream(cap, backend, labeler, args, is_camera):
    win = "detect  |  q:quit  space:pause  s:save  [ ]:conf  l:lang"
    writer = None
    if args.save:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        writer = cv2.VideoWriter(args.save, cv2.VideoWriter_fourcc(*"mp4v"),
                                 fps if fps > 1 else 30.0, (w, h))
        print("저장 중: %s (%dx%d @ %.0f fps)" % (args.save, w, h, fps))

    times = deque(maxlen=30)
    conf = args.conf
    paused = False
    show_help = False
    n_frame = 0
    records = []
    last_frame = None
    last_dets = []

    print("\n실행 중. 창에서 q 를 누르면 끝납니다.")
    while True:
        if not paused:
            ok, frame = cap.read()
            if not ok:
                if is_camera:
                    print("[오류] 프레임을 못 읽었습니다. 카메라 연결을 확인하십시오.")
                else:
                    print("영상 끝.")
                break
            t0 = time.perf_counter()
            dets = backend(frame, conf)
            times.append(time.perf_counter() - t0)
            n_frame += 1
            last_frame, last_dets = frame, dets
            if args.json:
                records.append(dict(frame=n_frame,
                                    objects=[d.as_dict() for d in dets]))
        else:
            frame, dets = last_frame, last_dets
            if frame is None:
                break

        vis = draw_detections(frame.copy(), dets, labeler, not args.no_score)
        infer_ms = 1000 * sum(times) / len(times) if times else 0.0
        hud = ["%s | %s | conf %.2f" % (backend.name,
                                        getattr(backend, "device", "-"), conf),
               "%.0f ms  (%.1f fps)  objects: %d" % (infer_ms,
                                                     1000 / infer_ms if infer_ms else 0,
                                                     len(dets))]
        if paused:
            hud.append("PAUSED")
        if show_help:
            hud += ["q quit / space pause / s save",
                    "[ ] confidence / l ko-en / h help"]
        vis = draw_hud(vis, hud)

        if writer is not None:
            writer.write(vis)
        if not args.no_window:
            cv2.imshow(win, vis)
            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27):
                break
            if k == ord(" "):
                paused = not paused
            elif k == ord("s"):
                name = "snapshot_%s.jpg" % time.strftime("%Y%m%d_%H%M%S")
                cv2.imwrite(name, vis)
                print("저장: %s  (%s)" % (name, summarize(dets)))
            elif k == ord("["):
                conf = max(0.05, round(conf - 0.05, 2))
            elif k == ord("]"):
                conf = min(0.95, round(conf + 0.05, 2))
            elif k == ord("l"):
                labeler.lang = "en" if labeler.lang == "ko" else "ko"
            elif k == ord("h"):
                show_help = not show_help
            if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                break
        if args.max_frames and n_frame >= args.max_frames:
            break

    cap.release()
    if writer is not None:
        writer.release()
    cv2.destroyAllWindows()
    if args.json and records:
        write_json(args.json, records, source=str(args.source))
    if times:
        print("프레임 %d장, 평균 추론 %.0f ms (%.1f fps)"
              % (n_frame, 1000 * sum(times) / len(times),
                 len(times) / sum(times)))
    return 0


def write_json(path, data, source=""):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(source=source, data=data), f, ensure_ascii=False, indent=2)
    print("JSON 저장: %s" % path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="카메라·영상·이미지에서 사물을 찾아 박스와 이름표를 그린다.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0",
                    help="카메라 번호(0,1,…) 또는 이미지/영상 파일 경로 (기본 0)")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "yolo", "world", "ssdlite", "frcnn", "frcnn50"],
                    help="검출기 (기본 auto: ultralytics 가 있으면 yolo)")
    ap.add_argument("--weights", default=None,
                    help="yolo 가중치 (기본 yolo11n.pt, 더 정확하게는 yolo11s.pt)")
    ap.add_argument("--prompt", default=None,
                    help="world 백엔드에서 찾을 것을 쉼표로 나열 (영어 권장). "
                         "예: \"door, stairs, safety helmet\"")
    ap.add_argument("--device", default="auto", help="auto | cuda | cpu")
    ap.add_argument("--conf", type=float, default=None,
                    help="신뢰도 문턱값 (기본: world 0.15, 나머지 0.5)")
    ap.add_argument("--lang", default="ko", choices=["ko", "en"],
                    help="이름표 언어 (기본 ko)")
    ap.add_argument("--font-size", type=int, default=18, help="이름표 글자 크기")
    ap.add_argument("--save", default=None, help="결과를 저장할 파일 (.jpg / .mp4)")
    ap.add_argument("--json", default=None, help="검출 결과를 JSON 으로 저장")
    ap.add_argument("--no-score", action="store_true", help="이름표에 신뢰도 숨김")
    ap.add_argument("--no-window", action="store_true", help="창 없이 실행")
    ap.add_argument("--max-frames", type=int, default=0, help="이 장수만 처리하고 종료")
    ap.add_argument("--list-cameras", action="store_true", help="카메라 목록만 출력")
    a = ap.parse_args(argv)

    if a.list_cameras:
        cams = list_cameras()
        print("사용 가능한 카메라:", cams or "없음")
        return 0

    print("검출기 준비 중 (%s)…" % a.backend)
    t0 = time.perf_counter()
    prompt = [s for s in (a.prompt or "").split(",") if s.strip()] or None
    if prompt and a.backend != "world":
        print("[알림] --prompt 는 --backend world 에서만 씁니다. 무시합니다.")
    try:
        backend = make_backend(a.backend, a.device, a.weights, prompt)
    except Exception as e:                        # noqa: BLE001
        print("[오류] 검출기를 만들지 못했습니다: %s" % e)
        return 1
    if a.conf is None:                            # 백엔드마다 적당한 값이 다르다
        a.conf = getattr(backend, "suggested_conf", 0.5)
    print("준비 완료: %s / %s (%.1f초, conf %.2f)"
          % (backend.name, getattr(backend, "device", "-"),
             time.perf_counter() - t0, a.conf))
    if a.backend == "world":
        print("찾는 것: %s" % ", ".join(backend.classes))

    labeler = Labeler(a.lang, a.font_size)

    src = str(a.source)
    if src.isdigit():
        cap = open_camera(int(src))
        if cap is None:
            print("[오류] 카메라 %s번을 열 수 없습니다." % src)
            cams = list_cameras()
            print("  사용 가능한 카메라:", cams or "없음 (연결 확인)")
            print("  파일로도 쓸 수 있습니다:  python detect.py --source 영상.mp4")
            return 1
        return run_stream(cap, backend, labeler, a, is_camera=True)

    if not os.path.isfile(src):
        print("[오류] 파일이 없습니다: %s" % src)
        return 1
    if Path(src).suffix.lower() in IMAGE_EXT:
        return run_image(src, backend, labeler, a)
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print("[오류] 영상을 열 수 없습니다: %s" % src)
        return 1
    return run_stream(cap, backend, labeler, a, is_camera=False)


if __name__ == "__main__":
    sys.exit(main())
