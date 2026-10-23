import QtQuick

Canvas {
    id: canvas

    anchors.fill: parent
    anchors.margins: 8
    opacity: 0.46

    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    function drawSnowflake(ctx, x, y, size, rotation, alpha) {
        ctx.save()
        ctx.translate(x, y)
        ctx.rotate(rotation)
        ctx.globalAlpha = alpha
        ctx.strokeStyle = "#FFFFFF"
        ctx.lineWidth = Math.max(1, size * 0.022)
        ctx.lineCap = "round"
        for (let arm = 0; arm < 6; ++arm) {
            ctx.rotate(Math.PI / 3)
            ctx.beginPath()
            ctx.moveTo(0, 0)
            ctx.lineTo(0, -size / 2)
            ctx.moveTo(0, -size * 0.28)
            ctx.lineTo(size * 0.14, -size * 0.42)
            ctx.moveTo(0, -size * 0.28)
            ctx.lineTo(-size * 0.14, -size * 0.42)
            ctx.moveTo(0, -size * 0.42)
            ctx.lineTo(size * 0.09, -size * 0.52)
            ctx.moveTo(0, -size * 0.42)
            ctx.lineTo(-size * 0.09, -size * 0.52)
            ctx.stroke()
        }
        ctx.restore()
    }

    onPaint: {
        const ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        drawSnowflake(ctx, width * 0.83, height * 0.24, width * 0.50, 0.0, 0.85)
        drawSnowflake(ctx, width * 0.97, height * 0.73, width * 0.24, 0.25, 0.55)
        drawSnowflake(ctx, width * 0.61, height * 0.94, width * 0.16, 0.72, 0.42)
    }
}
