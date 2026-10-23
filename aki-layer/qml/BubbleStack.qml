import QtQuick

Item {
    id: bubbleStack

    property int defaultLifetimeMs: 15000
    property int minimumRemainingMs: 5000
    property int bubbleWidth: 460
    property bool hoverPaused: false
    property int nextId: 1

    signal bubbleDismissed(string bubbleId)

    function pushMessage(text, character) {
        pushMessageWithId(text, "bubble-" + nextId++, character)
    }

    function pushMessageWithId(text, bubbleId, character) {
        bubbleModel.append({
            "bubbleId": bubbleId,
            "messageText": text,
            "character": character || "Aki"
        })
        scheduleInputRegionRefresh()
    }

    function removeBubble(id) {
        for (let index = 0; index < bubbleModel.count; ++index) {
            if (bubbleModel.get(index).bubbleId === id) {
                bubbleModel.remove(index)
                break
            }
        }
        bubbleDismissed(id)
        scheduleInputRegionRefresh()
    }

    function noteHover(hovered) {
        if (hovered) {
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
        id: bubbleModel
    }

    Column {
        id: bubbleColumn
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 24
        anchors.rightMargin: 24
        width: bubbleStack.bubbleWidth
        spacing: 10

        Repeater {
            model: bubbleModel

            NotificationBubble {
                property string bubbleId: model.bubbleId

                width: bubbleColumn.width
                messageText: model.messageText
                character: model.character
                defaultLifetimeMs: bubbleStack.defaultLifetimeMs
                minimumRemainingMs: bubbleStack.minimumRemainingMs
                globalPaused: bubbleStack.hoverPaused
                stack: bubbleStack

                onDismissRequested: bubbleStack.removeBubble(bubbleId)
                onHoverChanged: function(hovered) { bubbleStack.noteHover(hovered) }
            }
        }
    }

    Timer {
        id: hoverRelease
        interval: 120
        onTriggered: bubbleStack.hoverPaused = false
    }

    Timer {
        id: inputRegionRefresh
        interval: 0
        onTriggered: inputRegionController.refresh()
    }
}
