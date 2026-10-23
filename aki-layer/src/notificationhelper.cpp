#include "notificationhelper.h"

#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusReply>
#include <QDebug>
#include <QStringList>
#include <QVariantMap>

NotificationHelper::NotificationHelper(QObject *parent)
    : QObject(parent)
{
}

bool NotificationHelper::notify(const QString &input)
{
    const QString body = QStringLiteral("You input %1").arg(input);
    QDBusMessage message = QDBusMessage::createMethodCall(
        QStringLiteral("org.freedesktop.Notifications"),
        QStringLiteral("/org/freedesktop/Notifications"),
        QStringLiteral("org.freedesktop.Notifications"),
        QStringLiteral("Notify"));
    message << QStringLiteral("Aki")
            << 0u
            << QStringLiteral("dialog-information")
            << QStringLiteral("Aki")
            << body
            << QStringList()
            << QVariantMap()
            << 5000;

    QDBusConnection bus = QDBusConnection::sessionBus();
    if (!bus.isConnected()) {
        const QString error = QStringLiteral("session D-Bus is unavailable");
        qWarning("aki-layer: %s", qPrintable(error));
        emit failed(error);
        return false;
    }

    const QDBusReply<uint> reply = bus.call(message);
    if (!reply.isValid()) {
        const QString error = reply.error().message();
        qWarning("aki-layer: notification failed: %s", qPrintable(error));
        emit failed(error);
        return false;
    }
    return true;
}
