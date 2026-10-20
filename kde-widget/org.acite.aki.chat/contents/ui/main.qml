import QtQuick
import QtQuick.Layouts
import QtWebSockets
import org.kde.kirigami as Kirigami
import org.kde.notification
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasmoid
import "../config/bridge-config.js" as BridgeConfig
import "../js/message-format.js" as MessageFormat

PlasmoidItem {
    id: root

    property bool handshaken: false
    property bool backendReady: false
    property bool busy: false
    property bool stopping: false
    property bool otherClientActive: false
    property string connectionState: "offline"
    property string lastError: ""
    property string activeTurnId: ""
    property string modelName: ""
    property string historyBefore: ""
    property bool historyHasMore: false
    property bool historyLoading: false
    property bool historyLoaded: false
    property int queuePosition: 0
    property int unreadCount: 0
    property int contextLimit: 0
    property int contextTokens: 0
    property int requestSeq: 0
    property int reconnectDelay: 1000
    property int streamAssistantIndex: -1
    property int streamReasoningIndex: -1
    property var pendingCalls: ({})
    property var pendingApprovals: []
    property var statusInfo: ({})
    property var lastUsage: ({})
    property string clientName: "aki-plasmoid-" + Math.random().toString(36).slice(2, 8)
    property alias chatModel: messageModel

    readonly property string bridgeUrl: BridgeConfig.url()

    function baseRow(item) {
        var row = {
            messageId: "",
            role: "",
            kind: "",
            text: "",
            reasoning: "",
            toolName: "",
            toolCallId: "",
            toolArgs: "",
            toolResult: "",
            status: "",
            timestamp: 0,
            streaming: false,
            collapsed: false,
            source: "",
            approvalId: "",
            toolInput: "",
            timeoutS: 0,
            turnId: ""
        };
        for (var key in item)
            row[key] = item[key];
        return row;
    }

    function appendModelItem(item) {
        chatModel.append(baseRow(item));
    }

    function insertModelItem(item) {
        chatModel.insert(0, baseRow(item));
    }

    function updateModelItem(index, patch) {
        if (index < 0 || index >= chatModel.count)
            return;
        for (var key in patch)
            chatModel.setProperty(index, key, patch[key]);
    }

    function findModelIndex(key, value) {
        for (var i = 0; i < chatModel.count; ++i) {
            if (chatModel.get(i)[key] === value)
                return i;
        }
        return -1;
    }

    function mergeToolFields(target, source) {
        if (source.toolName)
            target.toolName = source.toolName;
        if (source.toolArgs)
            target.toolArgs = source.toolArgs;
        if (source.toolResult)
            target.toolResult = source.toolResult;
        if (source.status)
            target.status = source.status;
        if (source.approvalId)
            target.approvalId = source.approvalId;
        if (source.toolInput)
            target.toolInput = source.toolInput;
    }

    function mergeHistoryItems(items) {
        var merged = [];
        for (var i = 0; i < items.length; ++i) {
            var item = items[i];
            var found = -1;
            if (item.kind === "tool" && item.toolCallId) {
                for (var j = 0; j < merged.length; ++j) {
                    if (merged[j].toolCallId === item.toolCallId) {
                        found = j;
                        break;
                    }
                }
            }
            if (found >= 0) {
                mergeToolFields(merged[found], item);
            } else {
                merged.push(item);
            }
        }
        return merged;
    }

    function appendHistoryItems(items, reset) {
        var merged = mergeHistoryItems(items);
        if (reset)
            chatModel.clear();
        if (reset) {
            for (var i = 0; i < merged.length; ++i)
                appendModelItem(merged[i]);
        } else {
            for (var j = merged.length - 1; j >= 0; --j)
                insertModelItem(merged[j]);
        }
    }

    function showRpcError(error) {
        var message = error && (error.message || error.code);
        lastError = message ? String(message) : "Aki 请求失败";
    }

    function call(method, params, callback) {
        if (socket.status !== WebSocket.Open) {
            if (callback)
                callback({ error: { message: "GUI 桥接未连接" } });
            return;
        }
        requestSeq += 1;
        var id = "gui-" + requestSeq;
        if (callback)
            pendingCalls[id] = callback;
        socket.sendTextMessage(JSON.stringify({
            jsonrpc: "2.0",
            id: id,
            method: method,
            params: params || {}
        }));
    }

    function handleSocketMessage(raw) {
        var message;
        try {
            message = JSON.parse(raw);
        } catch (error) {
            return;
        }
        if (message.method === "aki.event" && message.params) {
            handleEvent(message.params);
            return;
        }
        if (message.id !== undefined && message.id !== null) {
            var callback = pendingCalls[message.id];
            delete pendingCalls[message.id];
            if (callback)
                callback(message);
        }
    }

    function handleSocketStatus(status) {
        if (status === WebSocket.Open) {
            connectionState = "handshaking";
            reconnectDelay = 1000;
            lastError = "";
            sendHello();
        } else if (status === WebSocket.Connecting) {
            connectionState = "connecting";
        } else if (status === WebSocket.Closing) {
            connectionState = "offline";
        } else if (status === WebSocket.Closed || status === WebSocket.Error) {
            handleDisconnect();
        }
    }

    function handleDisconnect() {
        handshaken = false;
        backendReady = false;
        busy = false;
        connectionState = "offline";
        if (socket.errorString)
            lastError = socket.errorString;
        for (var id in pendingCalls) {
            var callback = pendingCalls[id];
            delete pendingCalls[id];
            if (callback)
                callback({ error: { message: "GUI 桥接已断开" } });
        }
        socket.active = false;
        scheduleReconnect();
    }

    function scheduleReconnect() {
        if (reconnectTimer.running)
            return;
        reconnectTimer.interval = reconnectDelay;
        reconnectTimer.start();
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    }

    function startBridge() {
        if (socket.active)
            return;
        if (!BridgeConfig.token) {
            connectionState = "offline";
            lastError = "GUI 桥接 token 未生成，请运行 install-widget.py";
            return;
        }
        connectionState = "connecting";
        lastError = "";
        socket.url = bridgeUrl;
        socket.active = true;
    }

    function reconnect() {
        socket.active = false;
        reconnectDelay = 1000;
        startBridge();
    }

    function sendHello() {
        call("aki.hello", {
            client_name: clientName,
            client_version: "0.1.0",
            protocol_version: 1
        }, function(message) {
            if (message.error) {
                showRpcError(message.error);
                connectionState = "offline";
                socket.active = false;
                return;
            }
            handshaken = true;
            connectionState = "ready";
            loadStatus();
            if (!busy)
                loadHistory(!historyLoaded);
        });
    }

    function loadStatus(updateBusy) {
        call("aki.status", {}, function(message) {
            if (message.error)
                return;
            var info = message.result || {};
            statusInfo = info;
            modelName = info.model || "";
            contextLimit = info.context_limit || 0;
            lastUsage = info.last_usage || ({});
            contextTokens = lastUsage.context_tokens || 0;
            backendReady = !!info.backend_ready;
            otherClientActive = !!(info.active_turn && info.active_turn.client_name !== clientName);
            if (updateBusy !== false && info.active_turn && info.active_turn.client_name === clientName) {
                busy = true;
                activeTurnId = info.active_turn.turn_id || "";
            }
        });
    }

    function loadHistory(reset) {
        if (historyLoading)
            return;
        historyLoading = true;
        var params = { limit: 50, order: "desc" };
        if (!reset && historyBefore)
            params.before = historyBefore;
        call("aki.chat.history", params, function(message) {
            historyLoading = false;
            if (message.error) {
                showRpcError(message.error);
                return;
            }
            var result = message.result || {};
            var source = result.messages || [];
            var items = [];
            for (var i = source.length - 1; i >= 0; --i) {
                var item = MessageFormat.normalizeMessage(source[i]);
                if (item)
                    items.push(item);
            }
            appendHistoryItems(items, reset);
            historyBefore = result.next_before || "";
            historyHasMore = !!result.has_more;
            historyLoaded = true;
        });
    }

    function loadOlderHistory() {
        if (historyHasMore && !historyLoading)
            loadHistory(false);
    }

    function sendInput(text) {
        if (!handshaken || !backendReady || !text)
            return;
        var clientMessageId = "gui-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
        appendModelItem({
            messageId: clientMessageId,
            role: "user",
            kind: "text",
            text: text,
            timestamp: Date.now(),
            source: "local"
        });
        call("aki.chat.send", {
            message: text,
            client_message_id: clientMessageId,
            include_reasoning: true
        }, function(message) {
            if (message.error) {
                showRpcError(message.error);
                var index = findModelIndex("messageId", clientMessageId);
                if (index >= 0)
                    chatModel.remove(index);
                return;
            }
            var result = message.result || {};
            activeTurnId = result.turn_id || "";
            busy = true;
            queuePosition = result.state === "queued" ? (result.queue_position || 0) : 0;
        });
    }

    function cancelTurn() {
        if (!busy || stopping)
            return;
        stopping = true;
        var params = {};
        if (activeTurnId)
            params.turn_id = activeTurnId;
        call("aki.chat.cancel", params, function(message) {
            if (message.error) {
                stopping = false;
                showRpcError(message.error);
            }
        });
    }

    function ensureAssistantItem(text) {
        if (streamAssistantIndex < 0 || streamAssistantIndex >= chatModel.count) {
            appendModelItem({
                role: "assistant",
                kind: "text",
                text: "",
                timestamp: Date.now(),
                streaming: true,
                turnId: activeTurnId
            });
            streamAssistantIndex = chatModel.count - 1;
        }
        var current = chatModel.get(streamAssistantIndex);
        updateModelItem(streamAssistantIndex, {
            text: (current.text || "") + text,
            streaming: true,
            turnId: activeTurnId
        });
    }

    function ensureReasoningItem(text) {
        if (streamReasoningIndex < 0 || streamReasoningIndex >= chatModel.count) {
            appendModelItem({
                role: "assistant",
                kind: "reasoning",
                reasoning: "",
                timestamp: Date.now(),
                streaming: true,
                collapsed: true,
                turnId: activeTurnId
            });
            streamReasoningIndex = chatModel.count - 1;
        }
        var current = chatModel.get(streamReasoningIndex);
        updateModelItem(streamReasoningIndex, {
            reasoning: (current.reasoning || "") + text,
            streaming: true,
            turnId: activeTurnId
        });
    }

    function finalizeAssistant(content) {
        if (content) {
            if (streamAssistantIndex >= 0 && streamAssistantIndex < chatModel.count) {
                updateModelItem(streamAssistantIndex, {
                    text: content,
                    streaming: false
                });
            } else {
                appendModelItem({
                    role: "assistant",
                    kind: "text",
                    text: content,
                    timestamp: Date.now(),
                    turnId: activeTurnId
                });
            }
        } else if (streamAssistantIndex >= 0) {
            updateModelItem(streamAssistantIndex, { streaming: false });
        }
        if (streamReasoningIndex >= 0)
            updateModelItem(streamReasoningIndex, { streaming: false });
        streamAssistantIndex = -1;
        streamReasoningIndex = -1;
    }

    function upsertTool(toolCallId, name, args, status) {
        var index = findModelIndex("toolCallId", toolCallId);
        if (index >= 0) {
            var patch = { status: status };
            if (name)
                patch.toolName = name;
            if (args !== undefined && args !== null)
                patch.toolArgs = args;
            updateModelItem(index, patch);
            return;
        }
        appendModelItem({
            role: "tool",
            kind: "tool",
            toolName: name || "tool",
            toolCallId: toolCallId,
            toolArgs: args,
            status: status,
            timestamp: Date.now(),
            turnId: activeTurnId
        });
    }

    function updateToolResult(toolCallId, result, status) {
        var index = findModelIndex("toolCallId", toolCallId);
        if (index >= 0) {
            updateModelItem(index, {
                toolResult: result || "",
                status: status || "success"
            });
        } else {
            appendModelItem({
                role: "tool",
                kind: "tool",
                toolCallId: toolCallId,
                toolResult: result || "",
                status: status || "success",
                timestamp: Date.now(),
                turnId: activeTurnId
            });
        }
    }

    function addPendingApproval(item) {
        var next = [];
        for (var i = 0; i < pendingApprovals.length; ++i) {
            if (pendingApprovals[i].approvalId !== item.approvalId)
                next.push(pendingApprovals[i]);
        }
        next.push(item);
        pendingApprovals = next;
    }

    function removePendingApproval(approvalId) {
        var next = [];
        for (var i = 0; i < pendingApprovals.length; ++i) {
            if (pendingApprovals[i].approvalId !== approvalId)
                next.push(pendingApprovals[i]);
        }
        pendingApprovals = next;
        if (pendingApprovals.length === 0)
            approvalNotification.close();
    }

    function addApproval(data) {
        var item = {
            role: "tool",
            kind: "approval",
            approvalId: data.approval_id || "",
            toolCallId: data.tool_call_id || "",
            toolName: data.tool_name || "tool",
            toolInput: data.input,
            status: "pending",
            timeoutS: data.timeout_s || 60,
            timestamp: Date.now(),
            turnId: activeTurnId
        };
        var existing = findModelIndex("approvalId", item.approvalId);
        if (existing >= 0)
            updateModelItem(existing, item);
        else
            appendModelItem(item);
        addPendingApproval(item);
        if (!Plasmoid.expanded) {
            unreadCount += 1;
            notifyApproval(item);
        }
    }

    function respondApproval(approvalId, decision, reason) {
        var params = { approval_id: approvalId, decision: decision };
        if (reason)
            params.message = reason;
        call("aki.approval.respond", params, function(message) {
            if (message.error) {
                showRpcError(message.error);
                return;
            }
            var index = findModelIndex("approvalId", approvalId);
            updateModelItem(index, { status: decision === "allow" ? "allowed" : "denied" });
            removePendingApproval(approvalId);
        });
    }

    function notifyApproval(item) {
        approvalNotification.title = "Aki 需要你的许可";
        approvalNotification.text = (item.toolName || "tool") + "\n"
                                    + MessageFormat.shortToolArgs(item.toolInput, 220);
        approvalNotification.sendEvent();
    }

    function addNotice(text) {
        appendModelItem({
            role: "system",
            kind: "notice",
            text: text,
            timestamp: Date.now(),
            turnId: activeTurnId
        });
    }

    function finishTurn(data) {
        busy = false;
        stopping = false;
        queuePosition = 0;
        if (streamAssistantIndex >= 0)
            updateModelItem(streamAssistantIndex, { streaming: false });
        if (streamReasoningIndex >= 0)
            updateModelItem(streamReasoningIndex, { streaming: false });
        streamAssistantIndex = -1;
        streamReasoningIndex = -1;
        for (var i = 0; i < pendingApprovals.length; ++i) {
            var index = findModelIndex("approvalId", pendingApprovals[i].approvalId);
            updateModelItem(index, { status: "expired" });
        }
        pendingApprovals = [];
        approvalNotification.close();
        if (!Plasmoid.expanded)
            unreadCount += 1;
        activeTurnId = "";
        historyLoaded = false;
        loadStatus(false);
        if (data && data.reason && data.reason !== "completed" && data.reason !== "cancelled")
            addNotice("turn 结束：" + data.reason);
    }

    function handleEvent(params) {
        var type = params.type || "";
        var data = params.data || {};
        if (type === "turn_queued") {
            busy = true;
            queuePosition = data.queue_position || 0;
        } else if (type === "turn_started") {
            busy = true;
            queuePosition = 0;
            otherClientActive = false;
            if (params.turn_id)
                activeTurnId = params.turn_id;
            streamAssistantIndex = -1;
            streamReasoningIndex = -1;
        } else if (type === "assistant_delta") {
            var text = data.text || "";
            if (data.reasoning)
                ensureReasoningItem(text);
            else
                ensureAssistantItem(text);
        } else if (type === "assistant_message") {
            finalizeAssistant(data.content || data.text || "");
        } else if (type === "tool_call") {
            upsertTool(data.tool_call_id || "", data.name || "tool", data.arguments, "running");
        } else if (type === "tool_result") {
            updateToolResult(data.tool_call_id || "", data.tool_return || "", data.status || "success");
        } else if (type === "approval_request") {
            addApproval(data);
        } else if (type === "usage") {
            lastUsage = data;
            contextTokens = data.context_tokens || contextTokens;
        } else if (type === "compaction") {
            addNotice("上下文已压缩");
        } else if (type === "error") {
            var message = data.message || data.code || "unknown error";
            lastError = String(message);
            addNotice("错误：" + message);
        } else if (type === "turn_end") {
            finishTurn(data);
        }
    }

    function onPopupOpened() {
        unreadCount = 0;
        if (!historyLoaded && !busy)
            loadHistory(true);
        if (handshaken)
            loadStatus();
    }

    ListModel { id: messageModel }

    WebSocket {
        id: socket
        active: false
        onStatusChanged: root.handleSocketStatus(socket.status)
        onTextMessageReceived: (message) => root.handleSocketMessage(message)
        onErrorStringChanged: {
            if (socket.errorString)
                root.lastError = socket.errorString;
        }
    }

    Timer {
        id: reconnectTimer
        repeat: false
        onTriggered: root.startBridge()
    }

    Notification {
        id: approvalNotification
        componentName: "akid"
        eventId: "approval-request"
        title: "Aki 需要你的许可"
        iconName: "dialog-warning"
        urgency: Notification.HighUrgency
        flags: Notification.Persistent
        autoDelete: false
        defaultAction: NotificationAction {
            label: "打开 Aki"
            onActivated: root.expanded = true
        }
    }

    Plasmoid.title: "Aki"
    Plasmoid.icon: "dialog-messages"
    Plasmoid.backgroundHints: PlasmaCore.Types.DefaultBackground
    Plasmoid.status: pendingApprovals.length > 0
                     ? PlasmaCore.Types.NeedsAttentionStatus
                     : (busy ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus)
    toolTipMainText: "Aki"
    toolTipSubText: connectionState === "ready"
                    ? (backendReady ? "在线" : "Letta 正在启动")
                    : "离线"

    compactRepresentation: CompactRepresentation {
        plasmoidItem: root
        busy: root.busy
        attention: root.pendingApprovals.length > 0
        unreadCount: root.unreadCount
    }

    fullRepresentation: FullRepresentation {
        chatRoot: root
    }

    Component.onCompleted: startBridge()
}
