# -*- coding: utf-8 -*-
"""계명대학교 수시모집 경쟁률 조회 GUI.

왼쪽에서 학과를 고르면 오른쪽에 그 학과의 전형별 경쟁률이 표와 그래프로 나온다.
데이터는 유웨이 실시간 경쟁률 페이지에서 가져온다 (kmu_ratio 모듈).

    python kmu_ratio_app.py
"""
from __future__ import annotations

import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

import kmu_ratio as kr

BG_HEAD = "#1f3f5b"
COL_HOT = "#c0392b"      # 경쟁률 높음
COL_MID = "#b9770e"
COL_LOW = "#5d6d7e"


def _fmt(n) -> str:
    return format(int(n), ",")


def ratio_color(ratio: float) -> str:
    if ratio >= 3.0:
        return COL_HOT
    if ratio >= 1.0:
        return COL_MID
    return COL_LOW


class RatioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.snap = None
        self.busy = False
        self.current_dept = None
        root.title("계명대학교 수시모집 경쟁률")
        root.geometry("1180x760")
        self._build()
        self.refresh(initial=True)

    # ================= UI =================================================
    def _build(self):
        # ---- 머리말 ----
        head = tk.Frame(self.root, bg=BG_HEAD)
        head.pack(fill="x")
        self.lb_title = tk.Label(head, text="계명대학교 수시모집 경쟁률", bg=BG_HEAD,
                                 fg="white", font=("맑은 고딕", 14, "bold"))
        self.lb_title.pack(side="left", padx=12, pady=8)
        self.lb_asof = tk.Label(head, text="", bg=BG_HEAD, fg="#cfe0ee",
                                font=("맑은 고딕", 9))
        self.lb_asof.pack(side="left", padx=6)
        ttk.Button(head, text="CSV 저장", command=self.save_csv).pack(
            side="right", padx=(4, 10), pady=6)
        self.btn_refresh = ttk.Button(head, text="새로고침", command=self.refresh)
        self.btn_refresh.pack(side="right", padx=4, pady=6)

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=6)

        # ---- 왼쪽: 학과 목록 ----
        left = ttk.Frame(body)
        body.add(left, weight=1)

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="학과 검색").pack(side="left")
        self.v_search = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.v_search)
        e.pack(side="left", fill="x", expand=True, padx=4)
        e.bind("<KeyRelease>", lambda ev: self._fill_dept_list())
        self.v_major = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="주요학과만", variable=self.v_major,
                        command=self._fill_dept_list).pack(side="left")

        cols = ("dept", "quota", "app", "ratio")
        self.tv_dept = ttk.Treeview(left, columns=cols, show="headings", height=26)
        for c, t, w, anchor in (("dept", "학과 (모집단위)", 210, "w"),
                                ("quota", "모집", 60, "e"),
                                ("app", "지원", 60, "e"),
                                ("ratio", "경쟁률", 74, "e")):
            self.tv_dept.heading(c, text=t, command=lambda cc=c: self._sort_dept(cc))
            self.tv_dept.column(c, width=w, anchor=anchor)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tv_dept.yview)
        self.tv_dept.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv_dept.pack(fill="both", expand=True)
        self.tv_dept.bind("<<TreeviewSelect>>", self._on_pick)
        for tag, col in (("hot", COL_HOT), ("mid", COL_MID), ("low", COL_LOW)):
            self.tv_dept.tag_configure(tag, foreground=col)
        self._sort_key, self._sort_desc = "quota", True

        # ---- 오른쪽: 상세 ----
        right = ttk.Frame(body)
        body.add(right, weight=2)

        self.lb_dept = tk.Label(right, text="왼쪽에서 학과를 선택하세요.",
                                font=("맑은 고딕", 13, "bold"), anchor="w")
        self.lb_dept.pack(fill="x")
        self.lb_total = tk.Label(right, text="", font=("맑은 고딕", 11), anchor="w",
                                 fg="#1f3f5b")
        self.lb_total.pack(fill="x", pady=(0, 6))

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)

        t1 = ttk.Frame(nb)
        nb.add(t1, text="전형별 경쟁률")
        cols2 = ("track", "quota", "app", "ratio")
        self.tv_track = ttk.Treeview(t1, columns=cols2, show="headings")
        for c, t, w, anchor in (("track", "전형", 400, "w"),
                                ("quota", "모집인원", 80, "e"),
                                ("app", "지원인원", 80, "e"),
                                ("ratio", "경쟁률", 90, "e")):
            self.tv_track.heading(c, text=t)
            self.tv_track.column(c, width=w, anchor=anchor)
        sb2 = ttk.Scrollbar(t1, orient="vertical", command=self.tv_track.yview)
        self.tv_track.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        self.tv_track.pack(fill="both", expand=True)
        for tag, col in (("hot", COL_HOT), ("mid", COL_MID), ("low", COL_LOW)):
            self.tv_track.tag_configure(tag, foreground=col)

        t2 = ttk.Frame(nb)
        nb.add(t2, text="그래프")
        self.chart_holder = t2
        self.canvas = None

        t3 = ttk.Frame(nb)
        nb.add(t3, text="전형별 총괄")
        cols3 = ("gb", "track", "quota", "app", "ratio")
        self.tv_sum = ttk.Treeview(t3, columns=cols3, show="headings")
        for c, t, w, anchor in (("gb", "구분", 60, "center"),
                                ("track", "전형", 340, "w"),
                                ("quota", "총모집", 80, "e"),
                                ("app", "지원", 80, "e"),
                                ("ratio", "경쟁률", 90, "e")):
            self.tv_sum.heading(c, text=t)
            self.tv_sum.column(c, width=w, anchor=anchor)
        sb3 = ttk.Scrollbar(t3, orient="vertical", command=self.tv_sum.yview)
        self.tv_sum.configure(yscrollcommand=sb3.set)
        sb3.pack(side="right", fill="y")
        self.tv_sum.pack(fill="both", expand=True)
        for tag, col in (("hot", COL_HOT), ("mid", COL_MID), ("low", COL_LOW)):
            self.tv_sum.tag_configure(tag, foreground=col)

        self.lb_status = tk.Label(self.root, text="", anchor="w", fg="#444")
        self.lb_status.pack(fill="x", padx=10, pady=(0, 6))

    # ================= 데이터 =============================================
    def refresh(self, initial: bool = False):
        if self.busy:
            return
        self.busy = True
        self.btn_refresh.config(state="disabled")
        self._status("경쟁률을 불러오는 중...")
        box = {}

        def work():
            # 작업 스레드에서는 tkinter 를 건드리지 않는다. 결과만 담아둔다.
            try:
                box["snap"] = kr.load(force=not initial)
            except Exception as e:                       # noqa: BLE001
                box["err"] = e
                box["tb"] = traceback.format_exc()
            box["done"] = True

        threading.Thread(target=work, daemon=True).start()
        self.root.after(80, lambda: self._poll(box))

    def _poll(self, box):
        if not box.get("done"):
            self.root.after(80, lambda: self._poll(box))
            return
        self.busy = False
        self.btn_refresh.config(state="normal")
        if "err" in box:
            messagebox.showerror("불러오기 실패", str(box["err"]))
            self._status("실패: %s" % box["err"])
            return
        self.snap = box["snap"]
        s = self.snap
        self.lb_title.config(text="%s %s" % (s.university, s.title))
        self.lb_asof.config(text="%s   (조회 %s)" % (s.as_of, s.fetched_at))
        self._fill_dept_list()
        self._fill_summary()
        keep = self.current_dept
        if keep:
            self._select_dept(keep)
        self._status("%d개 모집단위 / %d개 전형 · 출처 %s"
                     % (len(s.departments()), len(s.summary), s.source_url))

    # ================= 목록 ===============================================
    def _fill_dept_list(self):
        if not self.snap:
            return
        q = self.v_search.get().strip()
        depts = self.snap.departments(major_only=self.v_major.get(), top_n=30)
        if q:
            # 검색 중에는 주요학과 필터를 무시하고 전체에서 찾는다.
            depts = [d for d in self.snap.departments() if q in d]

        data = []
        for d in depts:
            quota, app, ratio = self.snap.dept_total(d)
            data.append((d, quota, app, ratio))
        key = {"dept": lambda r: r[0], "quota": lambda r: r[1],
               "app": lambda r: r[2], "ratio": lambda r: r[3]}[self._sort_key]
        data.sort(key=key, reverse=self._sort_desc)

        self.tv_dept.delete(*self.tv_dept.get_children())
        for d, quota, app, ratio in data:
            tag = "hot" if ratio >= 3 else ("mid" if ratio >= 1 else "low")
            self.tv_dept.insert("", "end", iid=d, tags=(tag,),
                                values=(d, _fmt(quota), _fmt(app), "%.2f : 1" % ratio))

    def _sort_dept(self, col):
        if self._sort_key == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = col, col != "dept"
        self._fill_dept_list()

    def _fill_summary(self):
        self.tv_sum.delete(*self.tv_sum.get_children())
        for s in self.snap.summary:
            tag = "hot" if s.ratio >= 3 else ("mid" if s.ratio >= 1 else "low")
            self.tv_sum.insert("", "end", tags=(tag,),
                               values=(s.quota_type, s.track, _fmt(s.quota),
                                       _fmt(s.applicants), "%.2f : 1" % s.ratio))

    # ================= 선택 ===============================================
    def _on_pick(self, _ev=None):
        sel = self.tv_dept.selection()
        if sel:
            self._select_dept(sel[0])

    def _select_dept(self, dept: str):
        # 모집인원을 공유하는 행은 묶어서 본다 (kmu_ratio.Group 참고).
        rows = self.snap.groups(dept)
        if not rows:
            return
        self.current_dept = dept
        quota, app, ratio = self.snap.dept_total(dept)
        college = self.snap.college_of(dept)
        self.lb_dept.config(text="%s   (%s)" % (dept, college))
        caveat = ("   ※ 일부 전형은 원본이 모집인원을 학과별로 배분해 적지 않아 "
                  "통합 모집인원이 대학 발표와 다를 수 있습니다")
        self.lb_total.config(
            text=("통합  모집 %s명 / 지원 %s명 / 경쟁률 %.2f : 1     ·  전형 %d개%s"
                  % (_fmt(quota), _fmt(app), ratio, len(rows),
                     caveat if self.snap.dept_has_caveat(dept) else "")),
            fg=ratio_color(ratio))

        self.tv_track.delete(*self.tv_track.get_children())
        for r in rows:
            tag = "hot" if r.ratio >= 3 else ("mid" if r.ratio >= 1 else "low")
            self.tv_track.insert("", "end", tags=(tag,),
                                 values=(r.label, _fmt(r.quota), _fmt(r.applicants),
                                         "%.2f : 1" % r.ratio))
        self._draw_chart(dept, rows)

    # ================= 그래프 =============================================
    def _draw_chart(self, dept, rows):
        try:
            import matplotlib
            matplotlib.use("TkAgg", force=False)
            from matplotlib import font_manager
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except Exception:                                 # noqa: BLE001
            return                                        # 그래프는 없어도 동작한다

        have = {f.name for f in font_manager.fontManager.ttflist}
        for name in ("Malgun Gothic", "NanumGothic", "AppleGothic"):
            if name in have:
                matplotlib.rcParams["font.family"] = name
                break
        matplotlib.rcParams["axes.unicode_minus"] = False

        if self.canvas is not None:
            self.canvas.get_tk_widget().destroy()
        fig = Figure(figsize=(7.4, 4.6), dpi=100, facecolor="w")
        ax = fig.add_subplot(111)

        rs = sorted(rows, key=lambda r: r.ratio)
        labels = [r.label.replace("정원내 ", "").replace("정원외 ", "[외] ") for r in rs]
        vals = [r.ratio for r in rs]
        colors = [ratio_color(v) for v in vals]
        ax.barh(range(len(rs)), vals, color=colors)
        ax.set_yticks(range(len(rs)))
        ax.set_yticklabels(labels, fontsize=8)
        ax.axvline(1.0, color="#888", ls="--", lw=1)      # 1:1 기준선
        ax.set_xlabel("경쟁률 ( : 1 )")
        ax.set_title("%s  전형별 경쟁률" % dept, fontsize=11)
        for i, (v, r) in enumerate(zip(vals, rs)):
            ax.text(v, i, "  %.2f  (%d/%d)" % (v, r.applicants, r.quota),
                    va="center", fontsize=8, color="#333")
        ax.set_xlim(0, max(vals + [1.0]) * 1.35)
        ax.grid(axis="x", alpha=0.3)
        fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(fig, master=self.chart_holder)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw()

    # ================= 기타 ===============================================
    def save_csv(self):
        if not self.snap:
            messagebox.showwarning("데이터 없음", "먼저 경쟁률을 불러오세요.")
            return
        f = filedialog.asksaveasfilename(
            title="CSV 저장", defaultextension=".csv",
            initialfile="계명대_경쟁률.csv",
            filetypes=[("CSV 파일", "*.csv"), ("모든 파일", "*.*")])
        if not f:
            return
        try:
            kr.to_csv(self.snap, f)
        except OSError as e:
            messagebox.showerror("저장 실패", str(e))
            return
        self._status("저장 완료: %s" % f)

    def _status(self, msg):
        self.lb_status.config(text=msg)
        self.root.update_idletasks()


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)      # Windows 고DPI 선명도
    except Exception:                                # noqa: BLE001
        pass
    RatioApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
