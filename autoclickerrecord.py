import customtkinter as ctk
from ctypes import POINTER, byref, WinDLL, WinError, get_last_error, wintypes
import json
import math
import os
from pathlib import Path
import threading
import tkinter as tk
import time
import pyautogui
from queue import Empty, SimpleQueue
from pynput import keyboard, mouse

BUTTON_NAMES = {
    mouse.Button.left: 'left',
    mouse.Button.right: 'right',
    mouse.Button.middle: 'middle',
}

pyautogui.PAUSE = 0
ctk.set_appearance_mode('light')

CANVAS = '#FFFFFF'
RAIL = '#EEF1F8'
INK = '#1F2940'
MUTED = '#55637C'
LINE = '#D7DFED'
ACCENT = '#384BB0'
HOVER = '#2A3B95'

MODE_DETAILS = {
    'Key sequence': ('Key sequence', 'Repeat one or more keys in order in the focused app.'),
    'Click': ('Rapid click', 'Repeat a left click at your cursor.'),
    'Hold': ('Click and hold', 'Alternate between holding and releasing the left button.'),
    'Record': ('Record macro', 'Record mouse movements and clicks, then replay them.'),
    'Background': ('Background', 'Send mouse input to a selected window that accepts background clicks.'),
}
ctk.set_default_color_theme('blue')


class BackgroundClickTarget:
    def __init__(self, window, process_id, point, title):
        self.window = window
        self.process_id = process_id
        self.point = point
        self.title = title

    @staticmethod
    def _process_id(window):
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, process_id)
        return process_id.value

    @classmethod
    def under_cursor(cls, app_window):
        point = wintypes.POINT()
        if not user32.GetCursorPos(point):
            raise WinError(get_last_error())

        window = user32.WindowFromPoint(point)
        process_id = cls._process_id(window)
        if not process_id or process_id == cls._process_id(app_window):
            raise ValueError('Hover over the game window and try again.')

        if not user32.ScreenToClient(window, point):
            raise WinError(get_last_error())

        title = (wintypes.WCHAR * 256)()
        user32.GetWindowTextW(user32.GetAncestor(window, 2), title, len(title))
        target = cls(window, process_id, point, title.value or 'Selected window')
        target._check_position()
        return target

    def _check_position(self, point=None):
        point = self.point if point is None else point
        if self._process_id(self.window) != self.process_id:
            raise ValueError('Target window closed. Pick it again.')
        if user32.IsIconic(user32.GetAncestor(self.window, 2)):
            raise ValueError('Restore the target window before clicking.')

        rect = wintypes.RECT()
        if not user32.GetClientRect(self.window, rect):
            raise WinError(get_last_error())
        if not (0 <= point.x < rect.right and 0 <= point.y < rect.bottom):
            raise ValueError('Click point is outside the window. Pick it again.')

    def click(self):
        self._check_position()
        position = (self.point.y << 16) | self.point.x
        for message, buttons in ((0x0201, 1), (0x0202, 0)):  # Left button down/up
            if not user32.PostMessageW(self.window, message, buttons, position):
                raise WinError(get_last_error())


user32 = WinDLL('user32', use_last_error=True)
for name, arguments, result in (
    ('GetCursorPos', (POINTER(wintypes.POINT),), wintypes.BOOL),
    ('WindowFromPoint', (wintypes.POINT,), wintypes.HWND),
    ('GetAncestor', (wintypes.HWND, wintypes.UINT), wintypes.HWND),
    ('GetWindowThreadProcessId', (wintypes.HWND, POINTER(wintypes.DWORD)), wintypes.DWORD),
    ('GetWindowTextW', (wintypes.HWND, wintypes.LPWSTR, wintypes.INT), wintypes.INT),
    ('ScreenToClient', (wintypes.HWND, POINTER(wintypes.POINT)), wintypes.BOOL),
    ('GetClientRect', (wintypes.HWND, POINTER(wintypes.RECT)), wintypes.BOOL),
    ('IsChild', (wintypes.HWND, wintypes.HWND), wintypes.BOOL),
    ('IsIconic', (wintypes.HWND,), wintypes.BOOL),
    ('GetForegroundWindow', (), wintypes.HWND),
    ('GetKeyState', (wintypes.INT,), wintypes.SHORT),
    ('PostMessageW', (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM), wintypes.BOOL),
):
    function = getattr(user32, name)
    function.argtypes = arguments
    function.restype = result


DEFAULT_HOTKEYS = {'play': ('=', 0, 0xBB), 'record': ('-', 0, 0xBD)}
HOTKEY_FILE = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'AutoClicker' / 'hotkeys.json'
MODIFIERS = ((2, 0x11, 'Ctrl'), (1, 0x12, 'Alt'), (4, 0x10, 'Shift'))
KEY_NAMES = {'minus': '-', 'equal': '=', 'space': 'Space', 'Return': 'Enter'}


def load_hotkeys():
    try:
        saved = json.loads(HOTKEY_FILE.read_text(encoding='utf-8'))
        result = {}
        for action in DEFAULT_HOTKEYS:
            label, modifiers, key = saved[action]
            if not isinstance(label, str) or not label or len(label) > 35:
                raise ValueError
            if type(modifiers) is not int or type(key) is not int or not 0 <= modifiers <= 7 or not 1 <= key <= 254:
                raise ValueError
            result[action] = (label, modifiers, key)
        if result['play'][1:] == result['record'][1:]:
            raise ValueError
        return result
    except (OSError, KeyError, TypeError, ValueError):
        return DEFAULT_HOTKEYS.copy()


