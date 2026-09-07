"""Hydrological Instrumentarium — HECinBOX pipeline icon plate.

Original line-glyphs (no trademarked logos) in one disciplined stroke,
laid out as a numbered specimen sheet.
"""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch, Circle, Rectangle, Arc, PathPatch, Polygon
from matplotlib.path import Path
from matplotlib.lines import Line2D

FONT_DIR = ("/Users/ehsankahrizi/Library/Application Support/Claude/"
            "local-agent-mode-sessions/skills-plugin/"
            "34d071b7-d375-4adf-adc3-adcc146e8f33/"
            "b7ed77d2-5891-4f21-a2ea-0e30a4581809/skills/canvas-design/"
            "canvas-fonts/")

def _font(name):
    try:
        return fm.FontProperties(fname=FONT_DIR + name)
    except Exception:
        return fm.FontProperties()

F_SANS   = _font("InstrumentSans-Regular.ttf")
F_SANS_B = _font("InstrumentSans-Bold.ttf")
F_MONO   = _font("IBMPlexMono-Regular.ttf")
F_DISP   = _font("BricolageGrotesque-Bold.ttf")

# ── Palette ────────────────────────────────────────────────────────────
PAPER   = "#EAF0F4"
PAPER2  = "#E1E9EF"
INK     = "#0E2A43"   # deep hydro navy (primary stroke)
WATER   = "#2E86C1"   # river blue (flow)
WATER2  = "#9CC9E4"   # pale water fill
AMBER   = "#E0922F"   # signal — rationed
TILE    = "#F4F8FB"
TILE_E  = "#CBD8E2"
FAINT   = "#B9CAD7"

LW   = 2.6   # primary stroke
LWt  = 1.6   # thin stroke

# ── Tile-local drawing helpers (coordinate space 0..100) ───────────────
def line(ax, x, y, color=INK, lw=LW, ls="-", cap="round", z=3):
    ax.add_line(Line2D(x, y, color=color, lw=lw, ls=ls,
                       solid_capstyle=cap, solid_joinstyle="round",
                       dash_capstyle="round", zorder=z))

def circle(ax, cx, cy, r, ec=INK, fc="none", lw=LW, z=3):
    ax.add_patch(Circle((cx, cy), r, ec=ec, fc=fc, lw=lw, zorder=z))

def poly(ax, pts, ec=INK, fc="none", lw=LW, closed=True, z=3, joinstyle="round"):
    ax.add_patch(Polygon(pts, closed=closed, ec=ec, fc=fc, lw=lw,
                         joinstyle=joinstyle, capstyle="round", zorder=z))

def rrect(ax, x, y, w, h, r=6, ec=INK, fc="none", lw=LW, z=3):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle=f"round,pad=0,rounding_size={r}",
                 ec=ec, fc=fc, lw=lw, zorder=z,
                 joinstyle="round", capstyle="round"))

def arc(ax, cx, cy, w, h, t1, t2, color=INK, lw=LW, z=3):
    ax.add_patch(Arc((cx, cy), w, h, theta1=t1, theta2=t2,
                     edgecolor=color, lw=lw, zorder=z, capstyle="round"))

def smooth_wave(ax, x0, x1, y, amp, n=240, color=WATER, lw=LW, phase=0, z=3):
    xs = np.linspace(x0, x1, n)
    ys = y + amp * np.sin((xs - x0) / (x1 - x0) * 2 * np.pi * 1.0 + phase)
    line(ax, xs, ys, color=color, lw=lw, z=z)

def bezier(ax, P, color=INK, lw=LW, z=3):
    P = np.array(P, float)
    verts = [P[0]]; codes = [Path.MOVETO]
    i = 1
    while i + 2 < len(P) + 1 and i + 2 <= len(P):
        verts += [P[i], P[i+1], P[i+2]]
        codes += [Path.CURVE4, Path.CURVE4, Path.CURVE4]
        i += 3
    ax.add_patch(PathPatch(Path(verts, codes), fill=False, ec=color,
                 lw=lw, zorder=z, joinstyle="round", capstyle="round"))

