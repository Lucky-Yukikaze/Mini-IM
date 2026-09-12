#include "core/sync/statestore.h"
#include "core/file/cleanup.h"
#include <QSet>
#include <QFile>
#include <QFileInfo>

#include <QCoreApplication>
#include <QSqlError>
#include <QTemporaryDir>
#include <QUuid>
#include <iostream>
#include <limits>
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

void Open(MiniImStateStore& store, const QTemporaryDir& root, const QString& user = QStringLiteral("bob"),
    const QString& endpoint = QStringLiteral("127.0.0.1:4433"), const QString& device = QStringLiteral("device"))
{
    if (!store.open(root.path(), endpoint, user, device))
    {
        throw std::runtime_error(store.errorString().toStdString());
    }
}

void Apply(MiniImStateStore& store, const QVector<MiniImStateEvent>& events, qsizetype count = -1)
{
    QVector<MiniImStateEvent> applied;
    if (!store.apply(events, &applied))
    {
        throw std::runtime_error(store.errorString().toStdString());
    }
    Require(count < 0 || applied.size() == count, "unexpected emitted event count");
}

MiniImStateEvent Message(quint64 position, const QString& id)
{
    return {position, QStringLiteral("event-%1").arg(position), QStringLiteral("message"),
        {{"id", id}, {"conversationId", "conversation"}, {"senderId", "alice"},
         {"seq", QVariant::fromValue(position)}, {"text", "private-message-body"},
         {"unreadCount", 2}, {"recalled", false}, {"burned", false}}};
}

MiniImStateEvent ReadCount(quint64 position, const QString& id, int unread)
{
    return {position, QStringLiteral("event-%1").arg(position), QStringLiteral("readCount"),
        {{"type", "readCount"}, {"messageId", id}, {"conversationId", "conversation"},
         {"globalSeq", QVariant::fromValue(position)}, {"unreadCount", unread}}};
}

MiniImStateEvent Receipt(quint64 position, const QString& user, quint64 read)
{
    return {position, QStringLiteral("event-%1").arg(position), QStringLiteral("receipt"),
        {{"type", "receipt"}, {"conversationId", "conversation"}, {"readerId", user},
         {"lastReadSeq", QVariant::fromValue(read)}}};
}

QVariantMap FindMessage(const QVariantMap& snapshot, const QString& id)
{
    for (const auto& value : snapshot.value(QStringLiteral("recentMessages")).toList())
    {
        const auto message = value.toMap();
        if (message.value(QStringLiteral("id")) == id)
        {
            return message;
        }
    }
    return {};
}

QVariantList Query(const QString& path, const QString& sql)
{
    const QString name = QUuid::createUuid().toString(QUuid::WithoutBraces);
    QVariantList result;
    QString error;
    {
        auto db = QSqlDatabase::addDatabase(QStringLiteral("QSQLITE"), name);
        db.setDatabaseName(path);
        if (!db.open())
        {
            error = db.lastError().text();
        }
        else
        {
            QSqlQuery query(db);
            if (!query.exec(sql))
            {
                error = query.lastError().text();
            }
            else
            {
                while (query.next())
                {
                    result.append(query.value(0));
                }
            }
        }
        db.close();
    }
    QSqlDatabase::removeDatabase(name);
    if (!error.isEmpty())
    {
        throw std::runtime_error(error.toStdString());
    }
    return result;
}

void CheckGapsAndDuplicateReplay()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    Apply(store, {Message(3, "third"), Message(1, "first")}, 2);
    Require(store.cursor() == 1 && store.hasGap(), "online event skipped missing history");
    store.close();
    Open(store, root);
    Require(store.cursor() == 1 && store.hasGap(), "gap was not durable");
    Require(!FindMessage(store.snapshot(), "third").isEmpty(), "out-of-order business state lost after reopen");
    Apply(store, {Message(1, "first"), Message(2, "second"), Message(3, "third")}, 1);
    Require(store.cursor() == 3 && !store.hasGap(), "replay did not fill gap");
    store.close();
    Open(store, root);
    Apply(store, {Message(2, "second")}, 0);
    Require(store.snapshot().value("recentMessages").toList().size() == 3, "duplicate message after reopen");

    auto conflicting = Message(4, "conflict");
    conflicting.eventId = QStringLiteral("event-1");
    QVector<MiniImStateEvent> applied;
    Require(!store.apply({Message(5, "must-rollback"), conflicting}, &applied), "event id conflict accepted");
    Require(applied.isEmpty() && store.cursor() == 3, "failed batch emitted state or advanced cursor");
    Require(FindMessage(store.snapshot(), "must-rollback").isEmpty(), "failed batch retained partial state");
}

void CheckAtomicFailureAndRetry()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    Apply(store, {Message(1, "first")});
    Query(store.databasePath(), QStringLiteral(
        "CREATE TRIGGER reject_cursor BEFORE UPDATE ON metadata WHEN NEW.key='cursor' "
        "BEGIN SELECT RAISE(ABORT, 'injected cursor write failure'); END"));
    QVector<MiniImStateEvent> applied;
    Require(!store.apply({Message(2, "second"), Message(3, "third")}, &applied), "injected write failure ignored");
    Require(applied.isEmpty() && store.cursor() == 1, "failed transaction leaked output");
    store.close();
    Open(store, root);
    Require(store.cursor() == 1 && !store.hasGap(), "cursor or seen events survived rollback");
    Require(store.snapshot().value("recentMessages").toList().size() == 1, "objects survived failed commit batch");
    Query(store.databasePath(), QStringLiteral("DROP TRIGGER reject_cursor"));
    Apply(store, {Message(2, "second"), Message(3, "third")}, 2);
    Require(store.cursor() == 3, "retry lost event identity after rollback");
}

