"""PDF 平面圖 -> 整理過的 DXF

用法:
    py pdf2dxf_clean.py 輸入.pdf 輸出資料夾 [--scale 100] [--unit cm|mm] [--no-grid-origin]

做的事:
  1. 依「線色|填充色|線寬」把向量分到有意義的圖層 (對照表見 LAYER_MAP, 可自行修改)
  2. 去除重複線段
  3. 把被拆成小段的虛線 / 點劃線接回一條線, 並自動辨識線型樣式 (建立對應的 DXF 線型)
  4. 實線: 共線相接的線段合併, 首尾相連的線段串成多段線 (LWPOLYLINE)
  5. 灰色牆柱等填充 -> 實心 HATCH (原本的轉換會遺失填充)
  6. 文字: 中文字型樣式、保留旋轉角度、去重複
  7. 以軸線 1 / A 交點為原點 (可用 --no-grid-origin 關閉), 並列出軸線間距供核對比例
  8. 輸出 DXF + 預覽 PNG + 整理報告
"""
from __future__ import annotations

import argparse
import collections
import math
import sys
from pathlib import Path

import ezdxf
import pymupdf
from ezdxf import colors
from ezdxf.addons.drawing import Frontend, RenderContext, layout
from ezdxf.addons.drawing import pymupdf as mupdf_backend

sys.stdout.reconfigure(encoding="utf-8")

# --------------------------------------------------------------------------
# 圖層對照表: key = "線色|填充色|線寬(pt)"  ("-" 表示無)
# kind: line = 實線, dash = 被拆段的虛線(需接回), fill = 填充, 可同時 fill+line
# tol : 虛線接回時允許的最大間隙 (pt, 紙面單位)
# off : 圖層預設關閉
# --------------------------------------------------------------------------
LAYER_MAP: dict[str, dict] = {
    "-|808080|0.00":      dict(name="A-牆柱-填充", desc="RC 牆柱灰色實心填充"),
    "000000|-|1.42":      dict(name="A-牆柱-輪廓", desc="牆柱剖切粗線"),
    "-|E1E1E1|0.00":      dict(name="A-樓梯-填充", desc="樓梯間淺灰填充"),
    "-|D8D8AF|0.00":      dict(name="A-填充-其他", desc="其他填充"),
    "-|D6D6D6|0.00":      dict(name="A-填充-其他", desc="其他填充"),
    "-|00FF00|0.00":      dict(name="A-填充-其他", desc="其他填充"),
    "-|000000|0.00":      dict(name="A-填充-其他", desc="其他填充"),
    "-|FFFFFF|0.00":      dict(name="X-遮罩", desc="Revit 白色遮罩區塊", off=True),
    "FFFFFF|-|0.28":      dict(name="X-遮罩", desc="Revit 白色遮罩線", off=True),
    "AAAAAA|-|0.28":      dict(name="A-家具設備", desc="家具、衛浴設備 (灰色細線)"),
    "000000|-|0.28":      dict(name="A-一般線", desc="門、樓梯、停車格、軸線圈、表格等細線"),
    "000000|-|0.71":      dict(name="A-一般線", desc="門、樓梯、停車格、軸線圈、表格等細線"),
    "000000|-|0.51":      dict(name="A-尺寸線", desc="尺寸線、圖框中線"),
    "000000|-|0.99":      dict(name="A-尺寸符號", desc="尺寸端點記號"),
    "000000|000000|0.99": dict(name="A-尺寸符號", desc="尺寸端點記號 (實心)"),
    "FF0000|-|0.28":      dict(name="S-軸線", desc="軸線 (紅色點劃線) 及紅色細虛線", kind="dash", tol=10),
    "FF0000|-|1.42":      dict(name="C-地界線", desc="紅色粗點劃線 (推測為地界線)", kind="dash", tol=8),
    "008040|-|1.42":      dict(name="A-範圍線", desc="綠色粗雙點劃線外框 (推測為建築/視圖範圍)", kind="dash", tol=15),
    "0000FF|-|1.42":      dict(name="A-開口範圍", desc="藍色粗虛線+X (推測為挑空/開口)", kind="dash", tol=10),
    "0000FF|-|0.51":      dict(name="A-虛線-藍", desc="藍色虛線 (推測為門扇軌跡或上方構件)", kind="dash", tol=5),
    "008000|-|0.51":      dict(name="A-上方投影線", desc="綠色虛線 (推測為上方梁/投影)", kind="dash", tol=5),
    "00A600|-|0.28":      dict(name="A-隱藏線", desc="綠色細虛線", kind="dash", tol=5),
    "00007F|-|0.51":      dict(name="A-其他-深藍", desc="深藍細弧線"),
    "008000|-|0.28":      dict(name="A-其他-彩色", desc="其他彩色細線"),
    "FF7F00|-|0.28":      dict(name="A-其他-彩色", desc="其他彩色細線"),
    "FF00FF|-|0.28":      dict(name="A-其他-彩色", desc="其他彩色細線"),
    "FF00FF|-|1.98":      dict(name="X-視圖框", desc="Revit 視圖裁切框", off=True),
}
TEXT_FONT = "msjh.ttc"            # 微軟正黑體
TEXT_FAMILY = "Microsoft JhengHei"
GRID_LABEL_SIZE = (16.0, 20.0)    # 軸線編號字高範圍 (pt)
DXF_LINEWEIGHTS = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100]

