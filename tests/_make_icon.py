"""生成应用图标.ico：深色圆角底 + 红涨绿跌蜡烛 + 金色均线。"""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角深色底
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=48, fill=(13, 17, 23, 255),
                    outline=(88, 166, 255, 255), width=4)

def candle(cx, top, bot, wick_t, wick_b, color):
    d.line([cx, wick_t, cx, wick_b], fill=color, width=6)
    d.rounded_rectangle([cx - 18, top, cx + 18, bot], radius=6, fill=color)

# 绿跌→红涨 三根蜡烛（左低到右高）
candle(72, 120, 190, 100, 205, (63, 185, 80, 255))     # 绿
candle(128, 90, 165, 68, 180, (63, 185, 80, 255))      # 绿
candle(184, 52, 128, 30, 145, (248, 81, 73, 255))      # 红

# 金色上升均线
pts = [(40, 200), (90, 170), (140, 130), (216, 55)]
d.line(pts, fill=(210, 153, 34, 255), width=8, joint="curve")
# 终端箭头
d.polygon([(216, 55), (196, 62), (206, 40)], fill=(210, 153, 34, 255))

img.save(r"c:\Users\Administrator\Desktop\stock_monitor\tools\monitor.ico",
         sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("ico saved")
