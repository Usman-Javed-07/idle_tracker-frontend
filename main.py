from pynput import mouse as pyn_mouse, keyboard as pyn_keyboard
import time
import tkinter as tk
from tkinter import ttk, messagebox
from plyer import notification

from backend.models import init_tables, get_user_by_id
from backend.auth import signup, login
from backend.activity import set_user_status
from backend.config import ADMIN_BOOTSTRAP
from backend.models import get_user_by_username_or_email, insert_user
from backend.auth import hash_password

INACTIVITY_SECONDS = 10
CHECK_INTERVAL_MS = 300

class GlobalActivityMonitor:
    """
    Global (system-wide) mouse & keyboard listener using pynput.
    Calls a callback on *any* movement/click/scroll/key press.
    """
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
            # Call into the Tk thread-safe: just invoke the callback;
            # it only updates a timestamp, so it's safe/lightweight.
            self.on_activity()

    # Mouse callbacks
    def _on_move(self, x, y):
        self._maybe_fire()
    def _on_click(self, x, y, button, pressed):
        self._maybe_fire()
    def _on_scroll(self, x, y, dx, dy):
        self._maybe_fire()

    # Keyboard callback
    def _on_press(self, key):
        self._maybe_fire()

    def start(self):
        if self._running:
            return
        self._running = True
        self._mouse_listener = pyn_mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll
        )
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
        self.geometry("480x300")
        self.resizable(False, False)

        init_tables()
        self._ensure_single_admin()

        self.current_user = None
        self.last_activity = time.monotonic()
        self.inactive_sent = False
        self.global_monitor = None

        self.frames = {}
        for F in (AuthFrame, TrackerFrame):
            frame = F(self)
            self.frames[F.__name__] = frame

        self.show_frame("AuthFrame")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        if self.global_monitor:
            self.global_monitor.stop()
        self.destroy()

    def _ensure_single_admin(self):
        # Create admin if missing
        row = get_user_by_username_or_email(ADMIN_BOOTSTRAP["username"])
        if not row:
            try:
                insert_user(
                    ADMIN_BOOTSTRAP["username"],
                    ADMIN_BOOTSTRAP["email"],
                    hash_password(ADMIN_BOOTSTRAP["password"]),
                    role="admin",
                )
            except Exception:
                pass

    def show_frame(self, name):
        for f in self.frames.values():
            f.pack_forget()
        frame = self.frames[name]
        frame.pack(fill="both", expand=True)

    def on_logged_in(self, user_dict):
        self.current_user = user_dict
        self.last_activity = time.monotonic()
        self.inactive_sent = False

        # Start system-wide activity monitor
        self.global_monitor = GlobalActivityMonitor(self.on_activity, min_interval=0.15)
        self.global_monitor.start()

        # No need for Tk bind_all anymore
        # for seq in ("<Any-KeyPress>", "<Any-Button>", "<Motion>", "<MouseWheel>"):
        #     self.bind_all(seq, self.on_activity, add="+")

        self.show_frame("TrackerFrame")
        self.after(CHECK_INTERVAL_MS, self._loop_check)

       

    def on_activity(self, event=None):
        self.last_activity = time.monotonic()
        if self.inactive_sent:
            # Mark active and reset flag
            set_user_status(self.current_user["id"], True)
            self.inactive_sent = False
            self.frames["TrackerFrame"].set_status(True)

    def _loop_check(self):
        now = time.monotonic()
        idle = now - self.last_activity
        self.frames["TrackerFrame"].update_idle(idle)

        if idle >= INACTIVITY_SECONDS and not self.inactive_sent:
            # Mark inactive, notify locally (user), admin will see via admin app
            set_user_status(self.current_user["id"], False)
            self.inactive_sent = True
            self.frames["TrackerFrame"].set_status(False)
            try:
                notification.notify(
                    title="You are inactive",
                    message=f"No activity for {INACTIVITY_SECONDS}s.",
                    timeout=3,
                )
            except Exception:
                pass

        self.after(CHECK_INTERVAL_MS, self._loop_check)


class AuthFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=16)

        ttk.Label(self, text="Login or Sign Up", font=("Segoe UI", 16, "bold")).pack(pady=(0,12))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # --- Login tab
        login_tab = ttk.Frame(nb, padding=10)
        self.login_id = tk.StringVar()
        self.login_pwd = tk.StringVar()

        ttk.Label(login_tab, text="Username or Email").pack(anchor="w")
        ttk.Entry(login_tab, textvariable=self.login_id).pack(fill="x")

        ttk.Label(login_tab, text="Password").pack(anchor="w", pady=(8,0))
        ttk.Entry(login_tab, show="*", textvariable=self.login_pwd).pack(fill="x")

        ttk.Button(login_tab, text="Login", command=self.do_login).pack(pady=10)

        nb.add(login_tab, text="Login")

        # --- Signup tab
        signup_tab = ttk.Frame(nb, padding=10)
        self.su_username = tk.StringVar()
        self.su_email = tk.StringVar()
        self.su_pwd = tk.StringVar()

        ttk.Label(signup_tab, text="Username").pack(anchor="w")
        ttk.Entry(signup_tab, textvariable=self.su_username).pack(fill="x")

        ttk.Label(signup_tab, text="Email").pack(anchor="w", pady=(8,0))
        ttk.Entry(signup_tab, textvariable=self.su_email).pack(fill="x")

        ttk.Label(signup_tab, text="Password").pack(anchor="w", pady=(8,0))
        ttk.Entry(signup_tab, show="*", textvariable=self.su_pwd).pack(fill="x")

        ttk.Button(signup_tab, text="Sign Up", command=self.do_signup).pack(pady=10)

        nb.add(signup_tab, text="Sign Up")

    def do_login(self):
        try:
            user = login(self.login_id.get().strip(), self.login_pwd.get().strip())
            if not user:
                messagebox.showerror("Login failed", "Invalid credentials")
                return
            if user["role"] != "user":
                messagebox.showwarning("Wrong app", "This app is for normal users only.")
                return
            self.master.on_logged_in(user)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def do_signup(self):
        try:
            uid = signup(self.su_username.get().strip(),
                         self.su_email.get().strip(),
                         self.su_pwd.get().strip(),
                         role="user")
            messagebox.showinfo("Success", f"Account created (id={uid}). You can login now.")
        except Exception as e:
            messagebox.showerror("Error", str(e))


class TrackerFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=20)
        self.status_var = tk.StringVar(value="Active")
        self.idle_var = tk.StringVar(value="Idle for 0.0s")
        ttk.Label(self, textvariable=self.status_var, font=("Segoe UI", 20, "bold")).pack(pady=(10,5))
        ttk.Label(self, textvariable=self.idle_var).pack()
        ttk.Label(self, text="(Move the mouse or press any key inside this window)").pack(pady=(10,0))

    def set_status(self, active: bool):
        self.status_var.set("Active" if active else "Inactive")

    def update_idle(self, seconds: float):
        self.idle_var.set(f"Idle for {seconds:.1f}s")


if __name__ == "__main__":
    app = UserApp()
    app.mainloop()

if __name__ == "__main__":
    import logging, os, sys, traceback
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        filename=os.path.join("logs", "user_app.log"),
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    try:
        print("Starting UserApp…")
        app = UserApp()
        # bring window to front in case it opened off-screen/minimized
        app.after(50, lambda: (app.lift(), app.attributes("-topmost", True)))
        app.after(1000, lambda: app.attributes("-topmost", False))
        app.mainloop()
    except Exception:
        tb = traceback.format_exc()
        logging.error(tb)
        print("User app crashed. See logs\\user_app.log")
        print(tb)
        sys.exit(1)
