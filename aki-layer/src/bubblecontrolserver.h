#pragma once

#include <QHash>
#include <QList>
#include <QLocalServer>
#include <QObject>
#include <QString>

class QLocalSocket;

class BubbleControlServer final : public QObject
{
    Q_OBJECT

public:
    explicit BubbleControlServer(QObject *parent = nullptr);

    bool listen(QString *error = nullptr);
    QString socketPath() const;

public slots:
    void notifyBubbleDismissed(const QString &bubbleId);

signals:
    void messageReceived(const QString &text, const QString &character, const QString &bubbleId);

private:
    void acceptConnections();
    void readClient(QLocalSocket *client);
    void dropClient(QLocalSocket *client);
    void broadcast(const QByteArray &line);

    QLocalServer m_server;
    QList<QLocalSocket *> m_clients;
    QHash<QLocalSocket *, QByteArray> m_buffers;
    QString m_socketPath;
    quint64 m_nextBubbleId = 1;
};