void CheckTerminalStatesSurviveReplay()
{
    for (const auto& type : {QStringLiteral("recall"), QStringLiteral("burn")})
    {
        QTemporaryDir root;
        MiniImStateStore store;
        Open(store, root);
        const MiniImStateEvent terminal{2, "event-2", type,
            {{"type", type}, {"messageId", "protected"}, {"conversationId", "conversation"}}};
        Apply(store, {terminal});
        store.close();
        Open(store, root);
        Apply(store, {Message(1, "protected")});
        Apply(store, {Message(3, "protected")});
        store.close();
        Open(store, root);
        const auto message = FindMessage(store.snapshot(), "protected");
        Require(message.value("text").toString().isEmpty(), "late message resurrected terminal text");
        Require(message.value("recalled").toBool(), "terminal flag lost");
        Require(message.value("burned").toBool() == (type == QStringLiteral("burn")), "burn flag lost");
        Apply(store, {Message(4, "existing")});
        auto next = terminal;
        next.position = 5;
        next.eventId = QStringLiteral("event-5");
        next.data["messageId"] = QStringLiteral("existing");
        Apply(store, {next});
        for (const auto& data : Query(store.databasePath(), QStringLiteral("SELECT data FROM objects")))
        {
            Require(!data.toByteArray().contains("private-message-body"), "terminal plaintext retained in objects");
        }
        Require(store.snapshot().value("unreadTotal").toInt() == 0, "terminal messages remain unread");
    }
}

void CheckAbsoluteReadCountsAndLegacyCache()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    Apply(store, {Message(1, "first"), Receipt(2, "new-member", 1)});
    Require(FindMessage(store.snapshot(), "first").value("unreadCount").toInt() == 2,
        "new member consumed an original recipient");
    Apply(store, {ReadCount(5, "late", 1), ReadCount(3, "late", 2), Message(4, "late")});
    Apply(store, {ReadCount(5, "late", 1)}, 0);
    Require(store.cursor() == 5, "out-of-order count did not fill continuous history");
    Require(FindMessage(store.snapshot(), "late").value("unreadCount").toInt() == 1,
        "old count or late message replaced absolute count");
    Apply(store, {{6, "event-6", "burn", {{"messageId", "late"}, {"conversationId", "conversation"}}},
        ReadCount(7, "late", 0)});
    store.close();
    Open(store, root);
    auto terminal = FindMessage(store.snapshot(), "late");
    Require(terminal.value("burned").toBool() && terminal.value("text").toString().isEmpty(),
        "read count restored a burned message");
    Query(store.databasePath(), "CREATE TRIGGER fail_count BEFORE UPDATE ON objects WHEN NEW.kind='readCount' "
        "BEGIN SELECT RAISE(ABORT,'injected count failure'); END");
    QVector<MiniImStateEvent> applied;
    Require(!store.apply({ReadCount(8, "first", 1)}, &applied), "failed count save was accepted");
    Require(applied.isEmpty() && store.cursor() == 7, "failed count advanced position or emitted state");
    Query(store.databasePath(), "DROP TRIGGER fail_count");
    const QString path = store.databasePath();
    store.close();
    Query(path, "DELETE FROM objects WHERE kind='readCount' AND id='first'");
    Open(store, root);
    Require(!FindMessage(store.snapshot(), "first").value("readCountKnown").toBool(),
        "legacy cache without authoritative count was declared current");
    Apply(store, {ReadCount(8, "first", 1)});
    store.close();
    Open(store, root);
    const auto restored = FindMessage(store.snapshot(), "first");
    Require(restored.value("readCountKnown").toBool() && restored.value("unreadCount").toInt() == 1,
        "correction event did not repair legacy cache after restart");
    Open(store, root, "another-user");
    Require(store.snapshot().value("readCounts").toList().isEmpty(), "count leaked across users");
}

