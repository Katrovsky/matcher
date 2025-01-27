import psutil
import json
import os
import sys
import time
import pygetwindow as gw
import win32gui
import win32process
from threading import Thread, Lock, Event, Timer
from tkinter import Tk, StringVar, ttk, Menu, messagebox
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
        
        # Инициализация Twitch API
        self.autocomplete = TwitchAutocomplete(
            "u49mvx60iql1zy1xbfeam5flr3rhxl",
            "tof8bhnt5lp74yu85n8oau3mb8bcfb"
        )
        
        # Создание основного окна
        self.root = Tk()
        self.root.title("Match Watchdog with Autocomplete")
        self.root.geometry("400x200")
        self.root.resizable(False, False)
        
        # Переменные для хранения значений
        self.process_var = StringVar()
        self.game_var = StringVar()
        
        # Создание таймера для отложенного поиска
        self.search_timer = DebounceTimer(1.0, self._perform_search)
        
        self.setup_ui()
        self.start_monitor()

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

    def setup_ui(self):
        main_frame = Frame(self.root)
        main_frame.pack(pady=10, padx=10, fill='both', expand=True)

        # Process selection
        ttk.Label(main_frame, text="Process Name:").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=5)
        self.process_menu = ttk.Combobox(main_frame, textvariable=self.process_var, state="readonly")
        self.process_menu.grid(row=0, column=1, sticky="ew", pady=5)
        self.refresh_process_list()

        # Game selection with debounced search
        ttk.Label(main_frame, text="Game Name:").grid(row=1, column=0, sticky="w", padx=(0, 5), pady=5)
        self.game_menu = ttk.Combobox(main_frame, textvariable=self.game_var)
        self.game_menu.grid(row=1, column=1, sticky="ew", pady=5)
        
        # Привязываем обработчик изменения текста
        self.game_var.trace_add("write", self._schedule_search)

        # Buttons
        button_frame = Frame(main_frame)
        button_frame.grid(row=2, column=0, columnspan=2, pady=10)
        button_frame.grid_columnconfigure(0, weight=1)
        button_frame.grid_columnconfigure(1, weight=1)

        ttk.Button(button_frame, text="Add Match", command=self.add_match).grid(
            row=0, column=0, padx=5, pady=5, sticky="ew")
        ttk.Button(button_frame, text="Refresh List", command=self.refresh_process_list).grid(
            row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(button_frame, text="Minimize to Tray", command=self.minimize_to_tray).grid(
            row=1, column=0, padx=5, pady=5, sticky="ew")
        ttk.Button(button_frame, text="Exit", command=self.exit_app).grid(
            row=1, column=1, padx=5, pady=5, sticky="ew")

        main_frame.columnconfigure(1, weight=1)
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

    def _schedule_search(self, *args):
        """Планирует отложенный поиск при вводе текста"""
        query = self.game_var.get()
        if query.strip():
            self.search_timer.schedule(query)

    def _perform_search(self, query):
        """Выполняет поиск и обновляет выпадающий список"""
        try:
            suggestions = self.autocomplete.search_categories(query)
            # Обновляем UI в главном потоке
            self.root.after(0, lambda: self.game_menu.configure(values=suggestions))
        except Exception as e:
            # В случае ошибки очищаем список
            self.root.after(0, lambda: self.game_menu.configure(values=[]))

    def refresh_process_list(self):
        processes = self.get_window_processes()
        self.process_menu['values'] = processes

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
                result = [self.matches[process_name] 
                         for process_name in running_processes 
                         if process_name in self.matches]

                if result != last_result:
                    try:
                        with open("result.txt", 'w', encoding='utf-8') as f:
                            f.write('\n'.join(result) if result else '')
                        last_result = result
                    except Exception as e:
                        print(f"Error writing result: {e}")
                
                time.sleep(1)
            except Exception:
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
        self.icon.run()

    def restore_app(self, icon, item):
        self.root.after(0, self.root.deiconify)
        icon.stop()

    def minimize_to_tray(self):
        self.root.withdraw()
        tray_thread = Thread(target=self.setup_tray_icon, daemon=True)
        tray_thread.start()

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