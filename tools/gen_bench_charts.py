#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为「Go vs Nim 性能实测」系列生成 SVG 配图（纯标准库，无第三方依赖）。

用法：
    python tools/gen_bench_charts.py

输出到各篇文章目录下：
    content/programming-misc/go-vs-nim-algorithms/charts/bench-regular.svg
    content/programming-misc/go-vs-nim-bigint/charts/{bench-bigint,bench-gmp,bench-pi-stages}.svg
    content/programming-misc/go-vs-nim-gc/charts/gc-bars.svg

设计要点：
  * 横向分组条形图（每根柱子一行 + 组内分行），杜绝「同组柱子重叠」问题；
  * 统一线性轴（不使用对数轴），极小值用最小可见宽度兜底，避免被压没；
  * 每根柱子右侧都标数值，不依赖肉眼估读。
"""

import math
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

W = 960                 # 画布宽
LEFT = 168              # 左侧组标签区宽
RIGHT = 92              # 右侧数值标签区宽
TOP = 96                # 标题 + 副标题 + 图例区高
BOTTOM = 56             # 轴标签区高
GROUP_PAD = 20          # 组间距
BAR_GAP = 2             # 组内柱间距

BG = "#ffffff"
GRID = "#e6e6e6"
AXIS = "#cccccc"
TEXT = "#1a1a1a"
SUB = "#666666"
AXL = "#888888"
FONT = "system-ui,-apple-system,'Segoe UI','Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif"

PALETTE = [
    "#0076A8",  # Go / deep blue
    "#E8A200",  # Nim / amber
    "#26A65B",  # green
    "#D64541",  # red
    "#5C6BC0",  # indigo
    "#8E44AD",  # purple
    "#D35400",  # orange
]


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def fmt(v):
    """数值显示：既能表达 0.5，也能表达 8104.29。"""
    if v == 0:
        return "0"
    if v < 1:
        return ("%.2f" % v).rstrip("0").rstrip(".")
    if v < 10:
        return ("%.1f" % v).rstrip("0").rstrip(".")
    return "{:,.0f}".format(v)


def linear_ticks(vmax, count=5):
    """线性轴：生成 0..vmax 之间的整齐刻度。"""
    raw = vmax / count
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw / mag <= m:
            step = m * mag
            break
    else:
        step = 10 * mag
    ticks, v = [], 0.0
    while v <= vmax + step * 0.001:
        ticks.append(v)
        v += step
    return ticks


def log_ticks(vmin, vmax):
    """对数轴：生成 1/2/5 × 10^k 形态的刻度。"""
    lo = math.floor(math.log10(max(vmin, 1e-6)))
    hi = math.ceil(math.log10(max(vmax, 1e-6)))
    ticks = []
    for k in range(lo, hi + 1):
        for m in (1, 2, 5):
            v = m * (10 ** k)
            if vmin * 0.5 <= v <= vmax * 2:
                ticks.append(v)
    return ticks


MIN_W = 2.5  # 极小值的最小可见宽度（px），保证再小的柱也不会完全消失


def draw_chart(path, title, subtitle, series, groups, log=False, axis_note="ms（越小越快）"):
    """
    series: ['Go', 'Nim', ...]
    groups: [ (组名, [v0, v1, ...]), ... ]
    """
    values = [v for _, vs in groups for v in vs]
    vmax = max(values)
    vmin = min(values)

    bar_h = 16 if len(series) <= 3 else 11
    row_h = bar_h + BAR_GAP
    group_h = len(series) * row_h
    H = TOP + len(groups) * group_h + max(0, len(groups) - 1) * GROUP_PAD + BOTTOM

    x0 = LEFT
    x1 = W - RIGHT

    if log:
        ticks = log_ticks(vmin, vmax)
        if len(ticks) < 2:
            ticks = linear_ticks(vmax)
            log = False
    if not log:
        ticks = linear_ticks(vmax)

    def sx(v):
        if log:
            lo = math.log10(ticks[0])
            hi = math.log10(ticks[-1])
            r = (math.log10(max(v, ticks[0] * 0.05)) - lo) / (hi - lo)
        else:
            r = v / float(ticks[-1])
        return x0 + max(0.0, min(1.0, r)) * (x1 - x0)

    out = []
    a = out.append
    a('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
      'style="max-width:100%%;height:auto" font-family="%s">' % (W, H, W, H, FONT))
    a('<rect x="0" y="0" width="%d" height="%d" fill="%s"/>' % (W, H, BG))
    a('<text x="28" y="34" font-size="20" font-weight="700" fill="%s">%s</text>' % (TEXT, esc(title)))
    a('<text x="28" y="56" font-size="13" fill="%s">%s</text>' % (SUB, esc(subtitle)))

    # 图例
    lx = 28
    ly = 72
    for i, name in enumerate(series):
        color = PALETTE[i % len(PALETTE)]
        a('<rect x="%d" y="%d" width="13" height="13" rx="2" fill="%s"/>'
          % (lx, ly, color))
        a('<text x="%d" y="%d" font-size="13" fill="#333">%s</text>'
          % (lx + 18, ly + 11, esc(name)))
        lx += 22 + len(name) * 8.2 + 22

    plot_bottom = H - BOTTOM
    # 网格 + 刻度
    for t in ticks:
        px = sx(t)
        a('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1"/>'
          % (px, TOP, px, plot_bottom, GRID))
        a('<text x="%.1f" y="%d" font-size="11.5" fill="%s" text-anchor="middle">%s</text>'
          % (px, plot_bottom + 18, AXL, fmt(t)))
    a('<text x="%.1f" y="%d" font-size="12" fill="%s" text-anchor="end">%s</text>'
      % (x1, plot_bottom + 38, SUB, esc(axis_note)))

    # 组 + 柱
    y = TOP
    for gi, (gname, vs) in enumerate(groups):
        group_top = y
        for i, v in enumerate(vs):
            by = group_top + i * row_h
            bw = max(MIN_W, sx(v) - x0)
            color = PALETTE[i % len(PALETTE)]
            a('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="2" fill="%s" opacity="0.92"/>'
              % (x0, by, bw, bar_h, color))
            a('<text x="%.1f" y="%.1f" font-size="11.5" fill="#333">%s</text>'
              % (x0 + bw + 5, by + bar_h - 4, fmt(v)))
        # 组标签（垂直居中于整组）
        cy = group_top + group_h / 2.0
        a('<text x="%d" y="%.1f" font-size="13" font-weight="600" fill="#333" text-anchor="end">%s</text>'
          % (LEFT - 14, cy + 4.5, esc(gname)))
        # 组分隔线
        if gi:
            a('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1" stroke-dasharray="3 3"/>'
              % (x0, group_top - GROUP_PAD / 2.0, x1, group_top - GROUP_PAD / 2.0, GRID))
        y = group_top + group_h + GROUP_PAD

    # 基线
    a('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5"/>'
      % (x0, TOP, x0, plot_bottom, AXIS))
    a('</svg>')

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("wrote %s (%d bytes, %dx%d)" % (os.path.relpath(path, ROOT), os.path.getsize(path), W, H))


def main():
    alg = os.path.join(ROOT, "content/programming-misc/go-vs-nim-algorithms/charts")
    big = os.path.join(ROOT, "content/programming-misc/go-vs-nim-bigint/charts")
    gc = os.path.join(ROOT, "content/programming-misc/go-vs-nim-gc/charts")

    # 一、常规 6 项算法（线性轴）
    draw_chart(
        os.path.join(alg, "bench-regular.svg"),
        "常规 6 项算法：Go vs Nim",
        "同一算法逻辑两端一致，7 次采样取中位数，单线程；单位 ms",
        ["Go 1.26.7", "Nim 2.2.12 (ORC)"],
        [
            ("fib32", [9.2, 44.3]),
            ("sieve50M", [448.1, 386.9]),
            ("quicksort2M", [154.2, 165.9]),
            ("matmul512", [122.6, 97.7]),
            ("fnv64MB", [93.0, 57.9]),
            ("alloc2M", [22.6, 66.9]),
        ],
        log=False,
        axis_note="耗时 ms — 越小越快",
    )

    # 二、大数运算三实现（对数轴）
    draw_chart(
        os.path.join(big, "bench-bigint.svg"),
        "大数运算：Go math/big vs Nim bigints vs Nim 自研库",
        "横轴为线性刻度，极小值以最小可见宽度显示；单位 ms",
        ["Go math/big", "Nim bigints", "Nim 自研大数库"],
        [
            ("fact10k", [8.0, 14.4, 55.8]),
            ("fibBig100k", [28.5, 102.0, 105.3]),
            ("mulBig500k", [1.0, 210.3, 37.4]),
            ("modpow2048", [0.5, 24.6, 0.5]),
        ],
        log=False,
        axis_note="耗时 ms（线性轴）— 越小越快",
    )

    # 三、GMP 四端（对数轴）
    draw_chart(
        os.path.join(big, "bench-gmp.svg"),
        "GMP 四端大数对比（含百万位 π）",
        "C+GMP 为原生参照线；横轴为线性刻度，极小值以最小可见宽度显示；单位 ms",
        ["Go math/big", "Go+GMP (cgo)", "Nim+GMP (FFI)", "C+GMP (原生)"],
        [
            ("fact100k", [731.69, 751.27, 682.91, 678.51]),
            ("fibBig500k", [498.86, 515.44, 440.76, 447.17]),
            ("mulBig50M", [159.91, 298.60, 296.08, 305.73]),
            ("modpow8192", [57.42, 36.42, 36.12, 36.31]),
            ("pi1000000", [8104.29, 1034.78, 740.49, 715.50]),
        ],
        log=False,
        axis_note="耗时 ms（线性轴）— 越小越快",
    )

    # 三之二、GMP 四端（不含 π，让前四项的差异看得清）
    draw_chart(
        os.path.join(big, "bench-gmp-nopi.svg"),
        "GMP 四端大数对比（去掉百万位 π）",
        "π 一项高达 8,104 ms，留在图里会把其余四项压成短线；本图剔除 π，单位 ms",
        ["Go math/big", "Go+GMP (cgo)", "Nim+GMP (FFI)", "C+GMP (原生)"],
        [
            ("fact100k", [731.69, 751.27, 682.91, 678.51]),
            ("fibBig500k", [498.86, 515.44, 440.76, 447.17]),
            ("mulBig50M", [159.91, 298.60, 296.08, 305.73]),
            ("modpow8192", [57.42, 36.42, 36.12, 36.31]),
        ],
        log=False,
        axis_note="耗时 ms（线性轴）— 越小越快",
    )

    # 四、百万位 π 阶段拆解（线性轴）
    draw_chart(
        os.path.join(big, "bench-pi-stages.svg"),
        "百万位 π 阶段拆解：Go math/big vs C+GMP",
        "Chudnovsky + binary splitting 切成四段分别计时；横轴线性刻度，单位 ms",
        ["Go math/big", "C+GMP"],
        [
            ("bs 乘法树", [1593.0, 504.5]),
            ("开方", [5705.1, 62.5]),
            ("最终乘法", [255.5, 32.9]),
            ("最终除法", [411.2, 78.0]),
        ],
        log=False,
        axis_note="耗时 ms（线性轴）— 越小越快",
    )

    # 五、GC 七项对比（对数轴）
    draw_chart(
        os.path.join(gc, "gc-bars.svg"),
        "GC 基准：Nim 6 种内存管理模式 vs Go GC",
        "同一份 Nim 代码分别以不同 --mm 编译；横轴线性刻度，单位 ms",
        ["Go GC", "refc", "mark&sweep", "arc", "orc", "boehm", "atomicArc"],
        [
            ("allocSmall3M", [3.0, 71.6, 59.8, 113.2, 124.0, 81.5, 113.4]),
            ("allocBig1500", [97.5, 33.4, 132.6, 18.5, 18.3, 475.5, 18.8]),
            ("churn4M", [108.8, 147.1, 85.2, 194.0, 200.9, 64.9, 196.8]),
            ("treeBuild200", [521.4, 709.1, 564.7, 632.8, 688.0, 301.1, 647.7]),
            ("cycleRefs300", [14.2, 15.6, 12.6, 12.2, 46.4, 8.8, 16.6]),
            ("strBuild20k", [117.3, 39.5, 237.2, 31.6, 32.2, 3311.7, 32.7]),
            ("seqGrowth10M", [102.6, 100.7, 99.0, 189.7, 190.6, 120.2, 193.0]),
        ],
        log=False,
        axis_note="耗时 ms（线性轴）— 越小越快",
    )

    # 五之二、GC 七项对比（去掉 boehm，让其余 6 个实现的差异看得清）
    draw_chart(
        os.path.join(gc, "gc-bars-noboehm.svg"),
        "GC 基准（去掉 boehm）：其余 6 个实现对比",
        "boehm 在 strBuild20k 上高达 3,312 ms，留在图里会把其余柱子压成一条线；本图剔除 boehm，单位 ms",
        ["Go GC", "refc", "mark&sweep", "arc", "orc", "atomicArc"],
        [
            ("allocSmall3M", [3.0, 71.6, 59.8, 113.2, 124.0, 113.4]),
            ("allocBig1500", [97.5, 33.4, 132.6, 18.5, 18.3, 18.8]),
            ("churn4M", [108.8, 147.1, 85.2, 194.0, 200.9, 196.8]),
            ("treeBuild200", [521.4, 709.1, 564.7, 632.8, 688.0, 647.7]),
            ("cycleRefs300", [14.2, 15.6, 12.6, 12.2, 46.4, 16.6]),
            ("strBuild20k", [117.3, 39.5, 237.2, 31.6, 32.2, 32.7]),
            ("seqGrowth10M", [102.6, 100.7, 99.0, 189.7, 190.6, 193.0]),
        ],
        log=False,
        axis_note="耗时 ms（线性轴）— 越小越快",
    )


if __name__ == "__main__":
    main()
