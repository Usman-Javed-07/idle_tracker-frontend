import time
import datetime as dt
import tkinter as tk
from tkinter import ttk, messagebox
from plyer import notification
from pynput import mouse as pyn_mouse, keyboard as pyn_keyboard

from backend.models import init_tables, get_user_by_username_or_email, insert_user, get_user_by_id
from backend.auth import login, hash_password
from backend.activity import set_user_status
from backend.config import ADMIN_BOOTSTRAP
from backend.notify import send_email
from backend.models import update_user_status

INACTIVITY_SECONDS = 10
CHECK_INTERVAL_MS = 300

class GlobalActivityMonitor:
    """Global (system-wide) mouse & keyboard listener using pynput."""
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
        self.geometry("520x330")
        self.resizable(False, False)

        init_tables()
        self._ensure_single_admin()

        self.current_user = None
        self.last_activity = time.monotonic()
        self.inactive_sent = False
        self.active_since = None               # when we last became active
        self.shift_started_today = False
        self.global_monitor = None

        self.frames = {}
        for F in (AuthFrame, TrackerFrame):
            frame = F(self)
            self.frames[F.__name__] = frame

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
        self.active_since = time.monotonic()   # start an active streak at login
        self.shift_started_today = False

        # Global listeners
        self.global_monitor = GlobalActivityMonitor(self.on_activity, min_interval=0.15)
        self.global_monitor.start()

        self.show_frame("TrackerFrame")
        self.after(CHECK_INTERVAL_MS, self._loop_check)

    def on_activity(self):
        self.last_activity = time.monotonic()
        now_local_time = dt.datetime.now().time()
        shift_time = dt.datetime.strptime(str(self.current_user["shift_start_time"]), "%H:%M:%S").time()

        # Handle shift start (once per day) if time passed
        if not self.shift_started_today and now_local_time >= shift_time:
            set_user_status(self.current_user["id"], "shift_start")
            self.shift_started_today = True
            self.frames["TrackerFrame"].set_status("Shift Start")

        # Coming back from inactive -> become active, start a new active streak
        if self.inactive_sent:
            set_user_status(self.current_user["id"], "active")
            self.inactive_sent = False
            self.active_since = time.monotonic()
            self.frames["TrackerFrame"].set_status("Active")

        # If we never marked active yet after shift start, mark active on first activity
        elif self.shift_started_today and self.active_since is None:
            set_user_status(self.current_user["id"], "active")
            self.active_since = time.monotonic()
            self.frames["TrackerFrame"].set_status("Active")

    def _loop_check(self):
        now = time.monotonic()
        idle = now - self.last_activity
        self.frames["TrackerFrame"].update_idle(idle)

        # Auto-mark shift start if the time has passed and we haven't set it yet
        now_local_time = dt.datetime.now().time()
        shift_time = dt.datetime.strptime(str(self.current_user["shift_start_time"]), "%H:%M:%S").time()
        if not self.shift_started_today and now_local_time >= shift_time:
            set_user_status(self.current_user["id"], "shift_start")
            self.shift_started_today = True
            self.frames["TrackerFrame"].set_status("Shift Start")

        # Inactivity → send toast + email + record active duration
        if idle >= INACTIVITY_SECONDS and not self.inactive_sent:
            active_duration = None
            if self.active_since is not None:
                active_duration = int(now - self.active_since)

            set_user_status(self.current_user["id"], "inactive", active_duration_seconds=active_duration)
            self.inactive_sent = True
            self.frames["TrackerFrame"].set_status("Inactive")

            # local toast
            try:
                notification.notify(
                    title="You are inactive",
                    message=f"No activity for {INACTIVITY_SECONDS} seconds.",
                    timeout=3,
                )
            except Exception:
                pass

            # email both admin & user
            u = self.current_user
            duration_txt = seconds_to_hhmmss(active_duration) if active_duration else "unknown"
            body = (f"User {u['name']} ({u['username']}, {u['email']}, {u['department']}) became INACTIVE.\n"
                    f"Active streak before inactivity: {duration_txt}.\n")
            try:
                send_email([u["email"], ADMIN_BOOTSTRAP["email"]],
                           subject=f"[IdleTracker] {u['username']} inactive",
                           body=body)
            except Exception:
                pass

        # End shift auto-off (optional)
        if self.shift_started_today:
            start_dt = dt.datetime.combine(dt.date.today(), shift_time)
            end_dt = start_dt + dt.timedelta(seconds=int(self.current_user["shift_duration_seconds"]))
            if dt.datetime.now() >= end_dt:
                update_user_status(self.current_user["id"], "off")

        self.after(CHECK_INTERVAL_MS, self._loop_check)


class AuthFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=16)
        ttk.Label(self, text="Login", font=("Segoe UI", 16, "bold")).pack(pady=(0,12))

        self.login_id = tk.StringVar()
        self.login_pwd = tk.StringVar()

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
        ttk.Label(self, text="(Global detection: move mouse or press any key anywhere)").pack(pady=(10,0))

    def set_status(self, status_text: str):
        self.status_var.set(status_text)

    def update_idle(self, seconds: float):
        self.idle_var.set(f"Idle for {seconds:.1f}s")


def seconds_to_hhmmss(sec):
    if sec is None: return "00:00:00"
    h = sec // 3600; m = (sec % 3600) // 60; s = sec % 60
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


if __name__ == "__main__":
    import logging, os, sys, traceback
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
