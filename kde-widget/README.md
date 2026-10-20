# Legacy Aki Plasma widget

This Plasma 6 applet and `engine/gui_bridge.py` are **legacy**. They are kept
for existing installations and reference, but they are no longer the
recommended GUI.

Use **tokifactor** for new desktop and Android clients. tokifactor discovers Aki
on a shared `(broker host, port, magic)` channel and shows an Aki-specific
status bar only when Aki answers a control probe. Plain chat remains available
when no Aki is present.

The installer refuses to install the widget unless `--allow-deprecated` is
passed explicitly:

```bash
python3 install-widget.py --allow-deprecated
python3 install-widget.py --status
python3 install-widget.py --uninstall
```

`--status` and `--uninstall` still work without the flag. Uninstalling the
widget leaves `akid.service`, MemFS, and chat history untouched.
