import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import "../js/message-format.js" as MessageFormat

Rectangle {
    id: root
    property var itemData: ({})
    property bool expanded: false
    Layout.fillWidth: true
    implicitHeight: layout.implicitHeight + Kirigami.Units.smallSpacing * 2
    color: Kirigami.Theme.alternateBackgroundColor
    radius: Kirigami.Units.smallSpacing

    function statusIcon() {
        var status = itemData.status || "";
        if (status === "success")
            return "dialog-ok";
        if (status === "error" || status === "failed")
            return "dialog-error";
        if (status === "requested")
            return "dialog-question";
        return "view-refresh";
    }

    function statusColor() {
        var status = itemData.status || "";
        if (status === "success")
            return Kirigami.Theme.positiveTextColor;
        if (status === "error" || status === "failed")
            return Kirigami.Theme.negativeTextColor;
        return Kirigami.Theme.disabledTextColor;
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
                source: root.statusIcon()
                implicitWidth: Kirigami.Units.iconSizes.small
                implicitHeight: Kirigami.Units.iconSizes.small
            }
            Text {
                text: root.itemData.toolName || "tool"
                font.bold: true
                color: Kirigami.Theme.textColor
            }
            Text {
                text: root.itemData.status || ""
                color: root.statusColor()
                font.pointSize: Kirigami.Theme.smallFont.pointSize
            }
            Item { Layout.fillWidth: true }
            PlasmaComponents.ToolButton {
                icon.name: root.expanded ? "arrow-up" : "arrow-down"
                onClicked: root.expanded = !root.expanded
            }
        }

        Text {
            visible: !root.expanded && MessageFormat.shortToolArgs(root.itemData.toolArgs).length > 0
            Layout.fillWidth: true
            text: MessageFormat.shortToolArgs(root.itemData.toolArgs, 180)
            color: Kirigami.Theme.disabledTextColor
            font.family: "monospace"
            wrapMode: Text.WrapAnywhere
            maximumLineCount: 2
            elide: Text.ElideRight
        }

        Text {
            visible: root.expanded && MessageFormat.formatToolArgs(root.itemData.toolArgs).length > 0
            Layout.fillWidth: true
            text: "参数"
            color: Kirigami.Theme.disabledTextColor
            font.pointSize: Kirigami.Theme.smallFont.pointSize
        }

        ScrollView {
            visible: root.expanded && MessageFormat.formatToolArgs(root.itemData.toolArgs).length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(Math.max(argsArea.contentHeight, 24), 240)
            clip: true
            TextEdit {
                id: argsArea
                width: parent.width
                readOnly: true
                text: MessageFormat.formatToolArgs(root.itemData.toolArgs)
                textFormat: TextEdit.PlainText
                font.family: "monospace"
                wrapMode: TextEdit.WrapAnywhere
                selectByMouse: true
                color: Kirigami.Theme.textColor
            }
        }

        Text {
            visible: root.expanded && (root.itemData.toolResult || "").length > 0
            Layout.fillWidth: true
            text: "结果"
            color: Kirigami.Theme.disabledTextColor
            font.pointSize: Kirigami.Theme.smallFont.pointSize
        }

        ScrollView {
            visible: root.expanded && (root.itemData.toolResult || "").length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(Math.max(resultArea.contentHeight, 24), 280)
            clip: true
            TextEdit {
                id: resultArea
                width: parent.width
                readOnly: true
                text: root.itemData.toolResult || ""
                textFormat: TextEdit.PlainText
                font.family: "monospace"
                wrapMode: TextEdit.WrapAnywhere
                selectByMouse: true
                color: Kirigami.Theme.textColor
            }
        }
    }
}
