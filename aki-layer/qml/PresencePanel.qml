import QtQuick
import QtQuick.Layouts
import "Themes.js" as Themes

Item {
    id: panel

    property var presence: ({})
    property var theme: Themes.get("Aki")
    property real clockMs: Date.now()
    property bool expanded: true
    property bool cardHovered: false
    property bool edgeHovered: false
    property bool expandGrace: false
    property int expandedWidth: 246
    property int autoCollapseMs: 7000
    property int leaveCollapseMs: 1200

    signal inputRegionDirty()

    readonly property bool known: Object.keys(presence || {}).length > 0
    readonly property bool active: presence.active === true
    readonly property bool away: presence.away === true
    readonly property bool valid: presence.valid !== false
    readonly property int pollSeconds: Math.max(1, Number(presence.pollSeconds || 15))
    readonly property real receivedAtMs: Number(presence.receivedAtMs || 0)
    readonly property real snapshotAtMs: Number(presence.sampledAtMs || receivedAtMs)
    readonly property bool stale: known && snapshotAtMs > 0
            && clockMs - snapshotAtMs > Math.max(45000, pollSeconds * 4000)
    readonly property bool pointerInside: cardHovered || edgeHovered
    readonly property bool noInputKnown: known
            && (Number(presence.lastInputAtMs || 0) > 0
                || (presence.noInputSeconds !== null
                    && presence.noInputSeconds !== undefined))

    readonly property color activeColor: "#2E8B5B"
    readonly property color idleColor: "#8B7B84"
    readonly property color awayColor: "#B1692C"
    readonly property color staleColor: "#8B817D"
    readonly property color stateColor: !known || stale || !valid
            ? staleColor
            : away
              ? awayColor
              : active
                ? activeColor
                : idleColor
    readonly property string stateText: !known
            ? "等待中"
            : active
              ? "活跃"
              : "非活跃"
    readonly property string awayText: !known ? "--" : away ? "是" : "否"
    readonly property string activeDurationText: known
            ? formatDuration(liveActiveSeconds)
            : "--"
    readonly property string noInputDurationText: noInputKnown
            ? formatCounter(liveNoInputSeconds)
            : "--"
    readonly property string footerText: !known
            ? "等待 akid 上报"
            : stale
              ? "状态已过期"
              : !valid
                ? "最近一次采样无效"
                : "akid 每 " + pollSeconds + " 秒采样"
    readonly property real liveActiveSeconds: {
        if (!active)
            return 0
        const endMs = stale && snapshotAtMs > 0 ? snapshotAtMs : clockMs
        const sinceMs = Number(presence.activeSinceMs || 0)
        if (sinceMs > 0)
            return Math.max(0, (endMs - sinceMs) / 1000)
        return Number(presence.activeSeconds || 0)
    }
    readonly property real liveNoInputSeconds: {
        if (!noInputKnown)
            return 0
        const endMs = stale && snapshotAtMs > 0 ? snapshotAtMs : clockMs
        const lastInputMs = Number(presence.lastInputAtMs || 0)
        if (lastInputMs > 0)
            return Math.max(0, (endMs - lastInputMs) / 1000)
        return Math.max(0, Number(presence.noInputSeconds || 0))
    }

    width: expandedWidth
    height: card.height
    opacity: !known ? 0.82 : stale ? 0.68 : 1

    Behavior on opacity {
        NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
    }

    onExpandedChanged: {
        inputRegionDirty()
        if (expanded && !expandGrace)
            scheduleCollapse(autoCollapseMs)
    }
    onHeightChanged: inputRegionDirty()
    onPointerInsideChanged: {
        if (pointerInside) {
            collapseTimer.stop()
        } else if (expanded && !expandGrace) {
            scheduleCollapse(leaveCollapseMs)
        }
    }

    Component.onCompleted: scheduleCollapse(autoCollapseMs)

    function formatDuration(seconds) {
        const total = Math.max(0, Math.floor(Number(seconds) || 0))
        const hours = Math.floor(total / 3600)
        const minutes = Math.floor((total % 3600) / 60)
        const secs = total % 60
        if (hours > 0)
            return hours + " 小时 " + minutes + " 分"
        if (minutes > 0)
            return minutes + " 分 " + secs + " 秒"
        return secs + " 秒"
    }

    function pad2(value) {
        return value < 10 ? "0" + value : String(value)
    }

    function formatCounter(seconds) {
        const total = Math.max(0, Math.floor(Number(seconds) || 0))
        const hours = Math.floor(total / 3600)
        const minutes = Math.floor((total % 3600) / 60)
        const secs = total % 60
        if (hours > 0)
            return hours + ":" + pad2(minutes) + ":" + pad2(secs)
        return minutes + ":" + pad2(secs)
    }

    function expandPanel() {
        expandGrace = true
        graceTimer.restart()
        expanded = true
        scheduleCollapse(autoCollapseMs)
    }

    function scheduleCollapse(delay) {
        if (pointerInside || expandGrace) {
            collapseTimer.stop()
            return
        }
        collapseTimer.interval = delay
        collapseTimer.restart()
    }

    Item {
        anchors.fill: parent
        property bool blocksInput: panel.expanded
    }

    Rectangle {
        id: card
        width: panel.expandedWidth
        height: content.implicitHeight + 28
        x: panel.expanded ? 0 : panel.width + 28
        opacity: panel.expanded ? 1 : 0
        visible: panel.expanded || opacity > 0.01
        radius: 18
        border.width: 1
        border.color: panel.theme.border

        gradient: Gradient {
            GradientStop { position: 0.0; color: panel.theme.gradientTop }
            GradientStop { position: 0.56; color: panel.theme.gradientMid }
            GradientStop { position: 1.0; color: panel.theme.gradientBottom }
        }

        Behavior on x {
            NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
        }

        Behavior on opacity {
            NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
        }

        HoverHandler {
            onHoveredChanged: {
                panel.cardHovered = hovered
                if (hovered) {
                    panel.expandGrace = false
                    graceTimer.stop()
                }
            }
        }

        ColumnLayout {
            id: content
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: 18
            anchors.rightMargin: 16
            anchors.topMargin: 14
            spacing: 7

            RowLayout {
                Layout.fillWidth: true
                spacing: 7

                Text {
                    text: "用户状态"
                    color: panel.theme.accent
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    opacity: 0.72
                }

                Item { Layout.fillWidth: true }

                Item {
                    Layout.preferredWidth: 10
                    Layout.preferredHeight: 10

                    Rectangle {
                        anchors.centerIn: parent
                        width: 16
                        height: 16
                        radius: 8
                        color: panel.stateColor
                        opacity: 0.14
                    }

                    Rectangle {
                        anchors.centerIn: parent
                        width: 8
                        height: 8
                        radius: 4
                        color: panel.stateColor
                    }
                }

                Text {
                    text: panel.stateText
                    color: panel.stateColor
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }

                Rectangle {
                    Layout.preferredWidth: awayBadge.implicitWidth + 12
                    Layout.preferredHeight: 19
                    radius: 9
                    visible: panel.known && panel.away
                    color: panel.awayColor
                    opacity: 0.16

                    Text {
                        id: awayBadge
                        anchors.centerIn: parent
                        text: "已离开"
                        color: panel.awayColor
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: panel.theme.border
                opacity: 0.7
            }

            RowLayout {
                Layout.fillWidth: true

                Text {
                    text: "连续活跃"
                    color: panel.theme.text
                    font.pixelSize: 13
                    opacity: 0.68
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: panel.activeDurationText
                    color: panel.theme.text
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
            }

            RowLayout {
                Layout.fillWidth: true

                Text {
                    text: "连续未输入"
                    color: panel.theme.text
                    font.pixelSize: 13
                    opacity: 0.68
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: panel.noInputDurationText
                    color: panel.theme.text
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
            }

            RowLayout {
                Layout.fillWidth: true

                Text {
                    text: "离开判定"
                    color: panel.theme.text
                    font.pixelSize: 13
                    opacity: 0.68
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: panel.awayText
                    color: panel.away ? panel.awayColor : panel.theme.accent
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                }
            }

            Text {
                Layout.fillWidth: true
                text: panel.footerText
                color: panel.theme.accent
                font.pixelSize: 11
                opacity: 0.54
            }
        }
    }

    // The collapsed card leaves no visible tab. A transparent strip along the
    // right screen edge remains as the hover target that brings it back.
    Item {
        id: edgeSensor
        x: panel.width + 24 - width
        anchors.verticalCenter: parent.verticalCenter
        width: 8
        height: 180
        property bool blocksInput: true

        HoverHandler {
            onHoveredChanged: {
                panel.edgeHovered = hovered
                if (hovered)
                    panel.expandPanel()
            }
        }
    }

    Timer {
        id: collapseTimer
        repeat: false
        onTriggered: {
            if (!panel.pointerInside)
                panel.expanded = false
        }
    }

    Timer {
        id: graceTimer
        interval: panel.autoCollapseMs
        repeat: false
        onTriggered: {
            panel.expandGrace = false
            if (panel.expanded && !panel.pointerInside)
                panel.expanded = false
        }
    }

    Timer {
        interval: 1000
        repeat: true
        running: panel.known
        triggeredOnStart: true
        onTriggered: panel.clockMs = Date.now()
    }
}
