# -*- coding: utf-8 -*-
"""사물 인지 GUI — 옵션을 화면에서 고르고 결과를 바로 본다.

    python detect_gui.py

검출·그리기는 detect.py 것을 그대로 쓴다. 여기는 화면과 조작만 담당한다.
수집·추론은 작업 스레드에서 하고, tkinter 는 메인 스레드에서만 건드린다.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import Counter, deque

import cv2
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coco_ko import COCO_KO, to_ko
from detect import (IMAGE_EXT, Labeler, WorldBackend, draw_detections,
                    list_cameras, make_backend, open_camera)

from PIL import Image, ImageTk

BACKENDS = [
    ("auto     (ultralytics 있으면 YOLO)", "auto"),
    ("yolo     YOLO11 — COCO 80종, 가장 빠름", "yolo"),
    ("world    YOLO-World — 찾을 것을 글자로", "world"),
    ("frcnn    Faster R-CNN MobileNet", "frcnn"),
    ("frcnn50  Faster R-CNN ResNet50 — 가장 정확", "frcnn50"),
    ("ssdlite  SSDLite (의존성 최소)", "ssdlite"),
]
YOLO_WEIGHTS = ["yolo11n.pt", "yolo11s.pt", "yolo11m.pt", "yolo11l.pt", "yolo11x.pt"]
WORLD_WEIGHTS = ["yolov8s-worldv2.pt", "yolov8m-worldv2.pt", "yolov8l-worldv2.pt"]
RESOLUTIONS = [("카메라 기본", None), ("640 x 480", (640, 480)),
               ("1280 x 720", (1280, 720)), ("1920 x 1080", (1920, 1080))]
CLASS_NAMES = sorted(COCO_KO)                    # COCO 80종


class Worker(threading.Thread):
    """프레임을 읽어 추론하고 큐에 넣는다. tkinter 는 절대 건드리지 않는다."""

    def __init__(self, cap, backend, conf_get, q):
        super().__init__(daemon=True)
        self.cap = cap
        self.backend = backend
        self.conf_get = conf_get                 # 슬라이더를 매 프레임 다시 읽는다
        self.q = q
        self.stop_flag = threading.Event()
        self.error = None

    def run(self):
        try:
            while not self.stop_flag.is_set():
                ok, frame = self.cap.read()
                if not ok:
                    self.q.put(("end", None, None))
                    break
                t0 = time.perf_counter()
                dets = self.backend(frame, self.conf_get())
                dt = time.perf_counter() - t0
                if self.q.full():
                    try:
                        self.q.get_nowait()      # 밀리면 오래된 프레임부터 버린다
                    except queue.Empty:
                        pass
                self.q.put(("frame", (frame, dets, dt), None))
        except Exception as e:                    # noqa: BLE001
            self.error = e
            self.q.put(("error", None, e))

    def stop(self):
        self.stop_flag.set()


class DetectGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("사물 인지 — 박스 & 태그")
        root.geometry("1180x760")
        root.minsize(980, 640)

        self.backend = None
        self.backend_key = None                  # 지금 올라간 검출기 식별자
        self.cap = None
        self.worker = None
        self.writer = None
        self.q = queue.Queue(maxsize=2)
        self.times = deque(maxlen=30)
        # tkinter 변수(Tcl)는 스레드 안전하지 않다. 워커가 슬라이더를 직접
        # 읽으면 안 되므로, GUI 스레드가 갱신하는 평범한 float 을 따로 둔다.
        self.conf = 0.5
        self.photo = None                        # GC 방지용 참조
        self.last_vis = None
        self.last_dets = []
        self.records = []
        self.n_frame = 0
        self.labeler = Labeler("ko")

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(30, self._poll)
        self._refresh_cameras()

    # ---------------------------------------------------------------- 화면
    def _build(self):
        left = ttk.Frame(self.root, padding=10)
        left.pack(side="left", fill="y")
        right = ttk.Frame(self.root, padding=(0, 10, 10, 10))
        right.pack(side="right", fill="both", expand=True)

        # ---- 입력 ----
        g = ttk.LabelFrame(left, text="입력", padding=8)
        g.pack(fill="x")
        self.v_srcmode = tk.StringVar(value="camera")
        ttk.Radiobutton(g, text="카메라", variable=self.v_srcmode, value="camera",
                        command=self._sync_source).grid(row=0, column=0, sticky="w")
        self.cb_cam = ttk.Combobox(g, state="readonly", width=10, values=[])
        self.cb_cam.grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(g, text="다시 찾기", width=9,
                   command=self._refresh_cameras).grid(row=0, column=2)

        ttk.Radiobutton(g, text="파일", variable=self.v_srcmode, value="file",
                        command=self._sync_source).grid(row=1, column=0, sticky="w",
                                                        pady=(6, 0))
        self.v_file = tk.StringVar()
        ttk.Entry(g, textvariable=self.v_file, width=18).grid(row=1, column=1,
                                                              sticky="we", padx=4,
                                                              pady=(6, 0))
        ttk.Button(g, text="찾아보기", width=9,
                   command=self._browse).grid(row=1, column=2, pady=(6, 0))

        ttk.Label(g, text="해상도").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.cb_res = ttk.Combobox(g, state="readonly", width=22,
                                   values=[n for n, _ in RESOLUTIONS])
        self.cb_res.current(0)
        self.cb_res.grid(row=2, column=1, columnspan=2, sticky="we", pady=(6, 0))
        g.columnconfigure(1, weight=1)

        # ---- 검출기 ----
        g = ttk.LabelFrame(left, text="검출기", padding=8)
        g.pack(fill="x", pady=(10, 0))
        self.cb_backend = ttk.Combobox(g, state="readonly", width=34,
                                       values=[n for n, _ in BACKENDS])
        self.cb_backend.current(0)
        self.cb_backend.grid(row=0, column=0, columnspan=2, sticky="we")
        self.cb_backend.bind("<<ComboboxSelected>>", lambda e: self._sync_backend())

        ttk.Label(g, text="YOLO 가중치").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.cb_weights = ttk.Combobox(g, state="readonly", width=16,
                                       values=YOLO_WEIGHTS)
        self.cb_weights.current(0)
        self.cb_weights.grid(row=1, column=1, sticky="we", pady=(6, 0))

        ttk.Label(g, text="장치").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.cb_device = ttk.Combobox(g, state="readonly", width=16,
                                      values=["auto", "cuda", "cpu"])
        self.cb_device.current(0)
        self.cb_device.grid(row=2, column=1, sticky="we", pady=(6, 0))
        g.columnconfigure(1, weight=1)

        # ---- 표시 ----
        g = ttk.LabelFrame(left, text="표시", padding=8)
        g.pack(fill="x", pady=(10, 0))
        self.v_conf = tk.DoubleVar(value=0.5)
        row = ttk.Frame(g)
        row.pack(fill="x")
        ttk.Label(row, text="신뢰도").pack(side="left")
        self.lb_conf = ttk.Label(row, text="0.50", width=5)
        self.lb_conf.pack(side="right")
        ttk.Scale(g, from_=0.05, to=0.95, variable=self.v_conf, orient="horizontal",
                  command=self._sync_conf).pack(fill="x")

        row = ttk.Frame(g)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="이름표").pack(side="left")
        self.v_lang = tk.StringVar(value="ko")
        ttk.Radiobutton(row, text="한글", variable=self.v_lang, value="ko",
                        command=self._sync_lang).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(row, text="영문", variable=self.v_lang, value="en",
                        command=self._sync_lang).pack(side="left")

        row = ttk.Frame(g)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="글자 크기").pack(side="left")
        self.v_font = tk.IntVar(value=18)
        ttk.Spinbox(row, from_=10, to=40, width=5, textvariable=self.v_font,
                    command=self._sync_lang).pack(side="left", padx=6)
        self.v_score = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="신뢰도 표시",
                        variable=self.v_score).pack(side="left", padx=(10, 0))

        # ---- 클래스 고르기 / 프롬프트 ----
        self.fr_cls = ttk.LabelFrame(left, text="찾을 물체 (선택 안 하면 전부)",
                                     padding=8)
        g = self.fr_cls
        g.pack(fill="both", expand=True, pady=(10, 0))

        # world 백엔드용 — 찾을 것을 글자로 적는다
        self.fr_prompt = ttk.Frame(g)
        ttk.Label(self.fr_prompt, justify="left",
                  text="영어로, 쉼표로 구분.\n한글로 보고 싶으면 door=문 처럼.").pack(
                      anchor="w")
        self.txt_prompt = tk.Text(self.fr_prompt, height=5, wrap="word")
        self.txt_prompt.pack(fill="x", pady=(4, 0))
        self.txt_prompt.insert("1.0", ", ".join(WorldBackend.DEFAULT_PROMPT))
        ttk.Button(self.fr_prompt, text="프롬프트 적용",
                   command=self._apply_prompt).pack(anchor="e", pady=(4, 0))

        # 고정 어휘 백엔드용 — COCO 80종에서 고른다
        self.fr_list = ttk.Frame(g)
        self.fr_list.pack(fill="both", expand=True)
        row = ttk.Frame(self.fr_list)
        row.pack(fill="x")
        ttk.Button(row, text="전체 해제", width=10,
                   command=lambda: self.lst.selection_clear(0, "end")).pack(side="left")
        ttk.Button(row, text="사람만", width=8,
                   command=self._only_person).pack(side="left", padx=4)
        box = ttk.Frame(self.fr_list)
        box.pack(fill="both", expand=True, pady=(6, 0))
        sb = ttk.Scrollbar(box, orient="vertical")
        self.lst = tk.Listbox(box, selectmode="extended", height=8, exportselection=False,
                              yscrollcommand=sb.set)
        sb.config(command=self.lst.yview)
        sb.pack(side="right", fill="y")
        self.lst.pack(side="left", fill="both", expand=True)
        for n in CLASS_NAMES:
            self.lst.insert("end", "%s  (%s)" % (to_ko(n), n))

        # ---- 실행 ----
        g = ttk.Frame(left)
        g.pack(fill="x", pady=(10, 0))
        self.bt_start = ttk.Button(g, text="시작", command=self.start)
        self.bt_start.pack(side="left", fill="x", expand=True)
        self.bt_stop = ttk.Button(g, text="정지", command=self.stop, state="disabled")
        self.bt_stop.pack(side="left", fill="x", expand=True, padx=(6, 0))

        g = ttk.Frame(left)
        g.pack(fill="x", pady=(6, 0))
        ttk.Button(g, text="화면 저장", command=self.save_snapshot).pack(
            side="left", fill="x", expand=True)
        ttk.Button(g, text="JSON 저장", command=self.save_json).pack(
            side="left", fill="x", expand=True, padx=(6, 0))
        self.v_record = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="영상으로 녹화 (시작할 때 파일을 묻는다)",
                        variable=self.v_record).pack(anchor="w", pady=(6, 0))

        # ---- 오른쪽: 영상 + 목록 ----
        self.canvas = tk.Label(right, background="#202020")
        self.canvas.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(right, columns=("cls", "conf", "box"),
                                 show="headings", height=7)
        for c, t, w in (("cls", "물체", 140), ("conf", "신뢰도", 80),
                        ("box", "박스 (x1, y1, x2, y2)", 260)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", pady=(8, 0))

        self.status = ttk.Label(right, text="대기 중", anchor="w")
        self.status.pack(fill="x", pady=(6, 0))

        self._sync_source()
        self._sync_backend()

    # ------------------------------------------------------------ 보조
    def _refresh_cameras(self):
        self.status.config(text="카메라 찾는 중…")
        self.root.update_idletasks()
        cams = list_cameras(4)
        self.cb_cam["values"] = [str(c) for c in cams] or ["없음"]
        self.cb_cam.current(0)
        self.status.config(text="카메라: %s" % (cams or "없음 — 파일로 쓰십시오"))

    def _browse(self):
        p = filedialog.askopenfilename(
            title="이미지 또는 동영상",
            filetypes=[("이미지·동영상", "*.jpg *.jpeg *.png *.bmp *.webp "
                                        "*.mp4 *.avi *.mov *.mkv"),
                       ("모든 파일", "*.*")])
        if p:
            self.v_file.set(p)
            self.v_srcmode.set("file")
            self._sync_source()

    def _sync_conf(self, v):
        """슬라이더가 움직일 때 GUI 스레드에서 값을 복사해 둔다."""
        self.conf = float(v)
        self.lb_conf.config(text="%.2f" % self.conf)

    def _sync_source(self):
        cam = self.v_srcmode.get() == "camera"
        self.cb_cam.config(state="readonly" if cam else "disabled")
        self.cb_res.config(state="readonly" if cam else "disabled")

    def _sync_backend(self):
        key = BACKENDS[self.cb_backend.current()][1]
        if key == "world":
            self.cb_weights.config(values=WORLD_WEIGHTS, state="readonly")
            if self.cb_weights.get() not in WORLD_WEIGHTS:
                self.cb_weights.current(0)
            self.fr_list.pack_forget()
            self.fr_prompt.pack(fill="both", expand=True)
            self.fr_cls.config(text="찾을 것 — 글자로 지정")
            self._suggest_conf(WorldBackend.suggested_conf)
        else:
            self.cb_weights.config(values=YOLO_WEIGHTS,
                                   state="readonly" if key in ("auto", "yolo")
                                   else "disabled")
            if self.cb_weights.get() not in YOLO_WEIGHTS:
                self.cb_weights.current(0)
            self.fr_prompt.pack_forget()
            self.fr_list.pack(fill="both", expand=True)
            self.fr_cls.config(text="찾을 물체 (선택 안 하면 전부)")
            self._suggest_conf(0.5)

    def _suggest_conf(self, value):
        """백엔드마다 잘 맞는 문턱값이 다르다. 바꾸고 그 사실을 알린다."""
        if abs(self.v_conf.get() - value) < 1e-6:
            return
        self.v_conf.set(value)
        self._sync_conf(value)
        self.status.config(text="신뢰도를 %.2f 로 맞췄습니다 "
                                "(open-vocabulary 는 점수가 낮게 나옵니다)."
                                % value)

    def _prompt_list(self):
        return [s for s in self.txt_prompt.get("1.0", "end").split(",") if s.strip()]

    def _apply_prompt(self):
        names = self._prompt_list()
        if not names:
            messagebox.showwarning("프롬프트", "찾을 것을 하나 이상 적으십시오.")
            return
        if not isinstance(self.backend, WorldBackend):
            self.status.config(text="프롬프트는 다음 시작 때 반영됩니다.")
            return
        self.status.config(text="프롬프트 적용 중…")
        self.root.update_idletasks()
        try:
            applied = self.backend.set_classes(names)
        except Exception as e:                    # noqa: BLE001
            messagebox.showerror("프롬프트", str(e))
            return
        self.status.config(text="적용: %s" % ", ".join(applied))

    def _sync_lang(self):
        self.labeler = Labeler(self.v_lang.get(), self.v_font.get())
        if self.last_vis is not None and self.worker is None:
            self._render(self.last_frame, self.last_dets, 0.0)

    def _only_person(self):
        self.lst.selection_clear(0, "end")
        self.lst.selection_set(CLASS_NAMES.index("person"))
        self.lst.see(CLASS_NAMES.index("person"))

    def _allowed(self):
        """GUI 스레드에서만 부른다(_poll / _run_image).

        world 백엔드는 프롬프트 자체가 곧 찾을 목록이라 목록 필터를 쓰지 않는다.
        """
        if isinstance(self.backend, WorldBackend):
            return None
        sel = self.lst.curselection()
        return {CLASS_NAMES[i] for i in sel} if sel else None

    # ------------------------------------------------------------ 실행
    def _ensure_backend(self):
        key = BACKENDS[self.cb_backend.current()][1]
        weights = (self.cb_weights.get()
                   if key in ("auto", "yolo", "world") else None)
        device = self.cb_device.get()
        prompt = self._prompt_list() if key == "world" else None
        sig = (key, weights, device)
        if self.backend is not None and self.backend_key == sig:
            # 프롬프트만 바뀌었으면 모델을 다시 만들지 않고 갈아끼운다.
            if key == "world" and prompt and prompt != self.backend.classes:
                self.status.config(text="프롬프트 적용 중…")
                self.root.update_idletasks()
                try:
                    self.backend.set_classes(prompt)
                except Exception as e:            # noqa: BLE001
                    messagebox.showerror("프롬프트", str(e))
                    return False
            return True
        self.status.config(text="검출기 준비 중… (처음이면 가중치를 내려받습니다)")
        self.root.update_idletasks()
        try:
            self.backend = make_backend(key, device, weights, prompt)
            self.backend_key = sig
            self.times.clear()                   # 이전 검출기의 시간은 버린다
        except Exception as e:                    # noqa: BLE001
            self.backend = None
            messagebox.showerror("검출기 오류", str(e))
            self.status.config(text="검출기를 만들지 못했습니다.")
            return False
        return True

    def start(self):
        if self.worker is not None:
            return
        if not self._ensure_backend():
            return

        if self.v_srcmode.get() == "file":
            path = self.v_file.get().strip()
            if not os.path.isfile(path):
                messagebox.showwarning("입력", "파일을 고르십시오.")
                return
            if os.path.splitext(path)[1].lower() in IMAGE_EXT:
                return self._run_image(path)
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                messagebox.showerror("입력", "영상을 열 수 없습니다:\n%s" % path)
                return
        else:
            idx = self.cb_cam.get()
            if not idx.isdigit():
                messagebox.showwarning("입력", "쓸 수 있는 카메라가 없습니다.")
                return
            cap = open_camera(int(idx))
            if cap is None:
                messagebox.showerror("입력", "카메라 %s번을 열 수 없습니다." % idx)
                return
            res = RESOLUTIONS[self.cb_res.current()][1]
            if res:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, res[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, res[1])

        if self.v_record.get():
            dst = filedialog.asksaveasfilename(
                title="녹화 파일", defaultextension=".mp4",
                filetypes=[("MP4", "*.mp4")])
            if dst:
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                self.writer = cv2.VideoWriter(dst, cv2.VideoWriter_fourcc(*"mp4v"),
                                              fps if fps > 1 else 30.0, (w, h))

        self.cap = cap
        self.times.clear()
        self.records.clear()
        self.n_frame = 0
        self.conf = self.v_conf.get()
        self.worker = Worker(cap, self.backend, lambda: self.conf, self.q)
        self.worker.start()
        self.bt_start.config(state="disabled")
        self.bt_stop.config(state="normal")

    def _run_image(self, path):
        frame = cv2.imread(path)
        if frame is None:
            messagebox.showerror("입력", "이미지를 읽을 수 없습니다:\n%s" % path)
            return
        self.times.clear()                       # 한 장짜리는 그 장의 시간만 쓴다
        self.records.clear()
        self.n_frame = 1
        t0 = time.perf_counter()
        dets = self.backend(frame, self.conf)
        dt = time.perf_counter() - t0
        dets = self._filter(dets)
        self.records.append(dict(frame=1, objects=[d.as_dict() for d in dets]))
        self._render(frame, dets, dt)

    def _filter(self, dets):
        allow = self._allowed()
        return [d for d in dets if allow is None or d.label in allow]

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.join(timeout=1.5)
            self.worker = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        if self.writer is not None:
            self.writer.release()
            self.writer = None
        self.bt_start.config(state="normal")
        self.bt_stop.config(state="disabled")

    # ------------------------------------------------------------ 표시
    def _poll(self):
        item = None
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
        if item is not None:
            kind, payload, err = item
            if kind == "frame":
                frame, dets, dt = payload
                self.times.append(dt)
                self.n_frame += 1
                dets = self._filter(dets)
                self.records.append(dict(frame=self.n_frame,
                                         objects=[d.as_dict() for d in dets]))
                self._render(frame, dets, dt)
            elif kind == "end":
                self.stop()
                self.status.config(text="영상 끝. 프레임 %d장." % self.n_frame)
            elif kind == "error":
                self.stop()
                messagebox.showerror("실행 오류", str(err))
        self.root.after(15, self._poll)

    def _render(self, frame, dets, dt):
        self.last_frame, self.last_dets = frame, dets
        vis = draw_detections(frame.copy(), dets, self.labeler, self.v_score.get())
        self.last_vis = vis
        if self.writer is not None:
            self.writer.write(vis)

        cw = max(320, self.canvas.winfo_width())
        ch = max(240, self.canvas.winfo_height())
        h, w = vis.shape[:2]
        s = min(cw / w, ch / h)
        if s < 1.0:
            vis_show = cv2.resize(vis, (int(w * s), int(h * s)),
                                  interpolation=cv2.INTER_AREA)
        else:
            vis_show = vis
        img = Image.fromarray(cv2.cvtColor(vis_show, cv2.COLOR_BGR2RGB))
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.config(image=self.photo)

        self.tree.delete(*self.tree.get_children())
        for d in sorted(dets, key=lambda d: -d.score):
            self.tree.insert("", "end", values=(
                self.labeler.text(d.label), "%.1f%%" % (d.score * 100),
                "%.0f, %.0f, %.0f, %.0f" % d.box))

        ms = 1000 * sum(self.times) / len(self.times) if self.times else dt * 1000
        c = Counter(self.labeler.text(d.label) for d in dets)
        self.status.config(text="%s | %s | %.0f ms (%.1f fps) | 물체 %d개%s"
                           % (self.backend.name,
                              getattr(self.backend, "device", "-"), ms,
                              1000 / ms if ms else 0, len(dets),
                              "  —  " + ", ".join("%s x%d" % (k, v)
                                                  for k, v in c.most_common())
                              if dets else ""))

    # ------------------------------------------------------------ 저장
    def save_snapshot(self):
        if self.last_vis is None:
            messagebox.showinfo("저장", "먼저 실행하십시오.")
            return
        dst = filedialog.asksaveasfilename(
            title="화면 저장", defaultextension=".jpg",
            initialfile="snapshot_%s.jpg" % time.strftime("%Y%m%d_%H%M%S"),
            filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png")])
        if dst:
            cv2.imwrite(dst, self.last_vis)
            self.status.config(text="저장: %s" % dst)

    def save_json(self):
        data = self.records or [dict(frame=1,
                                     objects=[d.as_dict() for d in self.last_dets])]
        if not data:
            messagebox.showinfo("저장", "저장할 결과가 없습니다.")
            return
        dst = filedialog.asksaveasfilename(title="JSON 저장", defaultextension=".json",
                                           filetypes=[("JSON", "*.json")])
        if dst:
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(dict(source=self.v_file.get() or self.cb_cam.get(),
                               data=data), f, ensure_ascii=False, indent=2)
            self.status.config(text="JSON 저장: %s (%d 프레임)" % (dst, len(data)))

    def on_close(self):
        self.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                             # noqa: BLE001
        pass
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    DetectGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
