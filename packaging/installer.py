# -*- coding: utf-8 -*-
"""右键轮盘菜单 - 安装 / 卸载程序（图形对话框，无控制台）。

安装：把 RadialMenu.exe 装到 %LOCALAPPDATA%\\RadialMenu，
      创建桌面 + 开始菜单快捷方式，可选开机自启。
卸载：setup.exe --uninstall
"""
import os
import shutil
import sys

import pythoncom
import win32api
import win32com.client
import win32con
import win32gui

APP_NAME = "右键轮盘菜单"
EXE_NAME = "RadialMenu.exe"
INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "RadialMenu")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "RadialMenu"

MB = win32con.MB_ICONINFORMATION
MB_Q = win32con.MB_ICONQUESTION | win32con.MB_YESNO
IDYES = 6


def msg(text, caption=APP_NAME, flags=MB):
    return win32gui.MessageBox(0, text, caption, flags)


def resource_path(rel):
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def shell_link(link_path, target, desc, workdir):
    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(link_path)
    sc.Targetpath = target
    sc.Arguments = ""
    sc.WorkingDirectory = workdir
    sc.IconLocation = target + ",0"
    sc.Description = desc
    sc.save()


def get_desktop():
    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("WScript.Shell")
    return shell.SpecialFolders("Desktop")


def get_startmenu():
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs")


def set_autostart(enable, exe_path):
    h = win32api.RegOpenKeyEx(win32con.HKEY_CURRENT_USER, RUN_KEY, 0, win32con.KEY_ALL_ACCESS)
    try:
        if enable:
            win32api.RegSetValueEx(h, RUN_NAME, 0, win32con.REG_SZ, f'"{exe_path}"')
        else:
            try:
                win32api.RegDeleteValue(h, RUN_NAME)
            except Exception:
                pass
    finally:
        win32api.RegCloseKey(h)


def uninstall():
    removed = []
    # 删快捷方式
    for folder in (get_desktop(), get_startmenu()):
        for name in (APP_NAME + ".lnk", "卸载" + APP_NAME + ".lnk"):
            p = os.path.join(folder, name)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed.append(name)
                except Exception:
                    pass
    # 删自启项
    try:
        set_autostart(False, "")
    except Exception:
        pass
    # 删安装目录（若程序在跑，先尝试结束同名进程）
    if os.path.isdir(INSTALL_DIR):
        try:
            os.system(f'taskkill /F /IM {EXE_NAME} >nul 2>&1')
        except Exception:
            pass
        try:
            shutil.rmtree(INSTALL_DIR)
            removed.append("程序文件")
        except Exception as e:
            msg(f"无法删除程序目录（可能程序仍在运行）：\n{INSTALL_DIR}\n\n{e}",
                flags=win32con.MB_ICONWARNING)
    msg("已卸载「" + APP_NAME + "」。\n\n移除内容：" + ("、".join(removed) if removed else "（无）"))
    return 0


def install():
    src_exe = resource_path(EXE_NAME)
    if not os.path.exists(src_exe):
        msg(f"安装文件缺失，无法继续：\n{src_exe}", flags=win32con.MB_ICONERROR)
        return 1

    # 询问开机自启
    ans = msg("是否开机自动启动「" + APP_NAME + "」？\n\n"
              "选“是”：开机后自动在后台运行（托盘可见）。\n"
              "选“否”：需要时手动从桌面/开始菜单启动。", flags=MB_Q)
    autostart = (ans == IDYES)

    os.makedirs(INSTALL_DIR, exist_ok=True)
    dst_exe = os.path.join(INSTALL_DIR, EXE_NAME)

    # 若旧程序在运行，先结束以便覆盖
    os.system(f'taskkill /F /IM {EXE_NAME} >nul 2>&1')
    try:
        shutil.copy2(src_exe, dst_exe)
    except Exception as e:
        msg(f"复制程序文件失败：\n{e}", flags=win32con.MB_ICONERROR)
        return 1

    # 快捷方式
    errors = []
    try:
        shell_link(os.path.join(get_desktop(), APP_NAME + ".lnk"),
                   dst_exe, "右键拖拽快速打开文件夹", INSTALL_DIR)
    except Exception as e:
        errors.append("桌面快捷方式：" + str(e))
    try:
        shell_link(os.path.join(get_startmenu(), APP_NAME + ".lnk"),
                   dst_exe, "右键拖拽快速打开文件夹", INSTALL_DIR)
    except Exception as e:
        errors.append("开始菜单快捷方式：" + str(e))
    try:
        shell_link(os.path.join(get_startmenu(), "卸载" + APP_NAME + ".lnk"),
                   sys.executable, "卸载 " + APP_NAME, os.path.dirname(sys.executable))
        # 卸载快捷方式需要带 --uninstall 参数，重做一次
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("WScript.Shell")
        ulink = os.path.join(get_startmenu(), "卸载" + APP_NAME + ".lnk")
        sc = shell.CreateShortCut(ulink)
        sc.Targetpath = sys.executable
        sc.Arguments = "--uninstall"
        sc.WorkingDirectory = os.path.dirname(sys.executable)
        sc.IconLocation = sys.executable + ",0"
        sc.Description = "卸载 " + APP_NAME
        sc.save()
    except Exception as e:
        errors.append("卸载快捷方式：" + str(e))

    # 自启
    try:
        set_autostart(autostart, dst_exe)
    except Exception as e:
        errors.append("开机自启：" + str(e))

    detail = (f"安装位置：{INSTALL_DIR}\n"
              f"桌面快捷方式：已创建\n"
              f"开始菜单：已创建\n"
              f"开机自启：{'已开启' if autostart else '未开启'}\n\n"
              f"用法：任意位置按住鼠标右键拖动，"
              f"弹出轮盘后朝方向松手即可打开文件夹。\n"
              f"托盘图标可打开设置、查看日志或退出。")
    if errors:
        detail += "\n\n部分步骤未完成：\n" + "\n".join(errors)
    msg("安装完成！\n\n" + detail)
    return 0


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        sys.exit(uninstall())
    sys.exit(install())