def droplet(ax, cx, cy, s, ec=WATER, fc="none", lw=LW, z=3):
    P = [(cx, cy+1.4*s),
         (cx+1.05*s, cy+0.4*s), (cx+0.95*s, cy-0.85*s), (cx, cy-0.95*s),
         (cx-0.95*s, cy-0.85*s), (cx-1.05*s, cy+0.4*s), (cx, cy+1.4*s)]
    verts=[P[0]]; codes=[Path.MOVETO]
    for k in range(1,len(P),3):
        verts += [P[k],P[k+1],P[k+2]]; codes += [Path.CURVE4]*3
    ax.add_patch(PathPatch(Path(verts,codes), ec=ec, fc=fc, lw=lw,
                 zorder=z, joinstyle="round"))

# ── ICONS ──────────────────────────────────────────────────────────────
def ic_gauge(ax):  # 01 stream gauge — staff in moving water
    smooth_wave(ax, 18, 82, 40, 5.5, color=WATER, lw=LWt+0.4, z=2)
    smooth_wave(ax, 18, 82, 33, 4.5, color=WATER2, lw=LWt+0.2, phase=1.1, z=2)
    line(ax, [50,50], [40,84])                         # staff
    for ty in np.linspace(46, 80, 7):                  # tick marks
        line(ax, [50,57], [ty,ty], lw=LWt)
    poly(ax, [(50,84),(44,92),(56,92)], fc=AMBER, ec=INK, lw=LWt+0.3)  # flag

def ic_workstation(ax):  # 02 ready model — local machine
    rrect(ax, 22, 40, 40, 30, r=4)
    line(ax, [36,48],[40,40]); line(ax, [30,54],[28,28]); line(ax,[42,42],[40,28])
    rrect(ax, 66, 34, 14, 36, r=3)                     # tower
    circle(ax, 73, 62, 2.2, fc=AMBER, ec=AMBER, lw=0)
    line(ax, [69,77],[54,54], lw=LWt); line(ax,[69,77],[48,48], lw=LWt)
    smooth_wave(ax, 28, 56, 56, 3.2, color=WATER, lw=LWt+0.4, z=4)

def ic_cloud(ax):  # 03 cloud intake — upload
    verts=[(32,44)]; codes=[Path.MOVETO]
    verts.append((70,44)); codes.append(Path.LINETO)            # flat base
    for c1,c2,e in [((78,44),(78,58),(66,57)),                  # right bump
                    ((64,70),(56,70),(48,65)),                  # middle (tallest)
                    ((42,71),(30,68),(33,55)),                  # left bump
                    ((25,54),(25,45),(32,44))]:                 # close
        verts += [c1,c2,e]; codes += [Path.CURVE4]*3
    ax.add_patch(PathPatch(Path(verts,codes), fc="none", ec=INK, lw=LW,
                 joinstyle="round", capstyle="round", zorder=3))
    line(ax,[50,50],[38,60], color=WATER, lw=LW)               # upload shaft
    poly(ax,[(50,64),(43,55),(57,55)], fc=WATER, ec=WATER, lw=0)

def ic_clock(ax):  # 04 scheduler
    circle(ax, 50, 52, 24)
    line(ax,[50,50],[52,68]); line(ax,[50,62],[52,52])     # hands
    circle(ax, 50, 52, 1.8, fc=INK, ec=INK, lw=0)
    arc(ax, 50, 52, 62, 62, 60, 150, color=AMBER, lw=LWt+0.6)  # orbit
    poly(ax,[(28,72),(24,66),(33,67)], fc=AMBER, ec=AMBER, lw=0)

def ic_scanner(ax):  # 05 model scanner — magnifier over strata
    for i,yy in enumerate([34,42,50]):
        ax.add_patch(Rectangle((26,yy),34,7, ec=INK, fc=PAPER2 if i==1 else "none",
                     lw=LWt+0.4, zorder=2))
    circle(ax, 60, 60, 13, lw=LW)
    line(ax,[69,80],[51,40])                               # handle
    line(ax,[54,66],[60,60], lw=LWt); line(ax,[60,60],[54,66], lw=LWt)

