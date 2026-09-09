#include "core/sync/coordinator.h"

#include <QCoreApplication>
#include <QEventLoop>
#include <QThread>
#include <QSqlError>
#include <QTemporaryDir>
#include <QUuid>
#include <iostream>
#include <stdexcept>

namespace
{
void Require(bool condition, const char* message)
{
    if (!condition)
    {
        throw std::runtime_error(message);
    }
}

im::sync::SyncEvent Message(quint64 position)
{
    im::sync::SyncEvent event;
    event.set_global_seq(position);
    event.set_event_id("event-" + std::to_string(position));
    auto* message = event.mutable_message();
    message->set_message_id("message-" + std::to_string(position));
    message->set_client_msg_id("intent-" + std::to_string(position));
    message->set_conversation_id("conversation");
    message->set_sender_id("alice");
    message->set_seq(position);
    message->set_type(im::common::MSG_TEXT);
    message->set_content("body");
    return event;
}

struct Fixture
{
    QTemporaryDir root;
    MiniImStateStore store;
    QVector<im::envelope::Envelope> sent;
    int requestCount = 0;
    int readyCount = 0;
    int appliedCount = 0;
    int failedCount = 0;
    int errors = 0;
    MiniImSyncCoordinator sync;

    Fixture() : sync(store, [this](quint64 cursor, quint32 limit)
        {
            im::envelope::Envelope request;
            request.set_request_id("request-" + std::to_string(++requestCount));
            request.mutable_sync_request()->set_global_cursor(cursor);
            request.mutable_sync_request()->set_limit(limit);
            return request;
        }, [this](const std::string& payload)
        {
            im::envelope::Envelope request;
            Require(request.ParseFromString(payload), "valid serialized sync request");
            sent.append(request);
            return true;
        })
    {
        Require(store.open(root.path(), "endpoint", "bob", "device"), "open actual sync store");
        QObject::connect(&sync, &MiniImSyncCoordinator::ready, [&]() { ++readyCount; });
        QObject::connect(&sync, &MiniImSyncCoordinator::failed, [&]() { ++failedCount; });
        QObject::connect(&sync, &MiniImSyncCoordinator::errorRaised, [&]() { ++errors; });
        QObject::connect(&sync, &MiniImSyncCoordinator::eventApplied, [&](const MiniImStateEvent& event)
        {
            ++appliedCount;
            const QString objects = event.type == QStringLiteral("readCount")
                ? QStringLiteral("readCounts") : QStringLiteral("recentMessages");
            Require(!store.snapshot().value(objects).toList().isEmpty(), "state committed before delivery");
        });
        sync.start();
    }

    void reply(const std::string& id, std::initializer_list<im::sync::SyncEvent> events,
        bool more = false, quint64 advertised = 0)
    {
        im::envelope::Envelope envelope;
        envelope.set_request_id(id);
        auto* response = envelope.mutable_sync_response();
        response->set_has_more(more);
        response->set_new_global_cursor(advertised);
        for (const auto& event : events)
        {
            response->add_events()->CopyFrom(event);
        }
        Require(sync.handleEnvelope(envelope), "sync envelope consumed");
    }

