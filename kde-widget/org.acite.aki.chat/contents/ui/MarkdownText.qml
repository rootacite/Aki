import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import "../js/message-format.js" as MessageFormat

Item {
    id: root
    property string text: ""
    property color textColor: Kirigami.Theme.textColor
    implicitWidth: 0
    implicitHeight: column.implicitHeight

    ColumnLayout {
        id: column
        width: root.width
        spacing: Kirigami.Units.smallSpacing

        Repeater {
            model: MessageFormat.parseMarkdownBlocks(root.text)
            delegate: Item {
                id: blockItem
                Layout.fillWidth: true
                implicitHeight: blockLoader.item ? blockLoader.item.implicitHeight : 0

                Loader {
                    id: blockLoader
                    width: parent.width
                    sourceComponent: modelData.kind === "code" ? codeComponent : textComponent
                    onLoaded: {
                        item.text = modelData.text;
                        item.lang = modelData.lang || "";
                    }
                }
            }
        }
    }

    Component {
        id: textComponent
        Text {
            property string lang: ""
            width: parent.width
            textFormat: Text.MarkdownText
            wrapMode: Text.Wrap
            color: root.textColor
            onLinkActivated: (link) => Qt.openUrlExternally(link)
        }
    }

    Component {
        id: codeComponent
        Rectangle {
            id: codeRoot
            property string text: ""
            property string lang: ""
            width: parent.width
            implicitHeight: codeLayout.implicitHeight + Kirigami.Units.smallSpacing * 2
            color: Kirigami.Theme.alternateBackgroundColor
            radius: Kirigami.Units.smallSpacing

            ColumnLayout {
                id: codeLayout
                anchors.fill: parent
                anchors.margins: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: codeRoot.lang || "code"
                        color: Kirigami.Theme.disabledTextColor
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                    }
                    Item { Layout.fillWidth: true }
                    PlasmaComponents.ToolButton {
                        icon.name: "edit-copy"
                        onClicked: {
                            codeArea.selectAll();
                            codeArea.copy();
                        }
                    }
                }

                ScrollView {
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(Math.max(codeArea.contentHeight, 24), 320)
                    clip: true
                    TextEdit {
                        id: codeArea
                        width: parent.width
                        readOnly: true
                        text: codeRoot.text
                        textFormat: TextEdit.PlainText
                        font.family: "monospace"
                        wrapMode: TextEdit.WrapAnywhere
                        selectByMouse: true
                        color: Kirigami.Theme.textColor
                    }
                }
            }
        }
    }
}