EPS_KEY = 100.0   # 端點量化: 0.01 pt


# --------------------------------------------------------------------------
def hexcol(c) -> str:
    return "-" if not c else "%02X%02X%02X" % tuple(int(round(x * 255)) for x in c)


def path_key(p) -> str:
    return f"{hexcol(p['color'])}|{hexcol(p.get('fill'))}|{(p['width'] or 0):.2f}"


def bezier(p0, p1, p2, p3, n=8):
    pts = []
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
        pts.append((a * p0.x + b * p1.x + c * p2.x + d * p3.x,
                    a * p0.y + b * p1.y + c * p2.y + d * p3.y))
    return pts


def items_to_subpaths(items) -> list[list[tuple[float, float]]]:
    """PDF 路徑項目 -> 連續點列 (子路徑)."""
    subs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []

    def flush():
        nonlocal cur
        if len(cur) >= 2:
            subs.append(cur)
        cur = []

    for it in items:
        op = it[0]
        if op in ("re", "qu"):
            flush()
            q = it[1].quad if op == "re" else it[1]
            subs.append([(v.x, v.y) for v in (q.ul, q.ur, q.lr, q.ll, q.ul)])
            continue
        pts = [(it[1].x, it[1].y), (it[2].x, it[2].y)] if op == "l" else bezier(*it[1:5])
        if cur and math.dist(cur[-1], pts[0]) < 0.01:
            cur.extend(pts[1:])
        else:
            flush()
            cur = list(pts)
    flush()
    return subs


# ------------------------------- 線段處理 ---------------------------------
def dedupe(segs):
    seen, out = set(), []
    for a, b in segs:
        ka = (round(a[0] * EPS_KEY), round(a[1] * EPS_KEY))
        kb = (round(b[0] * EPS_KEY), round(b[1] * EPS_KEY))
        if ka == kb:
            continue
        k = (ka, kb) if ka <= kb else (kb, ka)
        if k not in seen:
            seen.add(k)
            out.append((a, b))
    return out


