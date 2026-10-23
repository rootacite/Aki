import QtQuick

Canvas {
    id: canvas

    anchors.fill: parent
    anchors.margins: 8
    opacity: 0.30

    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    function drawLeaf(ctx, x, y, size, rotation, alpha) {
        ctx.save()
        ctx.translate(x, y)
        ctx.rotate(rotation)
        ctx.scale(size / 100, size / 100)
        ctx.globalAlpha = alpha
        ctx.beginPath()
        ctx.moveTo(50, 4)
        ctx.lineTo(60, 24)
        ctx.lineTo(78, 14)
        ctx.lineTo(72, 34)
        ctx.lineTo(94, 30)
        ctx.lineTo(81, 46)
        ctx.lineTo(98, 55)
        ctx.lineTo(80, 62)
        ctx.lineTo(85, 82)
        ctx.lineTo(65, 70)
        ctx.lineTo(60, 92)
        ctx.lineTo(52, 76)
        ctx.lineTo(52, 98)
        ctx.lineTo(48, 98)
        ctx.lineTo(48, 76)
        ctx.lineTo(40, 92)
        ctx.lineTo(35, 70)
        ctx.lineTo(15, 82)
        ctx.lineTo(20, 62)
        ctx.lineTo(2, 55)
        ctx.lineTo(19, 46)
        ctx.lineTo(6, 30)
        ctx.lineTo(28, 34)
        ctx.lineTo(22, 14)
        ctx.lineTo(40, 24)
        ctx.closePath()
        ctx.fill()
        ctx.restore()
    }

    onPaint: {
        const ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        ctx.fillStyle = "#FFFFFF"
        drawLeaf(ctx, width * 0.82, height * 0.24, width * 0.52, -0.38, 0.55)
        drawLeaf(ctx, width * 0.97, height * 0.73, width * 0.28, 0.72, 0.34)
        drawLeaf(ctx, width * 0.60, height * 0.94, width * 0.18, -0.95, 0.24)
    }
}