def ic_settings(ax):  # 06 setting up — sliders
    ys=[64,52,40]; knobs=[64,40,72]
    for y,kx in zip(ys,knobs):
        line(ax,[26,74],[y,y], lw=LWt+0.6, color=FAINT)
        line(ax,[26,74],[y,y], lw=0)
        circle(ax, kx, y, 5.2, fc=TILE, ec=INK, lw=LW)
    circle(ax, 64,64,1.6, fc=AMBER, ec=AMBER, lw=0)

def ic_inject(ax):  # 07 data ingestion & BC update — brackets + droplet
    line(ax,[34,26,26,34],[34,42,58,66])   # [
    line(ax,[66,74,74,66],[34,42,58,66])   # ]
    droplet(ax, 50, 52, 9, ec=WATER, fc="none", lw=LW)
    line(ax,[50,50],[44,58], lw=LWt, color=WATER)
    poly(ax,[(50,60),(46,54),(54,54)], fc=WATER, ec=WATER, lw=0)

def ic_engine(ax):  # 08 HEC-RAS engine — sluice gate + turbine wheel
    rrect(ax, 24, 30, 30, 40, r=3)
    for gy in [40,50,60]: line(ax,[24,54],[gy,gy], lw=LWt)   # gate slats
    line(ax,[39,39],[70,80]); line(ax,[33,45],[80,80])       # winch
    circle(ax, 68, 50, 13)                                   # turbine
    for a in range(0,360,45):
        x=68+13*np.cos(np.radians(a)); y=50+13*np.sin(np.radians(a))
        line(ax,[68,x],[50,y], lw=LWt)
    circle(ax, 68,50,2.2, fc=INK, ec=INK, lw=0)
    smooth_wave(ax, 16, 30, 36, 2.6, color=WATER, lw=LWt+0.4, z=4)

def ic_results(ax):  # 09 results — stacked data discs (HDF-like, generic)
    def disc(y, fc):
        ax.add_patch(Arc((50,y),48,16,0,180,360, lw=0))
        # top ellipse
        ax.add_patch(matplotlib.patches.Ellipse((50,y),48,15, ec=INK, fc=fc, lw=LW, zorder=3))
    for y,fc in [(38,TILE),(50,PAPER2),(62,TILE)]:
        ax.add_patch(matplotlib.patches.Ellipse((50,y),48,15, ec=INK, fc=fc, lw=LW, zorder=3))
        line(ax,[26,26],[y, y+0], lw=0)
    # side walls
    line(ax,[26,26],[38,62]); line(ax,[74,74],[38,62])
    circle(ax, 50,62,1.8, fc=WATER, ec=WATER, lw=0)

def ic_validation(ax):  # 10 validation — model vs observation curves
    line(ax,[26,26,76],[72,30,30], lw=LWt+0.3, color=INK)   # axes
    xs=np.linspace(28,74,120)
    g=np.exp(-((xs-50)/12)**2)
    ax.plot(xs, 34+30*g, color=WATER, lw=LW, solid_capstyle="round", zorder=3)
    ax.plot(xs, 33+27*np.exp(-((xs-52)/13)**2), color=INK, lw=LW, ls=(0,(1,2)),
            dash_capstyle="round", zorder=3)
    circle(ax, 50,64,1.8, fc=AMBER, ec=AMBER, lw=0)

def ic_agent(ax):  # 11 warning agent — calm geometric sentinel
    line(ax,[50,50],[74,82]); circle(ax,50,84,2.4, fc=AMBER, ec=INK, lw=LWt)
    rrect(ax, 32, 44, 36, 28, r=8)                          # head
    circle(ax, 42, 58, 3.4, fc=WATER, ec=INK, lw=LWt)       # eyes
    circle(ax, 58, 58, 3.4, fc=WATER, ec=INK, lw=LWt)
    line(ax,[44,56],[50,50], lw=LWt)                        # mouth
    line(ax,[32,26],[58,58]); line(ax,[68,74],[58,58])      # ears

