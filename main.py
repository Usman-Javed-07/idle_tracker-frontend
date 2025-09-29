import io
import os
import random
import time
import tempfile
import datetime as dt
import threading  # <-- for background recording

import tkinter as tk
from tkinter import ttk, messagebox
from plyer import notification
from pynput import mouse as pyn_mouse, keyboard as pyn_keyboard

# screen & image libs
import numpy as np
from PIL import Image
import mss
import imageio

# timezone (Windows safe)
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
    list_admin_emails,  # <-- NEW: to email all admins
)
from backend.auth import login, hash_password
from backend.activity import set_user_status
from backend.config import ADMIN_BOOTSTRAP
from backend.notify import send_email

# OPTIONAL: if you add ALERT_RECIPIENTS in config.py, uncomment:
try:
    from backend.config import ALERT_RECIPIENTS
except Exception:
    ALERT_RECIPIENTS = []

INACTIVITY_SECONDS = 10
CHECK_INTERVAL_MS = 300
MIN_SCREENSHOTS_PER_SHIFT = 15


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
        if self._running: return
        self._running = True
        self._mouse_listener = pyn_mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
        self._key_listener = pyn_keyboard.Listener(on_press=self._on_press)
        self._mouse_listener.start(); self._key_listener.start()

    def stop(self):
        self._running = False
        try:
            if self._mouse_listener: self._mouse_listener.stop()
        except Exception: pass
        try:
            if self._key_listener: self._key_listener.stop()
        except Exception: pass


class UserApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("User Client - Idle Tracker")
        self.geometry("540x360")
        self.resizable(False, False)

        init_tables()
        self._ensure_single_admin()

        self.current_user = None
        self.last_activity = time.monotonic()
        self.inactive_sent = False
        self.active_since = None
        self.shift_started_today = False
        self.global_monitor = None

        # random screenshot scheduling
        self.screenshots_taken_today = 0
        self.next_screenshot_after_ms = None

        # recording state (prevents overlapping recordings)
        self._recording_in_progress = False

        self.frames = {}
        for F in (AuthFrame, TrackerFrame):
            frame = F(self); self.frames[F.__name__] = frame
        self.show_frame("AuthFrame")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
                    shift_start_time=ADMIN_BOOTSTRAP.get("shift_start_time", "09:00:00"),
                    shift_duration_seconds=32400,
                )
            except Exception:
                pass

    def show_frame(self, name):
        for f in self.frames.values(): f.pack_forget()
        self.frames[name].pack(fill="both", expand=True)

    def _on_close(self):
        if self.global_monitor: self.global_monitor.stop()
        self.destroy()

    def on_logged_in(self, user_dict):
        self.current_user = get_user_by_id(user_dict["id"])
        self.last_activity = time.monotonic()
        self.inactive_sent = False
        self.active_since = time.monotonic()
        self.shift_started_today = False
        self.screenshots_taken_today = 0
        self._plan_next_random_screenshot()

        self.global_monitor = GlobalActivityMonitor(self.on_activity, min_interval=0.15)
        self.global_monitor.start()

        self.show_frame("TrackerFrame")
        self.after(CHECK_INTERVAL_MS, self._loop_check)

    # ----------- activity & shift -----------
    def on_activity(self):
        self.last_activity = time.monotonic()
        now_local = dt.datetime.now(SHIFT_TZ)
        shift_time = dt.datetime.combine(
            now_local.date(),
            dt.datetime.strptime(str(self.current_user["shift_start_time"]), "%H:%M:%S").time()
        )
        shift_time = SHIFT_TZ.localize(shift_time) if hasattr(SHIFT_TZ, "localize") else shift_time.replace(tzinfo=SHIFT_TZ)

        if not self.shift_started_today and now_local >= shift_time:
            set_user_status(self.current_user["id"], "shift_start")
            self.shift_started_today = True
            self.frames["TrackerFrame"].set_status("Shift Start")

        if self.inactive_sent:
            set_user_status(self.current_user["id"], "active")
            self.inactive_sent = False
            self.active_since = time.monotonic()
            self.frames["TrackerFrame"].set_status("Active")
        elif self.shift_started_today and self.active_since is None:
            set_user_status(self.current_user["id"], "active")
            self.active_since = time.monotonic()
            self.frames["TrackerFrame"].set_status("Active")

    def _loop_check(self):
        now = time.monotonic()
        idle = now - self.last_activity
        self.frames["TrackerFrame"].update_idle(idle)

        now_local = dt.datetime.now(SHIFT_TZ)
        shift_time = dt.datetime.combine(
            now_local.date(),
            dt.datetime.strptime(str(self.current_user["shift_start_time"]), "%H:%M:%S").time()
        )
        shift_time = SHIFT_TZ.localize(shift_time) if hasattr(SHIFT_TZ, "localize") else shift_time.replace(tzinfo=SHIFT_TZ)

        if not self.shift_started_today and now_local >= shift_time:
            set_user_status(self.current_user["id"], "shift_start")
            self.shift_started_today = True
            self.frames["TrackerFrame"].set_status("Shift Start")

        # inactivity → email + DB + 5s recording (non-blocking)
        if idle >= INACTIVITY_SECONDS and not self.inactive_sent:
            active_duration = int(now - self.active_since) if self.active_since is not None else None
            event_id = record_event(self.current_user["id"], "inactive", active_duration_seconds=active_duration)
            update_user_status(self.current_user["id"], "inactive")
            self.inactive_sent = True
            self.frames["TrackerFrame"].set_status("Inactive")

            try:
                notification.notify(
                    title="You are inactive",
                    message=f"No activity for {INACTIVITY_SECONDS} seconds.",
                    timeout=3,
                )
            except Exception:
                pass

            u = self.current_user
            duration_txt = seconds_to_hhmmss(active_duration) if active_duration else "unknown"
            when_txt = now_local.strftime("%Y-%m-%d %H:%M:%S %Z")
            body = (
                f"User {u['name']} ({u['username']}, {u['email']}, {u['department']}) "
                f"became INACTIVE at {when_txt}.\n"
                f"Active streak before inactivity: {duration_txt}.\n"
            )

            # === UPDATED EMAIL FAN-OUT ===
            try:
                recipients = set()

                # the user
                if u.get("email"):
                    recipients.add(u["email"])

                # all admins from DB
                try:
                    for em in list_admin_emails():
                        if em:
                            recipients.add(em)
                except Exception:
                    pass

                # bootstrap admin (fallback)
                if ADMIN_BOOTSTRAP.get("email"):
                    recipients.add(ADMIN_BOOTSTRAP["email"])

                # optional extra recipients from env (comma-separated)
                for extra in ALERT_RECIPIENTS:
                    recipients.add(extra)

                if recipients:
                    send_email(
                        list(recipients),
                        subject=f"[IdleTracker] {u['username']} inactive",
                        body=body
                    )
            except Exception:
                pass

            # Start recording in a background thread so UI doesn't freeze
            if not self._recording_in_progress:
                self._recording_in_progress = True
                threading.Thread(
                    target=self._record_and_store,
                    args=(event_id, 5, 8),  # duration=5s, fps=8
                    daemon=True
                ).start()

        # end shift after 9h
        if self.shift_started_today:
            end_dt = shift_time + dt.timedelta(seconds=int(self.current_user["shift_duration_seconds"]))
            if now_local >= end_dt:
                update_user_status(self.current_user["id"], "off")

        # random screenshots over shift
        self._maybe_take_random_screenshot()

        self.after(CHECK_INTERVAL_MS, self._loop_check)

    # ----------- background recording wrapper -----------
    def _record_and_store(self, event_id, duration, fps):
        """Record screen and store bytes without blocking Tk mainloop."""
        try:
            video_bytes = self._record_screen_bytes(duration=duration, fps=fps)
            insert_recording_url(self.current_user["id"], video_bytes,
                                 duration_seconds=duration, event_id=event_id)
        except Exception as e:
            print("Recording failed:", e)
        finally:
            self._recording_in_progress = False

    # ----------- random screenshots -----------
    def _plan_next_random_screenshot(self):
        mins = random.randint(20, 35)
        self.next_screenshot_after_ms = int(mins * 60 * 1000)

    def _maybe_take_random_screenshot(self):
        if not self.shift_started_today:
            return
        if self.screenshots_taken_today >= MIN_SCREENSHOTS_PER_SHIFT:
            return
        if self.next_screenshot_after_ms is None:
            return
        self.next_screenshot_after_ms -= CHECK_INTERVAL_MS
        if self.next_screenshot_after_ms <= 0:
            try:
                img_bytes = self._capture_png_bytes()
                # For random activity screenshots, keep event_id=None intentionally
                insert_screenshot_url(self.current_user["id"], img_bytes, event_id=None, mime="image/png")
                self.screenshots_taken_today += 1
            except Exception:
                pass
            self._plan_next_random_screenshot()

    def _capture_png_bytes(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.rgb)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    # ----------- precise recording (imageio-ffmpeg + monotonic pacing) -----------
    def _record_screen_bytes(self, duration=5, fps=8):
        """
        Capture primary screen for `duration` seconds and return an MP4 (H.264/yuv420p).
        Uses precise monotonic timing so we always write duration*fps frames.
        """
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            out_path = tmp.name

        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

            writer = imageio.get_writer(
                out_path, fps=fps, codec="libx264", format="FFMPEG",
                ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
            )

            frames_needed = int(round(duration * fps))
            next_t = time.monotonic()
            try:
                for _ in range(frames_needed):
                    frame_bgra = np.array(sct.grab(monitor))      # BGRA
                    frame_rgb  = frame_bgra[:, :, :3][:, :, ::-1] # to RGB
                    writer.append_data(frame_rgb)

                    # pace to exact FPS using monotonic clock
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
        ttk.Label(self, text="Login", font=("Segoe UI", 16, "bold")).pack(pady=(0,12))
        self.login_id = tk.StringVar(); self.login_pwd = tk.StringVar()
        ttk.Label(self, text="Username or Email").pack(anchor="w")
        ttk.Entry(self, textvariable=self.login_id).pack(fill="x")
        ttk.Label(self, text="Password").pack(anchor="w", pady=(8,0))
        ttk.Entry(self, show="*", textvariable=self.login_pwd).pack(fill="x")
        ttk.Button(self, text="Login", command=self.do_login).pack(pady=10)

    def do_login(self):
        try:
            user = login(self.login_id.get().strip(), self.login_pwd.get().strip())
            if not user or user["role"] != "user":
                messagebox.showerror("Login failed", "Invalid credentials or not a user.")
                return
            self.master.on_logged_in(user)
        except Exception as e:
            messagebox.showerror("Error", str(e))


class TrackerFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=20)
        self.status_var = tk.StringVar(value="off")
        self.idle_var = tk.StringVar(value="Idle for 0.0s")
        ttk.Label(self, textvariable=self.status_var, font=("Segoe UI", 20, "bold")).pack(pady=(10,5))
        ttk.Label(self, textvariable=self.idle_var).pack()
        ttk.Label(self, text="(Global detection: keyboard/mouse anywhere; TZ: Asia/Karachi)").pack(pady=(10,0))

    def set_status(self, status_text: str):
        self.status_var.set(status_text)

    def update_idle(self, seconds: float):
        self.idle_var.set(f"Idle for {seconds:.1f}s")


def seconds_to_hhmmss(sec):
    if sec is None: return "00:00:00"
    h = sec // 3600; m = (sec % 3600) // 60; s = sec % 60
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


if __name__ == "__main__":
    import logging, sys, traceback
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        filename=os.path.join("logs", "user_app.log"),
        level=logging.DEBUG, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    try:
        print("Starting UserApp…")
        app = UserApp()
        app.after(50, lambda: (app.lift(), app.attributes("-topmost", True)))
        app.after(1000, lambda: app.attributes("-topmost", False))
        app.mainloop()
    except Exception:
        tb = traceback.format_exc()
        logging.error(tb)
        print("User app crashed. See logs\\user_app.log")
        print(tb)
        sys.exit(1)
