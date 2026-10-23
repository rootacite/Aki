#pragma once

#include <QObject>
#include <QString>

class NotificationHelper final : public QObject
{
    Q_OBJECT

public:
    explicit NotificationHelper(QObject *parent = nullptr);

    Q_INVOKABLE bool notify(const QString &input);

signals:
    void failed(const QString &message);
};