void CheckReadSnapshotAndAccountIsolation()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    Apply(store, {Message(1, "first"), Message(2, "second"), Receipt(3, "bob", 1), Receipt(4, "carol", 2),
        ReadCount(5, "first", 0), ReadCount(6, "second", 1)});
    Require(store.saveSession("session-bob", QString()), "initial session without ACK cannot persist");
    Require(store.saveSession("session-bob", "request-bob"), "session could not persist");
    store.close();
    Open(store, root);
    const auto snapshot = store.snapshot();
    Require(snapshot.value("unreadTotal").toInt() == 1, "own unread count wrong after reopen");
    Require(FindMessage(snapshot, "first").value("unreadCount").toInt() == 0, "reader count not restored");
    Require(FindMessage(snapshot, "second").value("unreadCount").toInt() == 1, "reader count decremented twice");
    Apply(store, {Receipt(7, "carol", 1)});
    Require(FindMessage(store.snapshot(), "second").value("unreadCount").toInt() == 1, "lower receipt regressed state");
    Require(store.sessionId() == QStringLiteral("session-bob") && store.lastAck() == QStringLiteral("request-bob"),
        "resume metadata lost");
    const QString original = store.databasePath();
    for (const auto& identity : QVector<QStringList>{
        {"carol", "127.0.0.1:4433", "device"}, {"bob", "127.0.0.1:4434", "device"},
        {"bob", "127.0.0.1:4433", "other-device"}})
    {
        Open(store, root, identity[0], identity[1], identity[2]);
        Require(store.databasePath() != original && store.cursor() == 0, "account identity reused cursor");
        Require(store.snapshot().value("recentMessages").toList().isEmpty(), "account identity leaked messages");
        Require(store.sessionId().isEmpty() && store.lastAck().isEmpty(), "account identity leaked session");
    }
    Open(store, root);
    Require(store.cursor() == 7 && store.snapshot().value("unreadTotal").toInt() == 1, "account return lost state");
}

QVariantMap Intent(const QString& id, const QString& text = QStringLiteral("pending secret"))
{
    return {{"conversationId", "conversation"}, {"clientMsgId", id}, {"text", text}, {"burnMode", 0}, {"burnTtlSec", 0}};
}

void CheckOutboxIdentityRecoveryAndConfirmation()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    auto first = store.outbox().enqueue("request-1", Intent("intent-1"));
    auto duplicate = store.outbox().enqueue("new-request-must-not-replace", Intent("intent-1"));
    Require(first.value("requestId") == duplicate.value("requestId"), "retry replaced original request id");
    bool rejected = false;
    try
    {
        store.outbox().enqueue("different-request", Intent("intent-1", "changed"));
    }
    catch (const std::exception&)
    {
        rejected = true;
    }
    Require(rejected, "same message intent accepted different content");
    store.close();
    Open(store, root);
    const auto restored = store.outbox().nextPending();
    Require(restored.value("requestId") == "request-1" && restored.value("text") == "pending secret",
        "unconfirmed message lost after reopen");
    Require(store.snapshot().value("messageSends").toList().size() == 1, "pending message absent from initial state");
    Open(store, root, "carol");
    Require(store.outbox().pending().isEmpty(), "unconfirmed messages crossed account boundary");
    Open(store, root);
    Require(store.outbox().nextPending().value("requestId") == "request-1", "switching accounts dropped pending intent");
    store.outbox().markAttempt("request-1");
    Require(store.outbox().acknowledge("request-1", false, 503, "retry later", ""), "temporary failure not recorded");
    Require(store.outbox().nextPending().value("status") == "pending", "temporary server failure abandoned intent");
    Require(store.outbox().acknowledge("request-1", false, 403, "permission denied", ""), "rejection not recorded");
    store.close();
    Open(store, root);
    Require(store.outbox().nextPending().isEmpty(), "permanently rejected message retried without user action");
    Require(store.outbox().pending()[0].toMap().value("error") == "permission denied", "failure reason lost");
    Require(store.outbox().retry("conversation", "intent-1"), "explicit retry rejected");
    Require(store.outbox().nextPending().value("requestId") == "request-1", "explicit retry changed original identity");
    Require(store.outbox().acknowledge("request-1", true, 0, "", "server-1"), "confirmation not saved");
    Require(store.outbox().acknowledge("request-1", false, 503, "late error", ""), "duplicate response not recognized");
    store.close();
    Open(store, root);
    Require(store.outbox().pending().isEmpty(), "confirmed request replayed after restart");
    Require(store.outbox().enqueue("another-request", Intent("intent-1")).value("serverMsgId") == "server-1",
        "confirmed intent lost stable identity");
    Require(!store.outbox().acknowledge("unknown", true, 0, "", "other"), "unrelated ACK treated as message confirmation");
    const auto bytes = Query(store.databasePath(), QStringLiteral("SELECT payload FROM message_outbox"));
    Require(bytes.size() == 1 && bytes[0].toByteArray().isEmpty(), "confirmed outbox retained message body");
    Open(store, root, "carol");
    Require(store.outbox().pending().isEmpty(), "pending messages crossed account boundary");
}

void CheckOutboxFailureAndTerminalCleanup()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    Query(store.databasePath(), QStringLiteral(
        "CREATE TRIGGER reject_intent BEFORE INSERT ON message_outbox BEGIN SELECT RAISE(ABORT, 'disk failure'); END"));
    bool rejected = false;
    try
    {
        store.outbox().enqueue("request", Intent("intent"));
    }
    catch (const std::exception&)
    {
        rejected = true;
    }
    Require(rejected && store.outbox().pending().isEmpty(), "failed local save reported durable intent");
    Query(store.databasePath(), QStringLiteral("DROP TRIGGER reject_intent"));
    store.outbox().enqueue("request", Intent("intent"));
    auto message = Message(1, "own-message");
    message.data["senderId"] = QStringLiteral("bob");
    message.data["clientMsgId"] = QStringLiteral("intent");
    Apply(store, {message});
    Query(store.databasePath(), QStringLiteral(
        "CREATE TRIGGER reject_cursor BEFORE UPDATE ON metadata WHEN NEW.key='cursor' "
        "BEGIN SELECT RAISE(ABORT, 'cursor failure'); END"));
    const MiniImStateEvent burned{2, "event-2", "burn",
        {{"type", "burn"}, {"messageId", "own-message"}, {"conversationId", "conversation"}}};
    QVector<MiniImStateEvent> applied;
    Require(!store.apply({burned}, &applied), "injected failure ignored");
    store.close();
    Open(store, root);
    Require(store.outbox().nextPending().value("text") == "pending secret", "failed terminal batch dropped original intent");
    Query(store.databasePath(), QStringLiteral("DROP TRIGGER reject_cursor"));
    Apply(store, {burned});
    store.close();
    Open(store, root);
    Require(store.outbox().pending().isEmpty(), "burned own message remains pending");
    Require(Query(store.databasePath(), QStringLiteral("SELECT payload FROM message_outbox"))[0].toByteArray().isEmpty(),
        "burned own message remains retrievable from pending payload");
}

