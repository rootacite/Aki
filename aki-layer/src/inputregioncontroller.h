#pragma once

#include <QObject>
#include <QPointer>

class QQuickWindow;

class InputRegionController final : public QObject
{
    Q_OBJECT

public:
    explicit InputRegionController(QObject *parent = nullptr);

    void setWindow(QQuickWindow *window);

public slots:
    void refresh();

private:
    QPointer<QQuickWindow> m_window;
};
