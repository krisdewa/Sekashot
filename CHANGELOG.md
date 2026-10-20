# Changelog

All notable changes to Sekashot are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-20

First public release.

### Added

- Tray-resident screenshot app: `Print Screen` captures, the tray holds the menu.
- Frozen, dimmed full-screen overlay with a live `W x H` readout, marching-ants
  selection, 8 resize handles, arrow-key nudge and `Ctrl+A`.
- Annotation tools: pen, line, arrow, rectangle, marker, text, blur/redact,
  colour picker and undo.
- Action toolbar: copy, save, close — plus auto-save and a shutter sound.
- Screen capture backends: `grim`, the XDG desktop portal, and an X11
  root-window grab.
- Global hotkey: grabbed directly on X11, installed as a compositor keybind on
  niri, and printed as a paste-ready snippet elsewhere.
- Desktop notifications that name the file a capture was saved to.
- CLI: `sekashot --capture`, `--full`, `--settings`, `--install-hotkey`.
- Debian package with menu entry, autostart entry and icons.
- `tools/quality_test.py`, a pixel-loss audit: it captures the screen, shrinks
  the overlay to half size, exports a selection and measures the exported image
  against the captured region. It also compares grim's PPM output against its
  PNG output and checks that the clipboard payload is lossless.

### Fixed

- **One key press no longer opens two overlays on X11.** The grab also delivers
  the key release, and the polling loop fired the capture callback for whatever
  event came next — so the release triggered a second capture. The loop now
  only reacts to `KeyPress`.
- **The X11 hotkey no longer claims a key it does not have.** `XGrabKey` does
  not fail at the call: when another client already owns the key — which is
  what GNOME, KDE, XFCE and Cinnamon do with `Print Screen` — the server
  answers later, at `sync()` time, with `BadAccess`, and the default error
  handler only prints it. The grab therefore looked successful while it was
  not, and `Print Screen` silently did nothing. The errors are collected now,
  and the combinations that really work (`Alt+Print`, `Super+Print`,
  `Alt+Super+Print`) are named in the notification.
- **The X11 grab keeps the panel's native resolution.** `QScreen.grabWindow()`
  returns device pixels, so on a HiDPI screen a 3840×2160 monitor produced a
  3840×2160 image even though its logical geometry is 1920×1080. The canvas was
  sized from the logical geometry, so the grab was silently downscaled by the
  scale factor and the export came out at half resolution. The canvas is now
  built in device pixels, and the pixmap's device-pixel ratio is dropped before
  it is copied in, so the pixels are transferred 1:1.
- **A failed capture is no longer shown as a screenshot.** The Wayland path fell
  through to the X11 grab, which does not fail loudly — it returns a black
  rectangle — so a missing capture helper produced an unexplained black overlay.
  Wayland now reports the missing helper, and the X11 path checks for an
  all-black frame, which is what a server without a compositing manager
  produces. `allow_black_capture` in the config opts back in.
- **A missing capture helper is now visible.** A keybind-spawned run reported
  failures on stderr, which the user never sees; it now posts a notification.
- **Copy and Save are no longer silent without a tray icon.** The tray message
  was a no-op exactly when it was needed, so notifications now fall back through
  D-Bus → `notify-send` → tray → stderr.
- **A running daemon is never left unreachable.** `QLocalServer.removeServer()`
  unlinks the socket unconditionally, even when a live instance owns it. A new
  instance probes the endpoint first and only removes a genuinely stale socket.
- **A copied image survives the process.** Qt's clipboard dies when the process
  exits, so the PNG is handed to `wl-copy` / `xclip`.
- **The save dialog is no longer hidden behind the overlay.** The overlay is
  hidden before the dialog opens.

### Notes on the implementation

- *One window, not five.* Wayland clients cannot position top-level windows or
  create true overlays, so the screenshot, dim mask, selection, both toolbars
  and the size badge are painted inside a single full-screen widget.
- *Annotations live in image coordinates.* The widget scales the screenshot to
  fit the output (HiDPI, fractional scaling, multi-monitor), while annotation
  points are stored in native screenshot pixels — so the exported PNG is
  pixel-exact regardless of display scaling. The export path crops from the
  original capture rather than from the scaled widget, and the device pixel
  ratio is normalised to 1.
- *The hot path loads no Qt.* A keybind spawns `sekashot --capture`, which
  answers the running daemon over an `AF_UNIX` socket before PyQt6 is even
  imported. grim is asked for raw PPM rather than PNG, and the tray-settle pause
  before the grab was cut from 250 ms to 80 ms.
- *Hotkeys on X11 need a second chance.* The desktop environment very often
  owns `Print Screen` already, so the app tries the plain key first and then
  the same key with `Alt`, `Mod3`, `Super` and `Alt+Super`, and reports the
  ones that actually landed.
- *Sound is built lazily.* QtMultimedia is kept off the path between startup and
  a published endpoint, so a missing multimedia backend disables the click
  rather than the capture.