void CheckDurableFileTasks()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    const QVariantMap task{{"clientFileId", "intent"}, {"requestId", "init-request"},
        {"finishRequestId", "finish-request"}, {"conversationId", "conversation"}, {"path", "/data/file"},
        {"direction", 2}, {"sourceFileId", "source"}, {"priority", 0}, {"fileSize", 1},
        {"sha256", "na"}, {"metadataReady", false}, {"status", "pending"}};
    Query(store.databasePath(), "CREATE TRIGGER reject_task BEFORE INSERT ON file_tasks BEGIN SELECT RAISE(ABORT,'injected'); END");
    bool rejected = false;
    try { store.fileTasks().create(task); } catch (const std::exception&) { rejected = true; }
    Require(rejected && store.fileTasks().pending().isEmpty(), "failed insertion accepted task");
    Query(store.databasePath(), "DROP TRIGGER reject_task");
    store.fileTasks().create(task);
    store.fileTasks().update("intent", {{"fileId", "server-file"}, {"fileSize", 128},
        {"sha256", "digest"}, {"metadataReady", true}, {"status", "transferring"}});
    store.close();
    Open(store, root, "carol");
    Require(store.fileTasks().pending().isEmpty(), "file task leaked into another account");
    Open(store, root);
    const auto restored = store.fileTasks().byRequest("init-request");
    Require(restored.value("fileId") == "server-file" && restored.value("fileSize").toInt() == 128,
        "file identity or metadata lost after restart");
    Require(store.snapshot().value("fileTasks").toList().size() == 1, "file task absent from snapshot");
    for (const auto& changes : {QVariantMap{{"path", "/different"}}, QVariantMap{{"sha256", "different"}},
        QVariantMap{{"fileId", "different"}}})
    {
        rejected = false;
        try { store.fileTasks().update("intent", changes); } catch (const std::exception&) { rejected = true; }
        Require(rejected, "existing file identity was replaced");
    }
    store.fileTasks().update("intent", {{"status", "finishing"}, {"transferredBytes", 128}});
    store.close();
    Open(store, root);
    Require(store.fileTasks().byRequest("finish-request").value("status") == "finishing", "finish intent not recovered");
    rejected = false;
    try { store.fileTasks().update("intent", {{"finishRequestId", "changed"}}); }
    catch (const std::exception&) { rejected = true; }
    Require(rejected, "unconfirmed completion request was replaced");
    store.fileTasks().update("intent", {{"status", "failed"}, {"finishRejected", true}});
    Query(store.databasePath(), "CREATE TRIGGER reject_retry BEFORE UPDATE ON file_tasks BEGIN SELECT RAISE(ABORT,'injected'); END");
    rejected = false;
    try { store.fileTasks().update("intent", {{"status", "pending"}, {"finishRequestId", "new-finish"}, {"finishRejected", false}}); }
    catch (const std::exception&) { rejected = true; }
    Require(rejected && store.fileTasks().byRequest("new-finish").isEmpty(), "failed retry saved a partial identity");
    Query(store.databasePath(), "DROP TRIGGER reject_retry");
    store.fileTasks().update("intent", {{"status", "pending"}, {"finishRequestId", "new-finish"}, {"finishRejected", false}});
    store.close();
    Open(store, root);
    Require(store.fileTasks().byRequest("finish-request").isEmpty(), "old completion response still maps to new attempt");
    Require(store.fileTasks().byRequest("new-finish").value("clientFileId") == "intent", "new completion identity was not restored");
    store.fileTasks().update("intent", {{"status", "completed"}});
    store.fileTasks().update("intent", {{"status", "failed"}});
    Require(store.fileTasks().pending().isEmpty(), "late failure regressed completed file");
    auto cancelled = task;
    cancelled["clientFileId"] = "cancelled";
    cancelled["requestId"] = "cancel-init";
    cancelled["finishRequestId"] = "cancel-finish";
    store.fileTasks().create(cancelled);
    store.fileTasks().update("cancelled", {{"status", "cancelling"}, {"cancelRequestId", "cancel-request"}});
    store.fileTasks().update("cancelled", {{"status", "cancelled"}});
    store.fileTasks().update("cancelled", {{"status", "transferring"}});
    store.close();
    Open(store, root);
    Require(store.fileTasks().pending().isEmpty(), "cancelled file resumed after restart");
}

