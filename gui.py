import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import tkinter.font as tkFont
from controller import Controller
from discoverer import start_discoverer
from config import get_resource
from virtual_controller import VirtualController
from midi_player import HapticsPlayer

controller_frame_size = 200
battery_height = 40

background_color = "#aaaaaa"
block_color = "#404040"
player_number_bg_color = "#8B8B8B"
haptics_panel_color = "#3a3a3a"
haptics_accent_color = "#5b8def"

CONTROLLER_UPDATED_EVENT = '<<ControllersUpdated>>'
HAPTICS_STATUS_EVENT = '<<HapticsStatus>>'

class PlayerInfoBlock:
    def __init__(self, parent):
        self.parent = parent
        self.controller_label = None
        self.player_led_label = None

        self.load_pictures()
        self.init_interface()

    def init_interface(self):
        self.main_frame = tk.Frame(self.parent, width=controller_frame_size, height=controller_frame_size + 8 + 40, bg=player_number_bg_color)
        self.main_frame.pack(padx=10, pady=10, side=tk.LEFT)
        self.main_frame.pack_propagate(False)

        self.controllers_frame = tk.Frame(self.main_frame, width=controller_frame_size, height=controller_frame_size - battery_height, bg=block_color)
        self.controllers_frame.pack()
        self.controllers_frame.pack_propagate(False)

        self.battery_frame = tk.Frame(self.main_frame, width=controller_frame_size, height=battery_height, bg=block_color, padx=50)
        self.battery_frame.pack()
        self.battery_frame.pack_propagate(False)

    def load_pictures(self):
        self.joycon2leftandright = tk.PhotoImage(file=get_resource("images/joycon2leftandright.png"))
        self.joycon2right_sideway = tk.PhotoImage(file=get_resource("images/joycon2right_sideway.png"))
        self.joycon2left_sideway = tk.PhotoImage(file=get_resource("images/joycon2left_sideway.png"))
        self.procontroller2 = tk.PhotoImage(file=get_resource("images/procontroller2.png"))
        self.battery_h = tk.PhotoImage(file=get_resource("images/battery_h.png"))
        self.battery_m = tk.PhotoImage(file=get_resource("images/battery_m.png"))
        self.battery_l = tk.PhotoImage(file=get_resource("images/battery_l.png"))
        self.player_leds = {nb: tk.PhotoImage(file=get_resource(f"images/player{nb}.png")) for nb in range(1,5)}

    def clearControllerInfo(self):
        if self.controller_label is not None:
            self.controller_label.destroy()
            self.controller_label = None

        if self.player_led_label is not None:
            self.player_led_label.destroy()
            self.player_led_label = None

    def get_image_for_battery_level(self, controller: Controller):
        if controller.battery_voltage > 3.25:
            return self.battery_h
        if controller.battery_voltage > 3.125: 
            return self.battery_m
        return self.battery_l

    def displayControllersInfo(self, virtualController : VirtualController):

        if not virtualController.is_single():
            image = self.joycon2leftandright
        elif virtualController.is_single_joycon_right():
            image = self.joycon2right_sideway
        elif virtualController.is_single_joycon_left():
            image = self.joycon2left_sideway
        else:
            image = self.procontroller2


        self.controller_label = tk.Label(self.controllers_frame, image=image, bg=block_color)
        self.controller_label.pack(fill="none", expand=True)

        # Battery levels
        if virtualController.is_single():
            # 1 controller
            self.battery_label = tk.Label(self.battery_frame, image=self.get_image_for_battery_level(virtualController.controllers[0]), bg=block_color)
            self.battery_label.pack()
        else:
            # 2 controllers
            self.battery_label = tk.Label(self.battery_frame, image=self.get_image_for_battery_level(virtualController.controllers[0]), bg=block_color)
            self.battery_label.pack(side='left')
            self.battery_label2 = tk.Label(self.battery_frame, image=self.get_image_for_battery_level(virtualController.controllers[1]), bg=block_color)
            self.battery_label2.pack(side='right')

        self.player_led_label = tk.Label(self.main_frame, image=self.player_leds[virtualController.player_number], bg=player_number_bg_color)
        self.player_led_label.pack(pady=20)

