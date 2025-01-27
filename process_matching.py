import psutil
import json
import os
import sys
import time
import pygetwindow as gw
import win32gui
import win32process
from threading import Thread, Lock, Event, Timer
from tkinter import Tk, StringVar, ttk, Menu, messagebox, BooleanVar
from tkinter.scrolledtext import ScrolledText
from tkinter import Frame
import pystray
from PIL import Image, ImageDraw
from twitch_autocomplete import TwitchAutocomplete

class DebounceTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.timer = None
        self.lock = Lock()

    def schedule(self, *args, **kwargs):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = Timer(self.delay, self.callback, args=args, kwargs=kwargs)
            self.timer.daemon = True
            self.timer.start()

    def cancel(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None

class MatchWatchdog:
    def __init__(self):
        self.matches = self.load_matches()
        self.lock = Lock()
        self.exit_event = Event()
        self.process_update_interval = 1000
        
        self.autocomplete = TwitchAutocomplete(
            "u49mvx60iql1zy1xbfeam5flr3rhxl",
            "tof8bhnt5lp74yu85n8oau3mb8bcfb"
        )
        
        self.root = Tk()
        self.root.title("Matchdog")
        self.root.geometry("320x140")
        self.root.resizable(False, False)
        
        self.process_var = StringVar()
        self.game_var = StringVar()
        self.close_to_tray_var = BooleanVar(value=True)
        
        self.search_timer = DebounceTimer(1.0, self._perform_search)
        
        self.setup_ui()
        self.setup_menu()
        self.start_monitor()
        self.update_process_list()

    def setup_menu(self):
        menubar = Menu(self.root)
        self.root.config(menu=menubar)  # Пустое меню (удалены настройки WebSocket)

    def load_matches(self, filename="matches.json"):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            messagebox.showerror("Error", f"{filename} contains invalid JSON")
            sys.exit(1)

    def save_matches(self, filename="matches.json"):
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.matches, f, ensure_ascii=False, indent=4)
        except Exception as e:
            messagebox.showerror("Error", f"Error saving matches: {e}")

    def update_process_list(self):
        if not self.exit_event.is_set():
            current_selection = self.process_var.get()
            processes = self.get_window_processes()
            
            current_values = list(self.process_menu['values'])
            if set(processes) != set(current_values):
                self.process_menu['values'] = processes
                if current_selection and current_selection in processes:
                    self.process_var.set(current_selection)
                elif current_selection:
                    self.process_var.set('')
            
            self.root.after(self.process_update_interval, self.update_process_list)

    def setup_ui(self):
        main_frame = Frame(self.root)
        main_frame.pack(pady=10, padx=10, fill='both', expand=True)

        ttk.Label(main_frame, text="Process:").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=5)
        self.process_menu = ttk.Combobox(main_frame, textvariable=self.process_var, state="readonly")
        self.process_menu.grid(row=0, column=1, sticky="ew", pady=5)

        ttk.Label(main_frame, text="Game:").grid(row=1, column=0, sticky="w", padx=(0, 5), pady=5)
        self.game_menu = ttk.Combobox(main_frame, textvariable=self.game_var)
        self.game_menu.grid(row=1, column=1, sticky="ew", pady=5)
        
        self.game_var.trace_add("write", self._schedule_search)

        button_frame = Frame(main_frame)
        button_frame.grid(row=2, column=0, columnspan=2, pady=10)
        button_frame.grid_columnconfigure(0, weight=1)
        button_frame.grid_columnconfigure(1, weight=1)
        button_frame.grid_columnconfigure(2, weight=1)

        ttk.Button(button_frame, text="Add Match", command=self.add_match).grid(
            row=0, column=0, padx=5, pady=5, sticky="ew")
        ttk.Button(button_frame, text="Minimize to Tray", command=self.minimize_to_tray).grid(
            row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Checkbutton(button_frame, text="Close to tray", variable=self.close_to_tray_var).grid(
            row=0, column=2, padx=5, pady=5, sticky="ew")

        main_frame.columnconfigure(1, weight=1)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _schedule_search(self, *args):
        query = self.game_var.get()
        if query.strip():
            self.search_timer.schedule(query)

    def _perform_search(self, query):
        try:
            suggestions = self.autocomplete.search_categories(query)
            self.root.after(0, lambda: self.game_menu.configure(values=suggestions))
        except Exception as e:
            self.root.after(0, lambda: self.game_menu.configure(values=[]))

    def get_window_processes(self):
        windows = []
        try:
            for win in gw.getAllWindows():
                if win.title.strip():
                    hwnd = win._hWnd
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    try:
                        process_name = psutil.Process(pid).name()
                        windows.append(process_name)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
        except Exception:
            pass
        return list(set(windows))

    def add_match(self):
        process_name = self.process_var.get().strip()
        game_name = self.game_var.get().strip()

        if not process_name or not game_name:
            messagebox.showwarning("Warning", "Process name and game name cannot be empty.")
            return

        with self.lock:
            self.matches[process_name] = game_name
            self.save_matches()
        
        self.process_var.set("")
        self.game_var.set("")
        messagebox.showinfo("Info", f"Added match: {process_name} -> {game_name}")

    def start_monitor(self):
        self.monitor_thread = Thread(target=self.monitor_processes, daemon=True)
        self.monitor_thread.start()

    def monitor_processes(self):
        last_result = None
        while not self.exit_event.is_set():
            try:
                running_processes = self.get_window_processes()
                result = next((self.matches[process_name] 
                         for process_name in running_processes 
                         if process_name in self.matches), None)
                
                if result != last_result:
                    # Обновление файла result.txt
                    try:
                        with open("result.txt", "w", encoding="utf-8") as f:
                            f.write(result if result else "")
                        last_result = result
                    except Exception as e:
                        print(f"Error writing result: {e}")
                
                time.sleep(1)
            except Exception as e:
                print(f"Error in monitor_processes: {e}")
                time.sleep(5)

    def setup_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((16, 16, 48, 48), fill=(0, 0, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Restore", self.restore_app),
            pystray.MenuItem("Exit", self.exit_app)
        )
        
        self.icon = pystray.Icon("Match Watchdog", image, "Match Watchdog", menu)
        self.icon.on_activate = self.on_tray_click
        self.icon.run()

    def on_tray_click(self, icon, button, time):
        if button == 1:
            self.restore_app(icon, None)

    def restore_app(self, icon, item):
        self.root.after(0, self.root.deiconify)
        icon.stop()

    def minimize_to_tray(self):
        self.root.withdraw()
        tray_thread = Thread(target=self.setup_tray_icon, daemon=True)
        tray_thread.start()

    def on_close(self):
        if self.close_to_tray_var.get():
            self.minimize_to_tray()
        else:
            self.exit_app()

    def exit_app(self, *args):
        self.exit_event.set()
        if hasattr(self, 'icon'):
            self.icon.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

def main():
    app = MatchWatchdog()
    app.run()

if __name__ == "__main__":
    main()