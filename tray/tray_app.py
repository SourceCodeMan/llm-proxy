import json
import threading
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
import pystray

CONFIG_FILE = Path.home() / ".llm_proxy_tray.json"
DEFAULT_SERVER = "http://ubuntu-server:8742"

COLORS = {
    "send":    "#00cc44",  # green  - GPU active
    "hold":    "#ffaa00",  # amber  - queuing
    "reject":  "#cc2200",  # red    - forcing cloud
    "offline": "#888888",  # gray   - server unreachable
}

LABELS = {
    "send":   "SEND – GPU",
    "hold":   "HOLD – Cloud (gaming)",
    "reject": "REJECT – Cloud",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {"server": DEFAULT_SERVER}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


config = load_config()


def make_icon(color: str, letter: str = "") -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=color)
    if letter:
        # draw a white letter in the centre
        try:
            font = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), letter, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((64 - w) / 2, (64 - h) / 2 - 2), letter, fill="white", font=font)
    return img


ICON_MAP = {
    "send":    make_icon(COLORS["send"],    "G"),
    "hold":    make_icon(COLORS["hold"],    "H"),
    "reject":  make_icon(COLORS["reject"],  "R"),
    "offline": make_icon(COLORS["offline"], "?"),
}


_tray_icon: pystray.Icon = None
_stop_refresh = threading.Event()


def get_status() -> dict | None:
    try:
        r = requests.get(f"{config['server']}/status", timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def post_flag(value: str):
    try:
        requests.post(f"{config['server']}/flag/{value}", timeout=3)
    except Exception as e:
        show_error(f"Could not reach server:\n{e}")


def refresh_icon():
    global _tray_icon
    if not _tray_icon:
        return
    status = get_status()
    if status:
        flag = status.get("flag", "offline")
        online = status.get("gandalf_online", False)
        depth = status.get("queue_depth", 0)
        gpu_str = "online" if online else "OFFLINE"
        tooltip = f"LLM Proxy | {LABELS.get(flag, flag)} | GPU: {gpu_str} | Queue: {depth}"
        _tray_icon.icon = ICON_MAP.get(flag, ICON_MAP["offline"])
    else:
        tooltip = "LLM Proxy | server unreachable"
        _tray_icon.icon = ICON_MAP["offline"]
    _tray_icon.title = tooltip


def auto_refresh_loop():
    while not _stop_refresh.wait(30):
        refresh_icon()


def on_set_flag(value: str):
    def handler(icon, item):
        post_flag(value)
        refresh_icon()
    return handler


def show_error(msg: str):
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("LLM Proxy Tray", msg, parent=root)
    root.destroy()


def open_config_dialog(icon, item):
    def save_and_close():
        config["server"] = entry.get().strip().rstrip("/")
        save_config(config)
        win.destroy()
        refresh_icon()

    win = tk.Tk()
    win.title("LLM Proxy – Server Config")
    win.geometry("420x110")
    win.resizable(False, False)
    tk.Label(win, text="Proxy server URL:", anchor="w").pack(fill="x", padx=12, pady=(12, 2))
    entry = tk.Entry(win, width=55)
    entry.insert(0, config["server"])
    entry.pack(padx=12)
    tk.Button(win, text="Save", command=save_and_close, width=10).pack(pady=8)
    win.mainloop()


def on_quit(icon):
    _stop_refresh.set()
    icon.stop()


def main():
    global _tray_icon

    status = get_status()
    flag = status.get("flag", "offline") if status else "offline"

    menu = pystray.Menu(
        pystray.MenuItem("Set: SEND  (GPU)",       on_set_flag("send")),
        pystray.MenuItem("Set: HOLD  (cloud/gaming)", on_set_flag("hold")),
        pystray.MenuItem("Set: REJECT (cloud)",    on_set_flag("reject")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh status",         lambda i, it: refresh_icon()),
        pystray.MenuItem("Configure server...",    open_config_dialog),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit",                   lambda i, it: on_quit(i)),
    )

    _tray_icon = pystray.Icon(
        name="llm_proxy",
        icon=ICON_MAP.get(flag, ICON_MAP["offline"]),
        title="LLM Proxy",
        menu=menu,
    )

    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    _tray_icon.run()


if __name__ == "__main__":
    main()
