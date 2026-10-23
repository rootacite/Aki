# aki-layer

Transparent full-screen canvas for Aki, implemented with Qt Quick and
LayerShellQt. It uses the layer-shell `overlay` layer, the highest layer
available, so it stays above normal application windows. The surface itself is
fully transparent; only notification bubbles in the top-right corner are visible.

The full-screen surface is not allowed to swallow desktop input. `aki-layer`
uses `QWindow::setMask()` (QtWayland maps this to
`wl_surface.set_input_region`) to expose only bubble rectangles; clicks anywhere
else pass through to desktop icons and other windows below.

The frontend also includes notification bubbles in the top-right corner:

- 15 second lifetime by default.
- Multiple bubbles stack downward.
- Hovering any bubble pauses every bubble's countdown and raises each remaining
  lifetime to at least 5 seconds.
- Each character has its own palette and silhouette artwork:
  - Aki: pink gradient with white autumn leaves.
  - Fuyu: light blue gradient with white snowflakes.
  - Haru: emerald green gradient with a paintbrush and headphones.
  - Natsu: red gradient with a mesh graph and geometric shapes.
- Bubble avatars are embedded from `assets/small/{aki,fuyu,haru,natsu}.jpg` and
  circle-cropped at draw time.
- Agent replies are split into display bubbles at blank lines using the same
  rules as tokifactor's `splitAgentParagraphs`: consecutive blank lines collapse,
  fenced Markdown code blocks stay intact, and text around a fence becomes its
  own paragraph. Each paragraph creates one notification bubble.

`akid` pushes final chat messages to these bubbles automatically. The
`--demo-bubbles N` option creates sample bubbles at startup for layout and hover
testing. The separate `--notify` option remains available for testing desktop
notifications.

## Control socket

`aki-layer` listens on `$XDG_RUNTIME_DIR/aki-layer.sock` (override with
`AKI_LAYER_SOCKET`). Each newline-delimited UTF-8 line creates one bubble. A
plain line defaults to Aki; use JSON for another character:

```json
{"character": "Fuyu", "text": "代码我看过了。"}
```

The server acknowledges each request with `queued <id>`; when that bubble
finishes its lifetime, it writes `dismissed <id>` back to every connected
client.

`akid` writes final chat messages to this socket automatically; notification
bubbles are not exposed to the model as a tool.

## Build

Requirements: Qt 6 (Quick, Quick Controls 2, DBus) and LayerShellQt.

```bash
cmake -S aki-layer -B aki-layer/build -G Ninja
cmake --build aki-layer/build
```

`akid` launches `aki-layer/build/aki-layer` automatically for the default Aki
profile when a Wayland session is available. Override the binary with
`AKI_LAYER_BIN`; disable the layer with `AKI_LAYER_ENABLED=0`.

## Manual run and notification smoke test

```bash
WAYLAND_DISPLAY=wayland-1 QT_QPA_PLATFORM=wayland aki-layer/build/aki-layer
WAYLAND_DISPLAY=wayland-1 QT_QPA_PLATFORM=wayland aki-layer/build/aki-layer --demo-bubbles 3
aki-layer/build/aki-layer --notify "hello"
```
