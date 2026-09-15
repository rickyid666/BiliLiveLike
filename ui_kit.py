# -*- coding: utf-8 -*-
"""绘制与配色工具: 圆角矩形 / 渐变 / 颜色混合"""
import tkinter as tk


def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in rgb)


def blend(c1, c2, t):
    """把 c1 按比例 t 混向 c2 (t=0 全 c1, t=1 全 c2)"""
    a, b = hex2rgb(c1), hex2rgb(c2)
    return rgb2hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def color_distance(c1, c2):
    """两个颜色的感知差异 (RGB 距离, 用于判断"哪个角色真的变了色")"""
    a, b = hex2rgb(c1), hex2rgb(c2)
    return sum(abs(a[i] - b[i]) for i in range(3))


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形 (用平滑多边形近似)"""
    r = max(0, min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, splinesteps=24, **kw)


def gradient(canvas, x1, y1, x2, y2, c_from, c_to, steps=64):
    """横向线性渐变 (逐列绘制)"""
    w = max(1, x2 - x1)
    steps = max(2, min(steps, w))
    for i in range(steps):
        t = i / (steps - 1)
        xa = x1 + w * i / steps
        xb = x1 + w * (i + 1) / steps + 1
        canvas.create_rectangle(xa, y1, xb, y2, fill=blend(c_from, c_to, t), outline="")


def shadow(canvas, x1, y1, x2, y2, r, base, level=0.05, layers=3):
    """用多层低透明度圆角矩形模拟投影"""
    for i in range(layers, 0, -1):
        off = i
        col = blend(base, "#0B1220", level * (layers - i + 1))
        round_rect(canvas, x1 + off * 0.4, y1 + off, x2 - off * 0.4, y2 + off,
                   r, fill=col, outline="")