def merge_collinear(segs, gap_tol):
    """共線線段合併. 回傳 [(p0, p1, pieces)], pieces = 原始小段的 (t0,t1) 列表."""
    rows = []
    for a, b in segs:
        ang = math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi
        if ang > math.pi - 5e-4:
            ang -= math.pi
        qa = round(ang / 1e-3)
        ang = qa * 1e-3 if abs(ang - qa * 1e-3) < 2e-4 else ang
        c, s = math.cos(ang), math.sin(ang)
        d = -s * a[0] + c * a[1]
        t0, t1 = c * a[0] + s * a[1], c * b[0] + s * b[1]
        rows.append((qa, d, min(t0, t1), max(t0, t1), ang))
    rows.sort()
    out = []
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and rows[j][0] == rows[i][0] and rows[j][1] - rows[j - 1][1] < 0.05:
            j += 1
        grp = sorted(rows[i:j], key=lambda r: r[2])
        ang = sum(r[4] for r in grp) / len(grp)
        d = sum(r[1] for r in grp) / len(grp)
        c, s = math.cos(ang), math.sin(ang)
        run = [grp[0][2], grp[0][3]]
        pieces = [(grp[0][2], grp[0][3])]
        for r in grp[1:]:
            if r[2] - run[1] <= gap_tol:
                run[1] = max(run[1], r[3])
                pieces.append((r[2], r[3]))
            else:
                out.append((ang, d, run, pieces))
                run, pieces = [r[2], r[3]], [(r[2], r[3])]
        out.append((ang, d, run, pieces))
        i = j
    res = []
    for ang, d, (t0, t1), pieces in out:
        c, s = math.cos(ang), math.sin(ang)
        p0 = (t0 * c - d * s, t0 * s + d * c)
        p1 = (t1 * c - d * s, t1 * s + d * c)
        res.append((p0, p1, pieces))
    return res


def chain(segs):
    """首尾相連且節點只連兩條的線段串成多段線."""
    def k(p):
        return (round(p[0] * EPS_KEY), round(p[1] * EPS_KEY))
    adj = collections.defaultdict(list)
    for i, (a, b) in enumerate(segs):
        adj[k(a)].append(i)
        adj[k(b)].append(i)
    used = [False] * len(segs)
    out = []
    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        a, b = segs[i]
        pts = collections.deque([a, b])
        for forward in (True, False):
            while True:
                end = pts[-1] if forward else pts[0]
                node = adj[k(end)]
                free = [c for c in node if not used[c]]
                if len(node) != 2 or not free:
                    break
                nxt = free[0]
                used[nxt] = True
                na, nb = segs[nxt]
                other = nb if k(na) == k(end) else na
                if forward:
                    pts.append(other)
                else:
                    pts.appendleft(other)
        out.append(list(pts))
    return out


