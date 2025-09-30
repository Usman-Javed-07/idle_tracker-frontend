import io
import os
import random
import time
import tempfile
import datetime as dt
import threading

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
    list_admin_emails,
    # you implement this in backend.models (simple upsert/insert into user_overtimes):
    insert_overtime,
)
from backend.auth import login, hash_password
from backend.activity import set_user_status
from backend.config import ADMIN_BOOTSTRAP
from backend.notify import send_email

try:
    from backend.config import ALERT_RECIPIENTS
except Exception:
    ALERT_RECIPIENTS = []

INACTIVITY_SECONDS = 10
CHECK_INTERVAL_MS = 300
MIN_SCREENSHOTS_PER_SHIFT = 15
LOGIN_PROMPT_EVERY_S = 3  # pre-login reminder


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
        self.global_monitor = GlobalActivityMonitor(self._on_global_activity, min_interval=0.15)
        self.global_monitor.start()

        # pre-login reminder state
        self._last_login_prompt = 0.0

        # random screenshot scheduling
        self.screenshots_taken_today = 0
        self.next_screenshot_after_ms = None

        # overtime state
        self._today_shift_end = None
        self._overtime_started_mono = None  # when user is actively working after shift end

        # recording state
        self._recording_in_progress = False

        self.frames = {}
        for F in (AuthFrame, TrackerFrame):
            frame = F(self); self.frames[F.__name__] = frame
        self.show_frame("AuthFrame")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(CHECK_INTERVAL_MS, self._loop_check)

    # ----- helpers -----
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

    def _compute_today_bounds(self):
        if not self.current_user:
            self._today_shift_end = None
            return
        now_local = dt.datetime.now(SHIFT_TZ)
        s = dt.datetime.strptime(str(self.current_user["shift_start_time"]), "%H:%M:%S").time()
        e = dt.datetime.strptime(str(self.current_user.get("shift_end_time", "18:00:00")), "%H:%M:%S").time()
        start = dt.datetime.combine(now_local.date(), s)
        end = dt.datetime.combine(now_local.date(), e)
        if end <= start: end += dt.timedelta(days=1)
        if hasattr(SHIFT_TZ, "localize"):
            start = SHIFT_TZ.localize(start); end = SHIFT_TZ.localize(end)
        else:
            start = start.replace(tzinfo=SHIFT_TZ); end = end.replace(tzinfo=SHIFT_TZ)
        self._today_shift_end = end

    # ----- UI frame switching -----
    def show_frame(self, name):
        for f in self.frames.values(): f.pack_forget()
        self.frames[name].pack(fill="both", expand=True)

    # ----- global activity handler -----
    def _on_global_activity(self):
        self.last_activity = time.monotonic()
        # If not logged in yet, remind to login every 3s
        if self.current_user is None:
            now = time.monotonic()
            if now - self._last_login_prompt >= LOGIN_PROMPT_EVERY_S:
                self._last_login_prompt = now
                try:
                    notification.notify(
                        title="Please log in",
                        message="Activity detected. Open the app and log in to start tracking.",
                        timeout=2,
                    )
                except Exception:
                    pass
            return

        # When logged in, any activity means Active
        if self.inactive_sent:
            update_user_status(self.current_user["id"], "active")
            self.inactive_sent = False
            self.active_since = time.monotonic()
            self.frames["TrackerFrame"].set_status("Active")
        elif self.active_since is None:
            update_user_status(self.current_user["id"], "active")
            self.active_since = time.monotonic()
            self.frames["TrackerFrame"].set_status("Active")

    # ----- login flow -----
    def on_logged_in(self, user_dict):
        self.current_user = get_user_by_id(user_dict["id"])
        self._compute_today_bounds()

        # become Active immediately on login
        update_user_status(self.current_user["id"], "active")
        self.frames["TrackerFrame"].set_status("Active")

        self.last_activity = time.monotonic()
        self.inactive_sent = False
        self.active_since = time.monotonic()
        self.screenshots_taken_today = 0
        self._plan_next_random_screenshot()
        self.show_frame("TrackerFrame")

    # ----- main loop -----
    def _loop_check(self):
        now = time.monotonic()
        idle = now - self.last_activity
        self.frames["TrackerFrame"].update_idle(idle)

        # overtime tracking (after end, only while user is actually active)
        if self.current_user:
            if self._today_shift_end is None:
                self._compute_today_bounds()
            now_local = dt.datetime.now(SHIFT_TZ)
            after_end = self._today_shift_end and (now_local >= self._today_shift_end)

            if after_end:
                if idle < INACTIVITY_SECONDS:
                    # actively working
                    if self._overtime_started_mono is None:
                        self._overtime_started_mono = time.monotonic()
                else:
                    # just became idle -> flush any accrued overtime
                    self._flush_overtime_segment()

            # inactivity handling
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
                when_txt = dt.datetime.now(SHIFT_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
                duration_txt = seconds_to_hhmmss(active_duration) if active_duration else "unknown"
                body = (f"User {u['name']} (@{u['username']}, {u['email']}, {u['department']}) "
                        f"became INACTIVE at {when_txt}. Active streak: {duration_txt}.")

                # email fan-out
                try:
                    recipients = set()
                    if u.get("email"): recipients.add(u["email"])
                    recipients.update(e for e in list_admin_emails() if e)
                    if ADMIN_BOOTSTRAP.get("email"): recipients.add(ADMIN_BOOTSTRAP["email"])
                    recipients.update(ALERT_RECIPIENTS)
                    if recipients:
                        send_email(list(recipients),
                                   subject=f"[IdleTracker] {u['username']} inactive",
                                   body=body)
                except Exception:
                    pass

                # non-blocking 5s recording
                if not self._recording_in_progress:
                    self._recording_in_progress = True
                    threading.Thread(
                        target=self._record_and_store, args=(event_id, 5, 8), daemon=True
                    ).start()

        # random screenshots
        self._maybe_take_random_screenshot()

        self.after(CHECK_INTERVAL_MS, self._loop_check)

    # ----- overtime helpers -----
    def _flush_overtime_segment(self):
        """Called when user becomes idle or app exits, to store the segment."""
        if self._overtime_started_mono is None or not self.current_user:
            return
        seconds = int(max(0, time.monotonic() - self._overtime_started_mono))
        self._overtime_started_mono = None
        if seconds <= 0:
            return
        try:
            # store as one row per day (aggregate on the backend if same day)
            ot_date = dt.datetime.now(SHIFT_TZ).date()
            insert_overtime(self.current_user["id"], ot_date, seconds)
        except Exception as e:
            print("Overtime insert failed:", e)

    # ----- background recording -----
    def _record_and_store(self, event_id, duration, fps):
        try:
            video_bytes = self._record_screen_bytes(duration=duration, fps=fps)
            insert_recording_url(self.current_user["id"], video_bytes,
                                 duration_seconds=duration, event_id=event_id)
        except Exception as e:
            print("Recording failed:", e)
        finally:
            self._recording_in_progress = False

    # ----- screenshots -----
    def _plan_next_random_screenshot(self):
        mins = random.randint(20, 35)
        self.next_screenshot_after_ms = int(mins * 60 * 1000)

    def _maybe_take_random_screenshot(self):
        if not self.current_user: return
        if self.screenshots_taken_today >= MIN_SCREENSHOTS_PER_SHIFT: return
        if self.next_screenshot_after_ms is None: return
        self.next_screenshot_after_ms -= CHECK_INTERVAL_MS
        if self.next_screenshot_after_ms <= 0:
            try:
                img_bytes = self._capture_png_bytes()
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
            buf = io.BytesIO(); img.save(buf, format="PNG")
            return buf.getvalue()

    # ----- recording -----
    def _record_screen_bytes(self, duration=5, fps=8):
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
                    frame_bgra = np.array(sct.grab(monitor))
                    frame_rgb = frame_bgra[:, :, :3][:, :, ::-1]
                    writer.append_data(frame_rgb)
                    next_t += 1.0 / fps
                    sleep_for = next_t - time.monotonic()
                    if sleep_for > 0: time.sleep(sleep_for)
            finally:
                writer.close()
        with open(out_path, "rb") as f:
            data = f.read()
        try: os.remove(out_path)
        except Exception: pass
        return data

    # ----- shutdown -----
    def _on_close(self):
        try:
            self._flush_overtime_segment()
        except Exception:
            pass
        if self.global_monitor: self.global_monitor.stop()
        self.destroy()


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
