.pragma library

function contentText(content) {
    if (typeof content === "string")
        return content;
    if (!Array.isArray(content))
        return "";
    var parts = [];
    for (var i = 0; i < content.length; ++i) {
        var block = content[i];
        if (typeof block === "string") {
            parts.push(block);
        } else if (block && block.type === "text" && typeof block.text === "string") {
            parts.push(block.text);
        }
    }
    return parts.join("");
}

function toStringValue(value) {
    if (value === undefined || value === null)
        return "";
    if (typeof value === "string")
        return value;
    if (Array.isArray(value)) {
        var text = contentText(value);
        if (text)
            return text;
        try {
            return JSON.stringify(value);
        } catch (error) {
            return String(value);
        }
    }
    if (typeof value === "object") {
        try {
            return JSON.stringify(value, null, 2);
        } catch (error) {
            return String(value);
        }
    }
    return String(value);
}

function stripSystemReminders(text) {
    if (!text)
        return "";
    return text
        .replace(/<system-reminder>[\s\S]*?<\/system-reminder>/gi, "")
        .replace(/^\s+|\s+$/g, "");
}

function parseTimestamp(value) {
    var parsed = Date.parse(value || "");
    return isNaN(parsed) ? Date.now() : parsed;
}

function isMqttText(text) {
    return text.indexOf("[akid MQTT channel message]") === 0;
}

function normalizeMessage(message) {
    if (!message || typeof message !== "object")
        return null;
    var type = message.message_type || message.role || "";
    var timestamp = parseTimestamp(message.date);
    if (type === "assistant_message") {
        return {
            messageId: message.id || "",
            role: "assistant",
            kind: "text",
            text: contentText(message.content),
            timestamp: timestamp,
            streaming: false,
            collapsed: false,
            source: ""
        };
    }
    if (type === "reasoning_message") {
        return {
            messageId: message.id || "",
            role: "assistant",
            kind: "reasoning",
            reasoning: toStringValue(message.reasoning),
            timestamp: timestamp,
            streaming: false,
            collapsed: true,
            source: ""
        };
    }
    if (type === "user_message") {
        var text = stripSystemReminders(contentText(message.content));
        if (!text)
            return null;
        return {
            messageId: message.id || "",
            role: "user",
            kind: "text",
            text: text,
            timestamp: timestamp,
            streaming: false,
            collapsed: false,
            source: isMqttText(text) ? "mqtt" : "local"
        };
    }
    if (type === "tool_call_message" || type === "approval_request_message") {
        var call = message.tool_call || {};
        return {
            messageId: message.id || "",
            role: "tool",
            kind: "tool",
            toolName: toStringValue(call.name || "unknown"),
            toolCallId: toStringValue(call.tool_call_id || call.id || ""),
            toolArgs: formatToolArgs(call.arguments),
            toolResult: "",
            status: "requested",
            timestamp: timestamp,
            streaming: false,
            collapsed: false
        };
    }
    if (type === "tool_return_message") {
        return {
            messageId: message.id || "",
            role: "tool",
            kind: "tool",
            toolName: toStringValue(message.tool_name || ""),
            toolCallId: toStringValue(message.tool_call_id || ""),
            toolArgs: "",
            toolResult: toolResultText(message.tool_return),
            status: toStringValue(message.status || "success"),
            timestamp: timestamp,
            streaming: false,
            collapsed: false
        };
    }
    if (type === "summary_message") {
        return {
            messageId: message.id || "",
            role: "system",
            kind: "notice",
            text: "上下文已压缩",
            timestamp: timestamp,
            streaming: false,
            collapsed: false
        };
    }
    if (type === "event_message") {
        return {
            messageId: message.id || "",
            role: "system",
            kind: "notice",
            text: message.event_type || "event",
            timestamp: timestamp,
            streaming: false,
            collapsed: false
        };
    }
    return null;
}

function toolResultText(value) {
    if (value === undefined || value === null)
        return "";
    if (typeof value === "string")
        return value;
    if (Array.isArray(value)) {
        var text = contentText(value);
        if (text)
            return text;
    }
    return formatToolArgs(value);
}

function parseMarkdownBlocks(text) {
    var source = text || "";
    var blocks = [];
    var fence = "```";
    var offset = 0;
    while (offset < source.length) {
        var start = source.indexOf(fence, offset);
        if (start < 0) {
            if (offset < source.length)
                blocks.push({ kind: "text", text: source.slice(offset) });
            break;
        }
        if (start > offset)
            blocks.push({ kind: "text", text: source.slice(offset, start) });
        var lineEnd = source.indexOf("\n", start + fence.length);
        if (lineEnd < 0) {
            blocks.push({ kind: "code", lang: source.slice(start + fence.length).trim(), text: "" });
            break;
        }
        var lang = source.slice(start + fence.length, lineEnd).trim();
        var end = source.indexOf(fence, lineEnd + 1);
        if (end < 0) {
            blocks.push({ kind: "code", lang: lang, text: source.slice(lineEnd + 1) });
            break;
        }
        blocks.push({ kind: "code", lang: lang, text: source.slice(lineEnd + 1, end) });
        offset = end + fence.length;
    }
    if (blocks.length === 0)
        blocks.push({ kind: "text", text: source });
    return blocks;
}

function formatToolArgs(value) {
    if (value === undefined || value === null || value === "")
        return "";
    var parsed = value;
    if (typeof value === "string") {
        try {
            parsed = JSON.parse(value);
        } catch (error) {
            return value;
        }
    }
    try {
        return JSON.stringify(parsed, null, 2);
    } catch (error) {
        return String(value);
    }
}

function shortToolArgs(value, limit) {
    var rendered = formatToolArgs(value).replace(/\s+/g, " ").trim();
    var max = limit || 240;
    return rendered.length > max ? rendered.slice(0, max) + "…" : rendered;
}

function formatTime(timestamp) {
    var date = new Date(timestamp || Date.now());
    var hours = date.getHours();
    var minutes = date.getMinutes();
    return (hours < 10 ? "0" : "") + hours + ":" + (minutes < 10 ? "0" : "") + minutes;
}