void CheckFileCancellationRecovery()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    const QVariantMap task{{"clientFileId", "intent"}, {"requestId", "init"}, {"finishRequestId", "finish"},
        {"conversationId", "conversation"}, {"path", "/data/file"}, {"direction", 1}, {"status", "pending"}};
    store.fileTasks().create(task);
    const auto path = store.databasePath();
    store.close();
    Query(path, "DROP INDEX idx_file_tasks_cancel_request");
    Query(path, "ALTER TABLE file_tasks DROP COLUMN cancel_request");
    Open(store, root);
    Require(store.fileTasks().byRequest("init").value("clientFileId") == "intent", "cancel migration lost task");
    Query(path, "CREATE TRIGGER reject_cancel BEFORE UPDATE ON file_tasks BEGIN SELECT RAISE(ABORT,'injected'); END");
    bool rejected = false;
    try { store.fileTasks().update("intent", {{"status", "cancelling"}, {"cancelRequestId", "cancel"}}); }
    catch (const std::exception&) { rejected = true; }
    Require(rejected && store.fileTasks().byRequest("cancel").isEmpty(), "cancel save failure persisted partial state");
    Query(path, "DROP TRIGGER reject_cancel");
    store.fileTasks().update("intent", {{"status", "cancelling"}, {"cancelRequestId", "cancel"}});
    store.fileTasks().update("intent", {{"status", "completed"}});
    store.fileTasks().update("intent", {{"status", "transferring"}});
    store.close();
    Open(store, root, "carol");
    Require(store.fileTasks().pending().isEmpty(), "file cancellation crossed account boundary");
    Open(store, root);
    Require(store.fileTasks().byRequest("cancel").value("status") == "cancelling", "pending cancellation not restored");
    rejected = false;
    try { store.fileTasks().update("intent", {{"cancelRequestId", "changed"}}); }
    catch (const std::exception&) { rejected = true; }
    Require(rejected, "pending cancellation changed its request identity");
    store.fileTasks().update("intent", {{"status", "cancel_failed"}, {"error", "rejected"}});
    store.fileTasks().update("intent", {{"status", "cancelling"}, {"cancelRequestId", "new-cancel"}});
    Require(store.fileTasks().byRequest("cancel").isEmpty(), "new cancellation action retained old response mapping");
    store.fileTasks().update("intent", {{"status", "cancelled"}});
    store.close();
    Open(store, root);
    Require(store.fileTasks().pending().isEmpty(), "confirmed cancellation replayed after restart");
    auto legacy = task;
    legacy["clientFileId"] = "legacy";
    legacy["requestId"] = "legacy-init";
    legacy["finishRequestId"] = "legacy-finish";
    store.fileTasks().create(legacy);
    store.fileTasks().update("legacy", {{"status", "cancelled"}});
    store.close();
    Open(store, root);
    const auto upgraded = store.fileTasks().task("legacy");
    Require(upgraded.value("status") == "cancelling", "legacy local cancellation did not queue server confirmation");
    store.close();
    Open(store, root);
    Require(store.fileTasks().task("legacy").value("cancelRequestId") == upgraded.value("cancelRequestId"),
        "legacy cancellation migration changed request on reopen");
}


void CheckDeliveryAndConfirmationPersistence()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    const MiniImStateEvent delivery{3, "delivery-event", "delivery", {{"type", "delivery"},
        {"messageId", "first"}, {"conversationId", "conversation"}, {"userId", "bob"},
        {"status", "delivered"}, {"deliveredAtMs", 50}, {"globalSeq", 3}}};
    Apply(store, {delivery, Message(1, "first")});
    Require(store.cursor() == 1 && store.hasGap(), "delivery must not skip history");
    bool rejected = false;
    try
    {
        store.saveConfirmation("ahead", 3);
    }
    catch (const std::exception&)
    {
        rejected = true;
    }
    Require(rejected && store.pendingConfirmation().isEmpty(), "cannot confirm uncommitted prefix");
    store.saveConfirmation("original", 1);
    Apply(store, {Receipt(2, "bob", 1)});
    store.close();
    Open(store, root);
    Require(store.cursor() == 3 && !store.hasGap(), "complete durable prefix restored");
    Require(store.pendingConfirmation().value("requestId") == "original", "pending confirmation identity survives restart");
    Require(store.pendingConfirmation().value("cursor").toULongLong() == 1, "pending confirmation body does not grow");
    Require(store.snapshot().value("deliveries").toList().size() == 1, "delivery restored before message and receipt");
    Require(store.snapshot().value("unreadTotal").toInt() == 0, "receipt still projects read state");
    Query(store.databasePath(), "CREATE TRIGGER reject_confirm BEFORE DELETE ON metadata "
        "WHEN OLD.key='sync_confirmation' BEGIN SELECT RAISE(ABORT,'confirmation failure'); END");
    rejected = false;
    try
    {
        store.completeConfirmation("original");
    }
    catch (const std::exception&)
    {
        rejected = true;
    }
    Require(rejected && store.confirmedCursor() == 0, "confirmation completion rolls back as a whole");
    Require(!store.pendingConfirmation().isEmpty(), "failed completion keeps original intent");
    Query(store.databasePath(), "DROP TRIGGER reject_confirm");
    store.completeConfirmation("unrelated");
    Require(store.confirmedCursor() == 0, "unknown acknowledgment cannot advance position");
    store.completeConfirmation("original");
    store.completeConfirmation("original");
    Require(store.confirmedCursor() == 1 && store.pendingConfirmation().isEmpty(), "acknowledgment settles once");
    store.saveConfirmation("next", 3);
    store.close();
    Open(store, root, "alice");
    Require(store.pendingConfirmation().isEmpty() && store.confirmedCursor() == 0, "confirmations isolate users");
    Require(store.snapshot().value("deliveries").toList().isEmpty(), "deliveries isolate users");
    store.close();
    Open(store, root);
    Require(store.pendingConfirmation().value("requestId") == "next", "returning account restores confirmation");
    Apply(store, {delivery}, 0);
    Require(store.snapshot().value("deliveries").toList().size() == 1, "replay cannot duplicate delivery");
}

