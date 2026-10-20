import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras

Item {
    id: full
    property var chatRoot
    Layout.preferredWidth: 420
    Layout.preferredHeight: 640
    Layout.minimumWidth: 360
    Layout.minimumHeight: 480

    Component.onCompleted: chatRoot.onPopupOpened()
    onVisibleChanged: {
        if (visible)
            chatRoot.onPopupOpened();
    }

    function statusText() {
        if (chatRoot.otherClientActive)
            return "其他客户端正在对话";
        if (chatRoot.connectionState === "ready")
            return chatRoot.backendReady ? "在线" : "Letta 正在启动";
        if (chatRoot.connectionState === "connecting" || chatRoot.connectionState === "handshaking")
            return "连接中…";
        return "离线";
    }

    function statusColor() {
        if (chatRoot.connectionState === "ready" && chatRoot.backendReady)
            return Kirigami.Theme.positiveTextColor;
        if (chatRoot.connectionState === "offline")
            return Kirigami.Theme.negativeTextColor;
        return Kirigami.Theme.disabledTextColor;
    }

    function submitInput() {
        var text = input.text.trim();
        if (!text)
            return;
        input.text = "";
        chatRoot.sendInput(text);
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: headerLayout.implicitHeight + Kirigami.Units.largeSpacing * 2
            color: Kirigami.Theme.backgroundColor

            RowLayout {
                id: headerLayout
                anchors.fill: parent
                anchors.margins: Kirigami.Units.largeSpacing
                spacing: Kirigami.Units.smallSpacing

                Rectangle {
                    width: Kirigami.Units.iconSizes.medium
                    height: width
                    radius: width / 2
                    clip: true
                    color: Kirigami.Theme.alternateBackgroundColor
                    Image {
                        anchors.fill: parent
                        source: Qt.resolvedUrl("../images/aki_avi.jpg")
                        fillMode: Image.PreserveAspectCrop
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    Text {
                        text: "Aki"
                        font.bold: true
                        color: Kirigami.Theme.textColor
                    }
                    RowLayout {
                        spacing: Kirigami.Units.smallSpacing
                        Rectangle {
                            width: Kirigami.Units.smallSpacing
                            height: width
                            radius: width / 2
                            color: full.statusColor()
                        }
                        Text {
                            text: full.statusText()
                            color: Kirigami.Theme.disabledTextColor
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                        Text {
                            visible: (chatRoot.modelName || "").length > 0
                            text: "· " + chatRoot.modelName
                            color: Kirigami.Theme.disabledTextColor
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                    }
                }

                PlasmaComponents.ProgressBar {
                    visible: chatRoot.contextLimit > 0
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 4
                    from: 0
                    to: chatRoot.contextLimit
                    value: chatRoot.contextTokens
                }

                PlasmaComponents.ToolButton {
                    visible: chatRoot.connectionState !== "ready"
                    icon.name: "view-refresh"
                    onClicked: chatRoot.reconnect()
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            visible: (chatRoot.lastError || "").length > 0
            implicitHeight: errorLayout.implicitHeight + Kirigami.Units.smallSpacing * 2
            color: Kirigami.Theme.alternateBackgroundColor
            RowLayout {
                id: errorLayout
                anchors.fill: parent
                anchors.margins: Kirigami.Units.smallSpacing
                Kirigami.Icon {
                    source: "dialog-warning"
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: Kirigami.Units.iconSizes.small
                }
                Text {
                    Layout.fillWidth: true
                    text: chatRoot.lastError
                    color: Kirigami.Theme.negativeTextColor
                    wrapMode: Text.Wrap
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                }
                PlasmaComponents.ToolButton {
                    icon.name: "view-refresh"
                    onClicked: chatRoot.reconnect()
                }
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ListView {
                id: messageList
                anchors.fill: parent
                anchors.margins: Kirigami.Units.smallSpacing
                property bool atBottom: true
                clip: true
                model: chatRoot.chatModel
                spacing: Kirigami.Units.smallSpacing
                boundsBehavior: Flickable.StopAtBounds
                reuseItems: true
                cacheBuffer: 2000

                delegate: ChatMessage {
                    itemData: model
                    onApprovalAllowRequested: (approvalId, reason) => chatRoot.respondApproval(approvalId, "allow", reason)
                    onApprovalDenyRequested: (approvalId, reason) => chatRoot.respondApproval(approvalId, "deny", reason)
                }

                header: Item {
                    width: ListView.view ? ListView.view.width : 0
                    height: loadOlderButton.visible ? loadOlderButton.implicitHeight + Kirigami.Units.smallSpacing : 0
                    PlasmaComponents.Button {
                        id: loadOlderButton
                        anchors.centerIn: parent
                        visible: chatRoot.historyHasMore
                        text: chatRoot.historyLoading ? "加载中…" : "加载更早消息"
                        enabled: !chatRoot.historyLoading
                        onClicked: chatRoot.loadOlderHistory()
                    }
                }

                onCountChanged: {
                    if (atBottom)
                        Qt.callLater(positionViewAtEnd);
                }
                onContentYChanged: {
                    atBottom = (contentY + height >= contentHeight - Kirigami.Units.largeSpacing);
                }
            }

            PlasmaExtras.PlaceholderMessage {
                visible: chatRoot.chatModel.count === 0
                anchors.centerIn: parent
                width: parent.width - Kirigami.Units.gridUnit * 4
                iconName: "dialog-messages"
                text: "和 Aki 说点什么吧"
                explanation: "消息会进入当前的 default 会话。"
            }

            PlasmaComponents.Button {
                visible: !messageList.atBottom && chatRoot.chatModel.count > 0
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: Kirigami.Units.smallSpacing * 2
                text: "跳到最新"
                icon.name: "go-bottom"
                onClicked: messageList.positionViewAtEnd()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: composerLayout.implicitHeight + Kirigami.Units.smallSpacing * 2
            color: Kirigami.Theme.backgroundColor

            ColumnLayout {
                id: composerLayout
                anchors.fill: parent
                anchors.margins: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.smallSpacing

                Text {
                    Layout.fillWidth: true
                    visible: chatRoot.busy
                    text: chatRoot.queuePosition > 0
                          ? "排队中 · 第 " + chatRoot.queuePosition + " 位"
                          : "Aki 正在回复…"
                    color: Kirigami.Theme.disabledTextColor
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                }

                PlasmaComponents.TextArea {
                    id: input
                    Layout.fillWidth: true
                    Layout.preferredHeight: Kirigami.Units.gridUnit * 3
                    enabled: chatRoot.handshaken && chatRoot.backendReady
                    placeholderText: enabled ? "和 Aki 说点什么…" : "等待 Aki 连接…"
                    wrapMode: TextEdit.Wrap
                    Keys.onPressed: (event) => {
                        if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                                && !(event.modifiers & Qt.ShiftModifier)) {
                            event.accepted = true;
                            submitInput();
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: "Enter 发送 · Shift+Enter 换行"
                        color: Kirigami.Theme.disabledTextColor
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        elide: Text.ElideRight
                    }
                    PlasmaComponents.Button {
                        visible: chatRoot.busy
                        text: "停止"
                        icon.name: "process-stop"
                        onClicked: chatRoot.cancelTurn()
                    }
                    PlasmaComponents.Button {
                        text: "发送"
                        icon.name: "document-send"
                        enabled: chatRoot.handshaken && chatRoot.backendReady && input.text.trim().length > 0
                        onClicked: submitInput()
                    }
                }
            }
        }
    }
}
