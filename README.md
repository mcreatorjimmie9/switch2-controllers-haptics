# switch2 controllers
An app to use switch 2 joycons on pc as gamepad, mouse, and MIDI haptics player.

### Usage

No need to pair the controller in the bluetooth settings.

Simply launch the app, and do what it says.

If you already paired the joycons in windows bluetooth settings, remove it before attempting to use it with this app.

### Using as a mouse

By default the app switches a joycon to mouse mode when it detects it's being used a mouse (side of of the joycon against a flat surface)

When in mouse mode, the following buttons are used as mouse buttons and no longer useable as gamepad buttons :
L/R : left click
ZL/ZR : right click
joystick : mouse wheel and middle button (click)

If you do not wish to use mouse mode, you can disable it in the config

### Using joycons sideways

By default, the app will always try to combine a right and left joycons together to make a single virtual controller.

If you wish to use both joycons sideway, you can hold SL\SR while turning them on
An other option is to set `combine_joycons` in the config to false so that the app will never try to combine joycons

### MIDI Haptics Singer

This feature allows you to play MIDI files through the Joy-Con 2 HD Rumble motors, similar to [SteamHapticsSinger](https://github.com/CrazyCritic89/SteamHapticsSinger) but for Nintendo Switch 2 Joy-Cons.

#### How to use

1. Connect your Joy-Con 2 controllers (left and/or right)
2. Click **Browse** in the "MIDI Haptics Singer" panel at the bottom of the window
3. Select a `.mid` or `.midi` file
4. Click **Play** to start haptic playback on your Joy-Cons
5. Click **Stop** to stop playback at any time
6. Toggle **Repeat** to loop the song continuously

#### MIDI Channel Mapping

- **MIDI Channel 0** -> Right Joy-Con haptic motor
- **MIDI Channel 1** -> Left Joy-Con haptic motor
- Channels 2-15 are ignored (each Joy-Con has one rumble motor)

#### MIDI File Tips

- Avoid multiple notes active at the same time on the same channel, since each Joy-Con motor can only produce one frequency at a time.
- Notes from MIDI channel 0 are played on the right Joy-Con, notes from channel 1 on the left.
- MIDI files may need to be edited with software such as [MidiEditor](https://www.midieditor.org/) for best results.
- Ready-to-play songs can be found in the [SteamHapticsSinger collection](https://mega.nz/#F!BWpEWKzB!r7WPw5bZ_domN4pk-FJsjg).

#### Configuration

Haptics singer settings can be adjusted in `resources/config.yaml`:

```yaml
haptics_singer:
  interval_usec: 10000       # Playback loop interval (lower = better fidelity, higher CPU)
  default_amplitude: 512     # Vibration strength (0-1023)
  use_lf_rumble: true        # Add subtle low-frequency rumble under the main tone
```