def ic_dashboard(ax):  # 12 live dashboard — gauge + sparkline panel
    rrect(ax, 22, 32, 56, 38, r=4)
    arc(ax, 40, 46, 22, 22, 20, 160, color=INK, lw=LW)
    line(ax,[40,48],[46,55], lw=LWt+0.3, color=AMBER)       # needle
    circle(ax,40,46,1.6, fc=INK, ec=INK, lw=0)
    xs=np.linspace(54,72,60); ys=58+4*np.sin((xs-54)/3)
    ax.plot(xs,ys, color=WATER, lw=LWt+0.6, solid_capstyle="round", zorder=3)
    line(ax,[54,72],[40,40], lw=LWt, color=FAINT)

def ic_email(ax):  # 13 email alert (SMTP)
    rrect(ax, 24, 36, 52, 34, r=4)
    line(ax,[24,50,76],[68,52,68])                          # flap
    poly(ax,[(70,70),(70,82),(82,76)], fc=AMBER, ec=INK, lw=LWt)  # signal corner
    line(ax,[72,72],[60,66], lw=0)

def ic_csv(ax):  # 14 CSV
    poly(ax,[(30,26),(30,74),(62,74),(70,66),(70,26)], fc=TILE, ec=INK, lw=LW)
    line(ax,[62,62,70],[74,66,66], lw=LWt)                  # dog-ear
    for gy in [36,44,52]: line(ax,[36,64],[gy,gy], lw=LWt, color=WATER)
    line(ax,[50,50],[34,54], lw=LWt, color=WATER)
    ax.text(50,30,"CSV", ha="center", va="center", fontproperties=F_MONO,
            fontsize=8.5, color=INK, zorder=5)

def ic_png(ax):  # 15 PNG
    poly(ax,[(30,26),(30,74),(62,74),(70,66),(70,26)], fc=TILE, ec=INK, lw=LW)
    line(ax,[62,62,70],[74,66,66], lw=LWt)
    circle(ax, 44, 56, 3.2, fc=AMBER, ec=INK, lw=LWt)       # sun
    line(ax,[34,46,54,66],[40,50,44,54], color=WATER, lw=LWt+0.4)  # mountains
    ax.text(50,32,"PNG", ha="center", va="center", fontproperties=F_MONO,
            fontsize=8.5, color=INK, zorder=5)

def ic_container(ax):  # 16 container — generic (Docker concept, original)
    rrect(ax, 24, 36, 52, 30, r=3)
    for cx in [34,44,54,66]: line(ax,[cx,cx],[36,66], lw=LWt)  # corrugation
    smooth_wave(ax, 20, 80, 30, 2.4, color=WATER, lw=LWt+0.5, z=4)
    # little stacked cubes on top
    for bx in [38,50]:
        rrect(ax, bx, 68, 9, 8, r=1.5, lw=LWt+0.4)
    circle(ax, 64,72,2.0, fc=AMBER, ec=AMBER, lw=0)

ICONS = [
    ("01","Stream Gauge","USGS / NOAA input", ic_gauge),
    ("02","Local Machine","ready model", ic_workstation),
    ("03","Cloud Intake","S3 model source", ic_cloud),
    ("04","Scheduler","real-time cadence", ic_clock),
    ("05","Model Scanner","detect .prj / HDF", ic_scanner),
    ("06","Configuration","window + threads", ic_settings),
    ("07","BC Injection","ingest + update", ic_inject),
    ("08","Hydraulic Engine","HEC-RAS run", ic_engine),
    ("09","Results Store","HDF5 extract", ic_results),
    ("10","Validation","model vs observed", ic_validation),
    ("11","Warning Agent","threshold watch", ic_agent),
    ("12","Live Dashboard","monitor", ic_dashboard),
    ("13","Email Alert","SMTP", ic_email),
    ("14","CSV Export","time series", ic_csv),
    ("15","PNG Export","maps + plots", ic_png),
    ("16","Container","portable deploy", ic_container),
]

