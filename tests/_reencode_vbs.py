"""把 VBS 转存为 GBK 编码（cscript 按 ANSI 解析）。"""
p = r"c:\Users\Administrator\Desktop\stock_monitor\tests\_make_shortcut.vbs"
text = open(p, "r", encoding="utf-8").read()
open(p, "w", encoding="gbk").write(text)
print("re-encoded to gbk")
