import QtQuick

Canvas {
    id: canvas

    anchors.fill: parent
    anchors.margins: 8
    opacity: 0.42

    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    function drawHeadphones(ctx, x, y, size, alpha) {
        ctx.save()
        ctx.translate(x, y)
        ctx.globalAlpha = alpha
        ctx.strokeStyle = "#FFFFFF"
        ctx.fillStyle = "#FFFFFF"
        ctx.lineWidth = Math.max(1.5, size * 0.075)
        ctx.lineCap = "round"
        ctx.beginPath()
        ctx.arc(0, 0, size * 0.34, Math.PI, Math.PI * 2)
        ctx.stroke()
        ctx.beginPath()
        ctx.arc(-size * 0.34, size * 0.08, size * 0.13, 0, Math.PI * 2)
        ctx.fill()
        ctx.beginPath()
        ctx.arc(size * 0.34, size * 0.08, size * 0.13, 0, Math.PI * 2)
        ctx.fill()
        ctx.restore()
    }

    function drawBrush(ctx, x, y, size, rotation, alpha) {
        ctx.save()
        ctx.translate(x, y)
        ctx.rotate(rotation)
        ctx.globalAlpha = alpha
        ctx.strokeStyle = "#FFFFFF"
        ctx.fillStyle = "#FFFFFF"
        ctx.lineCap = "round"
        ctx.lineWidth = size * 0.07
        ctx.beginPath()
        ctx.moveTo(-size * 0.36, size * 0.28)
        ctx.lineTo(size * 0.12, -size * 0.20)
        ctx.stroke()
        ctx.lineWidth = size * 0.16
        ctx.beginPath()
        ctx.moveTo(size * 0.14, -size * 0.22)
        ctx.lineTo(size * 0.22, -size * 0.30)
        ctx.stroke()
        ctx.beginPath()
        ctx.moveTo(size * 0.20, -size * 0.30)
        ctx.lineTo(size * 0.46, -size * 0.47)
        ctx.lineTo(size * 0.34, -size * 0.16)
        ctx.closePath()
        ctx.fill()
        ctx.restore()
    }

    function drawSplash(ctx, x, y, radius, alpha) {
        ctx.save()
        ctx.globalAlpha = alpha
        ctx.fillStyle = "#FFFFFF"
        ctx.beginPath()
        ctx.arc(x, y, radius, 0, Math.PI * 2)
        ctx.fill()
        ctx.restore()
    }

    onPaint: {
        const ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        drawHeadphones(ctx, width * 0.83, height * 0.28, width * 0.36, 0.85)
        drawBrush(ctx, width * 0.72, height * 0.75, width * 0.48, -0.52, 0.72)
        drawSplash(ctx, width * 0.93, height * 0.68, width * 0.025, 0.55)
        drawSplash(ctx, width * 0.87, height * 0.92, width * 0.018, 0.45)
        drawSplash(ctx, width * 0.96, height * 0.88, width * 0.012, 0.5)
    }
}
