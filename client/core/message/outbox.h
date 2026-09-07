// Stores original message intents and server confirmation state in the account database.
#ifndef MINI_IM_CORE_MESSAGE_OUTBOX_H_
#define MINI_IM_CORE_MESSAGE_OUTBOX_H_

#include <QSqlDatabase>
#include <QSqlQuery>
#include <QVariantMap>

class MiniImMessageOutbox final
{
public:
    void open(const QSqlDatabase& database);
    void close();
    QVariantMap enqueue(const QString& requestId, const QVariantMap& intent);
    QVariantMap nextPending() const;
    QVariantList pending() const;
    bool acknowledge(const QString& requestId, bool success, int code, const QString& error, const QString& entityId);
    void markAttempt(const QString& requestId);
    bool retry(const QString& conversationId, const QString& clientMsgId);
    void settleTerminal(const QString& conversationId, const QString& clientMsgId, const QString& entityId);

private:
    QSqlQuery run(const QString& sql, const QVariantList& values = {}) const;
    QVariantMap row(const QSqlQuery& query) const;
    QSqlDatabase m_db;
};

#endif
