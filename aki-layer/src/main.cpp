#include "notificationhelper.h"
#include "inputregioncontroller.h"
#include "bubblecontrolserver.h"

#include <QCommandLineParser>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QQuickWindow>

int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);
    QGuiApplication::setApplicationName(QStringLiteral("aki-layer"));
    QGuiApplication::setApplicationDisplayName(QStringLiteral("Aki Layer"));
    QGuiApplication::setOrganizationName(QStringLiteral("Aki"));

    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Aki transparent layer-shell canvas"));
    parser.addHelpOption();
    parser.addVersionOption();
    QCommandLineOption notifyOption(
        QStringLiteral("notify"),
        QStringLiteral("Send one test notification and exit."),
        QStringLiteral("text"));
    parser.addOption(notifyOption);
    QCommandLineOption demoOption(
        QStringLiteral("demo-bubbles"),
        QStringLiteral("Show N sample notification bubbles at startup."),
        QStringLiteral("count"),
        QStringLiteral("0"));
    parser.addOption(demoOption);
    parser.process(app);

    if (parser.isSet(notifyOption)) {
        NotificationHelper notifications;
        return notifications.notify(parser.value(notifyOption)) ? 0 : 1;
    }

    QQuickStyle::setStyle(QStringLiteral("Basic"));

    InputRegionController inputRegionController;
    BubbleControlServer bubbleServer;
    QString controlError;
    if (!bubbleServer.listen(&controlError))
        qWarning("aki-layer: control socket unavailable: %s", qPrintable(controlError));
    else
        qInfo("aki-layer: control socket %s", qPrintable(bubbleServer.socketPath()));
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("inputRegionController"), &inputRegionController);
    engine.rootContext()->setContextProperty(QStringLiteral("bubbleServer"), &bubbleServer);
    bool demoCountOk = false;
    const int demoBubbleCount = parser.value(demoOption).toInt(&demoCountOk);
    engine.rootContext()->setContextProperty(
        QStringLiteral("demoBubbleCount"), demoCountOk ? qMax(0, demoBubbleCount) : 0);
    QObject::connect(
        &engine,
        &QQmlApplicationEngine::objectCreationFailed,
        &app,
        [] { QCoreApplication::exit(1); },
        Qt::QueuedConnection);
    engine.load(QUrl(QStringLiteral("qrc:/qml/Main.qml")));
    if (engine.rootObjects().isEmpty()) {
        return 1;
    }

    auto *window = qobject_cast<QQuickWindow *>(engine.rootObjects().constFirst());
    inputRegionController.setWindow(window);
    if (window != nullptr) {
        QObject::connect(window, &QQuickWindow::widthChanged, &inputRegionController, &InputRegionController::refresh);
        QObject::connect(window, &QQuickWindow::heightChanged, &inputRegionController, &InputRegionController::refresh);
    }
    return app.exec();
}
