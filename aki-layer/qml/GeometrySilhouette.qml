import QtQuick

Canvas {
    id: canvas

    anchors.fill: parent
    anchors.margins: 8
    opacity: 0.36

    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    function line(ctx, x1, y1, x2, y2) {
        ctx.beginPath()
        ctx.moveTo(x1, y1)
        ctx.lineTo(x2, y2)
        ctx.stroke()
    }

    function node(ctx, x, y, radius) {
        ctx.beginPath()
        ctx.arc(x, y, radius, 0, Math.PI * 2)
        ctx.fill()
    }

    function drawMesh(ctx, w, h) {
        const nodes = [
            [0.61, 0.20], [0.76, 0.12], [0.92, 0.22], [0.70, 0.36],
            [0.86, 0.42], [0.98, 0.34], [0.79, 0.58], [0.94, 0.62]
        ]
        const edges = [
            [0, 1], [1, 2], [0, 3], [1, 3], [1, 4], [2, 4], [2, 5],
            [3, 4], [3, 6], [4, 5], [4, 6], [4, 7], [5, 7], [6, 7]
        ]
        ctx.strokeStyle = "#FFFFFF"
        ctx.fillStyle = "#FFFFFF"
        ctx.lineWidth = 1.2
        for (const edge of edges)
            line(ctx, nodes[edge[0]][0] * w, nodes[edge[0]][1] * h,
                 nodes[edge[1]][0] * w, nodes[edge[1]][1] * h)
        for (const point of nodes)
            node(ctx, point[0] * w, point[1] * h, 2.4)
    }

    function drawGeometry(ctx, w, h) {
        ctx.strokeStyle = "#FFFFFF"
        ctx.lineWidth = 1.4
        ctx.beginPath()
        ctx.moveTo(w * 0.60, h * 0.76)
        ctx.lineTo(w * 0.70, h * 0.94)
        ctx.lineTo(w * 0.50, h * 0.94)
        ctx.closePath()
        ctx.stroke()

        ctx.beginPath()
        for (let index = 0; index < 6; ++index) {
            const angle = Math.PI / 3 * index - Math.PI / 6
            const x = w * 0.86 + Math.cos(angle) * w * 0.095
            const y = h * 0.79 + Math.sin(angle) * w * 0.095
            if (index === 0)
                ctx.moveTo(x, y)
            else
                ctx.lineTo(x, y)
        }
        ctx.closePath()
        ctx.stroke()

        ctx.beginPath()
        ctx.arc(w * 0.96, h * 0.94, w * 0.035, 0, Math.PI * 2)
        ctx.stroke()
    }

    onPaint: {
        const ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        ctx.lineCap = "round"
        drawMesh(ctx, width, height)
        drawGeometry(ctx, width, height)
    }
}
