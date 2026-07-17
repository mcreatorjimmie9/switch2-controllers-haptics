"""
MIDI Haptics Singer for Joy-Con 2 controllers.

Ported from SteamHapticsSinger (https://github.com/CrazyCritic89/SteamHapticsSinger)
to work with switch2-controllers' BLE vibration interface.

MIDI channel mapping:
  Channel 0 -> Right Joy-Con haptic motor
  Channel 1 -> Left Joy-Con haptic motor
  Channels 2+ are ignored (Joy-Con only has one rumble motor per side)

Usage:
  - MIDI notes are converted to vibration frequencies
  - Each Joy-Con has a single rumble motor, so we use the high-frequency
    band of the VibrationData to play tones (the HF motor is the one
    that produces audible/tactile tones)
  - Frequency range is mapped from MIDI note range to Joy-Con's
    usable frequency range (~40 Hz - ~1252 Hz based on Joy-Con HD rumble specs)
"""

import asyncio
import logging
import struct
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable

from controller import Controller, VibrationData
from config import CONFIG

logger = logging.getLogger(__name__)

# Joy-Con 2 HD Rumble frequency range (in the encoding used by VibrationData)
# The VibrationData hf_freq field is 9 bits (0-511), and the actual physical
# frequency is approximately: f_hz = hf_freq * (sampling_rate / 2^9)
# Based on Switch HD rumble documentation, we map MIDI notes 21-108 to
# Joy-Con frequencies 1-511
JOYCON_FREQ_MIN = 1
JOYCON_FREQ_MAX = 511
MIDI_NOTE_MIN = 21  # A0
MIDI_NOTE_MAX = 108  # C8

# Pre-computed frequency lookup table for 128 MIDI notes
# Maps MIDI note number -> Joy-Con frequency value (0-511)
MIDI_TO_JOYCON_FREQ = [0] * 128

def _build_frequency_table():
    """
    Build the MIDI note to Joy-Con frequency lookup table.
    
    Uses a logarithmic mapping since musical notes follow equal temperament:
    freq = MIN + (MAX - MIN) * (note - NOTE_MIN) / (NOTE_MAX - NOTE_MIN)
    
    This provides a perceptually linear frequency spread across the
    MIDI note range, matching how the original SteamHapticsSinger
    maps notes to controller-specific frequency tables.
    """
    note_range = MIDI_NOTE_MAX - MIDI_NOTE_MIN
    freq_range = JOYCON_FREQ_MAX - JOYCON_FREQ_MIN
    for note in range(128):
        if note < MIDI_NOTE_MIN or note > MIDI_NOTE_MAX:
            MIDI_TO_JOYCON_FREQ[note] = 0
        else:
            # Linear interpolation across the Joy-Con frequency range
            normalized = (note - MIDI_NOTE_MIN) / note_range
            MIDI_TO_JOYCON_FREQ[note] = int(JOYCON_FREQ_MIN + normalized * freq_range)

_build_frequency_table()

# Note name display helpers
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

