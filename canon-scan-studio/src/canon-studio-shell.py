#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
canon-studio 的原生窗口外壳 —— GTK3 + WebKit2 4.1（本机 loong64 原生）

界面是 HTML/CSS/JS，这层只负责开一个真实的桌面窗口，并做网页做不到的事：
窗口级快捷键（Ctrl+Q 退出、F11 全屏、Ctrl+R 重载）、把 WebKit 数据目录指到
~/.cache 避免污染其它位置。
"""

import os
import sys
import signal

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, Gdk, GLib   # noqa: E402


def app_cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "canon-studio", "webkit")
    os.makedirs(d, exist_ok=True)
    return d


class StudioWindow(Gtk.Window):
    def __init__(self, url):
        super().__init__(title="Canon 扫描")
        self.url = url
        self.set_default_size(1380, 880)
        self.set_size_request(880, 600)
        self.set_position(Gtk.WindowPosition.CENTER)

        ctx = WebKit2.WebContext.get_default()
        try:
            dm = ctx.get_website_data_manager()
            dm.set_base_cache_directory(app_cache_dir())
        except Exception:                            # noqa: BLE001
            pass

        self.view = WebKit2.WebView.new_with_context(ctx)
        try:
            s = self.view.get_settings()
            s.set_property("enable-smooth-scrolling", True)
            s.set_property("enable-page-cache", False)
            s.set_property("enable-developer-extras",
                           bool(os.environ.get("CANON_DEVTOOLS")))
            s.set_property("allow-file-access-from-file-urls", False)
        except Exception:                            # noqa: BLE001
            pass

        self.view.load_uri(url)
        self.add(self.view)

        # 深色窗口底色，避免加载瞬间闪白
        try:
            self.override_background_color(
                Gtk.StateFlags.NORMAL, Gdk.RGBA(0.027, 0.031, 0.047, 1.0))
        except Exception:                            # noqa: BLE001
            pass

        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key)

    def on_key(self, _w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        k = ev.keyval
        if ctrl and k in (Gdk.KEY_q, Gdk.KEY_Q):
            Gtk.main_quit()
            return True
        if k == Gdk.KEY_F11:
            if self.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                self.unfullscreen()
            else:
                self.fullscreen()
            return True
        if ctrl and k in (Gdk.KEY_r, Gdk.KEY_R):
            self.view.reload()
            return True
        return False


def main():
    if len(sys.argv) < 2:
        print("用法: shell.py <url>", file=sys.stderr)
        return 2
    url = sys.argv[1]

    # 某些显卡/驱动组合下 WebKit 的合成模式会花屏或黑屏，允许用环境变量降级
    if os.environ.get("CANON_WEBKIT_NO_COMPOSITING") == "1":
        os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

    win = StudioWindow(url)
    win.show_all()

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)

    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
