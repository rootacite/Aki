.pragma library
// Replaced by install-widget.py with a per-user token and bridge port.
var host = "127.0.0.1";
var port = 8765;
var token = "";
function url() {
    return "ws://" + host + ":" + port + "/?token=" + encodeURIComponent(token);
}