def note_to_display_name(note: int) -> str:
    """Convert MIDI note number to human-readable name like 'C-4' or 'A#5'."""
    if note < 0 or note > 127:
        return "OFF"
    name = NOTE_NAMES[note % 12]
    octave = (note // 12) - 1
    return f"{name}-{octave}"

# Default amplitude for haptic playback (0-1023 range for VibrationData)
# Overridden by config if available
DEFAULT_AMPLITUDE = 512
MAX_AMPLITUDE = 1023

# Number of supported MIDI channels for Joy-Con playback
# Channel 0 = Right Joy-Con, Channel 1 = Left Joy-Con
CHANNEL_COUNT = 2

# Special value indicating a note should stop
NOTE_STOP = -1


### MIDI File Parser ###

@dataclass
class TempoEvent:
    """Represents a tempo change in the MIDI file."""
    tick: int
    microseconds_per_quarter: int  # e.g. 500000 for 120 BPM

@dataclass
class MidiEvent:
    """Represents a single MIDI note-on or note-off event."""
    tick: int          # Absolute tick position in the MIDI file
    channel: int       # MIDI channel (0-15)
    note: int          # MIDI note number (0-127), or NOTE_STOP for note-off
    velocity: int      # MIDI velocity (0-127)
    is_note_on: bool   # True if this is a note-on event


class MidiFile:
    """
    Minimal MIDI file parser for Standard MIDI Files (Format 0 and Format 1).
    
    Extracts note-on and note-off events with their absolute tick positions,
    channels, note numbers, and velocities. This is sufficient for haptics
    playback which only cares about when notes start and stop.
    """
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.events: list[MidiEvent] = []
        self.tempo_events: list[TempoEvent] = []
        self.ticks_per_quarter: int = 480  # Default
        self._parse()
    
    def _read_variable_length(self, data: bytes, offset: int) -> tuple[int, int]:
        """Read a MIDI variable-length quantity. Returns (value, new_offset)."""
        value = 0
        while True:
            if offset >= len(data):
                raise ValueError("Unexpected end of MIDI data while reading variable-length quantity")
            byte = data[offset]
            offset += 1
            value = (value << 7) | (byte & 0x7F)
            if not (byte & 0x80):
                break
        return value, offset
    
    def _parse(self):
        """Parse the MIDI file and extract all note events."""
        with open(self.filepath, 'rb') as f:
            data = f.read()
        
        if len(data) < 14:
            raise ValueError("File too small to be a valid MIDI file")
        
        # Parse header chunk
        header_tag = data[0:4]
        if header_tag != b'MThd':
            raise ValueError(f"Invalid MIDI file: expected MThd header, got {header_tag}")
        
        header_length = struct.unpack('>I', data[4:8])[0]
        if header_length < 6:
            raise ValueError("MIDI header too short")
        
        fmt = struct.unpack('>H', data[8:10])[0]
        num_tracks = struct.unpack('>H', data[10:12])[0]
        self.ticks_per_quarter = struct.unpack('>H', data[12:14])[0]
        
        logger.info(f"MIDI file: format={fmt}, tracks={num_tracks}, ticks_per_quarter={self.ticks_per_quarter}")
        
        # Parse track chunks
        offset = 8 + header_length  # Skip past header chunk (4 tag + 4 length + data)
        
        for track_num in range(num_tracks):
            if offset + 8 > len(data):
                logger.warning(f"Unexpected end of data at track {track_num}")
                break
            
            track_tag = data[offset:offset + 4]
            if track_tag != b'MTrk':
                raise ValueError(f"Invalid MIDI file: expected MTrk at track {track_num}, got {track_tag}")
            
            track_length = struct.unpack('>I', data[offset + 4:offset + 8])[0]
            track_end = offset + 8 + track_length
            offset += 8
            
            self._parse_track(data, offset, track_end, track_num)
            offset = track_end
    
    def _parse_track(self, data: bytes, start: int, end: int, track_num: int):
        """Parse a single track chunk and extract note events."""
        offset = start
        abs_tick = 0
        running_status = 0  # MIDI running status
        
        while offset < end:
            # Read delta time
            delta, offset = self._read_variable_length(data, offset)
            abs_tick += delta
            
            if offset >= end:
                break
            
            # Read status byte
            status = data[offset]
            
            if status == 0xFF:
                # Meta event
                offset += 1  # skip status byte
                if offset >= end:
                    break
                meta_type = data[offset]
                offset += 1
                length, offset = self._read_variable_length(data, offset)
                
                # Tempo meta-event (type 0x51) - 3 bytes: microseconds per quarter note
                if meta_type == 0x51 and length == 3:
                    uspq = (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]
                    self.tempo_events.append(TempoEvent(tick=abs_tick, microseconds_per_quarter=uspq))
                    logger.debug(f"Tempo change at tick {abs_tick}: {uspq} us/quarter ({60000000 / uspq:.1f} BPM)")
                
                offset += length
                # Reset running status on meta events
                running_status = 0
                continue
            
            if status == 0xF0 or status == 0xF7:
                # SysEx event - skip it
                offset += 1
                length, offset = self._read_variable_length(data, offset)
                offset += length
                running_status = 0
                continue
            
            if status & 0x80:
                # New status byte
                running_status = status
                offset += 1
            # else: use running_status (data byte is the first parameter)
            
            if running_status == 0:
                continue
            
            message_type = running_status & 0xF0
            channel = running_status & 0x0F
            
            if message_type == 0x90:
                # Note On
                if offset + 1 >= end:
                    break
                note = data[offset]
                velocity = data[offset + 1]
                offset += 2
                
                is_note_on = velocity > 0
                if is_note_on:
                    self.events.append(MidiEvent(
                        tick=abs_tick,
                        channel=channel,
                        note=note,
                        velocity=velocity,
                        is_note_on=True
                    ))
                else:
                    # Velocity 0 note-on = note-off
                    self.events.append(MidiEvent(
                        tick=abs_tick,
                        channel=channel,
                        note=note,
                        velocity=0,
                        is_note_on=False
                    ))
            
            elif message_type == 0x80:
                # Note Off
                if offset + 1 >= end:
                    break
                note = data[offset]
                velocity = data[offset + 1]
                offset += 2
                
                self.events.append(MidiEvent(
                    tick=abs_tick,
                    channel=channel,
                    note=note,
                    velocity=velocity,
                    is_note_on=False
                ))
            
            elif message_type in (0xA0, 0xB0, 0xE0):
                # 2-data-byte messages (polyphonic key pressure, control change, pitch bend)
                offset += 2
            elif message_type in (0xC0, 0xD0):
                # 1-data-byte messages (program change, channel pressure)
                offset += 1
            else:
                # Unknown message type, skip
                offset += 1
        
        logger.debug(f"Track {track_num}: parsed {sum(1 for e in self.events if True)} events so far")
    
    def sort_events(self):
        """Sort all events by tick position. Required after parsing all tracks."""
        self.events.sort(key=lambda e: (e.tick, e.channel))
        self.tempo_events.sort(key=lambda e: e.tick)
    
    def _get_microseconds_per_quarter_at_tick(self, tick: int) -> int:
        """Get the tempo (microseconds per quarter note) at a given tick position."""
        if not self.tempo_events:
            return 500000  # Default 120 BPM
        # Find the last tempo event at or before this tick
        uspq = 500000
        for te in self.tempo_events:
            if te.tick <= tick:
                uspq = te.microseconds_per_quarter
            else:
                break
        return uspq
    
    def get_tick_at_time(self, elapsed_seconds: float) -> int:
        """
        Convert elapsed time in seconds to MIDI tick position.
        
        Handles tempo changes correctly by computing cumulative time
        across tempo segments, matching how SteamHapticsSinger's
        MidiFile_getTickFromTime works.
        """
        if not self.tempo_events:
            # No tempo changes - simple calculation
            default_uspq = 500000
            ticks_per_second = self.ticks_per_quarter * (1_000_000 / default_uspq)
            return int(elapsed_seconds * ticks_per_second)
        
        # Build a sorted list of tempo change points with cumulative times
        # Start with the initial tempo (before any tempo events)
        initial_uspq = self.tempo_events[0].microseconds_per_quarter if self.tempo_events[0].tick == 0 else 500000
        
        # Calculate cumulative seconds at each tempo change
        remaining_seconds = elapsed_seconds
        prev_tick = 0
        current_uspq = initial_uspq
        tempo_points = [(0, 500000)]  # (tick, uspq) - start with default
        
        # Merge default start with actual tempo events
        all_tempos = []
        if self.tempo_events[0].tick > 0:
            all_tempos.append(TempoEvent(tick=0, microseconds_per_quarter=500000))
        all_tempos.extend(self.tempo_events)
        
        for te in all_tempos:
            tick_delta = te.tick - prev_tick
            if tick_delta > 0 and current_uspq > 0:
                seconds_for_segment = (tick_delta * current_uspq) / (self.ticks_per_quarter * 1_000_000)
                if seconds_for_segment > remaining_seconds:
                    # The target time falls within this segment
                    ticks_into_segment = (remaining_seconds * self.ticks_per_quarter * 1_000_000) / current_uspq
                    return int(prev_tick + ticks_into_segment)
                remaining_seconds -= seconds_for_segment
            prev_tick = te.tick
            current_uspq = te.microseconds_per_quarter
        
        # Past the last tempo event - continue with the last tempo
        if remaining_seconds > 0 and current_uspq > 0:
            ticks_into_segment = (remaining_seconds * self.ticks_per_quarter * 1_000_000) / current_uspq
            return int(prev_tick + ticks_into_segment)
        
        return prev_tick
    
    def get_duration_seconds(self) -> float:
        """Get the total duration of the MIDI file in seconds, accounting for tempo changes."""
        if not self.events:
            return 0
        last_tick = max(e.tick for e in self.events)
        # Convert last tick to seconds by summing all tempo segments
        if not self.tempo_events:
            default_uspq = 500000
            return last_tick / (self.ticks_per_quarter * (1_000_000 / default_uspq))
        
        all_tempos = []
        if self.tempo_events[0].tick > 0:
            all_tempos.append(TempoEvent(tick=0, microseconds_per_quarter=500000))
        all_tempos.extend(self.tempo_events)
        
        total_seconds = 0.0
        prev_tick = 0
        current_uspq = 500000
        
        for te in all_tempos:
            tick_delta = min(te.tick, last_tick) - prev_tick
            if tick_delta > 0 and current_uspq > 0:
                total_seconds += (tick_delta * current_uspq) / (self.ticks_per_quarter * 1_000_000)
            if te.tick >= last_tick:
                break
            prev_tick = te.tick
            current_uspq = te.microseconds_per_quarter
        
        # Handle remaining ticks after last tempo event
        if last_tick > prev_tick and current_uspq > 0:
            total_seconds += ((last_tick - prev_tick) * current_uspq) / (self.ticks_per_quarter * 1_000_000)
        
        return total_seconds


class HapticsPlayer:
    """
    Plays MIDI files through Joy-Con 2 HD Rumble motors.
    
    This is the core playback engine, adapted from SteamHapticsSinger's
    playSong() function. It reads MIDI events in real-time and sends
    vibration commands to the connected Joy-Con controllers.
    
    Channel mapping (matching SteamHapticsSinger convention):
      MIDI Channel 0 -> Right Joy-Con motor
      MIDI Channel 1 -> Left Joy-Con motor
    """
    
    def __init__(self, controllers: list[Controller], interval_usec: int = None):
        """
        Args:
            controllers: List of connected Controller objects.
                         Index 0 should be left Joy-Con, index 1 should be right Joy-Con.
                         Can also work with a single Joy-Con.
            interval_usec: Playback loop interval in microseconds (default from config, or 10000).
                          Lower values give better fidelity but higher CPU usage.
        """
        self.controllers = controllers
        if interval_usec is None:
            interval_usec = CONFIG.haptics_singer_config.interval_usec
        self.interval_sec = interval_usec / 1_000_000
        self.use_lf_rumble = CONFIG.haptics_singer_config.use_lf_rumble
        self.default_amplitude = CONFIG.haptics_singer_config.default_amplitude
        self._playing = False
        self._stop_event = asyncio.Event()
        self._playback_thread: Optional[threading.Thread] = None
        self._status_callback: Optional[Callable[[str], None]] = None
        self._left_note: int = NOTE_STOP
        self._right_note: int = NOTE_STOP
    
    def set_status_callback(self, callback: Callable[[str], None]):
        """Set a callback to receive status updates (e.g., for GUI display)."""
        self._status_callback = callback
    
    def _update_status(self, text: str):
        if self._status_callback:
            self._status_callback(text)
    
    def _get_left_controller(self) -> Optional[Controller]:
        """Get the left Joy-Con controller, if available."""
        for c in self.controllers:
            if c.is_joycon_left():
                return c
        # Fall back to first controller if no left joy-con found
        return self.controllers[0] if self.controllers else None
    
    def _get_right_controller(self) -> Optional[Controller]:
        """Get the right Joy-Con controller, if available."""
        for c in self.controllers:
            if c.is_joycon_right():
                return c
        # Fall back to first controller if no right joy-con found
        return self.controllers[0] if self.controllers else None
    
    async def play_note(self, channel: int, note: int, velocity: int):
        """
        Send a haptic vibration command to the appropriate Joy-Con.
        
        Adapted from SteamHapticsSinger's SteamHaptics_PlayNote().
        For Joy-Con, we use the high-frequency rumble motor to produce
        tonal haptic feedback. The frequency is derived from the MIDI
        note number via the lookup table.
        
        Args:
            channel: MIDI channel (0=right, 1=left)
            note: MIDI note number (0-127), or NOTE_STOP (-1) to stop
            velocity: MIDI velocity (0-127), used to control amplitude
        """
        if channel < 0 or channel >= CHANNEL_COUNT:
            return
        
        controller = self._get_right_controller() if channel == 0 else self._get_left_controller()
        if controller is None or controller.client is None or not controller.client.is_connected:
            return
        
        vibration = VibrationData()
        
        if note == NOTE_STOP:
            # Stop vibration - set amplitude to 0
            vibration.lf_amp = 0
            vibration.hf_amp = 0
            vibration.lf_en_tone = False
            vibration.hf_en_tone = False
        else:
            # Map MIDI note to Joy-Con frequency
            freq = MIDI_TO_JOYCON_FREQ[note] if 0 <= note < 128 else 0
            
            # Map velocity to amplitude (0-1023 range)
            # Use a reasonable mapping that gives good haptic response
            if velocity > 0:
                amplitude = int(MAX_AMPLITUDE * velocity / 127)
            else:
                amplitude = self.default_amplitude
            # Clamp to safe range
            amplitude = max(0, min(amplitude, MAX_AMPLITUDE))
            
            # Use the HF motor for tonal playback (it's the one that can
            # produce distinct frequencies).
            vibration.hf_freq = freq
            vibration.hf_en_tone = True
            vibration.hf_amp = amplitude
            
            # Optionally add a subtle LF rumble component for richer feel
            if self.use_lf_rumble:
                vibration.lf_freq = max(1, freq // 4)
                vibration.lf_en_tone = True
                vibration.lf_amp = amplitude // 4
        
        try:
            await controller.set_vibration(vibration)
        except Exception as e:
            logger.debug(f"Vibration command error: {e}")
    
    async def stop_all(self):
        """Stop all haptic output on both Joy-Cons."""
        for i in range(CHANNEL_COUNT):
            await self.play_note(i, NOTE_STOP, 0)
        self._left_note = NOTE_STOP
        self._right_note = NOTE_STOP
    
    async def play_async(self, midi_file: MidiFile, repeat: bool = False):
        """
        Play a MIDI file asynchronously.
        
        Adapted from SteamHapticsSinger's playSong() function.
        Iterates through MIDI events in real-time based on elapsed wall-clock time,
        sending haptic commands for each note on/off event.
        """
        midi_file.sort_events()
        
        if not midi_file.events:
            self._update_status("MIDI file is empty!")
            return
        
        self._playing = True
        self._stop_event.clear()
        
        self._update_status(f"Playing: {Path(midi_file.filepath).name}")
        logger.info(f"Starting MIDI haptics playback: {midi_file.filepath}")
        logger.info(f"Total events: {len(midi_file.events)}, Duration: {midi_file.get_duration_seconds():.1f}s")
        
        # Track the last accepted event per channel (same logic as SteamHapticsSinger)
        accepted_event_per_channel = [None] * CHANNEL_COUNT
        
        # Time tracking
        t_origin = time.monotonic()
        
        # Iterate through all events
        event_index = 0
        total_events = len(midi_file.events)
        
        while event_index < total_events and self._playing:
            await asyncio.sleep(self.interval_sec)
            
            # Calculate current tick based on elapsed time
            elapsed = time.monotonic() - t_origin
            current_tick = midi_file.get_tick_at_time(elapsed)
            
            # Collect events to play for this iteration
            events_to_play = [None] * CHANNEL_COUNT
            
            # Process all events up to the current tick
            while event_index < total_events and midi_file.events[event_index].tick < current_tick:
                event = midi_file.events[event_index]
                
                # Skip non-note events
                if event.is_note_on is None:
                    event_index += 1
                    continue
                
                event_channel = event.channel
                
                # Only process channels we support (0 and 1)
                if event_channel < 0 or event_channel >= CHANNEL_COUNT:
                    event_index += 1
                    continue
                
                if event.is_note_on:
                    # Note-on event: accept it for this channel
                    events_to_play[event_channel] = event
                    accepted_event_per_channel[event_channel] = event
                else:
                    # Note-off event: only accept if it matches the last played note-on
                    previous_event = accepted_event_per_channel[event_channel]
                    if previous_event is not None and previous_event.note == event.note:
                        # Don't stop if both events are on the same tick
                        if event.tick != previous_event.tick:
                            events_to_play[event_channel] = event
                            accepted_event_per_channel[event_channel] = event
                
                event_index += 1
            
            # Send haptic commands for collected events
            for ch in range(CHANNEL_COUNT):
                selected_event = events_to_play[ch]
                if selected_event is None:
                    continue
                
                if selected_event.is_note_on:
                    await self.play_note(ch, selected_event.note, selected_event.velocity)
                    if ch == 0:
                        self._right_note = selected_event.note
                    else:
                        self._left_note = selected_event.note
                    self._update_display(ch, selected_event.note)
                else:
                    await self.play_note(ch, NOTE_STOP, 0)
                    if ch == 0:
                        self._right_note = NOTE_STOP
                    else:
                        self._left_note = NOTE_STOP
                    self._update_display(ch, NOTE_STOP)
        
        # Stop all notes when playback ends
        await self.stop_all()
        self._update_status("Playback completed")
        logger.info("MIDI haptics playback completed")
    
    def _update_display(self, channel: int, note: int):
        """Update the status display with currently playing notes (matching SteamHapticsSinger's display)."""
        right_name = note_to_display_name(self._right_note) if self._right_note != NOTE_STOP else "OFF"
        left_name = note_to_display_name(self._left_note) if self._left_note != NOTE_STOP else "OFF"
        self._update_status(f"RIGHT: {right_name:>5s}  |  LEFT: {left_name:>5s}")
    
    def play(self, midi_filepath: str, repeat: bool = False):
        """
        Start playing a MIDI file in a background thread.
        
        Args:
            midi_filepath: Path to the MIDI file to play.
            repeat: If True, loop the song continuously.
        """
        if self._playing:
            logger.warning("Already playing, stop current playback first")
            return
        
        # Load and parse the MIDI file
        try:
            midi_file = MidiFile(midi_filepath)
        except Exception as e:
            self._update_status(f"Error loading MIDI: {e}")
            logger.error(f"Failed to load MIDI file: {e}")
            return
        
        def run_playback():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._play_loop(midi_file, repeat))
            except Exception as e:
                logger.exception(f"Playback error: {e}")
                self._update_status(f"Error: {e}")
            finally:
                self._playing = False
        
        self._playback_thread = threading.Thread(target=run_playback, daemon=True)
        self._playback_thread.start()
    
    async def _play_loop(self, midi_file: MidiFile, repeat: bool):
        """Internal loop for repeat playback."""
        while self._playing:
            await self.play_async(midi_file, repeat=False)
            if not repeat or not self._playing:
                break
            # Reload the file for repeat (reset event state)
            try:
                midi_file = MidiFile(midi_file.filepath)
            except Exception:
                break
    
    def stop(self):
        """Stop the current playback."""
        self._playing = False
        self._stop_event.set()
        # Stop vibrations immediately by sending a stop command via a temporary loop
        # (the playback thread's loop is shutting down)
        async def stop_all():
            for c in self.controllers:
                if c.client and c.client.is_connected:
                    await c.set_vibration(VibrationData())
        try:
            asyncio.run(stop_all())
        except Exception:
            pass
        self._update_status("Stopped")
        logger.info("MIDI haptics playback stopped")
    
    @property
    def is_playing(self) -> bool:
        """Whether a MIDI file is currently being played."""
        return self._playing