class HapticsPanel:
    """MIDI Haptics Singer panel integrated into the controller window.
    
    Provides a file selector, play/stop/repeat controls, and a real-time
    note display showing what's being played on each Joy-Con.
    """

    def __init__(self, parent):
        self.parent = parent
        self.midi_player: HapticsPlayer = None
        self.selected_midi_path = None
        self.repeat_var = tk.BooleanVar(value=False)
        self._haptics_queue = queue.Queue()

    def init_interface(self):
        """Build the haptics singer UI panel."""
        self.frame = tk.LabelFrame(
            self.parent,
            text=" MIDI Haptics Singer ",
            font=("Arial", 12, "bold"),
            bg=haptics_panel_color,
            fg="white",
            padx=10,
            pady=8
        )

        # Row 1: File selector
        file_row = tk.Frame(self.frame, bg=haptics_panel_color)
        file_row.pack(fill=tk.X, pady=(0, 6))

        self.file_label = tk.Label(
            file_row, text="No file selected",
            font=("Arial", 10), bg=haptics_panel_color, fg="#cccccc",
            anchor="w", width=40
        )
        self.file_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.browse_btn = tk.Button(
            file_row, text="Browse...", command=self._browse_midi,
            font=("Arial", 9), width=8
        )
        self.browse_btn.pack(side=tk.RIGHT, padx=(5, 0))

        # Row 2: Controls
        controls_row = tk.Frame(self.frame, bg=haptics_panel_color)
        controls_row.pack(fill=tk.X, pady=(0, 6))

        self.play_btn = tk.Button(
            controls_row, text="Play", command=self._on_play,
            font=("Arial", 10, "bold"), width=8, bg="#4CAF50", fg="white",
            state=tk.DISABLED
        )
        self.play_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = tk.Button(
            controls_row, text="Stop", command=self._on_stop,
            font=("Arial", 10, "bold"), width=8, bg="#f44336", fg="white",
            state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.repeat_check = tk.Checkbutton(
            controls_row, text="Repeat", variable=self.repeat_var,
            font=("Arial", 10), bg=haptics_panel_color, fg="white",
            selectcolor=haptics_panel_color, activebackground=haptics_panel_color,
            activeforeground="white"
        )
        self.repeat_check.pack(side=tk.LEFT, padx=(0, 10))

        # Note display (matches SteamHapticsSinger's real-time display)
        self.note_display = tk.Label(
            self.frame, text="RIGHT:  OFF   |   LEFT:  OFF",
            font=("Consolas", 11), bg="#2a2a2a", fg=haptics_accent_color,
            anchor="center", padx=10, pady=4, width=42
        )
        self.note_display.pack(fill=tk.X, pady=(0, 4))

        # Status bar
        self.status_label = tk.Label(
            self.frame, text="Ready - select a MIDI file to begin",
            font=("Arial", 9), bg=haptics_panel_color, fg="#999999",
            anchor="w"
        )
        self.status_label.pack(fill=tk.X)

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def _browse_midi(self):
        """Open file dialog to select a MIDI file."""
        filepath = filedialog.askopenfilename(
            title="Select MIDI File",
            filetypes=[("MIDI Files", "*.mid *.midi"), ("All Files", "*.*")]
        )
        if filepath:
            self.selected_midi_path = filepath
            # Show just the filename
            from pathlib import Path
            self.file_label.config(text=Path(filepath).name)
            self.play_btn.config(state=tk.NORMAL)
            self.status_label.config(text=f"Loaded: {Path(filepath).name}")

    def _on_play(self):
        """Start MIDI haptics playback."""
        if self.selected_midi_path is None:
            return

        # Check if we have a player with controllers
        if self.midi_player is None:
            self.status_label.config(text="No Joy-Cons connected! Connect Joy-Cons first.")
            return

        self.play_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.browse_btn.config(state=tk.DISABLED)

        # Start playback in background
        self.midi_player.play(self.selected_midi_path, repeat=self.repeat_var.get())

    def _on_stop(self):
        """Stop MIDI haptics playback."""
        if self.midi_player and self.midi_player.is_playing:
            self.midi_player.stop()

        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.browse_btn.config(state=tk.NORMAL)

    def set_midi_player(self, player: HapticsPlayer):
        """Set the HapticsPlayer instance (called when controllers connect)."""
        self.midi_player = player
        # Wire up the status callback to update the GUI thread-safely
        player.set_status_callback(self._on_haptics_status)

    def clear_midi_player(self):
        """Clear the HapticsPlayer (called when controllers disconnect)."""
        if self.midi_player and self.midi_player.is_playing:
            self.midi_player.stop()
        self.midi_player = None
        self._on_stop()  # Reset button states

    def _on_haptics_status(self, text: str):
        """Callback from HapticsPlayer - thread-safe GUI update."""
        self._haptics_queue.put(text)
        if self.frame and self.frame.winfo_exists():
            try:
                self.frame.event_generate(HAPTICS_STATUS_EVENT)
            except tk.TclError:
                pass

    def handle_status_event(self):
        """Process pending status updates from the queue (called from GUI thread)."""
        try:
            while True:
                text = self._haptics_queue.get_nowait()
                # Update note display or status
                if "RIGHT:" in text and "LEFT:" in text:
                    # This is a note display update
                    self.note_display.config(text=text)
                else:
                    # This is a status message
                    self.status_label.config(text=text)
                    # Check if playback completed
                    if "completed" in text.lower() or "stopped" in text.lower():
                        self.play_btn.config(state=tk.NORMAL)
                        self.stop_btn.config(state=tk.DISABLED)
                        self.browse_btn.config(state=tk.NORMAL)
                        self.note_display.config(text="RIGHT:  OFF   |   LEFT:  OFF")
        except queue.Empty:
            pass


class ControllerWindow:
    def __init__(self):
        self.root = None
        self.main_frame = None
        self.no_controllers = True
        self.message_queue = queue.Queue()
        self.quit_event = threading.Event()
        self.virtual_controllers = [None] * 8
        self.haptics_panel = HapticsPanel(None)  # Will be reparented in init_interface
    
    def init_interface(self):
        self.root = tk.Tk()
        photo = tk.PhotoImage(file = get_resource('images/icon.png'))
        self.root.wm_iconphoto(False, photo)
        self.root.title("Switch2 Controllers - MIDI Haptics Singer")
        self.root.geometry("1100x550+50+50")
        self.root.minsize(1100,550)
        self.root.config(bg=background_color, padx=10, pady=10)
        self.font = tkFont.Font(family="Arial", size=16, weight="bold")
        self.pairing_hint_image = tk.PhotoImage(file=get_resource("images/pairing_hint.png"))

        self.haptics_panel = HapticsPanel(self.root)
        self.haptics_panel.init_interface()

        # Bind haptics status event
        self.root.bind(HAPTICS_STATUS_EVENT, lambda e: self.haptics_panel.handle_status_event())

        self.update([None])

    def update(self, controllers_info):
        self.no_controllers = all(c is None for c in controllers_info)
        self.virtual_controllers = controllers_info

        if self.main_frame is not None:
            self.main_frame.destroy()

        self.main_frame = tk.Frame(self.root, bg=background_color)
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # Top section: controllers display
        controllers_container = tk.Frame(self.main_frame, bg=background_color)
        controllers_container.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        if self.no_controllers:
            tk.Label(controllers_container, text="Press button of a paired controller, or hold sync button to pair", font=self.font, bg=background_color).pack()
            pairing_hint = tk.Label(controllers_container, image=self.pairing_hint_image, bg=background_color)
            pairing_hint.pack(pady=10)
            self.haptics_panel.clear_midi_player()
        else:
            self.players_info = [PlayerInfoBlock(controllers_container) for i in range(4)]

            for i, player_info in enumerate(self.players_info):
                controller_info = controllers_info[i]
                if controller_info is not None:
                    player_info.displayControllersInfo(controller_info)

            # Update the haptics player with connected Joy-Con controllers
            all_controllers = []
            for vc in controllers_info:
                if vc is not None:
                    all_controllers.extend(vc.controllers)

            if all_controllers:
                player = HapticsPlayer(all_controllers)
                self.haptics_panel.set_midi_player(player)

        # Bottom section: haptics panel
        self.haptics_panel.pack(fill=tk.X, side=tk.BOTTOM, pady=(10, 0))

    def start(self):
        def update_controllers_callback_threadsafe(controllers: list[VirtualController]):
            self.message_queue.put(controllers)
            self.root.event_generate(CONTROLLER_UPDATED_EVENT)
        
        self.root.bind(CONTROLLER_UPDATED_EVENT, lambda e : self.update(self.message_queue.get()))
        t = threading.Thread(target=start_discoverer, args=(update_controllers_callback_threadsafe, self.quit_event))
        t.start()

        def on_quit():
            self.quit_event.set()
            # Stop MIDI playback before closing
            if self.haptics_panel.midi_player and self.haptics_panel.midi_player.is_playing:
                self.haptics_panel.midi_player.stop()
            self.root.destroy()

        self.root.protocol("WM_DELETE_WINDOW", on_quit)

        self.root.mainloop()

if __name__ == "__main__":
    window = ControllerWindow()
    window.init_interface()
    window.start()