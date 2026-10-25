#pragma once

#include <QHash>
#include <QList>
#include <QLocalServer>
#include <QObject>
#include <QString>
#include <QVariantMap>

class QLocalSocket;

class BubbleControlServer final : public QObject
{
    Q_OBJECT
    Q_PROPERTY(QVariantMap presence READ presence NOTIFY presenceChanged)

public:
    explicit BubbleControlServer(QObject *parent = nullptr);

    bool listen(QString *error = nullptr);
    QString socketPath() const;
    QVariantMap presence() const;

public slots:
    void notifyBubbleDismissed(const QString &bubbleId);

signals:
    void messageReceived(const QString &text, const QString &character, const QString &bubbleId);
    void presenceChanged();

private:
    void acceptConnections();
    void readClient(QLocalSocket *client);
    void dropClient(QLocalSocket *client);
    void broadcast(const QByteArray &line);

    QLocalServer m_server;
    QList<QLocalSocket *> m_clients;
    QHash<QLocalSocket *, QByteArray> m_buffers;
    QString m_socketPath;
    QVariantMap m_presence;
    quint64 m_nextBubbleId = 1;
};
