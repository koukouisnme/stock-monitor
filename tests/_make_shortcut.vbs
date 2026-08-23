Set s = CreateObject("WScript.Shell")
desk = s.SpecialFolders("Desktop")
Set lnk = s.CreateShortcut(desk & "\A股监控台.lnk")
lnk.TargetPath = "C:\Users\Administrator\Desktop\stock_monitor\启动监控台.bat"
lnk.WorkingDirectory = "C:\Users\Administrator\Desktop\stock_monitor"
lnk.IconLocation = "C:\Users\Administrator\Desktop\stock_monitor\tools\monitor.ico,0"
lnk.Description = "A-Stock Monitor: Web + Tunnel"
lnk.Save
WScript.Echo "shortcut created"
