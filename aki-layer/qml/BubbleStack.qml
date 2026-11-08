import QtQuick
import "Themes.js" as Themes

Item {
    id: bubbleStack

    property int defaultLifetimeMs: 15000
    property int minimumRemainingMs: 5000
    property int bubbleWidth: 460
    property bool hoverPaused: false
    property int nextId: 1
    property int maxActiveCount: 5
    property int maxMissedCount: 50
    property alias activeCount: activeModel.count
    property alias missedCount: missedModel.count
    property alias missedRightMargin: missedRegion.anchors.rightMargin
    property int missedPeekEdgeHeight: 8
    property int missedPeekLayerStep: 5
    property int missedPeekMaxLayers: 3
    property int missedPeekLayers: Math.min(missedModel.count, missedPeekMaxLayers)
    property int missedPeekHeight: missedPeekLayers > 0
            ? missedPeekEdgeHeight + (missedPeekLayers - 1) * missedPeekLayerStep
            : 0
    property int missedExpandedMaxHeight: Math.min(420, Math.max(180, height * 0.55))
    property bool missedExpanded: false
    property bool missedHovered: false
    property var hoveredBubbles: ({})

    signal bubbleDismissed(string bubbleId)

    function pad2(value) {
        return value < 10 ? "0" + value : String(value)
    }

    function formatBubbleTime(epochMs) {
        const value = new Date(epochMs)
        const now = new Date()
        const sameDay = value.getFullYear() === now.getFullYear()
                && value.getMonth() === now.getMonth()
                && value.getDate() === now.getDate()
        const clock = pad2(value.getHours()) + ":" + pad2(value.getMinutes())
        if (sameDay)
            return clock
        return pad2(value.getMonth() + 1) + "-" + pad2(value.getDate()) + " " + clock
    }

    function pushMessage(text, character) {
        pushMessageWithId(text, "bubble-" + nextId++, character)
    }

    function pushMessageWithId(text, bubbleId, character) {
        while (activeModel.count >= maxActiveCount) {
            collapseBubble(activeModel.get(0).bubbleId)
        }
        activeModel.append({
            "bubbleId": bubbleId || ("bubble-" + nextId++),
            "messageText": text,
            "character": character || "Aki",
            "timeText": formatBubbleTime(Date.now())
        })
        scheduleInputRegionRefresh()
    }

    function clearAllNotifications() {
        while (activeModel.count > 0) {
            const id = activeModel.get(0).bubbleId
            activeModel.remove(0)
            bubbleDismissed(id)
        }
        while (missedModel.count > 0) {
            const id = missedModel.get(0).bubbleId
            missedModel.remove(0)
            bubbleDismissed(id)
        }
        hoveredBubbles = ({})
        missedExpanded = false
        missedHovered = false
        refreshHoverPause()
        scheduleInputRegionRefresh()
    }

    function findBubbleIndex(model, id) {
        for (let index = 0; index < model.count; ++index) {
            if (model.get(index).bubbleId === id)
                return index
        }
        return -1
    }

    function removeBubble(id) {
        let index = findBubbleIndex(activeModel, id)
        if (index >= 0) {
            activeModel.remove(index)
        } else {
            index = findBubbleIndex(missedModel, id)
            if (index >= 0)
                missedModel.remove(index)
        }
        if (index >= 0)
            bubbleDismissed(id)
        noteBubbleHover(id, false)
        if (missedModel.count === 0) {
            missedExpanded = false
            missedHovered = false
        }
        refreshHoverPause()
        scheduleInputRegionRefresh()
    }

    function collapseBubble(id) {
        const index = findBubbleIndex(activeModel, id)
        if (index < 0)
            return
        const item = activeModel.get(index)
        const folded = {
            "bubbleId": item.bubbleId,
            "messageText": item.messageText,
            "character": item.character,
            "timeText": item.timeText
        }
        activeModel.remove(index)
        noteBubbleHover(id, false)
        if (missedModel.count >= maxMissedCount) {
            const dropped = missedModel.get(0).bubbleId
            missedModel.remove(0)
            bubbleDismissed(dropped)
        }
        missedModel.append(folded)
        refreshHoverPause()
        scheduleInputRegionRefresh()
    }

    function anyBubbleHovered() {
        for (const key in hoveredBubbles)
            return true
        return false
    }

    function noteBubbleHover(bubbleId, hovered) {
        const next = ({})
        for (const key in hoveredBubbles)
            next[key] = true
        if (hovered)
            next[bubbleId] = true
        else
            delete next[bubbleId]
        hoveredBubbles = next
        refreshHoverPause()
    }

    function noteMissedHover(hovered) {
        missedHovered = hovered
        if (hovered) {
            missedCollapseDelay.stop()
            missedExpanded = true
        } else {
            missedCollapseDelay.restart()
        }
        refreshHoverPause()
    }

    function refreshHoverPause() {
        if (missedHovered || anyBubbleHovered()) {
            hoverRelease.stop()
            hoverPaused = true
        } else {
            hoverRelease.restart()
        }
    }

    function scheduleInputRegionRefresh() {
        inputRegionRefresh.restart()
    }

    ListModel {
        id: activeModel
    }

    ListModel {
        id: missedModel
    }

    // The missed drawer is a child of one hover region. This keeps the
    // pointer inside the same QML hit-test tree while the drawer slides in or
    // out, avoiding expand/collapse oscillation. The region is not an overlay,
    // so the per-bubble close buttons remain clickable.
    Item {
        id: missedRegion
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 0
        anchors.rightMargin: bubbleStack.missedExpanded ? 8 : 72
        width: bubbleStack.missedExpanded ? bubbleStack.bubbleWidth + 32 : bubbleStack.bubbleWidth - 32
        height: bubbleStack.missedExpanded
                ? 24 + missedDrawer.height + 10
                : bubbleStack.missedPeekHeight + 6
        visible: missedModel.count > 0
        property bool blocksInput: visible

        HoverHandler {
            onHoveredChanged: bubbleStack.noteMissedHover(hovered)
        }

        onHeightChanged: bubbleStack.scheduleInputRegionRefresh()
        onWidthChanged: bubbleStack.scheduleInputRegionRefresh()

        // A thin layered card edge remains at the top when bubbles are folded
        // away. Each recent missed message contributes one edge.
        Item {
            id: missedPeekStack
            anchors.top: parent.top
            anchors.topMargin: 0
            anchors.left: parent.left
            anchors.right: parent.right
            height: bubbleStack.missedPeekHeight
            visible: !bubbleStack.missedExpanded

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    bubbleStack.missedExpanded = !bubbleStack.missedExpanded
                    bubbleStack.scheduleInputRegionRefresh()
                }
            }

            Repeater {
                model: bubbleStack.missedPeekLayers

                Rectangle {
                    readonly property int sourceIndex: missedModel.count - 1 - index
                    readonly property var entry: sourceIndex >= 0
                            ? missedModel.get(sourceIndex)
                            : ({ "character": "Aki" })
                    readonly property var layerTheme: Themes.get(entry.character)

                    width: parent.width - index * 14
                    height: bubbleStack.missedPeekEdgeHeight
                    x: (parent.width - width) / 2
                    y: index * bubbleStack.missedPeekLayerStep
                    radius: height / 2
                    color: layerTheme.gradientMid
                    border.width: 1
                    border.color: layerTheme.borderHover
                    opacity: 1.0 - index * 0.12
                    z: bubbleStack.missedPeekLayers - index
                }
            }
        }

        // Folded state parks the drawer fully above the surface. There is no
        // summary bar; only the layered edges above remain visible.
        Item {
            id: missedDrawer
            x: 16
            width: bubbleStack.bubbleWidth
            height: missedModel.count === 0
                    ? 0
                    : Math.min(drawerHeader.height + 8 + missedList.contentHeight, bubbleStack.missedExpandedMaxHeight)
            y: bubbleStack.missedExpanded ? 24 : -height - 12
            clip: true
            property bool blocksInput: bubbleStack.missedExpanded

            Behavior on y {
                NumberAnimation { duration: 190; easing.type: Easing.OutCubic }
            }

            Behavior on height {
                NumberAnimation { duration: 190; easing.type: Easing.OutCubic }
            }

            onYChanged: bubbleStack.scheduleInputRegionRefresh()
            onHeightChanged: bubbleStack.scheduleInputRegionRefresh()

            Item {
                id: drawerHeader
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: 28

                Text {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    text: "折叠通知 (" + missedModel.count + ")"
                    color: "#A53A61"
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                }

                Rectangle {
                    id: clearMissedBtn
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: clearMissedRow.implicitWidth + 18
                    implicitHeight: 24
                    radius: 12
                    color: clearMissedArea.containsMouse ? "#E8FBF2F6" : "#CCFBF2F6"
                    border.width: 1
                    border.color: clearMissedArea.containsMouse ? "#C9F07FA6" : "#78F3A9C0"
                    property bool blocksInput: bubbleStack.missedExpanded

                    Row {
                        id: clearMissedRow
                        anchors.centerIn: parent
                        spacing: 4
                        Text {
                            text: "一键清空"
                            color: "#A53A61"
                            font.pixelSize: 11
                            font.weight: Font.Medium
                        }
                    }

                    MouseArea {
                        id: clearMissedArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: bubbleStack.clearAllNotifications()
                    }
                }
            }

            ListView {
                id: missedList
                anchors.top: drawerHeader.bottom
                anchors.topMargin: 8
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                clip: true
                model: missedModel
                spacing: 8
                interactive: bubbleStack.missedExpanded
                boundsBehavior: Flickable.StopAtBounds
                cacheBuffer: 200

                onContentHeightChanged: bubbleStack.scheduleInputRegionRefresh()

                delegate: NotificationBubble {
                    property string bubbleId: model.bubbleId

                    width: missedList.width
                    messageText: model.messageText
                    character: model.character
                    timeText: model.timeText
                    activeNotification: false
                    autoDismiss: false
                    showDismiss: true
                    blocksInput: bubbleStack.missedExpanded
                    stack: bubbleStack

                    onDismissRequested: bubbleStack.removeBubble(bubbleId)
                }
            }
        }
    }

    Column {
        id: activeColumn
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 24 + (missedModel.count > 0 && bubbleStack.missedExpanded
                                 ? missedDrawer.height + 10 : 0)
        anchors.rightMargin: 24
        width: bubbleStack.bubbleWidth
        spacing: 10

        Item {
            id: activeHeader
            width: activeColumn.width
            height: visible ? 24 : 0
            visible: activeModel.count >= 2 || (activeModel.count > 0 && bubbleStack.anyBubbleHovered())

            onVisibleChanged: bubbleStack.scheduleInputRegionRefresh()

            Rectangle {
                anchors.right: parent.right
                implicitWidth: clearActiveRow.implicitWidth + 18
                implicitHeight: 24
                radius: 12
                color: clearActiveArea.containsMouse ? "#E8FBF2F6" : "#CCFBF2F6"
                border.width: 1
                border.color: clearActiveArea.containsMouse ? "#C9F07FA6" : "#78F3A9C0"
                property bool blocksInput: parent.visible

                Row {
                    id: clearActiveRow
                    anchors.centerIn: parent
                    spacing: 4
                    Text {
                        text: "一键清空 (" + activeModel.count + ")"
                        color: "#A53A61"
                        font.pixelSize: 11
                        font.weight: Font.Medium
                    }
                }

                MouseArea {
                    id: clearActiveArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: bubbleStack.clearAllNotifications()
                }
            }
        }

        Repeater {
            model: activeModel

            NotificationBubble {
                property string bubbleId: model.bubbleId

                width: activeColumn.width
                messageText: model.messageText
                character: model.character
                timeText: model.timeText
                defaultLifetimeMs: bubbleStack.defaultLifetimeMs
                minimumRemainingMs: bubbleStack.minimumRemainingMs
                globalPaused: bubbleStack.hoverPaused
                stack: bubbleStack

                onDismissRequested: bubbleStack.removeBubble(bubbleId)
                onCollapseRequested: bubbleStack.collapseBubble(bubbleId)
                onHoverChanged: function(hovered) {
                    bubbleStack.noteBubbleHover(bubbleId, hovered)
                }
            }
        }
    }

    Timer {
        id: hoverRelease
        interval: 120
        onTriggered: {
            if (!bubbleStack.missedHovered && !bubbleStack.anyBubbleHovered())
                bubbleStack.hoverPaused = false
        }
    }

    Timer {
        id: missedCollapseDelay
        interval: 180
        onTriggered: {
            if (!bubbleStack.missedHovered) {
                bubbleStack.missedExpanded = false
                bubbleStack.refreshHoverPause()
            }
        }
    }

    Timer {
        id: inputRegionRefresh
        interval: 0
        onTriggered: {
            if (typeof inputRegionController !== "undefined" && inputRegionController)
                inputRegionController.refresh()
        }
    }
}
