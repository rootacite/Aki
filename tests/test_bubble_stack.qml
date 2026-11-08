import QtQuick
import QtTest
import "../aki-layer/qml"

TestCase {
    name: "BubbleStackTests"
    width: 1920
    height: 1080
    visible: false

    BubbleStack {
        id: stack
        anchors.fill: parent
    }

    SignalSpy {
        id: dismissedSpy
        target: stack
        signalName: "bubbleDismissed"
    }

    function init() {
        stack.clearAllNotifications()
        dismissedSpy.clear()
    }

    function test_max_five_active_bubbles_and_auto_folding() {
        // Push 8 messages
        for (let i = 1; i <= 8; ++i) {
            stack.pushMessage("Message " + i, "Aki")
        }

        // Active model count must not exceed 5
        compare(stack.maxActiveCount, 5)
        compare(stack.activeCount, 5)
        // 8 pushed, exactly 5 active, 3 automatically folded
        compare(stack.missedCount, 3)
        compare(stack.missedPeekLayers, 3)
        verify(stack.missedPeekHeight > 0)

        // When folded, the missedRegion has 72px clearance from the right screen edge
        compare(stack.missedRightMargin, 72)
    }

    function test_clear_all_notifications() {
        for (let i = 1; i <= 7; ++i) {
            stack.pushMessage("Test " + i, "Fuyu")
        }

        compare(stack.missedPeekLayers, 2)
        dismissedSpy.clear()

        // Clear all notifications in one click
        stack.clearAllNotifications()

        compare(stack.missedPeekLayers, 0)
        compare(stack.missedExpanded, false)
        // 5 active + 2 missed = 7 dismissed signals emitted
        compare(dismissedSpy.count, 7)
    }

    function test_toggle_drawer_expansion() {
        stack.pushMessage("Fold me 1", "Haru")
        stack.pushMessage("Fold me 2", "Haru")
        stack.pushMessage("Fold me 3", "Haru")
        stack.pushMessage("Fold me 4", "Haru")
        stack.pushMessage("Fold me 5", "Haru")
        stack.pushMessage("Fold me 6", "Haru") // Causes 1 to fold

        compare(stack.missedExpanded, false)
        compare(stack.missedPeekLayers, 1)

        // Simulate opening drawer
        stack.missedExpanded = true
        compare(stack.missedExpanded, true)

        // Clearing closes drawer
        stack.clearAllNotifications()
        compare(stack.missedExpanded, false)
        compare(stack.missedPeekLayers, 0)
    }
}