    void sql(const QString& statement)
    {
        const auto name = QUuid::createUuid().toString();
        {
            auto database = QSqlDatabase::addDatabase("QSQLITE", name);
            database.setDatabaseName(store.databasePath());
            Require(database.open(), "open failure-injection connection");
            QSqlQuery query(database);
            Require(query.exec(statement), query.lastError().text().toStdString().c_str());
        }
        QSqlDatabase::removeDatabase(name);
    }
};


void WaitUntil(const std::function<bool()>& predicate, int timeoutMs = 2000)
{
    QElapsedTimer timeout;
    timeout.start();
    while (!predicate() && timeout.elapsed() < timeoutMs)
    {
        QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
        QThread::msleep(2);
    }
    Require(predicate(), "timed out waiting for sync confirmation");
}

void CheckDurableConfirmationRetries()
{
    Fixture fixture;
    fixture.reply(fixture.sent.first().request_id(), {Message(1), Message(3)}, true, 3);
    WaitUntil([&]() { return fixture.sent.last().has_sync_applied(); });
    const auto original = fixture.sent.last();
    const auto id = QString::fromStdString(original.request_id());
    Require(original.sync_applied().global_cursor() == 1, "confirm only committed continuous prefix");
    Require(fixture.store.pendingConfirmation().value("requestId") == id, "intent durable before sending");
    const auto count = fixture.sent.size();
    fixture.sync.handleConfirmationResult(id, false, 503, {});
    WaitUntil([&]() { return fixture.sent.size() > count && fixture.sent.last().has_sync_applied(); }, 6500);
    Require(fixture.sent.last().SerializeAsString() == original.SerializeAsString(), "retry same identity and body");
    fixture.sync.stop();
    fixture.store.close();
    Require(fixture.store.open(fixture.root.path(), "endpoint", "bob", "device"), "reopen durable state");
    fixture.sync.start();
    WaitUntil([&]() { return fixture.sent.last().has_sync_applied(); });
    Require(fixture.sent.last().request_id() == original.request_id(), "restart uses original request");
    Require(fixture.sent.last().sync_applied().global_cursor() == 1, "restart does not widen pending intent");
    fixture.sql("CREATE TRIGGER reject_ack BEFORE DELETE ON metadata WHEN OLD.key='sync_confirmation' "
        "BEGIN SELECT RAISE(ABORT,'injected ack persistence failure'); END");
    fixture.sync.handleConfirmationResult(id, true, 0, "1");
    Require(fixture.failedCount == 1 && fixture.store.confirmedCursor() == 0, "ack save failure stops confirmation");
    Require(!fixture.store.pendingConfirmation().isEmpty(), "ack save failure remains recoverable");
    fixture.sql("DROP TRIGGER reject_ack");
    fixture.sync.start();
    WaitUntil([&]() { return fixture.sent.last().has_sync_applied(); });
    fixture.sync.handleConfirmationResult(id, true, 0, "1");
    Require(fixture.store.confirmedCursor() == 1, "matching response settles original intent");
    Require(!fixture.sync.handleConfirmationResult(id, true, 0, "1"), "duplicate ack has no repeated effect");
    fixture.reply("", {Message(2)});
    WaitUntil([&]() { return fixture.sent.last().has_sync_applied() && fixture.sent.last().sync_applied().global_cursor() == 3; });
    Require(fixture.sent.last().request_id() != original.request_id(), "new prefix uses new identity");
    fixture.sync.handleConfirmationResult(QString::fromStdString(fixture.sent.last().request_id()), true, 0, "99");
    Require(fixture.failedCount == 2 && fixture.store.confirmedCursor() == 1, "wrong response cursor cannot settle intent");
}

void CheckConfirmationSaveFailureAndDeliveryMapping()
{
    Fixture fixture;
    fixture.reply(fixture.sent.first().request_id(), {Message(1)}, false, 1);
    fixture.sql("CREATE TRIGGER reject_confirmation BEFORE INSERT ON metadata WHEN NEW.key='sync_confirmation' "
        "BEGIN SELECT RAISE(ABORT,'injected confirmation persistence failure'); END");
    WaitUntil([&]() { return fixture.failedCount == 1; });
    for (const auto& request : fixture.sent)
    {
        Require(!request.has_sync_applied(), "unsaved intent must never reach transport");
    }
    fixture.sql("DROP TRIGGER reject_confirmation");
    fixture.sync.start();
    im::sync::SyncEvent event;
    event.set_global_seq(2);
    event.set_event_id("delivery-2");
    auto* delivery = event.mutable_delivery_updated();
    delivery->set_event_id("delivery-2");
    delivery->set_message_id("message-1");
    delivery->set_conversation_id("conversation");
    delivery->set_user_id("bob");
    delivery->set_status("delivered");
    delivery->set_delivered_at_ms(12);
    fixture.reply(fixture.sent.last().request_id(), {event}, false, 2);
    const auto restored = fixture.store.snapshot().value("deliveries").toList();
    Require(restored.size() == 1 && restored.first().toMap().value("globalSeq").toInt() == 2,
        "delivery mapped and saved with stable event position");
    Require(fixture.store.cursor() == 2 && fixture.sync.isReady(), "delivery is a supported sync event");
}

void CheckReadCountMapping()
{
    Fixture fixture;
    im::sync::SyncEvent event;
    event.set_global_seq(2);
    event.set_event_id("count-2");
    auto* count = event.mutable_read_count_updated();
    count->set_event_id("count-2");
    count->set_message_id("message-1");
    count->set_conversation_id("conversation");
    count->set_unread_count(0);
    fixture.reply("", {event});
    fixture.reply(fixture.sent.first().request_id(), {Message(1)}, false, 2);
    const auto snapshot = fixture.store.snapshot();
    Require(fixture.store.cursor() == 2 && fixture.sync.isReady(), "read count is a supported sync event");
    const auto saved = snapshot.value("readCounts").toList().first().toMap();
    Require(saved.value("globalSeq").toInt() == 2 && saved.value("unreadCount").toInt() == 0,
        "read count lost its identity, position or zero count");
    Require(snapshot.value("recentMessages").toList().first().toMap().value("unreadCount").toInt() == 0,
        "message arriving after count changed its value");
}

void CheckPaginationAndLateReply()
{
    Fixture fixture;
    const auto first = fixture.sent.last().request_id();
    fixture.reply("", {Message(3)});
    Require(fixture.store.cursor() == 0 && !fixture.sync.isReady(), "live event cannot skip history");
    Require(fixture.sent.size() == 1, "one active sync request");
    fixture.reply(first, {Message(1)}, true, 1);
    Require(fixture.sent.size() == 2, "request next page after committing first");
    Require(fixture.sent.last().sync_request().global_cursor() == 1, "next page uses stored continuous cursor");
    const auto second = fixture.sent.last().request_id();
    fixture.reply("", {Message(2)});
    Require(fixture.store.cursor() == 3 && !fixture.sync.isReady(), "filled live gap still waits for pending response");
    fixture.reply(first, {Message(1)});
    Require(!fixture.sync.isReady() && fixture.readyCount == 0, "late prior page cannot finish a current request");
    fixture.reply(second, {Message(2), Message(3)}, false, 3);
    Require(fixture.sync.isReady() && fixture.readyCount == 1, "matching final page releases writes");
    Require(fixture.appliedCount == 3, "live and replay events delivered once");
    fixture.reply(second, {Message(2), Message(3)});
    Require(fixture.readyCount == 1 && fixture.appliedCount == 3, "duplicate response has no repeated side effects");
}

void CheckNonAdvancingPages()
{
    Fixture fixture;
    const auto request = fixture.sent.last().request_id();
    fixture.reply("", {Message(3)});
    fixture.reply(request, {}, false, 3);
    Require(!fixture.sync.isReady() && fixture.sent.size() == 1, "empty gap page retains original request");
    fixture.reply(request, {Message(3)}, true, 3);
    Require(!fixture.sync.isReady() && fixture.sent.size() == 1, "non-advancing page cannot cause a request loop");
    Require(fixture.errors == 1, "one gap warning per active request");
    fixture.reply(request, {Message(1), Message(2), Message(3)}, false, 3);
    Require(fixture.sync.isReady() && fixture.store.cursor() == 3, "original request can heal an unresolved gap");

    Fixture advertised;
    advertised.reply(advertised.sent.last().request_id(), {}, false, 100);
    Require(!advertised.sync.isReady() && advertised.store.cursor() == 0, "advertised position never advances cache");
}

void CheckFailedCommitAndStop()
{
    Fixture fixture;
    const auto first = fixture.sent.last().request_id();
    fixture.sql("CREATE TRIGGER reject_sync BEFORE INSERT ON seen BEGIN SELECT RAISE(ABORT,'test rollback'); END");
    fixture.reply(first, {Message(1)}, false, 1);
    Require(fixture.failedCount == 1 && fixture.appliedCount == 0, "failed commit emits no business event");
    Require(fixture.store.cursor() == 0 && !fixture.sync.isReady(), "failed commit never releases writes");
    Require(fixture.store.snapshot().value("recentMessages").toList().isEmpty(), "failed transaction rolls back content");
    fixture.reply(first, {Message(1)}, false, 1);
    Require(fixture.appliedCount == 0, "stopped coordinator ignores queued responses");
    fixture.sql("DROP TRIGGER reject_sync");
    fixture.sync.start();
    fixture.reply(fixture.sent.last().request_id(), {Message(1)}, false, 1);
    Require(fixture.sync.isReady() && fixture.appliedCount == 1, "restart resumes from committed state");
    fixture.sync.stop();
    fixture.reply("", {Message(2)});
    Require(fixture.store.cursor() == 1 && !fixture.sync.isReady(), "disconnect stops incoming application");
}

void CheckControlMetadataReplay()
{
    Fixture fixture;
    fixture.reply(fixture.sent.last().request_id(), {Message(1)}, false, 1);
    int metadata = 0;
    QObject::connect(&fixture.sync, &MiniImSyncCoordinator::fileUpdated,
        [&](const im::file::FileUpdated& file)
        {
            Require(file.file_size() == 64 && file.sha256() == "digest", "control metadata remains intact");
            ++metadata;
        });
    im::envelope::Envelope envelope;
    envelope.set_seq(2);
    auto* file = envelope.mutable_file_updated();
    file->set_event_id("event-2");
    file->set_file_id("file");
    file->set_conversation_id("conversation");
    file->set_file_size(64);
    file->set_sha256("digest");
    file->set_version(1);
    fixture.sync.handleEnvelope(envelope);
    fixture.sync.handleEnvelope(envelope);
    Require(metadata == 2 && fixture.appliedCount == 2, "duplicate control metadata still reaches file task once per packet");
    Require(fixture.store.cursor() == 2, "duplicate file event does not advance cursor twice");
}
}

int main(int argc, char** argv)
{
    QCoreApplication application(argc, argv);
    try
    {
        CheckReadCountMapping();
        CheckDurableConfirmationRetries();
        CheckConfirmationSaveFailureAndDeliveryMapping();
        CheckPaginationAndLateReply();
        CheckNonAdvancingPages();
        CheckFailedCommitAndStop();
        CheckControlMetadataReplay();
        std::cout << "sync coordination: pagination, late replies, gaps, atomic failure, stop and metadata passed\n";
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
