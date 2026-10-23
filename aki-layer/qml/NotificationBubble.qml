import QtQuick
import QtQuick.Layouts
import Qt5Compat.GraphicalEffects
import "Themes.js" as Themes

Item {
    id: bubble

    property bool blocksInput: true
    property bool hovered: false
    property bool dismissing: false
    property bool collapsing: false
    property bool globalPaused: false
    property bool activeNotification: true
    property bool autoDismiss: true
    property bool showDismiss: false
    property bool seen: false
    property int defaultLifetimeMs: 15000
    property int minimumRemainingMs: 5000
    property int remainingMs: defaultLifetimeMs
    property string messageText: ""
    property string timeText: ""
    property string character: "Aki"
    property var theme: Themes.get(character)
    property var stack: null

    signal dismissRequested()
    signal collapseRequested()
    signal hoverChanged(bool hovered)

    width: 460
    height: contentRow.implicitHeight + 38
    opacity: 0
    scale: hovered ? 1.012 : 1.0

    transform: Translate {
        id: slide
        x: 28
        y: 0
    }

    Behavior on scale {
        NumberAnimation { duration: 120; easing.type: Easing.OutCubic }
    }

    Rectangle {
        id: body
        anchors.fill: parent
        radius: 22
        border.width: hovered ? 1.5 : 1
        border.color: hovered ? bubble.theme.borderHover : bubble.theme.border
        gradient: Gradient {
            GradientStop { position: 0.0; color: bubble.theme.gradientTop }
            GradientStop { position: 0.52; color: bubble.theme.gradientMid }
            GradientStop { position: 1.0; color: bubble.theme.gradientBottom }
        }
    }

    Loader {
        anchors.fill: body
        source: bubble.theme.background
    }

    RowLayout {
        id: contentRow
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 18
        spacing: 14

        Item {
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: 58
            Layout.preferredHeight: 58

            Image {
                id: avatarSource
                anchors.fill: parent
                source: bubble.theme.avatar
                fillMode: Image.PreserveAspectCrop
                visible: false
            }

            Rectangle {
                id: avatarMask
                anchors.fill: parent
                radius: width / 2
                visible: false
            }

            OpacityMask {
                anchors.fill: parent
                source: avatarSource
                maskSource: avatarMask
                cached: true
            }

            Rectangle {
                anchors.fill: parent
                radius: width / 2
                color: "transparent"
                border.width: 2
                border.color: "#FFFFF7FA"
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Text {
                    text: bubble.theme.name
                    color: bubble.theme.accent
                    font.pixelSize: 17
                    font.weight: Font.DemiBold
                }

                Rectangle {
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 18
                    radius: 9
                    color: bubble.theme.tagBg

                    Text {
                        anchors.centerIn: parent
                        text: bubble.theme.tag
                        color: bubble.theme.tagText
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                }

                Item { Layout.fillWidth: true }

                Text {
                    Layout.alignment: Qt.AlignVCenter
                    text: bubble.timeText
                    color: bubble.theme.accent
                    opacity: 0.62
                    font.pixelSize: 12
                }

                Text {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: visible ? 16 : 0
                    visible: bubble.showDismiss
                    text: "×"
                    color: bubble.theme.accent
                    opacity: 0.58
                    font.pixelSize: 16

                    MouseArea {
                        anchors.fill: parent
                        onClicked: bubble.dismiss()
                    }
                }
            }

            Text {
                Layout.fillWidth: true
                text: bubble.messageText
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: bubble.theme.text
                font.pixelSize: 17
                lineHeight: 1.2
            }
        }
    }

    HoverHandler {
        id: hoverHandler
        onHoveredChanged: {
            bubble.hovered = hovered
            if (!bubble.activeNotification)
                return
            if (hovered)
                bubble.seen = true
            bubble.hoverChanged(hovered)
        }
    }

    Timer {
        interval: 100
        repeat: true
        running: bubble.activeNotification && bubble.autoDismiss
                 && !bubble.globalPaused && !bubble.dismissing && !bubble.collapsing
        onTriggered: {
            bubble.remainingMs = Math.max(0, bubble.remainingMs - interval)
            if (bubble.remainingMs <= 0) {
                if (bubble.seen)
                    bubble.dismiss()
                else
                    bubble.collapse()
            }
        }
    }

    onGlobalPausedChanged: {
        if (activeNotification && autoDismiss && globalPaused)
            remainingMs = Math.max(remainingMs, minimumRemainingMs)
    }

    ParallelAnimation {
        id: entryAnimation
        NumberAnimation { target: bubble; property: "opacity"; from: 0; to: 1; duration: 220; easing.type: Easing.OutCubic }
        NumberAnimation { target: slide; property: "x"; from: 28; to: 0; duration: 260; easing.type: Easing.OutCubic }
        onFinished: if (bubble.stack) bubble.stack.scheduleInputRegionRefresh()
    }

    ParallelAnimation {
        id: exitAnimation
        NumberAnimation { target: bubble; property: "opacity"; to: 0; duration: 180; easing.type: Easing.InCubic }
        NumberAnimation { target: slide; property: "x"; to: 20; duration: 180; easing.type: Easing.InCubic }
        NumberAnimation { target: slide; property: "y"; to: -6; duration: 180; easing.type: Easing.InCubic }
        onFinished: bubble.dismissRequested()
    }

    ParallelAnimation {
        id: collapseAnimation
        NumberAnimation { target: bubble; property: "opacity"; to: 0; duration: 180; easing.type: Easing.InCubic }
        NumberAnimation { target: slide; property: "y"; to: -18; duration: 200; easing.type: Easing.InCubic }
        onFinished: bubble.collapseRequested()
    }

    function dismiss() {
        if (dismissing || collapsing)
            return
        dismissing = true
        exitAnimation.start()
    }

    function collapse() {
        if (dismissing || collapsing)
            return
        collapsing = true
        collapseAnimation.start()
    }

    Component.onCompleted: {
        if (activeNotification && autoDismiss && globalPaused)
            remainingMs = Math.max(remainingMs, minimumRemainingMs)
        entryAnimation.start()
        if (stack)
            stack.scheduleInputRegionRefresh()
    }

    Component.onDestruction: {
        if (stack)
            stack.scheduleInputRegionRefresh()
    }

    onHeightChanged: {
        if (stack)
            stack.scheduleInputRegionRefresh()
    }

    onYChanged: {
        if (stack)
            stack.scheduleInputRegionRefresh()
    }
}
