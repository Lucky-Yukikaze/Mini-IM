// Exercises control write persistence and scheduling using real account databases.
#include "core/session/writecoordinator.h"
#include "core/sync/statestore.h"
#include <QCoreApplication>
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

void Query(const QString& path, const QString& sql)
{
    const auto name = QUuid::createUuid().toString(QUuid::WithoutBraces);
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
        }
        db.close();
    }
    QSqlDatabase::removeDatabase(name);
    Require(error.isEmpty(), error.toUtf8().constData());
}

struct Fixture
{
    QTemporaryDir root;
    MiniImStateStore state;
    QVector<im::envelope::Envelope> sent;
    int ids = 0;
    int failures = 0;
    bool sendAccepted = true;
    MiniImControlWriteCoordinator queue{state.controlWrites(), [this](im::envelope::Envelope envelope)
        {
            sent.append(envelope);
            return sendAccepted;
        }, [this]() { return QStringLiteral("request-%1").arg(++ids); }};

    Fixture()
    {
        open();
        QObject::connect(&queue, &MiniImControlWriteCoordinator::failed, &queue, [this]() { ++failures; });
        queue.start();
    }
    void open(const QString& user = QStringLiteral("alice"))
    {
        Require(state.open(root.path(), "endpoint", user, "device"), "account open failed");
    }
};

im::envelope::Envelope Create(const std::string& title = "group")
{
    im::envelope::Envelope body;
    auto* create = body.mutable_create_conversation();
    create->set_client_conv_id("create-intent");
    create->set_title(title);
    create->set_type(im::common::CONVERSATION_GROUP);
    create->add_member_ids("bob");
    return body;
}

im::envelope::Envelope Rename()
{
    im::envelope::Envelope body;
    body.mutable_rename_conversation()->set_conversation_id("group-id");
    body.mutable_rename_conversation()->set_title("new title");
    return body;
}

void CheckOrderRetryAndTerminalResults()
{
    Fixture f;
    Require(f.queue.enqueue(Create()), "create save rejected");
    Require(f.queue.enqueue(Create()), "same create intent rejected");
    Require(!f.queue.enqueue(Create("changed")), "changed intent accepted");
    Require(f.queue.enqueue(Rename()), "rename save rejected");
    Require(f.sent.isEmpty(), "writes sent before sync catch-up");
    Require(f.state.controlWrites().pending().size() == 2, "create repeated business intent");
    f.queue.setSyncReady(true);
    f.queue.pump();
    const auto original = f.sent[0];
    const auto request = QString::fromStdString(original.request_id());
    f.queue.handleResult(request, false, 503, "retry", "");
    f.queue.pump();
    Require(f.sent.size() == 1, "transient error bypassed retry delay");
    f.queue.stop();
    f.state.close();
    f.open();
    f.queue.start();
    f.queue.setSyncReady(true);
    f.queue.pump();
    Require(f.sent.size() == 2 && f.sent[1].SerializeAsString() == original.SerializeAsString(),
        "reopen changed request id, time or original body");
    f.queue.handleResult(request, true, 0, "ok", "group-id");
    Require(f.sent.size() == 3 && f.sent.back().has_rename_conversation(), "next write order broken");
    f.queue.handleResult(request, false, 409, "late failure", "");
    Require(f.state.controlWrites().pending().size() == 1, "late failure regressed confirmed write");
    const auto renameRequest = QString::fromStdString(f.sent.back().request_id());
    f.queue.handleResult(renameRequest, false, 403, "forbidden", "");
    f.queue.handleResult(renameRequest, true, 0, "late success", "group-id");
    Require(f.state.controlWrites().pending()[0].toMap().value("status") == "failed", "definite failure was changed");
    Require(!f.state.controlWrites().pending()[0].toMap().contains("payload"), "wire body leaked to page snapshot");
    f.queue.stop();
    f.open("bob");
    Require(f.state.controlWrites().pending().isEmpty(), "control intent leaked to other account");
    f.open();
    f.queue.start();
    f.queue.setSyncReady(true);
    f.queue.pump();
    Require(f.sent.size() == 3, "terminal request was resent after restart");
}

void CheckUpgradeKeepsExistingCache()
{
    Fixture f;
    f.queue.stop();
    f.state.outbox().enqueue("old-message", {{"conversationId", "conversation"},
        {"clientMsgId", "old-intent"}, {"text", "preserved"}});
    Require(f.state.saveSession("old-session", "old-ack"), "setup session failed");
    Query(f.state.databasePath(), "DROP TABLE control_outbox");
    f.state.close();
    f.open();
    f.state.close();
    f.open();
    Require(f.state.sessionId() == "old-session" && f.state.lastAck() == "old-ack", "upgrade lost session");
    Require(f.state.outbox().nextPending().value("text") == "preserved", "upgrade lost pending message");
    Require(f.state.controlWrites().pending().isEmpty(), "upgrade invented control writes");
    f.queue.start();
    Require(f.queue.enqueue(Create()), "upgraded account cannot save control write");
}

void CheckSaveAttemptAndAcknowledgementFailures()
{
    Fixture f;
    Query(f.state.databasePath(), "CREATE TRIGGER reject_insert BEFORE INSERT ON control_outbox "
        "BEGIN SELECT RAISE(ABORT, 'insert failed'); END");
    Require(!f.queue.enqueue(Create()), "failed persistence accepted user intent");
    Require(f.state.controlWrites().pending().isEmpty(), "failed persistence left partial intent");
    Query(f.state.databasePath(), "DROP TRIGGER reject_insert");
    Query(f.state.databasePath(), "CREATE TRIGGER reject_attempt BEFORE UPDATE OF attempts ON control_outbox "
        "BEGIN SELECT RAISE(ABORT, 'attempt failed'); END");
    f.queue.setSyncReady(true);
    Require(f.queue.enqueue(Create()), "saved intent reported rejected after attempt failure");
    Require(f.failures == 1 && f.sent.isEmpty(), "attempt failure allowed send or failed to stop");
    Query(f.state.databasePath(), "DROP TRIGGER reject_attempt");
    f.queue.start();
    f.queue.setSyncReady(true);
    f.queue.pump();
    const auto request = QString::fromStdString(f.sent[0].request_id());
    Query(f.state.databasePath(), "CREATE TRIGGER reject_ack BEFORE UPDATE OF status ON control_outbox "
        "BEGIN SELECT RAISE(ABORT, 'ack failed'); END");
    f.queue.handleResult(request, true, 0, "ok", "group-id");
    Require(f.failures == 2 && !f.state.controlWrites().nextPending().isEmpty(), "ACK failure lost durable intent");
    Query(f.state.databasePath(), "DROP TRIGGER reject_ack");
    f.queue.start();
    f.queue.setSyncReady(true);
    f.queue.pump();
    Require(f.sent.size() == 2 && QString::fromStdString(f.sent.back().request_id()) == request,
        "ACK persistence recovery changed identity");
    f.queue.handleResult(request, true, 0, "ok", "group-id");
    Require(f.state.controlWrites().pending().isEmpty(), "ACK retry failed to settle request");
}
}

int main(int argc, char** argv)
{
    QCoreApplication application(argc, argv);
    try
    {
        CheckOrderRetryAndTerminalResults();
        CheckSaveAttemptAndAcknowledgementFailures();
        CheckUpgradeKeepsExistingCache();
        std::cout << "control writes: persistence, order, terminal state, account isolation and failures passed\n";
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
