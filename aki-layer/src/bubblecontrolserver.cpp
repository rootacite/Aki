#include "bubblecontrolserver.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QLocalSocket>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QStandardPaths>

#include <utility>

BubbleControlServer::BubbleControlServer(QObject *parent)
    : QObject(parent)
{
    connect(&m_server, &QLocalServer::newConnection, this, &BubbleControlServer::acceptConnections);
}

bool BubbleControlServer::listen(QString *error)
{
    QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (runtime.isEmpty())
        runtime = QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation);
    if (runtime.isEmpty())
        runtime = QDir::tempPath();

    m_socketPath = qEnvironmentVariable("AKI_LAYER_SOCKET");
    if (m_socketPath.isEmpty())
        m_socketPath = runtime + QStringLiteral("/aki-layer.sock");

    QDir().mkpath(QFileInfo(m_socketPath).absolutePath());
    QFile::remove(m_socketPath);
    m_server.setSocketOptions(QLocalServer::UserAccessOption);
    if (!m_server.listen(m_socketPath)) {
        if (error != nullptr)
            *error = m_server.errorString();
        return false;
    }
    QFile::setPermissions(
        m_socketPath,
        QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    return true;
}

QString BubbleControlServer::socketPath() const
{
    return m_socketPath;
}

void BubbleControlServer::notifyBubbleDismissed(const QString &bubbleId)
{
    QByteArray line = QByteArrayLiteral("dismissed");
    if (!bubbleId.isEmpty()) {
        line += ' ';
        line += bubbleId.toUtf8();
    }
    line += '\n';
    broadcast(line);
}

void BubbleControlServer::acceptConnections()
{
    while (QLocalSocket *client = m_server.nextPendingConnection()) {
        m_clients.append(client);
        m_buffers.insert(client, QByteArray());
        connect(client, &QLocalSocket::readyRead, this, [this, client] { readClient(client); });
        connect(client, &QLocalSocket::disconnected, this, [this, client] { dropClient(client); });
    }
}

void BubbleControlServer::readClient(QLocalSocket *client)
{
    QByteArray &buffer = m_buffers[client];
    buffer += client->readAll();
    while (true) {
        const qsizetype newline = buffer.indexOf('\n');
        if (newline < 0)
            return;
        const QByteArray line = buffer.left(newline).trimmed();
        buffer.remove(0, newline + 1);
        if (line.isEmpty())
            continue;
        const QString bubbleId = QStringLiteral("ctrl-%1").arg(m_nextBubbleId++);
        QString text = QString::fromUtf8(line);
        QString character = QStringLiteral("Aki");
        if (line.startsWith('{')) {
            QJsonParseError parseError;
            const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
            if (parseError.error == QJsonParseError::NoError && document.isObject()) {
                const QJsonObject object = document.object();
                text = object.value(QStringLiteral("text")).toString().trimmed();
                character = object.value(QStringLiteral("character")).toString().trimmed();
            }
        }
        if (text.isEmpty())
            continue;
        if (character.isEmpty())
            character = QStringLiteral("Aki");
        emit messageReceived(text, character, bubbleId);
        client->write(QByteArrayLiteral("queued ") + bubbleId.toUtf8() + '\n');
        client->flush();
    }
}

void BubbleControlServer::dropClient(QLocalSocket *client)
{
    m_clients.removeAll(client);
    m_buffers.remove(client);
    client->deleteLater();
}

void BubbleControlServer::broadcast(const QByteArray &line)
{
    for (QLocalSocket *client : std::as_const(m_clients)) {
        if (client->state() != QLocalSocket::ConnectedState)
            continue;
        client->write(line);
        client->flush();
    }
}
