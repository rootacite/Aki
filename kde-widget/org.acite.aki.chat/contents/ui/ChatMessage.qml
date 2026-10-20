import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import "../js/message-format.js" as MessageFormat

Item {
    id: delegate
    property var itemData: ({})
    property bool reasoningExpanded: false

    signal approvalAllowRequested(string approvalId, string reason)
    signal approvalDenyRequested(string approvalId, string reason)

    width: ListView.view ? ListView.view.width : implicitWidth
    implicitHeight: contentLoader.item ? contentLoader.item.implicitHeight : 0

    Loader {
        id: contentLoader
        width: parent.width
        sourceComponent: delegate.itemData.kind === "notice"
                         ? noticeComponent
                         : (delegate.itemData.kind === "reasoning" ? reasoningComponent : messageComponent)
    }

    Component {
        id: noticeComponent
        Item {
            width: parent.width
            implicitHeight: noticeBubble.implicitHeight + Kirigami.Units.smallSpacing * 2
            Rectangle {
                id: noticeBubble
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width * 0.8, noticeText.implicitWidth + Kirigami.Units.largeSpacing * 2)
                implicitHeight: noticeText.implicitHeight + Kirigami.Units.smallSpacing * 2
                radius: height / 2
                color: Kirigami.Theme.alternateBackgroundColor
                Text {
                    id: noticeText
                    anchors.centerIn: parent
                    width: parent.width - Kirigami.Units.largeSpacing * 2
                    text: delegate.itemData.text || ""
                    color: Kirigami.Theme.disabledTextColor
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
            }
        }
    }

    Component {
        id: reasoningComponent
        ColumnLayout {
            width: parent.width
            spacing: Kirigami.Units.smallSpacing
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: reasoningLayout.implicitHeight + Kirigami.Units.smallSpacing * 2
                color: Kirigami.Theme.alternateBackgroundColor
                radius: Kirigami.Units.smallSpacing
                ColumnLayout {
                    id: reasoningLayout
                    anchors.fill: parent
                    anchors.margins: Kirigami.Units.smallSpacing
                    spacing: Kirigami.Units.smallSpacing
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: "思考过程"
                            color: Kirigami.Theme.disabledTextColor
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                        Item { Layout.fillWidth: true }
                        PlasmaComponents.ToolButton {
                            icon.name: delegate.reasoningExpanded ? "arrow-up" : "arrow-down"
                            onClicked: delegate.reasoningExpanded = !delegate.reasoningExpanded
                        }
                    }
                    Text {
                        visible: delegate.reasoningExpanded
                        Layout.fillWidth: true
                        text: delegate.itemData.reasoning || ""
                        color: Kirigami.Theme.disabledTextColor
                        wrapMode: Text.Wrap
                    }
                }
            }
        }
    }

    Component {
        id: messageComponent
        RowLayout {
            id: row
            width: parent.width
            spacing: Kirigami.Units.smallSpacing
            layoutDirection: delegate.itemData.role === "user" ? Qt.RightToLeft : Qt.LeftToRight

            Rectangle {
                visible: delegate.itemData.role !== "user"
                Layout.alignment: Qt.AlignTop
                width: Kirigami.Units.iconSizes.smallMedium
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
                Layout.maximumWidth: row.width * 0.82
                Layout.alignment: delegate.itemData.role === "user" ? Qt.AlignRight : Qt.AlignLeft
                spacing: Kirigami.Units.smallSpacing / 2

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: bubbleContent.implicitHeight + Kirigami.Units.smallSpacing * 2
                    radius: Kirigami.Units.smallSpacing
                    color: delegate.itemData.role === "user"
                           ? Kirigami.Theme.highlightColor
                           : Kirigami.Theme.alternateBackgroundColor

                    ColumnLayout {
                        id: bubbleContent
                        anchors.fill: parent
                        anchors.margins: Kirigami.Units.smallSpacing
                        spacing: Kirigami.Units.smallSpacing

                        MarkdownText {
                            visible: delegate.itemData.kind === "text"
                            Layout.fillWidth: true
                            text: delegate.itemData.text || ""
                            textColor: delegate.itemData.role === "user"
                                       ? Kirigami.Theme.highlightedTextColor
                                       : Kirigami.Theme.textColor
                        }

                        ToolCallCard {
                            visible: delegate.itemData.kind === "tool"
                            itemData: delegate.itemData
                        }

                        ApprovalCard {
                            visible: delegate.itemData.kind === "approval"
                            itemData: delegate.itemData
                            onAllowRequested: (approvalId, reason) => delegate.approvalAllowRequested(approvalId, reason)
                            onDenyRequested: (approvalId, reason) => delegate.approvalDenyRequested(approvalId, reason)
                        }
                    }
                }

                RowLayout {
                    Layout.alignment: delegate.itemData.role === "user" ? Qt.AlignRight : Qt.AlignLeft
                    spacing: Kirigami.Units.smallSpacing
                    Text {
                        text: MessageFormat.formatTime(delegate.itemData.timestamp)
                        color: Kirigami.Theme.disabledTextColor
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                    }
                    Rectangle {
                        visible: delegate.itemData.source === "mqtt"
                        implicitWidth: mqttLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                        implicitHeight: mqttLabel.implicitHeight + Kirigami.Units.smallSpacing / 2
                        radius: height / 2
                        color: Kirigami.Theme.alternateBackgroundColor
                        Text {
                            id: mqttLabel
                            anchors.centerIn: parent
                            text: "MQTT"
                            color: Kirigami.Theme.disabledTextColor
                            font.pointSize: Kirigami.Theme.smallFont.pointSize - 1
                        }
                    }
                    PlasmaComponents.BusyIndicator {
                        visible: delegate.itemData.streaming === true
                        implicitWidth: Kirigami.Units.iconSizes.small
                        implicitHeight: Kirigami.Units.iconSizes.small
                        running: visible
                    }
                }
            }
        }
    }
}