void CheckMonotonicObjectVersions()
{
    QTemporaryDir root;
    MiniImStateStore store;
    Open(store, root);
    Apply(store, {
        {4, "event-4", "conversation", {{"conversationId", "conversation"}, {"title", "new"}}},
        {3, "event-3", "file", {{"conversationId", "conversation"}, {"fileId", "file"}, {"version", 5}, {"completed", true}}},
        {2, "event-2", "file", {{"conversationId", "conversation"}, {"fileId", "file"}, {"version", 4}, {"completed", false}}},
        {1, "event-1", "conversation", {{"conversationId", "conversation"}, {"title", "old"}}}
    });
    store.close();
    Open(store, root);
    const auto snapshot = store.snapshot();
    Require(snapshot.value("conversations").toList()[0].toMap().value("title") == "new", "old title replaced new state");
    Require(snapshot.value("files").toList()[0].toMap().value("completed").toBool(), "old file version regressed completion");
    Require(store.cursor() == 4 && !store.hasGap(), "out-of-order objects did not complete cursor");
}
}

void CheckCancelledDownloadCleanup()
{
    QTemporaryDir root;
    const auto write = [](const QString& path, const QByteArray& bytes)
    {
        QFile file(path);
        Require(file.open(QIODevice::WriteOnly), "cleanup fixture open failed");
        Require(file.write(bytes) == bytes.size(), "cleanup fixture write failed");
    };
    const QString target = root.filePath("saved.bin");
    const QString part = target + ".miniim-file-id.part";
    write(target, "original destination");
    write(part, "cancelled bytes");
    QVariantMap task{{"clientFileId", "intent"}, {"fileId", "file-id"}, {"path", target},
        {"fileName", "saved.bin"}, {"direction", 2}, {"status", "cancelled"}, {"cancelRequestId", "cancel"}};
    MiniImFileCleanup cleanup;
    for (const auto& status : {"pending", "transferring", "failed", "cancelling", "cancel_failed", "completed"})
    {
        auto active = task;
        active.insert("status", status);
        Require(cleanup.preview({active}).value("items").toList().isEmpty(), "recoverable task offered for cleanup");
    }
    const auto preview = cleanup.preview({task});
    Require(preview.value("items").toList().size() == 1, "cancelled fragment missing from preview");
    Require(QFileInfo::exists(part), "preview removed file");
    auto changed = task;
    changed.insert("status", "failed");
    Require(!cleanup.apply(preview.value("token").toString(), {changed}).value("ok").toBool(), "changed task deleted");
    Require(QFileInfo::exists(part), "changed task fragment removed");
    auto fresh = cleanup.preview({task});
    write(part, "external change with different length");
    Require(!cleanup.apply(fresh.value("token").toString(), {task}).value("ok").toBool(), "changed file deleted");
    fresh = cleanup.preview({task});
    cleanup.reset();
    Require(!cleanup.apply(fresh.value("token").toString(), {task}).value("ok").toBool(), "old account token accepted");
    auto other = task;
    other.insert("clientFileId", "other");
    other.insert("path", part);
    other.insert("status", "completed");
    Require(cleanup.preview({task, other}).value("items").toList().isEmpty(), "formal target offered for removal");
    other.insert("path", target);
    other.insert("status", "pending");
    Require(cleanup.preview({task, other}).value("items").toList().isEmpty(), "shared staging path offered for removal");
    fresh = cleanup.preview({task});
    const auto result = cleanup.apply(fresh.value("token").toString(), {task});
    Require(result.value("ok").toBool() && result.value("removed").toInt() == 1, "cancelled fragment cleanup failed");
    Require(!QFileInfo::exists(part), "cancelled fragment remains");
    QFile saved(target);
    Require(saved.open(QIODevice::ReadOnly) && saved.readAll() == "original destination", "formal destination changed");
    Require(!cleanup.apply(fresh.value("token").toString(), {task}).value("ok").toBool(), "token reused");
    Require(cleanup.preview({task}).value("items").toList().isEmpty(), "removed fragment reappeared");
}

