import sys, re, collections, pymupdf, ezdxf, openpyxl
sys.stdout.reconfigure(encoding="utf-8")
src, out = sys.argv[1], sys.argv[2]
doc = pymupdf.open(src); page = doc[0]
SCALE = 2.54 / 72 * 100          # cm per pt at 1:100

# ---------- 1. room schedule ----------
lines = []
for b in page.get_text("dict")["blocks"]:
    for l in b.get("lines", []):
        t = "".join(s["text"] for s in l["spans"]).strip()
        if t: lines.append((pymupdf.Rect(l["bbox"]), t))
rooms = []
for r, t in lines:
    m = re.fullmatch(r"([\d.]+)㎡", t)
    if not m: continue
    cx = (r.x0 + r.x1) / 2
    def near(pred, below):
        best = None
        for r2, t2 in lines:
            if not pred(t2) or not (r2.x0 - 3 <= cx <= r2.x1 + 3): continue
            dy = (r2.y0 - r.y1) if below else (r.y0 - r2.y1)
            if -2 <= dy <= 25 and (best is None or dy < best[0]): best = (dy, t2)
        return best[1] if best else ""
    name = near(lambda s: not re.search(r"㎡|^\d|^B\d|床", s), below=False)
    beds = near(lambda s: "床" in s, below=True)
    num = near(lambda s: re.fullmatch(r"B\d{3}", s) is not None, below=True)
    if not num:
        num = near(lambda s: re.fullmatch(r"B\d{3}", s) is not None, below=False)
    rooms.append((num, name, float(m.group(1)), beds.strip("【】")))
bedmap = {}
for rb, tb in lines:
    if not re.fullmatch(r"【\d+床】", tb): continue
    cand = [(abs((r2.y0+r2.y1)/2-(rb.y0+rb.y1)/2)+abs(rb.x0-r2.x1)/10, t2) for r2, t2 in lines
            if re.fullmatch(r"B\d{3}", t2) and abs((r2.y0+r2.y1)/2-(rb.y0+rb.y1)/2) < 15 and -5 < rb.x0-r2.x1 < 60]
    if cand: bedmap[min(cand)[1]] = tb.strip("【】")
rooms = [(n, nm, a, bedmap.get(n, "") if nm == "寢室" else b) for n, nm, a, b in rooms]
rooms.sort(key=lambda x: (x[1], x[0]))
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "室名面積表"
ws.append(["房號", "室名", "面積(㎡)", "床數"])
for row in rooms: ws.append(row)
ws.append([]); ws.append(["", "合計", round(sum(x[2] for x in rooms), 2), ""])
ws2 = wb.create_sheet("依室名統計"); ws2.append(["室名", "間數", "面積合計(㎡)"])
agg = collections.defaultdict(lambda: [0, 0.0])
for _, n, a, _ in rooms: agg[n][0] += 1; agg[n][1] += a
for n, (c, a) in sorted(agg.items(), key=lambda x: -x[1][1]): ws2.append([n, c, round(a, 2)])
for w in (ws, ws2):
    for col in "ABCD": w.column_dimensions[col].width = 22
wb.save(f"{out}/室名面積表.xlsx")
print(f"rooms: {len(rooms)}, total {sum(x[2] for x in rooms):.2f} m2, beds {sum(int(re.sub(r'\D','',b) or 0) for *_, b in rooms)}")
for r in rooms: print("  ", r)

# ---------- 2+3. layers by color/width -> DXF (cm) and PNG per layer ----------
def cname(c):
    if c is None: return "NOFILL"
    return {(0,0,0):"黑",(0.67,0.67,0.67):"灰",(1,0,0):"紅",(0,0,1):"藍",(1,1,1):"白"}.get(
        tuple(round(x,2) for x in c), "RGB_%02X%02X%02X" % tuple(int(x*255) for x in c))
paths = page.get_drawings()
H = page.rect.height
def P(pt): return (pt.x * SCALE, (H - pt.y) * SCALE)   # flip Y, convert to cm
dxf = ezdxf.new("R2018"); dxf.units = ezdxf.units.CM; msp = dxf.modelspace()
groups = collections.defaultdict(list)
for p in paths:
    layer = f"{cname(p['color'] or p.get('fill'))}_w{(p['width'] or 0):.2f}"
    groups[layer].append(p)
    if layer not in dxf.layers:
        c = p["color"] or p.get("fill") or (0,0,0)
        dxf.layers.add(layer).rgb = tuple(int(x*255) for x in c)
    for it in p["items"]:
        if it[0] == "l": msp.add_line(P(it[1]), P(it[2]), dxfattribs={"layer": layer})
        elif it[0] == "re": msp.add_lwpolyline([P(q) for q in it[1].quad], close=True, dxfattribs={"layer": layer})
        elif it[0] == "qu": msp.add_lwpolyline([P(q) for q in it[1]], close=True, dxfattribs={"layer": layer})
        elif it[0] == "c": msp.add_spline([P(q) for q in it[1:]], dxfattribs={"layer": layer})
for r, t in lines:   # text too
    msp.add_text(t, height=r.height*SCALE*0.8, dxfattribs={"layer": "TEXT"}).set_placement(P(pymupdf.Point(r.x0, r.y1)))
dxf.saveas(f"{out}/平面圖_cm.dxf")
print("\nlayers (paths, total length m):")
for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:15]:
    L = sum(abs(it[2]-it[1]) for p in v for it in p["items"] if it[0]=="l") * SCALE / 100
    print(f"  {k:22s} {len(v):6d}  {L:9.1f} m")

# wall-only rendering: thick black lines (w1.42) on blank page
def render(sel, name):
    d2 = pymupdf.open(); pg = d2.new_page(width=page.rect.width, height=H)
    sh = pg.new_shape()
    for p in paths:
        if not sel(p): continue
        for it in p["items"]:
            if it[0]=="l": sh.draw_line(it[1], it[2])
            elif it[0]=="re": sh.draw_rect(it[1])
            elif it[0]=="qu": sh.draw_quad(it[1])
        sh.finish(color=p["color"], fill=p.get("fill"), width=max(p["width"] or 0.3, 0.3), closePath=False)
    sh.commit(); d2.save(f"{out}/{name}.pdf"); pg.get_pixmap(dpi=40).save(f"{out}/{name}.png")
render(lambda p: p["color"] and cname(p["color"])=="黑" and (p["width"] or 0) >= 1.4, "只留粗黑線_牆柱")
render(lambda p: p["color"] and cname(p["color"]) in ("紅","藍"), "只留紅藍線")
