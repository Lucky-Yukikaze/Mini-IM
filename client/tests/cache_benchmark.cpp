// Measures the production cache snapshot without network or page-rendering costs.
#include "core/sync/statestore.h"

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QJsonObject>
#include <iostream>

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    if (argc != 2)
    {
        std::cerr << "usage: mini_im_cache_benchmark <isolated-cache-directory>\n";
        return 2;
    }
    MiniImStateStore store;
    QElapsedTimer timer;
    timer.start();
    if (!store.open(app.arguments().at(1), QStringLiteral("cache-measurement"),
            QStringLiteral("alice"), QStringLiteral("benchmark-device")))
    {
        std::cerr << store.errorString().toStdString() << '\n';
        return 1;
    }
    const double openMs = timer.nsecsElapsed() / 1000000.0;
    timer.restart();
    const auto snapshot = store.snapshot();
    const double snapshotMs = timer.nsecsElapsed() / 1000000.0;
    if (snapshot.isEmpty())
    {
        std::cerr << store.errorString().toStdString() << '\n';
        return 1;
    }
    timer.restart();
    const auto encoded = QJsonDocument(QJsonObject::fromVariantMap(snapshot)).toJson(QJsonDocument::Compact);
    const double encodeMs = timer.nsecsElapsed() / 1000000.0;
    const QJsonObject result{
        {"database", store.databasePath()}, {"qtVersion", qVersion()}, {"openMs", openMs}, {"snapshotMs", snapshotMs},
        {"encodeMs", encodeMs}, {"encodedBytes", static_cast<double>(encoded.size())},
        {"messages", static_cast<double>(snapshot.value("recentMessages").toList().size())},
        {"conversations", static_cast<double>(snapshot.value("conversations").toList().size())},
        {"deliveries", static_cast<double>(snapshot.value("deliveries").toList().size())},
        {"readCounts", static_cast<double>(snapshot.value("readCounts").toList().size())},
        {"unreadTotal", snapshot.value("unreadTotal").toInt()}
    };
    std::cout << QJsonDocument(result).toJson(QJsonDocument::Compact).constData() << '\n';
    return 0;
}