void CheckIncrementalUnreadCommitAndRecovery()
{
    QTemporaryDir directory;
    MiniImStateStore store;
    Open(store, directory);
    Require(store.unreadTotal() == 0, "new cache has unread messages");
    Apply(store, {Receipt(1, "bob", 80),
        {2, "event-2", "burn", {{"messageId", "late"}, {"conversationId", "conversation"}}}});
    for (quint64 index = 1; index <= 200; ++index)
    {
        auto event = Message(index + 2, QStringLiteral("incremental-%1").arg(index));
        event.data.insert("seq", QVariant::fromValue(201 - index));
        if (index % 5 == 0)
        {
            event.data.insert("senderId", "bob");
        }
        Apply(store, {event, event}, 1);
        if (index % 25 == 0)
        {
            MiniImStateStore reopened;
            Open(reopened, directory);
            Require(store.unreadTotal() == reopened.unreadTotal(), "incremental unread differs from full rebuild");
        }
    }
    Require(store.unreadTotal() == 96, "early receipt or self message counted as unread");
    auto late = Message(203, "late");
    Apply(store, {late, Receipt(204, "alice", 500), Receipt(205, "bob", 20)});
    Require(store.unreadTotal() == 96, "terminal or backwards/other receipt changed unread");
    auto changed = Message(206, "incremental-1");
    changed.data.insert("seq", 200);
    changed.data.insert("conversationId", "another-conversation");
    Apply(store, {changed});
    Require(store.unreadTotal() == 96, "updated message counted twice");
    Query(store.databasePath(), "CREATE TRIGGER unread_fail BEFORE UPDATE ON metadata WHEN NEW.key='cursor' "
        "BEGIN SELECT RAISE(ABORT,'unread commit failure'); END");
    QVector<MiniImStateEvent> applied;
    const QVector<MiniImStateEvent> batch{Message(207, "rollback-message"), Receipt(208, "bob", 300),
        {209, "event-209", "recall", {{"messageId", "incremental-1"}, {"conversationId", "another-conversation"}}}};
    Require(!store.apply(batch, &applied) && applied.isEmpty(), "failed batch was published");
    Require(store.unreadTotal() == 96 && store.cursor() == 206, "rollback changed cached unread or cursor");
    Query(store.databasePath(), "DROP TRIGGER unread_fail");
    Apply(store, batch);
    Require(store.unreadTotal() == 0, "committed mixed batch has incorrect unread");
    Open(store, directory);
    Require(store.unreadTotal() == 0 && store.cursor() == 209, "restart changed committed unread");
    auto another = Message(210, "new-unread");
    another.data.insert("seq", 301);
    Apply(store, {another});
    Require(store.unreadTotal() == 1, "post-restart message did not increase unread");
    Open(store, directory, "other");
    Require(store.unreadTotal() == 0, "unread leaked across accounts");
    Open(store, directory);
    Require(store.unreadTotal() == 1, "account return did not rebuild unread");
    Apply(store, {Receipt(211, "bob", std::numeric_limits<quint64>::max())});
    Require(store.unreadTotal() == 0, "maximum receipt position did not clear unread");
    Open(store, directory);
    Require(store.unreadTotal() == 0, "maximum receipt differs after rebuild");
}

void CheckBidirectionalHistory()
{
    QTemporaryDir directory;
    MiniImStateStore store;
    Open(store, directory);
    Require(store.messagePage("empty", "", "latest").value("messages").toList().isEmpty(), "empty history not empty");
    QVector<MiniImStateEvent> events;
    for (quint64 index = 1; index <= 125; ++index)
    {
        auto message = Message(index, QStringLiteral("direction-%1").arg(index, 3, 10, QChar('0')));
        message.data.insert("seq", QVariant::fromValue((index + 1) / 2));
        events.append(message);
    }
    Apply(store, events);
    const auto unread = store.unreadTotal();
    const auto position = store.cursor();
    auto page = store.messagePage("conversation", "", "latest");
    Require(page.value("hasOlder").toBool() && !page.value("hasNewer").toBool(), "latest boundaries wrong");
    QSet<QString> backwards;
    while (true)
    {
        Require(page.value("ok").toBool(), "backward page failed");
        for (const auto& item : page.value("messages").toList())
        {
            const auto id = item.toMap().value("id").toString();
            Require(!backwards.contains(id), "backward page duplicated an equal-sequence message");
            backwards.insert(id);
        }
        if (!page.value("hasOlder").toBool()) break;
        page = store.messagePage("conversation", page.value("beforeCursor").toString(), "older");
    }
    Require(backwards.size() == 125 && page.value("hasNewer").toBool(), "backward traversal lost history");
    QSet<QString> forwards;
    while (true)
    {
        Require(page.value("ok").toBool(), "forward page failed");
        const auto list = page.value("messages").toList();
        Require(list.size() <= 50, "forward page exceeds limit");
        QString previous;
        for (const auto& item : list)
        {
            const auto id = item.toMap().value("id").toString();
            Require(!forwards.contains(id) && (previous.isEmpty() || previous < id), "forward page repeated or reordered");
            forwards.insert(id);
            previous = id;
        }
        if (!page.value("hasNewer").toBool()) break;
        page = store.messagePage("conversation", page.value("afterCursor").toString(), "newer");
    }
    Require(forwards == backwards, "forward traversal lost history");
    Require(store.cursor() == position && store.unreadTotal() == unread, "navigation changed sync or unread");
    const auto boundary = page.value("afterCursor").toString();
    const auto empty = store.messagePage("conversation", boundary, "newer");
    Require(empty.value("messages").toList().isEmpty() && !empty.value("hasNewer").toBool()
        && empty.value("hasOlder").toBool() && empty.value("beforeCursor") == boundary, "empty page lost boundary");
    Require(!store.messagePage("conversation", "", "newer").value("ok").toBool(), "missing forward boundary accepted");
    Require(!store.messagePage("conversation", boundary, "latest").value("ok").toBool(), "latest with boundary accepted");
    Require(!store.messagePage("conversation", boundary, "invalid").value("ok").toBool(), "invalid direction accepted");
    Require(!store.messagePage("other", boundary, "newer").value("ok").toBool(), "foreign boundary accepted");
    Require(!store.messagePage("conversation", QString(2049, QChar('x')), "older").value("ok").toBool(), "oversized boundary accepted");
    Apply(store, {Message(126, "newest"),
        {127, "event-127", "burn", {{"messageId", "newest"}, {"conversationId", "conversation"}}}});
    const auto added = store.messagePage("conversation", boundary, "newer");
    const auto latest = added.value("messages").toList();
    Require(latest.size() == 1 && latest.first().toMap().value("burned").toBool()
        && latest.first().toMap().value("text").toString().isEmpty(), "forward page restored burned content");
    Open(store, directory);
    Require(store.messagePage("conversation", boundary, "newer").value("messages") == added.value("messages"),
        "restart changed boundary results");
    Open(store, directory, "other");
    Require(store.messagePage("conversation", boundary, "newer").value("messages").toList().isEmpty(), "forward history leaked account");
}

