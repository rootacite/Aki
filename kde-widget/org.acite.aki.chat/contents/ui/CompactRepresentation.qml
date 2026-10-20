import QtQuick
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasmoid

Item {
    id: root
    required property PlasmoidItem plasmoidItem
    property bool busy: false
    property bool attention: false
    property int unreadCount: 0

    implicitWidth: Kirigami.Units.iconSizes.medium
    implicitHeight: Kirigami.Units.iconSizes.medium

    Rectangle {
        id: avatarFrame
        anchors.fill: parent
        radius: width / 2
        clip: true
        color: Kirigami.Theme.alternateBackgroundColor
        border.width: root.busy ? 2 : 1
        border.color: root.busy ? Kirigami.Theme.highlightColor : "transparent"

        Image {
            anchors.fill: parent
            source: Qt.resolvedUrl("../images/aki_avi.jpg")
            fillMode: Image.PreserveAspectCrop
        }
    }

    Rectangle {
        visible: root.attention || root.unreadCount > 0
        anchors.right: parent.right
        anchors.top: parent.top
        width: root.unreadCount > 0 ? Math.max(Kirigami.Units.gridUnit / 2, badgeText.implicitWidth + 4) : Kirigami.Units.smallSpacing * 2
        height: width
        radius: width / 2
        color: root.attention ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.highlightColor
        Text {
            id: badgeText
            anchors.centerIn: parent
            visible: root.unreadCount > 0
            text: root.unreadCount > 9 ? "9+" : String(root.unreadCount)
            color: Kirigami.Theme.highlightedTextColor
            font.pointSize: Kirigami.Theme.smallFont.pointSize - 2
        }
    }

    MouseArea {
        anchors.fill: parent
        onClicked: root.plasmoidItem.expanded = !root.plasmoidItem.expanded
    }
}