def detect_pattern(pieces):
    """從原始小段推算虛線樣式 (pt). 回傳 tuple 或 None (視為實線)."""
    if len(pieces) < 4:
        return None
    inner = pieces[1:-1]
    dashes = [round((b - a) * 4) / 4 for a, b in inner]
    gaps = [round((pieces[i + 1][0] - pieces[i][1]) * 4) / 4 for i in range(len(pieces) - 1)]
    gaps = [g for g in gaps if g > 0.2]
    if not gaps or not dashes:
        return None
    g = collections.Counter(gaps).most_common(1)[0][0]
    cnt = collections.Counter(dashes)
    if len(cnt) == 1:
        return (dashes[0], -g)
    (long_d, _), (short_d, _) = sorted(cnt.most_common(2), key=lambda x: -x[0])
    n_long, n_short = cnt[long_d], cnt[short_d]
    if n_short >= 1.6 * n_long:
        return (long_d, -g, short_d, -g, short_d, -g)
    return (long_d, -g, short_d, -g)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("outdir")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--scale", type=float, default=100, help="圖面比例分母, 預設 1:100")
    ap.add_argument("--unit", choices=["cm", "mm"], default="cm")
    ap.add_argument("--no-grid-origin", action="store_true")
    args = ap.parse_args()

    out = Path(args.outdir)
    stem = Path(args.pdf).stem
    page = pymupdf.open(args.pdf)[args.page]
    unit_per_mm = 0.1 if args.unit == "cm" else 1.0
    S = 25.4 / 72 * args.scale * unit_per_mm          # 每 pt 的模型單位

    # ---------- 1. 收集 ----------
    strokes = collections.defaultdict(list)   # layer -> [(a,b)]
    fills = collections.defaultdict(list)     # layer -> [(rgb, [subpaths])]
    layer_info: dict[str, dict] = {}
    stats = collections.defaultdict(lambda: collections.Counter())
    for p in page.get_drawings():
        key = path_key(p)
        cfg = LAYER_MAP.get(key) or dict(name=f"Z-未分類_{key.replace('|', '_')}", desc="未分類")
        name = cfg["name"]
        info = layer_info.setdefault(name, dict(cfg, keys=set(), color=None, width=0.0))
        info["keys"].add(key)
        info["color"] = info["color"] or p["color"] or p.get("fill")
        info["width"] = max(info["width"], p["width"] or 0)
        subs = items_to_subpaths(p["items"])
        stats[name]["PDF路徑"] += 1
        if p.get("fill") is not None and "f" in p["type"]:
            fills[name].append((p["fill"], subs))
        if p["color"] is not None and "s" in p["type"]:
            for sp in subs:
                for a, b in zip(sp, sp[1:]):
                    strokes[name].append((a, b))
            stats[name]["原始線段"] += sum(len(sp) - 1 for sp in subs)

    # ---------- 2. 文字 ----------
    texts = []
    seen_txt = set()
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            t = "".join(s["text"] for s in ln["spans"]).strip()
            size = max(s["size"] for s in spans)
            ox, oy = spans[0]["origin"]
            k = (t, round(ox, 1), round(oy, 1))
            if k in seen_txt:
                continue
            seen_txt.add(k)
            dx, dy = ln["dir"]
            rot = math.degrees(math.atan2(-dy, dx))
            bbox = pymupdf.Rect(ln["bbox"])
            texts.append(dict(text=t, size=size, x=ox, y=oy, rot=rot, bbox=bbox, font=spans[0]["font"]))

    # ---------- 3. 線段整理 ----------
    merged = {}   # layer -> list of (p0,p1,pieces)
    for name, segs in strokes.items():
        info = layer_info[name]
        segs = dedupe(segs)
        stats[name]["去重後"] = len(segs)
        tol = info.get("tol", 0.01) if info.get("kind") == "dash" else 0.01
        merged[name] = merge_collinear(segs, tol)

    # ---------- 4. 軸線原點 ----------
    x0, y0 = 0.0, page.rect.height     # 預設: 頁面左下角
    grids_v, grids_h = [], []
    if "S-軸線" in merged:
        labels = [t for t in texts if GRID_LABEL_SIZE[0] <= t["size"] <= GRID_LABEL_SIZE[1]
                  and len(t["text"]) <= 3]
        for p0, p1, _ in merged["S-軸線"]:
            L = math.dist(p0, p1)
            if L < 200:
                continue
            if abs(p0[0] - p1[0]) < 0.05:     # 垂直軸線
                x = p0[0]
                nm = {t["text"] for t in labels if abs((t["bbox"].x0 + t["bbox"].x1) / 2 - x) < 6}
                if nm:
                    grids_v.append((x, "/".join(sorted(nm))))
            elif abs(p0[1] - p1[1]) < 0.05:   # 水平軸線
                y = p0[1]
                nm = {t["text"] for t in labels if abs((t["bbox"].y0 + t["bbox"].y1) / 2 - y) < 6}
                if nm:
                    grids_h.append((y, "/".join(sorted(nm))))
        grids_v = sorted(set(grids_v))
        grids_h = sorted(set(grids_h), reverse=True)  # PDF y 向下, 由下往上排
        if not args.no_grid_origin and grids_v and grids_h:
            x0 = grids_v[0][0]
            y0 = grids_h[0][0]

    def P(pt):
        return ((pt[0] - x0) * S, (y0 - pt[1]) * S)

    # ---------- 5. 建立 DXF ----------
    doc = ezdxf.new("R2018", setup=True)
    doc.units = ezdxf.units.CM if args.unit == "cm" else ezdxf.units.MM
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LTSCALE"] = 1.0
    doc.header["$PSLTSCALE"] = 0
    msp = doc.modelspace()

    style = doc.styles.add("中文", font=TEXT_FONT)
    style.set_extended_font_data(TEXT_FAMILY)

    def lw_of(width_pt):
        mm = width_pt * 25.4 / 72 * 100
        return min(DXF_LINEWEIGHTS, key=lambda v: abs(v - mm))

    for name, info in layer_info.items():
        lay = doc.layers.add(name, color=7)
        c = tuple(int(round(x * 255)) for x in (info["color"] or (0, 0, 0)))
        if c not in ((0, 0, 0), (255, 255, 255)):   # 黑/白用 ACI 7, 深淺背景都看得到
            lay.rgb = c
        lay.dxf.lineweight = lw_of(info["width"])
        lay.description = f"{info['desc']}  [來源 {', '.join(sorted(info['keys']))}]"
        if info.get("off"):
            lay.off()
    for name in ("A-文字", "A-文字-小", "S-軸線編號"):
        doc.layers.add(name, color=7 if name != "S-軸線編號" else 1)

    # 線型
    linetypes: dict[tuple, str] = {}

    def ltype_for(pattern_pt):
        if pattern_pt in linetypes:
            return linetypes[pattern_pt]
        pat = [round(v * S, 4) for v in pattern_pt]
        nm = "PDF_" + "_".join(f"{abs(v):g}" for v in pattern_pt[::2]) + f"_G{abs(pattern_pt[1]):g}"
        doc.linetypes.add(nm, pattern=[sum(abs(v) for v in pat)] + pat,
                          description=f"由 PDF 量測: {pattern_pt} pt")
        linetypes[pattern_pt] = nm
        return nm

    # 實線 / 虛線
    for name, rows in merged.items():
        attrs = {"layer": name}
        if layer_info[name].get("kind") == "dash":
            pats = [detect_pattern(pc) for _, _, pc in rows]
            weight = collections.Counter()
            for (p0, p1, _), pat in zip(rows, pats):
                if pat:
                    weight[pat] += math.dist(p0, p1)
            total = sum(weight.values()) or 1
            # 只保留佔該圖層長度 >= 10% 的主要樣式, 其餘歸到最接近的主要樣式
            main = [pt for pt, w in weight.most_common() if w / total >= 0.10] or list(weight)[:1]

            def snap(pat):
                if pat is None or pat in main:
                    return pat
                same = [m for m in main if len(m) == len(pat)] or main
                return min(same, key=lambda m: sum(abs(x - y) for x, y in zip(m, pat)))

            for (p0, p1, pieces), pat in zip(rows, pats):
                pat = snap(pat)
                a = dict(attrs)
                if pat:
                    a["linetype"] = ltype_for(pat)
                msp.add_line(P(p0), P(p1), dxfattribs=a)
                stats[name]["LINE"] += 1
        else:
            for pts in chain([(p0, p1) for p0, p1, _ in rows]):
                closed = len(pts) > 3 and math.dist(pts[0], pts[-1]) < 0.01
                if closed:
                    pts = pts[:-1]
                if len(pts) == 2:
                    msp.add_line(P(pts[0]), P(pts[1]), dxfattribs=attrs)
                    stats[name]["LINE"] += 1
                else:
                    msp.add_lwpolyline([P(q) for q in pts], close=closed, dxfattribs=attrs)
                    stats[name]["LWPOLYLINE"] += 1

    # 填充
    for name, lst in fills.items():
        for rgb, subs in lst:
            h = msp.add_hatch(dxfattribs={"layer": name})
            c = tuple(int(round(x * 255)) for x in rgb)
            if c in ((0, 0, 0), (255, 255, 255)):
                h.set_solid_fill(color=7)
            else:
                h.set_solid_fill(color=colors.BYLAYER, rgb=c)
            n = 0
            for sp in subs:
                if len(sp) >= 3:
                    h.paths.add_polyline_path([P(q) for q in sp], is_closed=True)
                    n += 1
            if n == 0:
                msp.delete_entity(h)
            else:
                stats[name]["HATCH"] += 1

    # 文字
    for t in texts:
        if GRID_LABEL_SIZE[0] <= t["size"] <= GRID_LABEL_SIZE[1] and len(t["text"]) <= 3:
            layer = "S-軸線編號"
        elif t["size"] < 6:
            layer = "A-文字-小"
        else:
            layer = "A-文字"
        msp.add_text(t["text"], height=t["size"] * 0.8 * S, rotation=t["rot"],
                     dxfattribs={"layer": layer, "style": "中文"}).set_placement(P((t["x"], t["y"])))
        stats[layer]["TEXT"] += 1

    # ---------- 6. 存檔 / 檢查 ----------
    dxf_path = out / f"{stem}_整理.dxf"
    auditor = doc.audit()
    doc.saveas(dxf_path)

    # 預覽 PNG (由 DXF 重新繪製, 用來核對整理結果)
    ctx = RenderContext(doc)
    be = mupdf_backend.PyMuPdfBackend()
    Frontend(ctx, be).draw_layout(msp)
    png = be.get_pixmap_bytes(layout.Page(841, 594, layout.Units.mm, margins=layout.Margins.all(5)),
                              fmt="png", dpi=100,
                              settings=layout.Settings(fit_page=True))
    (out / f"{stem}_整理_預覽.png").write_bytes(png)

    # ---------- 7. 報告 ----------
    lines = []
    lines.append(f"來源: {args.pdf}")
    lines.append(f"比例 1:{args.scale:g}, 單位 {args.unit}, 1 pt = {S:.4f} {args.unit}")
    lines.append(f"原點: {'軸線 ' + grids_v[0][1] + ' / ' + grids_h[0][1] + ' 交點' if (not args.no_grid_origin and grids_v and grids_h) else '頁面左下角'}")
    lines.append("")
    lines.append("圖層                 原始線段   輸出                          說明")
    total_in = total_out = 0
    for name in sorted(stats):
        st = stats[name]
        outs = {k: v for k, v in st.items() if k in ("LINE", "LWPOLYLINE", "HATCH", "TEXT")}
        total_in += st.get("原始線段", 0)
        total_out += sum(outs.values())
        desc = layer_info.get(name, {}).get("desc", "文字")
        off = " (關閉)" if layer_info.get(name, {}).get("off") else ""
        lines.append(f"{name:18s} {st.get('原始線段', 0):8d}   "
                     f"{', '.join(f'{k} {v}' for k, v in outs.items()):28s}  {desc}{off}")
    lines.append(f"\n合計: 原始線段 {total_in} -> 輸出圖元 {total_out}")
    if linetypes:
        lines.append("\n自動建立的線型 (pt, 負值為間隙):")
        for pat, nm in linetypes.items():
            lines.append(f"  {nm:28s} {pat}")
    if grids_v or grids_h:
        lines.append("\n軸線間距 (請用已知柱距核對比例是否正確):")
        for title, gs, idx in (("縱向軸線", grids_v, 0), ("橫向軸線", grids_h, 1)):
            row = []
            for (a, na), (b, nb) in zip(gs, gs[1:]):
                row.append(f"{na}-{nb}: {abs(b - a) * S / (100 if args.unit == 'cm' else 1000):.2f} m")
            lines.append(f"  {title}: " + "   ".join(row))
    lines.append(f"\nDXF 稽核: {len(auditor.errors)} 個錯誤, {len(auditor.fixes)} 個自動修正")
    report = "\n".join(lines)
    (out / f"{stem}_整理報告.txt").write_text(report, encoding="utf-8")
    print(report)
    print(f"\n輸出: {dxf_path}")


if __name__ == "__main__":
    main()