void CheckPagedHistoryAndUnread()
{
    QTemporaryDir directory;
    MiniImStateStore store;
    Open(store, directory, "bob");
    QVector<MiniImStateEvent> events;
    for (quint64 index = 1; index <= 125; ++index)
    {
        events.append(Message(index, QStringLiteral("paged-%1").arg(index)));
    }
    Apply(store, events);
    auto initial = store.snapshot();
    Require(initial.value("recentMessages").toList().size() == 50, "initial history is unbounded");
    Require(initial.value("unreadTotal").toInt() == 125, "unread count depends on visible page");
    auto page = store.messagePage("conversation");
    QSet<QString> ids;
    int pages = 0;
    while (true)
    {
        Require(page.value("ok").toBool(), "history read failed");
        const auto messages = page.value("messages").toList();
        Require(messages.size() <= 50, "history page is unbounded");
        for (const auto& value : messages)
        {
            const auto id = value.toMap().value("id").toString();
            Require(!ids.contains(id), "history cursor repeated a message");
            ids.insert(id);
        }
        ++pages;
        if (!page.value("hasMore").toBool())
        {
            break;
        }
        page = store.messagePage("conversation", page.value("cursor").toString());
    }
    Require(ids.size() == 125 && pages == 3, "history traversal lost messages");
    Apply(store, {Receipt(126, "bob", 100),
        {127, "event-127", "burn", {{"messageId", "paged-120"}, {"conversationId", "conversation"}}}});
    Require(store.unreadTotal() == 24, "read/burn did not update full unread count");
    Apply(store, {{128, "event-128", "recall", {{"messageId", "paged-1"}, {"conversationId", "conversation"}}}});
    initial = store.snapshot();
    Require(FindMessage(initial, "paged-1").isEmpty(), "old recall moved message into recent page");
    auto cursor = initial.value("historyByConversation").toMap().value("conversation").toMap().value("cursor").toString();
    Require(!store.messagePage("other", cursor).value("ok").toBool(), "cross-conversation cursor accepted");
    Require(!store.messagePage("conversation", "broken").value("ok").toBool(), "invalid cursor accepted");
    const auto path = store.databasePath();
    store.close();
    Query(path, "DROP INDEX objects_message_order");
    Query(path, "DROP INDEX objects_message_unread");
    Query(path, "DROP INDEX objects_delivery_message");
    Open(store, directory, "bob");
    Require(store.unreadTotal() == 24, "index migration changed read state");
    page = store.messagePage("conversation", store.messagePage("conversation", cursor).value("cursor").toString());
    const auto old = page.value("messages").toList().first().toMap();
    Require(old.value("recalled").toBool() && old.value("text").toString().isEmpty(), "history restored recalled content");
    Open(store, directory, "other");
    Require(store.messagePage("conversation").value("messages").toList().isEmpty(), "history leaked across accounts");
}

int main(int argc, char* argv[])
{
    QCoreApplication app(argc, argv);
    try
    {
        CheckIncrementalUnreadCommitAndRecovery();
        CheckBidirectionalHistory();
        CheckPagedHistoryAndUnread();
        CheckAbsoluteReadCountsAndLegacyCache();
        CheckDeliveryAndConfirmationPersistence();
        CheckGapsAndDuplicateReplay();
        CheckAtomicFailureAndRetry();
        CheckTerminalStatesSurviveReplay();
        CheckReadSnapshotAndAccountIsolation();
        CheckMonotonicObjectVersions();
        CheckDurableFileTasks();
        CheckCancelledDownloadCleanup();
        CheckFileCancellationRecovery();
        CheckOutboxIdentityRecoveryAndConfirmation();
        CheckOutboxFailureAndTerminalCleanup();
        std::cout << "State store: gaps, rollback, terminal states, read snapshots, isolation and versions passed\n";
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
