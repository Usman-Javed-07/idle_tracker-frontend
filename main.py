import io
import os
import random
import time
import tempfile
import datetime as dt
import threading
import sys
import atexit
import signal

import tkinter as tk
from tkinter import ttk, messagebox
from plyer import notification
from pynput import mouse as pyn_mouse, keyboard as pyn_keyboard

import numpy as np
from PIL import Image
import mss
import imageio

# timezone
try:
    from zoneinfo import ZoneInfo
    SHIFT_TZ = ZoneInfo("Asia/Karachi")
except Exception:
    import pytz
    SHIFT_TZ = pytz.timezone("Asia/Karachi")

from backend.models import (
    init_tables, get_user_by_username_or_email, insert_user, get_user_by_id,
    update_user_status, record_event,
    insert_screenshot_url, insert_recording_url,
    list_admin_emails, insert_overtime,
)
from backend.auth import login, hash_password
from backend.config import ADMIN_BOOTSTRAP
from backend.notify import send_email

try:
    from backend.config import ALERT_RECIPIENTS
except Exception:
    ALERT_RECIPIENTS = []

INACTIVITY_SECONDS = 10
CHECK_INTERVAL_MS = 250          # fast loop so 1s ticks are precise
MIN_SCREENSHOTS_PER_SHIFT = 15
LOGIN_PROMPT_EVERY_S = 15        # pre-login reminder cadence

APP_NAME = "Mars Capital"
APP_ICON = os.path.join(os.getcwd(), "assets", "mars.ico")
if not os.path.isfile(APP_ICON):
    APP_ICON = None

# Windows toast branding: set AppUserModelID so it won't say "Python"
if sys.platform.startswith("win"):
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Mars Capital")
    except Exception:
        pass


def _notify(title, message, timeout=5):
    kw = {"title": title, "message": message,
          "timeout": timeout, "app_name": APP_NAME}
    if APP_ICON:
        kw["app_icon"] = APP_ICON
    try:
        notification.notify(**kw)
    except Exception:
        pass


class GlobalActivityMonitor:
    def __init__(self, on_activity, min_interval=0.15):
        self.on_activity = on_activity
        self.min_interval = min_interval
        self._last_fire = 0.0
        self._mouse_listener = None
        self._key_listener = None
        self._running = False

    def _maybe_fire(self):
        now = time.monotonic()
        if now - self._last_fire >= self.min_interval:
            self._last_fire = now
            self.on_activity()

    def _on_move(self, x, y): self._maybe_fire()
    def _on_click(self, x, y, button, pressed): self._maybe_fire()
    def _on_scroll(self, x, y, dx, dy): self._maybe_fire()
    def _on_press(self, key): self._maybe_fire()

    def start(self):
        if self._running:
            return
        self._running = True
        self._mouse_listener = pyn_mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self._key_listener = pyn_keyboard.Listener(on_press=self._on_press)
        self._mouse_listener.start()
        self._key_listener.start()

    def stop(self):
        self._running = False
        try:
            if self._mouse_listener:
                self._mouse_listener.stop()
        except Exception:
            pass
        try:
            if self._key_listener:
                self._key_listener.stop()
        except Exception:
            pass


class UserApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("User Client - Idle Tracker")
        self.geometry("580x480")
        self.resizable(False, False)

        # Show on taskbar and allow minimize; disable close.
        self.protocol("WM_DELETE_WINDOW", self._block_close)
        self.bind_all("<Alt-F4>", lambda e: "break")

        init_tables()
        self._ensure_single_admin()

        # session/user state
        self.current_user = None
        self.last_activity = time.monotonic()
        self.inactive_sent = False
        self.active_since = None
        self.inactive_started_mono = None

        # today counters (seconds)
        self.active_seconds_today = 0
        self.inactive_seconds_today = 0
        self.overtime_seconds_today = 0

        # time bookkeeping for 1-second ticks
        self._last_tick = time.monotonic()

        self.global_monitor = GlobalActivityMonitor(
            self._on_global_activity, min_interval=0.15)
        self.global_monitor.start()

        # pre-login reminder
        self._last_login_prompt = 0.0

        # screenshots scheduling
        self.screenshots_taken_today = 0
        self.next_screenshot_after_ms = None

        # overtime window
        self._today_shift_end = None
        self._overtime_started_mono = None

        # recording state
        self._recording_in_progress = False

        # UI
        self.frames = {}
        for F in (AuthFrame, TrackerFrame):
            frame = F(self)
            self.frames[F.__name__] = frame
        self.show_frame("AuthFrame")

        # center the window on screen
        self.after(10, self._center_on_screen)

        # graceful shutdown hooks
        atexit.register(self._graceful_shutdown)
        for sig in ("SIGINT", "SIGTERM"):
            if hasattr(signal, sig):
                signal.signal(getattr(signal, sig), self._signal_exit)

        # main loop
        self.after(CHECK_INTERVAL_MS, self._loop_check)

    # ---- window helpers ----
    def _center_on_screen(self):
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw // 2) - (w // 2)
        y = (sh // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _block_close(self):
        # ignore user close; minimize is allowed via taskbar or button
        pass

    def _signal_exit(self, *_args):
        self._graceful_shutdown()
        os._exit(0)

    def _graceful_shutdown(self):
        try:
            # flush any running overtime slice
            self._flush_overtime_segment()
        except Exception:
            pass
        try:
            # set status to off if user was logged in
            if self.current_user:
                update_user_status(self.current_user["id"], "off")
        except Exception:
            pass
        try:
            if self.global_monitor:
                self.global_monitor.stop()
        except Exception:
            pass

    # ---- data helpers ----
    def _ensure_single_admin(self):
        row = get_user_by_username_or_email(ADMIN_BOOTSTRAP["username"])
        if not row:
            try:
                insert_user(
                    ADMIN_BOOTSTRAP["username"],
                    ADMIN_BOOTSTRAP["name"],
                    ADMIN_BOOTSTRAP["department"],
                    ADMIN_BOOTSTRAP["email"],
                    hash_password(ADMIN_BOOTSTRAP["password"]),
                    role="admin",
                    shift_start_time=ADMIN_BOOTSTRAP.get(
                        "shift_start_time", "09:00:00"),
                    shift_duration_seconds=32400,
                )
            except Exception:
                pass

    def _compute_today_bounds(self):
        if not self.current_user:
            self._today_shift_end = None
            return
        now_local = dt.datetime.now(SHIFT_TZ)
        s = dt.datetime.strptime(
            str(self.current_user["shift_start_time"]), "%H:%M:%S").time()
        e = dt.datetime.strptime(str(self.current_user.get(
            "shift_end_time", "18:00:00")), "%H:%M:%S").time()
        start = dt.datetime.combine(now_local.date(), s)
        end = dt.datetime.combine(now_local.date(), e)
        if end <= start:
            end += dt.timedelta(days=1)
        if hasattr(SHIFT_TZ, "localize"):
            start = SHIFT_TZ.localize(start)
            end = SHIFT_TZ.localize(end)
        else:
            start = start.replace(tzinfo=SHIFT_TZ)
            end = end.replace(tzinfo=SHIFT_TZ)
        self._today_shift_end = end

    def _today_total_seconds(self):
        return self.active_seconds_today + self.inactive_seconds_today

    # ---- UI switching ----
    def show_frame(self, name):
        for f in self.frames.values():
            f.pack_forget()
        self.frames[name].pack(fill="both", expand=True)

    # ---- activity handler ----
    def _on_global_activity(self):
        now = time.monotonic()
        self.last_activity = now
        if self.current_user is not None:
            if self.inactive_sent:
                update_user_status(self.current_user["id"], "active")
                self.inactive_sent = False
                self.active_since = now
                self.inactive_started_mono = None
                self.frames["TrackerFrame"].set_status("Active")
            elif self.active_since is None:
                update_user_status(self.current_user["id"], "active")
                self.active_since = now
                self.frames["TrackerFrame"].set_status("Active")

    # ---- login flow ----
    def on_logged_in(self, user_dict):
        self.current_user = get_user_by_id(user_dict["id"])
        self._compute_today_bounds()

        # reset counters and tick clock
        self.active_seconds_today = 0
        self.inactive_seconds_today = 0
        self.overtime_seconds_today = 0
        self._last_tick = time.monotonic()

        # Active immediately
        update_user_status(self.current_user["id"], "active")
        self.frames["TrackerFrame"].set_status("Active")

        # show details
        self.frames["TrackerFrame"].set_user_info(
            name=self.current_user.get("name") or "",
            username=self.current_user.get("username") or "",
            department=self.current_user.get("department") or "",
        )
        self.frames["TrackerFrame"].set_counters(
            self.active_seconds_today,
            self.inactive_seconds_today,
            self.overtime_seconds_today,
            self._today_total_seconds()
        )

        self.last_activity = time.monotonic()
        self.inactive_sent = False
        self.active_since = self.last_activity
        self.inactive_started_mono = None
        self.screenshots_taken_today = 0
        self._plan_next_random_screenshot()
        self.show_frame("TrackerFrame")

    # ---- main loop ----
    def _loop_check(self):
        now = time.monotonic()
        idle = now - self.last_activity
        self.frames["TrackerFrame"].update_idle(idle)

        # login reminder every 15s (pre-login)
        if self.current_user is None:
            if now - self._last_login_prompt >= LOGIN_PROMPT_EVERY_S:
                self._last_login_prompt = now
                _notify(
                    "Please log in", "Open the user panel and sign in to start tracking.", timeout=10)

        # overtime window
        after_end = False
        if self.current_user:
            if self._today_shift_end is None:
                self._compute_today_bounds()
            now_local = dt.datetime.now(SHIFT_TZ)
            after_end = bool(self._today_shift_end and (
                now_local >= self._today_shift_end))

        # === 1-second ticker for counters ===
        if now - self._last_tick >= 1.0:
            delta = int(now - self._last_tick)
            self._last_tick += delta
            if self.current_user:
                if idle < INACTIVITY_SECONDS:
                    self.active_seconds_today += delta
                    if after_end:
                        self.overtime_seconds_today += delta
                else:
                    self.inactive_seconds_today += delta

                # update UI every second
                self.frames["TrackerFrame"].set_counters(
                    self.active_seconds_today,
                    self.inactive_seconds_today,
                    self.overtime_seconds_today,
                    self._today_total_seconds()
                )

        # inactivity boundary (event + notification; layout won’t jump)
        if self.current_user:
            if idle >= INACTIVITY_SECONDS and not self.inactive_sent:
                active_duration = int(
                    now - self.active_since) if self.active_since is not None else None
                event_id = record_event(
                    self.current_user["id"], "inactive",
                    active_duration_seconds=active_duration
                )
                update_user_status(self.current_user["id"], "inactive")
                self.inactive_sent = True
                self.inactive_started_mono = now
                self.frames["TrackerFrame"].set_status("Inactive")
                _notify(
                    "You are inactive", f"No activity for {INACTIVITY_SECONDS} seconds.", timeout=3)

                # email fan-out in background
                self._fanout_inactive_email_async(active_duration)

                # short recording (non-blocking)
                if not self._recording_in_progress:
                    self._recording_in_progress = True
                    threading.Thread(
                        target=self._record_and_store, args=(event_id, 5, 8), daemon=True
                    ).start()

        # random screenshots
        self._maybe_take_random_screenshot()

        self.after(CHECK_INTERVAL_MS, self._loop_check)

    # ---- email fan-out (async) ----
    def _fanout_inactive_email_async(self, active_duration):
        def _send():
            try:
                u = self.current_user
                when_txt = dt.datetime.now(
                    SHIFT_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
                duration_txt = seconds_to_hhmmss(
                    active_duration) if active_duration else "unknown"
                body = (f"User {u['name']} (@{u['username']}, {u['email']}, {u['department']}) "
                        f"became INACTIVE at {when_txt}. Active streak: {duration_txt}.")
                recipients = set()
                if u.get("email"):
                    recipients.add(u["email"])
                recipients.update(e for e in list_admin_emails() if e)
                if ADMIN_BOOTSTRAP.get("email"):
                    recipients.add(ADMIN_BOOTSTRAP["email"])
                recipients.update(ALERT_RECIPIENTS)
                if recipients:
                    send_email(list(recipients),
                               subject=f"[IdleTracker] {u['username']} inactive",
                               body=body)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()

    # ---- overtime helpers ----
    def _flush_overtime_segment(self):
        if self._overtime_started_mono is None or not self.current_user:
            return
        now = time.monotonic()
        seconds = int(max(0, now - self._overtime_started_mono))
        self._overtime_started_mono = None
        if seconds <= 0:
            return
        try:
            ot_date = dt.datetime.now(SHIFT_TZ).date()
            self.overtime_seconds_today += seconds
            insert_overtime(self.current_user["id"], ot_date, seconds)
            self.frames["TrackerFrame"].set_counters(
                self.active_seconds_today,
                self.inactive_seconds_today,
                self.overtime_seconds_today,
                self._today_total_seconds()
            )
        except Exception:
            pass

    # ---- background recording ----
    def _record_and_store(self, event_id, duration, fps):
        try:
            video_bytes = self._record_screen_bytes(duration=duration, fps=fps)
            insert_recording_url(self.current_user["id"], video_bytes,
                                 duration_seconds=duration, event_id=event_id)
        except Exception:
            pass
        finally:
            self._recording_in_progress = False

    # ---- screenshots ----
    def _plan_next_random_screenshot(self):
        mins = random.randint(20, 35)
        self.next_screenshot_after_ms = int(mins * 60 * 1000)

    def _maybe_take_random_screenshot(self):
        if not self.current_user:
            return
        if self.screenshots_taken_today >= MIN_SCREENSHOTS_PER_SHIFT:
            return
        if self.next_screenshot_after_ms is None:
            return
        self.next_screenshot_after_ms -= CHECK_INTERVAL_MS
        if self.next_screenshot_after_ms <= 0:
            try:
                img_bytes = self._capture_png_bytes()
                insert_screenshot_url(
                    self.current_user["id"], img_bytes, event_id=None, mime="image/png")
                self.screenshots_taken_today += 1
            except Exception:
                pass
            self._plan_next_random_screenshot()

    def _capture_png_bytes(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(
                sct.monitors) > 1 else sct.monitors[0]
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.rgb)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    # ---- recording ----
    def _record_screen_bytes(self, duration=5, fps=8):
        # no duplicate -pix_fmt; keep faststart
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            out_path = tmp.name
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(
                sct.monitors) > 1 else sct.monitors[0]
            writer = imageio.get_writer(
                out_path, fps=fps, codec="libx264", format="FFMPEG",
                ffmpeg_params=["-movflags", "+faststart"]
            )
            frames_needed = int(round(duration * fps))
            next_t = time.monotonic()
            try:
                for _ in range(frames_needed):
                    frame_bgra = np.array(sct.grab(monitor))
                    frame_rgb = frame_bgra[:, :, :3][:, :, ::-1]
                    writer.append_data(frame_rgb)
                    next_t += 1.0 / fps
                    sleep_for = next_t - time.monotonic()
                    if sleep_for > 0:
                        time.sleep(sleep_for)
            finally:
                writer.close()
        with open(out_path, "rb") as f:
            data = f.read()
        try:
            os.remove(out_path)
        except Exception:
            pass
        return data


class AuthFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=16)
        ttk.Label(self, text="Login", font=(
            "Segoe UI", 16, "bold")).pack(pady=(0, 12))
        self.login_id = tk.StringVar()
        self.login_pwd = tk.StringVar()
        ttk.Label(self, text="Username or Email").pack(anchor="w")
        ttk.Entry(self, textvariable=self.login_id).pack(fill="x")
        ttk.Label(self, text="Password").pack(anchor="w", pady=(8, 0))
        ttk.Entry(self, show="*", textvariable=self.login_pwd).pack(fill="x")
        ttk.Button(self, text="Login", command=self.do_login).pack(pady=10)

    def do_login(self):
        try:
            user = login(self.login_id.get().strip(),
                         self.login_pwd.get().strip())
            if not user or user["role"] != "user":
                messagebox.showerror(
                    "Login failed", "Invalid credentials or not a user.")
                return
            self.master.on_logged_in(user)
        except Exception as e:
            messagebox.showerror("Error", str(e))


class TrackerFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=20)
        self.status_var = tk.StringVar(value="off")
        self.idle_var = tk.StringVar(value="Idle for 0.0s")

        # User details
        self.name_var = tk.StringVar(value="")
        self.username_var = tk.StringVar(value="")
        self.department_var = tk.StringVar(value="")

        # Counters
        self.active_today_var = tk.StringVar(value="00:00:00")
        self.inactive_today_var = tk.StringVar(value="00:00:00")
        self.overtime_today_var = tk.StringVar(value="00:00:00")
        self.total_today_var = tk.StringVar(value="00:00:00")

        # Header row (fixed layout so design doesn't jump)
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="User Panel", font=(
            "Segoe UI", 20, "bold")).pack(side="left", pady=(10, 8))
        ttk.Button(top, text="Minimize", command=self.master.iconify).pack(
            side="right", padx=6, pady=6)

        # Details
        details = ttk.Frame(self)
        details.pack(fill="x", pady=(4, 10))
        ttk.Label(details, textvariable=self.name_var, font=(
            "Segoe UI", 11), width=50, anchor="w").pack(anchor="w")
        ttk.Label(details, textvariable=self.username_var, font=(
            "Segoe UI", 11), width=50, anchor="w").pack(anchor="w")
        ttk.Label(details, textvariable=self.department_var, font=(
            "Segoe UI", 11), width=50, anchor="w").pack(anchor="w")

        ttk.Label(self, textvariable=self.status_var, font=(
            "Segoe UI", 16, "bold")).pack(pady=(6, 4))
        ttk.Label(self, textvariable=self.idle_var).pack()

        # Counters grid with fixed widths to avoid reflow
        grid = ttk.Frame(self)
        grid.pack(pady=10, fill="x")

        def row(label, var):
            r = ttk.Frame(grid)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, width=18, anchor="w").pack(side="left")
            ttk.Label(r, textvariable=var, font=("Consolas", 11,
                      "bold"), width=12, anchor="w").pack(side="left")
        row("Today Active:", self.active_today_var)
        row("Today Inactive:", self.inactive_today_var)
        row("Today Overtime:", self.overtime_today_var)
        row("Today Total:", self.total_today_var)

        ttk.Label(
            self, text="(Global detection: keyboard/mouse anywhere; TZ: Asia/Karachi)").pack(pady=(8, 0))

    def set_user_info(self, name: str, username: str, department: str):
        self.name_var.set(f"Name: {name}")
        self.username_var.set(f"Username: @{username}")
        self.department_var.set(f"Department: {department}")

    def set_status(self, status_text: str):
        self.status_var.set(status_text)

    def update_idle(self, seconds: float):
        self.idle_var.set(f"Idle for {seconds:.1f}s")

    def set_counters(self, active_s: int, inactive_s: int, overtime_s: int, total_s: int):
        self.active_today_var.set(seconds_to_hhmmss(active_s))
        self.inactive_today_var.set(seconds_to_hhmmss(inactive_s))
        self.overtime_today_var.set(seconds_to_hhmmss(overtime_s))
        self.total_today_var.set(seconds_to_hhmmss(total_s))


def seconds_to_hhmmss(sec):
    if sec is None:
        return "00:00:00"
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


if __name__ == "__main__":
    import logging
    import traceback
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        filename=os.path.join("logs", "user_app.log"),
        level=logging.DEBUG, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    try:
        print("Starting UserApp…")
        app = UserApp()
        # briefly keep on top so window is visible initially
        app.after(50, lambda: (app.lift(), app.attributes("-topmost", True)))
        app.after(1000, lambda: app.attributes("-topmost", False))
        app.mainloop()
    except Exception:
        tb = traceback.format_exc()
        logging.error(tb)
        print("User app crashed. See logs\\user_app.log")
        print(tb)
        sys.exit(1)
