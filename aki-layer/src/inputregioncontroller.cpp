#include "inputregioncontroller.h"

#include <QQuickItem>
#include <QQuickWindow>
#include <QRegion>

InputRegionController::InputRegionController(QObject *parent)
    : QObject(parent)
{
}

void InputRegionController::setWindow(QQuickWindow *window)
{
    m_window = window;
    refresh();
}

void InputRegionController::refresh()
{
    QQuickWindow *window = m_window.data();
    if (window == nullptr)
        return;

    // Layer-shell surfaces own the whole output by default. QtWayland maps
    // QWindow::setMask() to wl_surface.set_input_region(), so only items that
    // opt in with `blocksInput: true` receive pointer input.
    const QRectF windowRect(0, 0, window->width(), window->height());
    QRegion inputRegion;
    const auto collect = [&inputRegion, &windowRect](QQuickItem *item, QRectF currentClip, const auto &self) -> void {
        if (!item->isVisible() || item->width() <= 0 || item->height() <= 0)
            return;

        const QRectF itemSceneRect = item->mapRectToScene(QRectF(0, 0, item->width(), item->height()));
        if (item->clip()) {
            currentClip = currentClip.intersected(itemSceneRect);
            if (currentClip.isEmpty())
                return;
        }

        if (item->property("blocksInput").toBool()) {
            const QRectF effectiveRect = itemSceneRect.intersected(currentClip);
            if (effectiveRect.width() > 0 && effectiveRect.height() > 0)
                inputRegion += effectiveRect.toAlignedRect();
        }

        const QList<QQuickItem *> children = item->childItems();
        for (QQuickItem *child : children)
            self(child, currentClip, self);
    };
    if (QQuickItem *content = window->contentItem())
        collect(content, windowRect, collect);
    // QtWayland treats an empty mask as "clear the mask", which restores the
    // default full-window input region. Keep a 1x1 region outside the surface
    // so the compositor receives an explicit empty input region instead.
    if (inputRegion.isEmpty())
        inputRegion = QRegion(-1, -1, 1, 1);
    window->setMask(inputRegion);
}