# ── Plate layout ───────────────────────────────────────────────────────
NC, NR = 4, 4
fig = plt.figure(figsize=(13.0, 14.6), dpi=200)
fig.patch.set_facecolor(PAPER)

# faint engineering grid on the ground
gax = fig.add_axes([0,0,1,1]); gax.set_xlim(0,1); gax.set_ylim(0,1); gax.axis("off")
for gx in np.linspace(0.05,0.95,19):
    gax.add_line(Line2D([gx,gx],[0.04,0.9], color="#DCE6ED", lw=0.5, zorder=0))
for gy in np.linspace(0.07,0.9,18):
    gax.add_line(Line2D([0.05,0.95],[gy,gy], color="#DCE6ED", lw=0.5, zorder=0))

# header
gax.text(0.063, 0.945, "HECinBOX", fontproperties=F_DISP, fontsize=40,
         color=INK, zorder=5)
gax.text(0.066, 0.917, "HYDROLOGICAL  INSTRUMENTARIUM", fontproperties=F_MONO,
         fontsize=11.5, color=WATER, zorder=5)
gax.text(0.937, 0.946, "PLATE I", fontproperties=F_MONO, fontsize=11.5,
         color=INK, ha="right", zorder=5)
gax.text(0.937, 0.920, "automated 2D unsteady pipeline · 16 instruments",
         fontproperties=F_SANS, fontsize=10.5, color="#5B7488", ha="right", zorder=5)
gax.add_line(Line2D([0.063,0.937],[0.905,0.905], color=INK, lw=1.4, zorder=5))

# tile grid geometry (in figure coords)
left, right = 0.055, 0.945
top, bot    = 0.875, 0.075
gw = (right-left)/NC
gh = (top-bot)/NR
pad = 0.011

for idx,(num,name,sub,fn) in enumerate(ICONS):
    r = idx // NC; c = idx % NC
    x0 = left + c*gw + pad
    y0 = top - (r+1)*gh + pad
    w = gw - 2*pad; h = gh - 2*pad
    ax = fig.add_axes([x0, y0, w, h])
    ax.set_xlim(0,100); ax.set_ylim(0,100); ax.axis("off")
    ax.set_aspect("auto")
    # tile
    ax.add_patch(FancyBboxPatch((6,18),88,78,
        boxstyle="round,pad=0,rounding_size=7",
        ec=TILE_E, fc=TILE, lw=1.3, zorder=1))
    # corner registration ticks
    for (mx,my,dx,dy) in [(12,90,6,0),(12,90,0,-6),(88,90,-6,0),(88,90,0,-6)]:
        ax.add_line(Line2D([mx,mx+dx],[my,my+dy], color=FAINT, lw=1.0, zorder=2))
    fn(ax)
    # specimen index + label band
    ax.text(13, 90, num, fontproperties=F_MONO, fontsize=9.5, color=WATER,
            va="center", ha="left", zorder=6)
    ax.text(50, 11.5, name, fontproperties=F_SANS_B, fontsize=11.5, color=INK,
            ha="center", va="center", zorder=6)
    ax.text(50, 3.5, sub.upper(), fontproperties=F_MONO, fontsize=6.6,
            color="#6B8295", ha="center", va="center", zorder=6)

# footer
gax.text(0.063, 0.045, "drawn to a single stroke · navy ink · river blue · amber for signal",
         fontproperties=F_SANS, fontsize=9.5, color="#6B8295", zorder=5)
gax.text(0.937, 0.045, "v3.1.5", fontproperties=F_MONO, fontsize=9.5,
         color=INK, ha="right", zorder=5)

out = "/Users/ehsankahrizi/Desktop/AutoHEC-RAS/assets/workflow_icons/HECinBox_icons_plate.png"
fig.savefig(out, dpi=200, facecolor=PAPER)
print("saved", out)
