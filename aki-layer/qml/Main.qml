import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShell

Window {
    id: root

    visible: true
    color: "transparent"
    title: "Aki Layer"
    width: 1
    height: 1

    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.scope: "aki-layer"
    LayerShell.Window.anchors: LayerShell.Window.AnchorTop
                               | LayerShell.Window.AnchorBottom
                               | LayerShell.Window.AnchorLeft
                               | LayerShell.Window.AnchorRight
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityOnDemand
    LayerShell.Window.activateOnShow: false

    Component.onCompleted: {
        inputRegionController.refresh()
        const demoCount = typeof demoBubbleCount === "number" ? demoBubbleCount : 0
        if (demoCount > 0) {
            const samples = [
                { character: "Fuyu", text: "代码我看过了。能跑，但不代表我不挑毛病。" },
                { character: "Haru", text: "配色再大胆一点嘛！画笔和耳机都准备好了～" },
                { character: "Natsu", text: "链路已经摸清。下一步做最小化验证。" },
                { character: "Aki", text: "秋叶落在窗边，Aki 在这里。" }
            ]
            for (let index = 1; index <= demoCount; ++index)
                bubbleStack.pushMessage(samples[(index - 1) % samples.length].text,
                                        samples[(index - 1) % samples.length].character)
        } else {
            greetingTimer.start()
        }
    }

    Timer {
        id: greetingTimer
        interval: 450
        onTriggered: bubbleStack.pushMessage("秋叶落在窗边，Aki 在这里。有什么想和我说的吗？")
    }

    Connections {
        target: bubbleServer
        function onMessageReceived(text, character, bubbleId) {
            bubbleStack.pushMessageWithId(text, bubbleId, character)
        }
    }

    Connections {
        target: bubbleStack
        function onBubbleDismissed(bubbleId) { bubbleServer.notifyBubbleDismissed(bubbleId) }
    }

    BubbleStack {
        id: bubbleStack
        anchors.fill: parent
    }

    PresencePanel {
        id: presencePanel
        anchors.right: parent.right
        anchors.rightMargin: 24
        anchors.verticalCenter: parent.verticalCenter
        presence: bubbleServer.presence
        z: 1
    }

    Connections {
        target: presencePanel
        function onInputRegionDirty() { presenceRegionRefresh.restart() }
    }

    Timer {
        id: presenceRegionRefresh
        interval: 0
        onTriggered: inputRegionController.refresh()
    }
}