def listen_for_hotkeys(events, stopped, commands, initial):
    # Run registration and dispatch on a separate thread with its own Windows message queue.
    register = user32.RegisterHotKey
    register.argtypes = (wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT)
    register.restype = wintypes.BOOL
    unregister = user32.UnregisterHotKey
    unregister.argtypes = (wintypes.HWND, wintypes.INT)
    unregister.restype = wintypes.BOOL
    peek = user32.PeekMessageW
    peek.argtypes = (POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
    peek.restype = wintypes.BOOL
    active = {}
    slot = {'play': 1, 'record': 2}

    def bind(action, shortcut, *, startup=False):
        old = active.get(action)
        if old and old[1][1:] == shortcut[1:]:
            events.put(('bound', (action, old[1])))
            return
        label, modifiers, key = shortcut
        identifier = slot[action] if not old else (old[0] ^ 4)
        if not register(None, identifier, modifiers | 0x4000, key):
            message = f'Windows could not register {label}. Choose another shortcut.'
            events.put(('error' if startup else 'bind_error', message))
            return
        if old:
            unregister(None, old[0])
        active[action] = (identifier, shortcut)
        if not startup:
            events.put(('bound', (action, shortcut)))

    try:
        for action, shortcut in initial.items():
            bind(action, shortcut, startup=True)
        message = wintypes.MSG()
        while not stopped.wait(0.01):
            while True:
                try:
                    action, shortcut = commands.get_nowait()
                except Empty:
                    break
                bind(action, shortcut)
            while peek(byref(message), None, 0x0312, 0x0312, 1):
                for action, (identifier, _) in active.items():
                    if message.wParam == identifier:
                        events.put(('key', action))
                        break
    finally:
        for identifier, _ in active.values():
            unregister(None, identifier)


class AutoClickerApp:

    def __init__(self, root):
        self.root = root
        self.root.title('Auto Clicker')
        self.root.geometry('780x495')
        self.root.minsize(560, 295)
        self.root.configure(fg_color=CANVAS)

        self.is_clicking = False
        self.key_job = None
        self.held_key = None
        self.key_controller = keyboard.Controller()
        self.mouse_is_down = False
        self.background_target = None
        self.background_job = None
        self.capture_job = None
        self.sequence = []
        self.sequence_listener = None
        self.sequence_job = None
        self.sequence_buttons = 0
        self.sequence_lock = threading.Lock()

        # Recording state
        self.recording = False
        self.recorded_events = []
        self.record_start = 0.0
        self.listener = None

        self.armed_mode = None
        self.shortcuts = load_hotkeys()
        self.hotkey_commands = SimpleQueue()
        self.mode_var = ctk.StringVar(value='Key sequence')
        self.build_interface()

        self.hotkey_events = SimpleQueue()
        self.stop_hotkeys = threading.Event()
        self.hotkey_thread = threading.Thread(target=listen_for_hotkeys,
            args=(self.hotkey_events, self.stop_hotkeys, self.hotkey_commands, self.shortcuts.copy()), daemon=True)
        self.hotkey_thread.start()

        self.update_mode_ui()

        self.click_thread = threading.Thread(target=self.clicker_loop, daemon=True)
        self.click_thread.start()

        self.root.after(20, self._poll)

        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def label(self, parent, text, *, size=13, color=INK, bold=False, **kwargs):
        return ctk.CTkLabel(parent, text=text, text_color=color, anchor='w', justify='left',
                            font=ctk.CTkFont('Segoe UI', size, weight='bold' if bold else 'normal'),
                            **kwargs)

    def button(self, parent, text, command, *, primary=False, width=110):
        button = ctk.CTkButton(
            parent, text=text, command=command, width=width, height=36, corner_radius=7,
            font=ctk.CTkFont('Segoe UI', 13, weight='bold'),
            text_color=CANVAS if primary else INK, fg_color=ACCENT if primary else RAIL,
            hover_color=HOVER if primary else LINE,
        )
        tk.Frame.configure(button, takefocus=True)
        tk.Misc.bind(button, '<Return>', lambda _event: button.invoke())
        tk.Misc.bind(button, '<space>', lambda _event: button.invoke())
        tk.Misc.bind(button, '<FocusIn>', lambda _event: button.configure(border_width=2, border_color=ACCENT))
        tk.Misc.bind(button, '<FocusOut>', lambda _event: button.configure(border_width=0))
        return button

    def field(self, parent, title, variable, row=0, column=0):
        self.label(parent, title, bold=True).grid(row=row, column=column, sticky='w')
        entry = ctk.CTkEntry(parent, textvariable=variable, height=39, width=116, corner_radius=7,
                             border_width=1, border_color=LINE, fg_color=CANVAS, text_color=INK,
                             font=ctk.CTkFont('Segoe UI', 16))
        entry.grid(row=row + 1, column=column, sticky='ew', padx=(0, 14), pady=(3, 14))
        entry.bind('<FocusIn>', lambda _event: entry.configure(border_color=ACCENT))
        entry.bind('<FocusOut>', lambda _event: entry.configure(border_color=LINE))
        return entry

    def build_interface(self):
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        rail = ctk.CTkScrollableFrame(self.root, width=152, height=0, corner_radius=0, fg_color=RAIL,
                                      scrollbar_fg_color=RAIL, scrollbar_button_color=LINE,
                                      scrollbar_button_hover_color=MUTED)
        rail.grid(row=0, column=0, sticky='ns')
        rail.grid_columnconfigure(0, weight=1)
        rail.grid_rowconfigure(6, weight=1)
        self.label(rail, 'AutoClicker', size=22, bold=True).grid(row=0, column=0, padx=18, pady=(24, 23), sticky='w')
        self.mode_buttons = {}
        for row, (mode, (title, _)) in enumerate(MODE_DETAILS.items(), 1):
            button = self.button(rail, title, lambda value=mode: self.select_mode(value), width=140)
            button.configure(anchor='w')
            button.grid(row=row, column=0, padx=12, pady=3, sticky='ew')
            self.mode_buttons[mode] = button

        ctk.CTkFrame(rail, height=1, fg_color=LINE).grid(row=7, column=0, padx=18, pady=(12, 10), sticky='ew')
        self.binding_button = None
        self.binding_pending = False
        self.shortcut_buttons = {}
        for row, (action, title) in enumerate((('play', 'Start / stop'), ('record', 'Record')), 8):
            shortcut = ctk.CTkFrame(rail, fg_color='transparent')
            shortcut.grid(row=row, column=0, padx=18, pady=(0, 9 if row == 8 else 18), sticky='ew')
            self.label(shortcut, title, color=MUTED).pack(anchor='w')
            button = self.button(shortcut, self.shortcuts[action][0], lambda value=action: self.pick_key(value), width=33)
            button.configure(height=28, fg_color=CANVAS)
            button.pack(fill='x')
            self.shortcut_buttons[action] = [button]
        self.root.bind('<KeyPress>', self.finish_key)

        body = ctk.CTkFrame(self.root, fg_color=CANVAS, corner_radius=0)
        body.grid(row=0, column=1, sticky='nsew')
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.content = ctk.CTkScrollableFrame(body, width=0, height=0, fg_color=CANVAS, corner_radius=0,
                                              scrollbar_button_color=LINE, scrollbar_button_hover_color=MUTED)
        self.content.grid(row=0, column=0, sticky='nsew')
        self.content.grid_columnconfigure(0, weight=1)
        self.page_title = self.label(self.content, '', size=29, bold=True)
        self.page_title.grid(row=0, column=0, sticky='w', padx=20, pady=(19, 0))
        self.page_detail = self.label(self.content, '', color=MUTED, wraplength=445)
        self.page_detail.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 12))
        self.panels = {
            mode: ctk.CTkFrame(self.content, fg_color='transparent') for mode in MODE_DETAILS
        }

        sequence = self.panels['Key sequence']
        sequence.grid_columnconfigure((0, 1), weight=1, uniform='sequence_settings')
        self.key_interval_var = ctk.StringVar(value='40')
        self.field(sequence, 'Interval (ms)', self.key_interval_var)
        self.label(sequence, 'Start / stop hotkey', bold=True).grid(row=0, column=1, sticky='w')
        play_shortcut = self.button(sequence, self.shortcuts['play'][0], lambda: self.pick_key('play'))
        play_shortcut.configure(height=39)
        play_shortcut.grid(row=1, column=1, sticky='ew', pady=(3, 14))
        self.shortcut_buttons['play'].append(play_shortcut)

        order_heading = ctk.CTkFrame(sequence, fg_color='transparent')
        order_heading.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(0, 5))
        self.label(order_heading, 'Keys in order', bold=True).pack(side='left')
        self.button(order_heading, 'Add key', self.add_key, width=82).pack(side='right')
        self.sequence_fields = ctk.CTkFrame(sequence, fg_color=RAIL, corner_radius=7)
        self.sequence_fields.grid(row=3, column=0, columnspan=2, sticky='ew')
        self.sequence_fields.grid_columnconfigure(1, weight=1)
        self.key_rows = []

        preview = ctk.CTkFrame(sequence, fg_color=RAIL, corner_radius=9)
        preview.grid(row=4, column=0, columnspan=2, sticky='ew', pady=(12, 7))
        self.label(preview, 'Sequence preview', color=MUTED, bold=True).pack(anchor='w', padx=16, pady=(8, 0))
        self.key_preview = self.label(preview, '', size=19, color=ACCENT, bold=True, wraplength=420)
        self.key_preview.pack(anchor='w', padx=16, fill='x')
        self.key_rate = self.label(preview, '', color=MUTED)
        self.key_rate.pack(anchor='w', padx=16, pady=(0, 6))
        self.key_interval_var.trace_add('write', self.update_key_preview)
        self.add_key('1')
        self.label(sequence, 'Switching windows stops the loop.', color=MUTED).grid(
            row=5, column=0, columnspan=2, sticky='w')
        self.content.bind('<Configure>', self.resize_content, add='+')

        click = self.panels['Click']
        self.interval_var = ctk.StringVar(value='0.1')
        self.field(click, 'Interval (seconds)', self.interval_var)
        self.cps_label = self.label(click, 'Estimated clicks per second: 10.0', color=MUTED)
        self.cps_label.grid(row=2, column=0, sticky='w')
        self.interval_var.trace_add('write', self.update_cps)

        hold = self.panels['Hold']
        self.hold_duration_var = ctk.StringVar(value='3.0')
        self.release_duration_var = ctk.StringVar(value='1.0')
        self.field(hold, 'Hold (seconds)', self.hold_duration_var, column=0)
        self.field(hold, 'Release (seconds)', self.release_duration_var, column=1)

        record = self.panels['Record']
        self.repeat_var = ctk.StringVar(value='1')
        self.field(record, 'Repeat count', self.repeat_var)
        self.record_button = self.button(record, 'Start Recording', self.toggle_recording, width=135)
        self.record_button.grid(row=2, column=0, sticky='w')
        self.record_info = self.label(record, 'No recording yet', color=MUTED, wraplength=430)
        self.record_info.grid(row=3, column=0, sticky='w', pady=(6, 0))

        background = self.panels['Background']
        self.target_label = self.label(background, 'No target selected', bold=True, wraplength=412)
        self.target_label.grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 6))
        self.pick_button = self.button(background, 'Pick target', self.pick_background_target)
        self.test_button = self.button(background, 'Test one click', self.test_background_click)
        self.pick_button.grid(row=1, column=0, sticky='w', padx=(0, 8))
        self.test_button.grid(row=1, column=1, sticky='w')
        self.field(background, 'Click interval (seconds)', self.interval_var, row=2)
        self.background_button = self.button(background, 'Repeat Click at Picked Point', self.toggle_background_clicks,
                                             width=195)
        self.background_button.grid(row=4, column=0, columnspan=2, sticky='w', pady=(0, 8))
        self.sequence_record = self.button(background, 'Record Sequence', self.toggle_sequence_record, width=145)
        self.sequence_record.grid(row=5, column=0, columnspan=2, sticky='w')

        footer = ctk.CTkFrame(body, fg_color=RAIL, corner_radius=0)
        footer.grid(row=1, column=0, sticky='ew')
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = self.label(footer, 'Ready', color=MUTED, wraplength=280)
        self.status_label.grid(row=0, column=0, padx=(12, 8), pady=10, sticky='ew')
        self.primary_action = self.button(footer, '', self.handle_primary_action, primary=True, width=140)
        self.primary_action.grid(row=0, column=1, padx=(0, 12), pady=10, sticky='e')
        body.bind('<Configure>', lambda event: self.status_label.configure(
            wraplength=max(100, event.width - self.primary_action.winfo_reqwidth() - 36)), add='+')
        self.primary_text = None

    def resize_content(self, event):
        width = max(150, event.width - 54)
        self.page_detail.configure(wraplength=width)
        self.key_preview.configure(wraplength=max(120, width - 32))
        self.target_label.configure(wraplength=width)
        self.record_info.configure(wraplength=width)

    def reveal_key(self, entry):
        canvas = self.content._parent_canvas
        self.content.update_idletasks()
        region = canvas.bbox('all')
        if region is None:
            return
        top = canvas.canvasy(0)
        bottom = top + canvas.winfo_height()
        entry_top = entry.winfo_rooty() - self.content.winfo_rooty()
        entry_bottom = entry_top + entry.winfo_height() + 5
        height = max(1, region[3] - region[1])
        if entry_bottom > bottom:
            canvas.yview_moveto((entry_bottom - canvas.winfo_height() - region[1]) / height)
        elif entry_top < top:
            canvas.yview_moveto((entry_top - region[1]) / height)

    def add_key(self, value=''):
        variable = ctk.StringVar(value=value)
        label = self.label(self.sequence_fields, '', bold=True)
        entry = ctk.CTkEntry(self.sequence_fields, textvariable=variable, height=31, width=80,
                             border_width=1, border_color=LINE, fg_color=CANVAS, text_color=INK)
        entry.bind('<FocusIn>', lambda _event: (entry.configure(border_color=ACCENT), self.reveal_key(entry)))
        entry.bind('<FocusOut>', lambda _event: entry.configure(border_color=LINE))
        remove = self.button(self.sequence_fields, 'Remove', lambda: self.remove_key(variable), width=72)
        remove.configure(height=29, fg_color=CANVAS)
        trace = variable.trace_add('write', self.update_key_preview)
        self.key_rows.append((variable, label, entry, remove, trace))
        self.layout_key_rows()
        if len(self.key_rows) > 1:
            self.reveal_key(entry)
            entry.focus_set()

    def remove_key(self, selected):
        if len(self.key_rows) <= 1:
            return
        for index, (variable, label, entry, remove, trace) in enumerate(self.key_rows):
            if variable is selected:
                self.key_rows.pop(index)
                variable.trace_remove('write', trace)
                for widget in (label, entry, remove):
                    widget.destroy()
                self.layout_key_rows()
                self.key_rows[min(index, len(self.key_rows) - 1)][2].focus_set()
                return

    def layout_key_rows(self):
        for index, (_, label, entry, remove, _) in enumerate(self.key_rows):
            label.configure(text=f'Key {index + 1}')
            label.grid(row=index, column=0, padx=(10, 12), pady=5, sticky='w')
            entry.grid(row=index, column=1, padx=(0, 8), pady=5, sticky='ew')
            remove.configure(state='normal' if len(self.key_rows) > 1 else 'disabled')
            remove.grid(row=index, column=2, padx=(0, 9), pady=5, sticky='e')
        self.update_key_preview()

    def update_key_preview(self, *_):
        names = [variable.get().strip()[:10].upper() or '?' for variable, *_ in self.key_rows]
        visible = ' → '.join(names[:5])
        rest = len(names) - 5
        self.key_preview.configure(text=f'{visible} → {f"+{rest} more" if rest > 0 else "repeat"}')
        try:
            delay = int(self.key_interval_var.get())
            rate = f'{1000 / delay:g} key presses per second' if delay >= 2 else 'Enter an interval of at least 2 ms.'
        except ValueError:
            rate = 'Enter an interval in milliseconds.'
        self.key_rate.configure(text=rate)

    def select_mode(self, mode):
        self.mode_var.set(mode)
        self.update_mode_ui()
        self.content._parent_canvas.yview_moveto(0)

    def handle_primary_action(self):
        mode = self.mode_var.get()
        if mode == 'Background':
            self.toggle_sequence_play()
        elif self.is_clicking or self.key_job is not None:
            self.toggle()
        elif self.armed_mode is not None:
            self.armed_mode = None
            self._show_message('Start canceled.', 'green')
        elif self.recording:
            self._show_message('Stop recording before starting playback.', 'orange')
        elif mode == 'Record' and not self.recorded_events:
            self._show_message('Record a macro before starting playback.', 'orange')
        else:
            self.armed_mode = mode
            self._show_message('Switch to the target app to start.', 'green')
        self.refresh_primary_action()

    def refresh_primary_action(self):
        mode = self.mode_var.get()
        if mode == 'Background':
            label = 'Stop sequence' if self.sequence_job is not None else 'Loop sequence'
        elif self.is_clicking or self.key_job is not None:
            label = 'Stop'
        elif self.armed_mode is not None:
            label = 'Cancel start'
        else:
            label = 'Start on switch'
        if label != self.primary_text:
            self.primary_text = label
            self.primary_action.configure(text=label)

    def close(self):
        self.stop_alternate_keys()
        self.stop_sequence()
        self.stop_background_clicks()
        self.stop_hotkeys.set()
        self.hotkey_thread.join()
        self.root.destroy()

    def pick_key(self, action):
        if self.binding_pending:
            return
        self.armed_mode = None
        self.clear_key_picker()
        self.binding_button = action
        self.set_shortcut_label(action, 'Press keys…')
        self.root.focus_set()
        self._show_message('Press a shortcut. Esc cancels.', 'green')

    def set_shortcut_label(self, action, text):
        for button in self.shortcut_buttons[action]:
            button.configure(text=text)

    def clear_key_picker(self):
        if self.binding_button is not None:
            action = self.binding_button
            self.set_shortcut_label(action, self.shortcuts[action][0])
        self.binding_button = None

    def finish_key(self, event):
        action = self.binding_button
        if action is None:
            return
        if self.binding_pending:
            return 'break'
        key = event.keycode
        if event.keysym == 'Escape':
            self.clear_key_picker()
            self._show_message('Shortcut edit canceled.', 'green')
            return 'break'
        if key in (0x10, 0x11, 0x12, 0x5B, 0x5C) or not 1 <= key <= 254:
            return 'break'
        held = [(flag, name) for flag, vk, name in MODIFIERS if user32.GetKeyState(vk) < 0]
        modifiers = sum(flag for flag, _ in held)
        label = '+'.join([name for _, name in held] + [KEY_NAMES.get(event.keysym, event.keysym.upper())])
        if any(other != action and shortcut[1:] == (modifiers, key) for other, shortcut in self.shortcuts.items()):
            self._show_message('That shortcut is already assigned. Press another.', 'orange')
            return 'break'
        self.binding_pending = True
        self.hotkey_commands.put((action, (label, modifiers, key)))
        return 'break'

    def apply_shortcut(self, action, shortcut):
        self.shortcuts[action] = shortcut
        self.binding_pending = False
        self.clear_key_picker()
        self.set_shortcut_label(action, shortcut[0])
        try:
            HOTKEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = HOTKEY_FILE.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.shortcuts), encoding='utf-8')
            temporary.replace(HOTKEY_FILE)
        except OSError:
            self._show_message('Shortcut updated for this session. Windows could not save it.', 'orange')
        else:
            self._show_message(f'Shortcut saved: {shortcut[0]}', 'green')

    def _show_message(self, text, color):
        palette = {'green': '#286054', 'orange': '#885512', 'red': MUTED}
        self.status_label.configure(text=text.removeprefix('Status: '), text_color=palette.get(color, MUTED))

    def update_mode_ui(self, *args):
        '''Show/hide fields depending on selected mode.'''
        mode = self.mode_var.get()
        if mode != 'Key sequence':
            self.stop_alternate_keys()
        else:
            self.is_clicking = False
            self._show_message('Focus the target app and press the play hotkey.', 'green')

        if mode == 'Background':
            self.is_clicking = False
            self._show_message('Status: Stopped', 'red')
        else:
            self.stop_sequence()
            self.stop_background_clicks()
            if self.capture_job is not None:
                self.root.after_cancel(self.capture_job)
                self.capture_job = None
                self.show_background_target()

        if self.recording and mode != 'Record':
            self.stop_recording()

        self.armed_mode = None
        for name, panel in self.panels.items():
            active = name == mode
            if active:
                panel.grid(row=2, column=0, padx=20, pady=(0, 12), sticky='new')
            else:
                panel.grid_remove()
            self.mode_buttons[name].configure(fg_color=ACCENT if active else RAIL,
                                               text_color=CANVAS if active else INK,
                                               hover_color=HOVER if active else LINE)
        title, detail = MODE_DETAILS[mode]
        self.page_title.configure(text=title)
        self.page_detail.configure(text=detail)
        self.refresh_primary_action()

        if self.mouse_is_down:
            pyautogui.mouseUp()
            self.mouse_is_down = False

    def update_cps(self, *args):
        try:
            interval = float(self.interval_var.get())
            if interval <= 1e-06:
                self.cps_label.configure(text='Estimated CPS: Max (Hardware Limit)')
            else:
                self.cps_label.configure(text=f'Estimated CPS: {1 / interval:.1f}')
        except ValueError:
            self.cps_label.configure(text='Estimated CPS: N/A')

    def toggle(self):
        if self.recording or self.mode_var.get() == 'Background':
            return
        if self.mode_var.get() == 'Key sequence':
            self.toggle_alternate_keys()
            return
        if not self.is_clicking and self.mode_var.get() == 'Record' and not self.recorded_events:
            self.record_info.configure(text='Record something first')
            return

        self.is_clicking = not self.is_clicking
        if self.is_clicking:
            self._show_message('Running', 'green')
            return
        self._show_message('Stopped', 'red')
        if self.mouse_is_down:
            pyautogui.mouseUp()
            self.mouse_is_down = False

    def toggle_alternate_keys(self):
        if self.key_job is not None:
            self.stop_alternate_keys()
            return

        names = [variable.get().strip().lower() for variable, *_ in self.key_rows]
        try:
            keys = tuple(name if len(name) == 1 and name.isascii() and name.isalnum()
                         else keyboard.Key[name] for name in names)
            delay = int(self.key_interval_var.get())
            if not 2 <= delay <= 2_147_483_647:
                raise ValueError
        except (KeyError, ValueError):
            self._show_message('Fill in each key and enter an interval of at least 2 ms.', 'orange')
            return

        window = user32.GetForegroundWindow()
        if not window or BackgroundClickTarget._process_id(window) == BackgroundClickTarget._process_id(self.root.winfo_id()):
            self._show_message('Focus the target app and press the play hotkey.', 'orange')
            return
        self._show_message(f'Running key sequence · {delay} ms per press', 'green')
        self.send_alternate_key(window, keys, delay, 0)

    def send_alternate_key(self, window, keys, delay, index):
        self.key_job = None
        if user32.GetForegroundWindow() != window:
            self._show_message('Key sequence stopped because focus changed.', 'orange')
            return
        self.held_key = keys[index]
        self.key_controller.press(self.held_key)
        self.key_job = self.root.after(delay // 2, self.release_alternate_key, window, keys, delay, index)

    def release_alternate_key(self, window, keys, delay, index):
        self.key_controller.release(self.held_key)
        self.held_key = None
        self.key_job = self.root.after(delay - delay // 2, self.send_alternate_key,
                                       window, keys, delay, (index + 1) % len(keys))

    def stop_alternate_keys(self):
        if self.key_job is not None:
            self.root.after_cancel(self.key_job)
            self.key_job = None
            self._show_message('Key sequence stopped.', 'orange')
        if self.held_key is not None:
            self.key_controller.release(self.held_key)
            self.held_key = None

    def _poll(self):
        while True:
            try:
                kind, key = self.hotkey_events.get_nowait()
            except Empty:
                break
            if kind == 'error':
                self._show_message(key, 'orange')
            elif kind == 'bind_error':
                self.binding_pending = False
                self._show_message(key, 'orange')
            elif kind == 'bound':
                self.apply_shortcut(*key)
            elif self.binding_pending:
                continue
            elif self.binding_button is not None:
                if key != self.binding_button:
                    self._show_message('That shortcut is already assigned. Press another.', 'orange')
                else:
                    self.clear_key_picker()
                    self._show_message('Shortcut is already set.', 'green')
            elif self.mode_var.get() == 'Background':
                if key == 'play':
                    self.toggle_sequence_play()
                else:
                    self.toggle_sequence_record()
            elif key == 'play':
                self.armed_mode = None
                self.toggle()
            else:
                if self.mode_var.get() != 'Record':
                    self.mode_var.set('Record')
                    self.update_mode_ui()
                self.toggle_recording()

        # Playback can end on its own (repeat count finished), so keep the label in sync
        if not self.is_clicking and not self.recording:
            if self.status_label.cget('text') == 'Running':
                self._show_message('Stopped', 'red')

        if self.armed_mode is not None:
            foreground = user32.GetForegroundWindow()
            if foreground and BackgroundClickTarget._process_id(foreground) != BackgroundClickTarget._process_id(self.root.winfo_id()):
                self.armed_mode = None
                self.toggle()
        self.refresh_primary_action()
        self.root.after(20, self._poll)

    def show_background_target(self):
        target = self.background_target
        label = f'{target.title} ({target.point.x}, {target.point.y})' if target else 'No target selected'
        self.target_label.configure(text=label)

    def pick_background_target(self):
        self.stop_sequence()
        self.stop_background_clicks()
        if self.capture_job is not None:
            self.root.after_cancel(self.capture_job)
        self.target_label.configure(text='Hover over the fishing button for 3 seconds.')
        self.capture_job = self.root.after(3000, self.capture_background_target)

    def capture_background_target(self):
        self.capture_job = None
        self.background_target = None
        try:
            self.background_target = BackgroundClickTarget.under_cursor(self.root.winfo_id())
        except (ValueError, OSError) as error:
            self._show_message(str(error), 'orange')
        else:
            self._show_message('Target selected', 'green')
        self.show_background_target()

    def send_background_click(self):
        if self.capture_job is not None:
            self._show_message('Wait for target capture', 'orange')
            return False
        if self.background_target is None:
            self._show_message('Pick a target first', 'orange')
            return False
        try:
            self.background_target.click()
        except (ValueError, OSError) as error:
            self._show_message(str(error), 'orange')
            return False
        return True

    def test_background_click(self):
        self.stop_sequence()
        self.stop_background_clicks()
        if self.send_background_click():
            self._show_message('Click sent. Check if the game responded.', 'green')

    def toggle_background_clicks(self):
        self.stop_sequence()
        if self.background_job is not None:
            self.stop_background_clicks()
            return
        try:
            interval = float(self.interval_var.get())
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError
            interval_ms = max(1, round(interval * 1000))
            if interval_ms > 2_147_483_647:
                raise ValueError
        except (ValueError, OverflowError):
            self._show_message('Enter a positive interval under 25 days', 'orange')
            return

        self.background_button.configure(text='Stop Repeated Click')
        self._show_message('Background clicks running', 'green')
        self.run_background_clicks(interval_ms)

    def run_background_clicks(self, interval_ms):
        self.background_job = None
        if self.send_background_click():
            self.background_job = self.root.after(interval_ms, self.run_background_clicks, interval_ms)
        else:
            self.background_button.configure(text='Repeat Click at Picked Point')

    def stop_background_clicks(self):
        if self.background_job is not None:
            self.root.after_cancel(self.background_job)
            self.background_job = None
            self.background_button.configure(text='Repeat Click at Picked Point')
            self._show_message('Status: Stopped', 'red')

    # ---------- Recording ----------

    def toggle_sequence_record(self):
        was_recording = self.sequence_listener is not None
        self.stop_sequence()
        self.stop_background_clicks()
        if was_recording:
            return
        target = self.background_target
        if target is None or self.capture_job is not None:
            self._show_message('Pick the game window first', 'orange')
            return
        self.sequence = []
        started = time.perf_counter()
        self.sequence_record_start = started

        self.recorded_presses = {}
        def capture(x, y, action, button=None):
            timestamp = time.perf_counter() - started
            point = wintypes.POINT(x, y)
            hit = user32.WindowFromPoint(point)
            inside = hit == target.window or bool(user32.IsChild(target.window, hit))
            if inside:
                inside = bool(user32.ScreenToClient(target.window, point))
            if inside:
                try:
                    target._check_position(point)
                except (ValueError, OSError):
                    inside = False
            with self.sequence_lock:
                if self.sequence_listener is not listener:
                    return
                if action == 'up':
                    previous = self.recorded_presses.pop(button, None)
                    if previous is None:
                        return
                    if not inside:
                        point = wintypes.POINT(*previous)
                elif not inside:
                    return
                if action == 'down':
                    self.recorded_presses[button] = (point.x, point.y)
                elif action == 'move':
                    for held in self.recorded_presses:
                        self.recorded_presses[held] = (point.x, point.y)
                self.sequence.append((timestamp, point.x, point.y, action, button))

        def click(x, y, button, pressed, *extra):
            if button in BUTTON_NAMES:
                capture(x, y, 'down' if pressed else 'up', BUTTON_NAMES[button])

        listener = mouse.Listener(on_move=lambda x, y, *extra: capture(x, y, 'move'), on_click=click)
        self.sequence_listener = listener
        listener.start()
        self.sequence_record.configure(text='Stop Recording')
        self._show_message('Recording mouse activity in the target window', 'green')

    def stop_sequence(self):
        with self.sequence_lock:
            listener = self.sequence_listener
            self.sequence_listener = None
        if listener is not None:
            self.sequence_cycle = max(0.01, time.perf_counter() - self.sequence_record_start)
            listener.stop()
            for button, (x, y) in self.recorded_presses.items():
                self.sequence.append((self.sequence_cycle, x, y, 'up', button))
            self.recorded_presses.clear()
            self.sequence_record.configure(text='Record Sequence')
            presses = sum(action == 'down' for _, _, _, action, _ in self.sequence)
            releases = sum(action == 'up' for _, _, _, action, _ in self.sequence)
            moves = len(self.sequence) - presses - releases
            self._show_message(f'Recorded {moves} moves, {presses} presses, {releases} releases',
                               'green' if self.sequence else 'orange')
        if self.sequence_job is not None:
            self.root.after_cancel(self.sequence_job)
            self.sequence_job = None
            self._show_message('Sequence stopped', 'orange')
        if self.sequence_buttons:
            try:
                self.release_sequence_buttons()
            except (OSError, ValueError):
                self.sequence_buttons = 0

    def toggle_sequence_play(self):
        playing = self.sequence_job is not None
        self.stop_sequence()
        self.stop_background_clicks()
        if playing:
            return
        if not self.sequence or self.background_target is None or self.capture_job is not None:
            self._show_message('Pick a target and record a sequence first', 'orange')
            return
        self._show_message('Looping recorded mouse activity', 'green')
        self.sequence_start = time.perf_counter()
        self.sequence_index = 0
        self.sent_cycles = 0
        self.advance_sequence()

    def post_sequence_event(self, x, y, action, button):
        target = self.background_target
        target._check_position(wintypes.POINT(x, y))
        buttons = self.sequence_buttons
        message = 0x0200  # WM_MOUSEMOVE
        if action != 'move':
            down, up, mask = {'left': (0x201, 0x202, 1), 'right': (0x204, 0x205, 2),
                              'middle': (0x207, 0x208, 16)}[button]
            message = down if action == 'down' else up
            buttons = buttons | mask if action == 'down' else buttons & ~mask
        if not user32.PostMessageW(target.window, message, buttons, (y << 16) | x):
            raise WinError(get_last_error())
        self.sequence_buttons = buttons
        self.sequence_position = (x, y)

    def release_sequence_buttons(self):
        for button, mask in (('left', 1), ('right', 2), ('middle', 16)):
            if self.sequence_buttons & mask:
                self.post_sequence_event(*self.sequence_position, 'up', button)

    def advance_sequence(self):
        self.sequence_job = None
        try:
            for _ in range(128):
                timestamp, x, y, action, button = self.sequence[self.sequence_index]
                delay = self.sequence_start + timestamp - time.perf_counter()
                if delay > 0:
                    self.sequence_job = self.root.after(math.ceil(delay * 1000), self.advance_sequence)
                    return
                self.post_sequence_event(x, y, action, button)
                self.sequence_index += 1
                if self.sequence_index == len(self.sequence):
                    self.release_sequence_buttons()
                    self.sent_cycles += 1
                    self._show_message(f'Sent loop {self.sent_cycles} to {self.background_target.title[:32]}', 'green')
                    self.sequence_index = 0
                    self.sequence_start = max(self.sequence_start + self.sequence_cycle, time.perf_counter())
            self.sequence_job = self.root.after(1, self.advance_sequence)
        except (ValueError, OSError) as error:
            self.stop_sequence()
            self._show_message(str(error), 'orange')

    def toggle_recording(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if self.is_clicking:
            self.record_info.configure(text='Stop playback before recording')
            return
        self.recorded_events = []
        self.record_start = time.perf_counter()
        self.recording = True
        self.record_button.configure(text='Stop Recording')
        self.record_info.configure(text='Recording... press record hotkey or Stop when done')
        self.listener = mouse.Listener(on_move=self.on_move, on_click=self.on_click)
        self.listener.start()

    def stop_recording(self):
        self.recording = False
        if self.listener is not None:
            self.listener.stop()
            self.listener = None

        # Drop the clicks made on the app's own Stop button so they aren't replayed
        left = self.root.winfo_rootx()
        top = self.root.winfo_rooty()
        right = left + self.root.winfo_width()
        bottom = top + self.root.winfo_height()
        while self.recorded_events:
            last = self.recorded_events[-1]
            if left <= last['x'] <= right and top <= last['y'] <= bottom:
                self.recorded_events.pop()
            else:
                break

        self.record_button.configure(text='Start Recording')
        self.record_info.configure(text=f'Recorded {len(self.recorded_events)} events')

    def _elapsed(self):
        return time.perf_counter() - self.record_start

    def on_move(self, x, y, *extra):
        if self.recording:
            self.recorded_events.append({'type': 'move', 't': self._elapsed(), 'x': x, 'y': y})

    def on_click(self, x, y, button, pressed, *extra):
        if self.recording:
            self.recorded_events.append({
                'type': 'down' if pressed else 'up',
                't': self._elapsed(),
                'x': x,
                'y': y,
                'button': BUTTON_NAMES.get(button, 'left'),
            })

    # ---------- Playback ----------

    def _get_repeats(self):
        try:
            return max(1, int(self.repeat_var.get()))
        except ValueError:
            return 1

    def _play_recording(self):
        events = list(self.recorded_events)
        if not events:
            self.is_clicking = False
            return

        repeats = self._get_repeats()
        try:
            for _ in range(repeats):
                if not self.is_clicking:
                    break
                start = time.perf_counter()
                for ev in events:
                    if not self.is_clicking:
                        break
                    wait = ev['t'] - (time.perf_counter() - start)
                    if wait > 0 and not self._sleep_while_active(wait):
                        break

                    if ev['type'] == 'move':
                        pyautogui.moveTo(ev['x'], ev['y'])
                    elif ev['type'] == 'down':
                        pyautogui.mouseDown(x=ev['x'], y=ev['y'], button=ev['button'])
                        self.mouse_is_down = True
                    else:
                        pyautogui.mouseUp(x=ev['x'], y=ev['y'], button=ev['button'])
                        self.mouse_is_down = False
        except pyautogui.FailSafeException:
            pass
        finally:
            if self.mouse_is_down:
                pyautogui.mouseUp()
                self.mouse_is_down = False
            self.is_clicking = False

    def clicker_loop(self):
        while True:
            if not self.is_clicking:
                time.sleep(0.01)
                continue

            mode = self.mode_var.get()
            if mode == 'Click':
                try:
                    interval = float(self.interval_var.get())
                except ValueError:
                    interval = 0.1
                pyautogui.click()
                time.sleep(max(interval, 0))
            elif mode == 'Hold':
                try:
                    hold_dur = float(self.hold_duration_var.get())
                except ValueError:
                    hold_dur = 3.0
                try:
                    release_dur = float(self.release_duration_var.get())
                except ValueError:
                    release_dur = 1.0

                pyautogui.mouseDown()
                self.mouse_is_down = True
                if not self._sleep_while_active(hold_dur):
                    continue

                pyautogui.mouseUp()
                self.mouse_is_down = False
                self._sleep_while_active(release_dur)
            elif mode == 'Record':
                self._play_recording()

    def _sleep_while_active(self, duration):
        '''Sleep in small increments, checking self.is_clicking so toggling off
        mid-hold releases the mouse promptly instead of waiting out the full duration.'''
        end_time = time.time() + max(duration, 0)
        while time.time() < end_time:
            if not self.is_clicking:
                if self.mouse_is_down:
                    pyautogui.mouseUp()
                    self.mouse_is_down = False
                return False
            time.sleep(0.02)
        return True


if __name__ == '__main__':
    root = ctk.CTk()
    app = AutoClickerApp(root)
    root.mainloop()
