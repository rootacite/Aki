import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import "../js/message-format.js" as MessageFormat

Rectangle {
    id: root
    property var itemData: ({})
    property bool pending: itemData.status === "pending"
    property int timeoutS: itemData.timeoutS || 60
    property double deadline: Date.now() + timeoutS * 1000
    property int remaining: Math.max(0, Math.ceil((deadline - Date.now()) / 1000))

    signal allowRequested(string approvalId, string reason)
    signal denyRequested(string approvalId, string reason)

    Layout.fillWidth: true
    implicitHeight: layout.implicitHeight + Kirigami.Units.smallSpacing * 2
    color: Kirigami.Theme.alternateBackgroundColor
    radius: Kirigami.Units.smallSpacing

    Timer {
        interval: 1000
        repeat: true
        running: root.pending
        onTriggered: root.remaining = Math.max(0, Math.ceil((root.deadline - Date.now()) / 1000))
    }

    ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing
            Kirigami.Icon {
                source: root.pending ? "dialog-warning" : "dialog-information"
                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                implicitHeight: Kirigami.Units.iconSizes.smallMedium
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Text {
                    text: root.pending ? "Aki 需要你的许可" : "工具审批"
                    font.bold: true
                    color: Kirigami.Theme.textColor
                }
                Text {
                    text: (root.itemData.toolName || "tool") + " · " + (root.pending ? root.remaining + " 秒后超时" : root.itemData.status)
                    color: root.pending ? Kirigami.Theme.textColor : Kirigami.Theme.disabledTextColor
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                }
            }
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(Math.max(inputArea.contentHeight, 24), 180)
            clip: true
            TextEdit {
                id: inputArea
                width: parent.width
                readOnly: true
                text: MessageFormat.formatToolArgs(root.itemData.toolInput)
                textFormat: TextEdit.PlainText
                font.family: "monospace"
                wrapMode: TextEdit.WrapAnywhere
                selectByMouse: true
                color: Kirigami.Theme.textColor
            }
        }

        PlasmaComponents.TextField {
            id: reasonField
            visible: root.pending
            Layout.fillWidth: true
            placeholderText: "拒绝理由（可选）"
        }

        RowLayout {
            Layout.fillWidth: true
            visible: root.pending
            Item { Layout.fillWidth: true }
            PlasmaComponents.Button {
                text: "拒绝"
                icon.name: "dialog-cancel"
                onClicked: root.denyRequested(root.itemData.approvalId || "", reasonField.text)
            }
            PlasmaComponents.Button {
                text: "允许"
                icon.name: "dialog-ok"
                onClicked: root.allowRequested(root.itemData.approvalId || "", "")
            }
        }
    }
}
