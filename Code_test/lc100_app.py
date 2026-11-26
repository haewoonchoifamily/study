# -*- coding: utf-8 -*-
"""Thorlabs LC100 라인 카메라 실시간 표시 프로그램.

라인스캔은 한 번에 한 줄(LC100 은 2048화소)이 들어오므로 두 가지로 함께 본다.

  ① 라인 프로파일 : 지금 들어온 한 줄의 화소별 신호 (포화 여부 확인)
  ② 워터폴        : 최근 N줄을 쌓아 2차원 이미지로 (시간축 변화 확인)

    python lc100_app.py                  # 카메라가 준비되면 카메라, 아니면 시뮬레이션
    python lc100_app.py --source sim     # 시뮬레이션 강제
    python lc100_app.py --source webcam  # 웹캠 한 줄 (표시 경로 점검용)
    python lc100_app.py --source lc100   # 실제 LC100 강제 (드라이버 필요)
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np

import lc100_cam as cam

WATERFALL_LINES = 400        # 워터폴에 쌓아 둘 줄 수
BG_HEAD = "#1f3f5b"


def setup_matplotlib():
    import matplotlib
    from matplotlib import font_manager
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Malgun Gothic", "NanumGothic", "AppleGothic"):
        if name in have:
            matplotlib.rcParams["font.family"] = name
            break
    matplotlib.rcParams["axes.unicode_minus"] = False


class Acquirer(threading.Thread):
    """카메라에서 계속 한 줄씩 읽어 큐에 넣는 작업 스레드.

    tkinter 는 이 스레드에서 절대 건드리지 않는다. GUI 는 큐만 비워 간다.
    """

    def __init__(self, source: cam.LineSource, q: queue.Queue):
        super().__init__(daemon=True)
        self.source = source
        self.q = q
        self.stop_flag = threading.Event()
        self.error = None
        self.n_read = 0

    def run(self):
        try:
            while not self.stop_flag.is_set():
                line = self.source.read_line()
                self.n_read += 1
                if self.q.full():
                    try:
                        self.q.get_nowait()       # 밀리면 오래된 줄부터 버린다
                    except queue.Empty:
                        pass
                self.q.put((time.time(), line))
        except Exception as e:                    # noqa: BLE001
            self.error = e

    def stop(self):
        self.stop_flag.set()


class LC100App:
    def __init__(self, root: tk.Tk, source_kind: str = "auto"):
        setup_matplotlib()
        self.root = root
        self.source_kind = source_kind
        self.source = None
        self.acq = None
        self.q = queue.Queue(maxsize=64)
        self.waterfall = None
        self.last_line = None
        self.n_lines = 0
        self.t_start = None
        self.paused = False
        self.autoscale = True
        self._last_draw = 0.0      # 화면 갱신 throttle
        self._last_clim = 0.0

        root.title("Thorlabs LC100 라인 카메라 뷰어")
        root.geometry("1240x820")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(300, self.start)

    # ================= UI =================================================
    def _build(self):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        head = tk.Frame(self.root, bg=BG_HEAD)
        head.pack(fill="x")
        self.lb_title = tk.Label(head, text="Thorlabs LC100 라인 카메라", bg=BG_HEAD,
                                 fg="white", font=("맑은 고딕", 13, "bold"))
        self.lb_title.pack(side="left", padx=12, pady=7)
        self.lb_src = tk.Label(head, text="", bg=BG_HEAD, fg="#cfe0ee",
                               font=("맑은 고딕", 9))
        self.lb_src.pack(side="left", padx=6)

        ttk.Button(head, text="저장", command=self.save).pack(side="right", padx=(4, 10), pady=5)
        ttk.Button(head, text="지우기", command=self.clear).pack(side="right", padx=4, pady=5)
        self.btn_pause = ttk.Button(head, text="일시정지", command=self.toggle_pause)
        self.btn_pause.pack(side="right", padx=4, pady=5)

        # ---- 조작 막대 ----
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="노출시간 [ms]").pack(side="left")
        self.v_exp = tk.StringVar(value="10")
        e = ttk.Entry(bar, textvariable=self.v_exp, width=8)
        e.pack(side="left", padx=(4, 2))
        e.bind("<Return>", lambda ev: self.apply_exposure())
        ttk.Button(bar, text="적용", width=6, command=self.apply_exposure).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        self.v_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="y축 자동", variable=self.v_auto).pack(side="left")
        self.v_avg = tk.IntVar(value=1)
        ttk.Label(bar, text="   평균화").pack(side="left")
        ttk.Spinbox(bar, from_=1, to=64, width=5, textvariable=self.v_avg).pack(side="left", padx=4)
        ttk.Label(bar, text="줄").pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(bar, text="소스").pack(side="left")
        self.v_source = tk.StringVar(value=self.source_kind)
        cb = ttk.Combobox(bar, textvariable=self.v_source, state="readonly", width=10,
                          values=["auto", "lc100", "webcam", "sim"])
        cb.pack(side="left", padx=4)
        ttk.Button(bar, text="다시 연결", command=self.restart).pack(side="left", padx=4)

        # ---- 그래프 ----
        self.fig = Figure(figsize=(12.2, 6.6), dpi=100, facecolor="w")
        gs = self.fig.add_gridspec(2, 1, height_ratios=[1, 1.25], hspace=0.28)
        self.ax_line = self.fig.add_subplot(gs[0])
        self.ax_wf = self.fig.add_subplot(gs[1])
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=(0, 4))

        self.lb_status = tk.Label(self.root, text="", anchor="w", fg="#333")
        self.lb_status.pack(fill="x", padx=12, pady=(0, 6))

        self._init_axes()

    def _init_axes(self):
        self.ax_line.clear()
        self.ax_line.set_title("① 라인 프로파일 (현재 줄)", fontsize=10)
        self.ax_line.set_xlabel("화소")
        self.ax_line.set_ylabel("신호")
        self.ax_line.grid(alpha=0.3)
        self.ax_wf.clear()
        self.ax_wf.set_title("② 워터폴 (최근 %d줄)" % WATERFALL_LINES, fontsize=10)
        self.ax_wf.set_xlabel("화소")
        self.ax_wf.set_ylabel("줄 (과거 ↓)")
        self.line_art = None
        self.sat_art = None
        self.wf_art = None
        self.cbar = None
        self.canvas.draw()

    # ================= 수집 ===============================================
    def start(self):
        kind = self.v_source.get()
        try:
            self.source = cam.make_source(kind)
            self.source.open()
        except cam.CameraError as e:
            messagebox.showwarning("카메라를 열 수 없음",
                                   "%s\n\n시뮬레이션으로 전환합니다." % e)
            self.source = cam.SimulatedSource().open()
            self.v_source.set("sim")
        except Exception as e:                    # noqa: BLE001
            messagebox.showerror("오류", str(e))
            return

        n = self.source.n_pixels
        self.waterfall = np.full((WATERFALL_LINES, n), np.nan)
        self.n_lines = 0
        self.t_start = time.time()
        self.lb_src.config(text="소스: %s   ·   %d화소" % (self.source.name, n))
        exp = self.source.get_exposure_ms()
        if np.isfinite(exp):
            self.v_exp.set("%.3g" % exp)

        self.q = queue.Queue(maxsize=64)
        self.acq = Acquirer(self.source, self.q)
        self.acq.start()
        self._init_axes()
        self.root.after(40, self._tick)

    def restart(self):
        self.stop_acq()
        self.start()

    def stop_acq(self):
        if self.acq is not None:
            self.acq.stop()
            self.acq.join(timeout=2.0)
            self.acq = None
        if self.source is not None:
            try:
                self.source.close()
            except Exception:                     # noqa: BLE001
                pass
            self.source = None

    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.config(text="재개" if self.paused else "일시정지")

    def clear(self):
        if self.waterfall is not None:
            self.waterfall[:] = np.nan
            self.n_lines = 0
            self.t_start = time.time()

    def apply_exposure(self):
        if self.source is None:
            return
        try:
            ms = float(self.v_exp.get())
        except ValueError:
            messagebox.showerror("입력 오류", "노출시간은 숫자여야 합니다.")
            return
        try:
            self.source.set_exposure_ms(ms)
        except cam.CameraError as e:
            messagebox.showerror("노출시간 설정 실패", str(e))

    # ================= 화면 갱신 ==========================================
    def _tick(self):
        if self.acq is None:
            return
        if self.acq.error is not None:
            err = self.acq.error
            self.acq = None
            messagebox.showerror("수집 중단", str(err))
            self.lb_status.config(text="수집 중단: %s" % err)
            return

        got = []
        while True:
            try:
                got.append(self.q.get_nowait())
            except queue.Empty:
                break

        if got and not self.paused:
            for _, line in got:
                self.waterfall = np.roll(self.waterfall, 1, axis=0)
                self.waterfall[0] = line
                self.n_lines += 1
            navg = max(1, int(self.v_avg.get()))
            self.last_line = np.nanmean(self.waterfall[:navg], axis=0)
            # 수집은 카메라 속도로 하되 화면은 10 fps 로 제한한다.
            # (워터폴 400x2048 을 매 줄마다 다시 그리면 이벤트 루프가 밀린다)
            now = time.time()
            if now - self._last_draw >= 0.1:
                self._last_draw = now
                self._draw()

        self._status()
        self.root.after(40, self._tick)

    def _draw(self):
        y = self.last_line
        if y is None:
            return
        x = np.arange(y.size)
        vmax = self.source.max_value if self.source else float(np.nanmax(y))

        # ① 라인 프로파일
        if self.line_art is None:
            (self.line_art,) = self.ax_line.plot(x, y, lw=0.8, color="#1f77b4")
            self.sat_art = self.ax_line.axhline(vmax, color="#c0392b", ls="--", lw=1,
                                                label="포화 %g" % vmax)
            self.ax_line.legend(loc="upper right", fontsize=8)
            self.ax_line.set_xlim(0, y.size - 1)
        else:
            self.line_art.set_data(x, y)
        if self.v_auto.get():
            lo, hi = float(np.nanmin(y)), float(np.nanmax(y))
            pad = max((hi - lo) * 0.08, 1.0)
            self.ax_line.set_ylim(lo - pad, hi + pad)
        else:
            self.ax_line.set_ylim(0, vmax * 1.05)

        sat = int(np.sum(y >= vmax * 0.995))
        self.ax_line.set_title(
            "① 라인 프로파일 — 최대 %.0f / 평균 %.0f%s"
            % (np.nanmax(y), np.nanmean(y),
               ("   ⚠ 포화 화소 %d개" % sat) if sat else ""),
            fontsize=10, color=("#c0392b" if sat else "black"))

        # ② 워터폴
        wf = self.waterfall
        if self.wf_art is None:
            self.wf_art = self.ax_wf.imshow(wf, aspect="auto", origin="upper",
                                            cmap="viridis", interpolation="nearest")
            self.cbar = self.fig.colorbar(self.wf_art, ax=self.ax_wf,
                                          fraction=0.046, pad=0.01)
            self.cbar.set_label("신호")
        else:
            self.wf_art.set_data(wf)
        # 색상범위 재계산은 비싸므로 0.5초에 한 번만
        now = time.time()
        if now - self._last_clim >= 0.5:
            self._last_clim = now
            finite = wf[np.isfinite(wf)]
            if finite.size:
                self.wf_art.set_clim(np.percentile(finite, 1), np.percentile(finite, 99.5))
        self.canvas.draw_idle()

    def _status(self):
        if self.acq is None:
            return
        dt = max(time.time() - (self.t_start or time.time()), 1e-6)
        rate = self.n_lines / dt
        exp = self.source.get_exposure_ms() if self.source else float("nan")
        self.lb_status.config(
            text="누적 %d줄   ·   %.1f 줄/초   ·   노출 %s   ·   큐 %d"
                 % (self.n_lines, rate,
                    ("%.3g ms" % exp) if np.isfinite(exp) else "-", self.q.qsize()))

    # ================= 저장 ===============================================
    def save(self):
        if self.waterfall is None or self.n_lines == 0:
            messagebox.showwarning("저장할 데이터 없음", "먼저 데이터를 수집하세요.")
            return
        stem = filedialog.asksaveasfilename(
            title="저장", defaultextension=".npz",
            initialfile="LC100_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"),
            filetypes=[("NumPy 묶음", "*.npz"), ("CSV", "*.csv"), ("모든 파일", "*.*")])
        if not stem:
            return
        base = Path(stem).with_suffix("")
        n = min(self.n_lines, WATERFALL_LINES)
        data = self.waterfall[:n][::-1]           # 시간 순서(오래된 것부터)
        try:
            np.savez_compressed(str(base) + ".npz", waterfall=data,
                                last_line=self.last_line,
                                source=self.source.name if self.source else "",
                                n_pixels=data.shape[1],
                                saved_at=datetime.now().isoformat())
            np.savetxt(str(base) + ".csv", data, delimiter=",", fmt="%.6g")
            self.fig.savefig(str(base) + ".png", facecolor="w", dpi=130)
        except OSError as e:
            messagebox.showerror("저장 실패", str(e))
            return
        self.lb_status.config(text="저장 완료: %s (.npz/.csv/.png), %d줄" % (base, n))

    def on_close(self):
        self.stop_acq()
        self.root.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Thorlabs LC100 라인 카메라 뷰어")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "lc100", "webcam", "sim"],
                    help="데이터 소스 (기본 auto: 카메라가 준비되면 카메라, 아니면 시뮬레이션)")
    ap.add_argument("--check", action="store_true", help="연결/드라이버 점검만 하고 종료")
    a = ap.parse_args(argv)

    if a.check:
        cam.print_diagnosis()
        return 0

    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                             # noqa: BLE001
        pass
    LC100App(root, a.source)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
