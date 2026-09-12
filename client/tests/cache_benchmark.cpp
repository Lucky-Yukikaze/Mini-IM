// Measures the production cache snapshot without network or page-rendering costs.
#include "core/sync/statestore.h"

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QSet>
#include <iostream>
#include <stdexcept>

namespace
{
QJsonObject MeasureEvents(MiniImStateStore& store, int count)
{
    const QString conversation = QStringLiteral("conversation-0");
    const auto recent = store.messagePage(conversation).value("messages").toList();
    const auto last = recent.isEmpty() ? 0 : recent.last().toMap().value("seq").toULongLong();
    const auto initialUnread = store.unreadTotal();
    QJsonArray samples;
    for (int index = 1; index <= count; ++index)
    {
        const auto identity = QStringLiteral("measurement-event-%1").arg(index);
        MiniImStateEvent event{static_cast<quint64>(index), identity, QStringLiteral("message"),
            {{"id", identity}, {"conversationId", conversation}, {"senderId", "bob"},
             {"seq", QVariant::fromValue(last + index)}, {"text", QString(128, QChar('x'))},
             {"recalled", false}, {"burned", false}}};
        QVector<MiniImStateEvent> applied;
        QElapsedTimer timer;
        timer.start();
        if (!store.apply({event}, &applied))
        {
            throw std::runtime_error(store.errorString().toStdString());
        }
        const double applyMs = timer.nsecsElapsed() / 1000000.0;
        timer.restart();
        const auto unread = store.unreadTotal();
        const double unreadMs = timer.nsecsElapsed() / 1000000.0;
        if (applied.size() != 1 || unread != initialUnread + index || store.cursor() != index)
        {
            throw std::runtime_error("event measurement lost a message, unread count or cursor");
        }
        samples.append(QJsonObject{{"applyMs", applyMs}, {"unreadMs", unreadMs},
            {"totalMs", applyMs + unreadMs}});
    }
    return {{"samples", samples}, {"finalUnread", static_cast<double>(store.unreadTotal())},
        {"cursor", static_cast<double>(store.cursor())}};
}
}

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    const bool verify = argc == 3 && app.arguments().at(2) == QStringLiteral("--verify-history");
    const int eventCount = argc == 4 && app.arguments().at(2) == "--measure-events"
        ? app.arguments().at(3).toInt() : 0;
    if (argc != 2 && !verify && !(eventCount >= 1 && eventCount <= 1000))
    {
        std::cerr << "usage: mini_im_cache_benchmark <isolated-cache-directory> [--verify-history | --measure-events 1..1000]\n";
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
    QJsonObject result{
        {"database", store.databasePath()}, {"qtVersion", qVersion()}, {"openMs", openMs}, {"snapshotMs", snapshotMs},
        {"encodeMs", encodeMs}, {"encodedBytes", static_cast<double>(encoded.size())},
        {"messages", static_cast<double>(snapshot.value("recentMessages").toList().size())},
        {"conversations", static_cast<double>(snapshot.value("conversations").toList().size())},
        {"deliveries", static_cast<double>(snapshot.value("deliveries").toList().size())},
        {"readCounts", static_cast<double>(snapshot.value("readCounts").toList().size())},
        {"unreadTotal", snapshot.value("unreadTotal").toInt()}
    };
    if (verify)
    {
        QSet<QString> ids;
        int deliveries = 0, counts = 0, pages = 0;
        for (const auto& item : snapshot.value("conversations").toList())
        {
            const auto conversation = item.toMap().value("id").toString();
            QString cursor;
            while (true)
            {
                const auto page = store.messagePage(conversation, cursor);
                if (!page.value("ok").toBool())
                {
                    std::cerr << page.value("error").toString().toStdString();
                    return 1;
                }
                const auto messages = page.value("messages").toList();
                if (messages.size() > 50)
                {
                    return 1;
                }
                for (const auto& message : messages)
                {
                    const auto id = message.toMap().value("id").toString();
                    if (id.isEmpty() || ids.contains(id))
                    {
                        return 1;
                    }
                    ids.insert(id);
                }
                deliveries += page.value("deliveries").toList().size();
                counts += page.value("readCounts").toList().size();
                ++pages;
                if (!page.value("hasMore").toBool())
                {
                    break;
                }
                const auto next = page.value("cursor").toString();
                if (next.isEmpty() || next == cursor || messages.isEmpty())
                {
                    return 1;
                }
                cursor = next;
            }
        }
        result.insert("historyMessages", ids.size());
        result.insert("historyDeliveries", deliveries);
        result.insert("historyReadCounts", counts);
        result.insert("historyPages", pages);
    }
    if (eventCount > 0)
    {
        try
        {
            result.insert("eventMeasurement", MeasureEvents(store, eventCount));
        }
        catch (const std::exception& error)
        {
            std::cerr << error.what() << '\n';
            return 1;
        }
    }
    std::cout << QJsonDocument(result).toJson(QJsonDocument::Compact).constData() << '\n';
    return 0;
}
