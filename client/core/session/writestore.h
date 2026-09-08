// Persists control write intents and immutable terminal results in the account database.
#ifndef MINI_IM_CORE_SESSION_WRITESTORE_H_
#define MINI_IM_CORE_SESSION_WRITESTORE_H_

#include <QSqlDatabase>
#include <QSqlQuery>
#include <QVariantMap>

class MiniImControlWriteStore final
{
public:
    void open(const QSqlDatabase& database);
    void close();
    QVariantMap enqueue(const QString& requestId, const QString& operation, const QString& conversationId,
        const QString& clientConvId, const QByteArray& payload);
    QVariantMap nextPending() const;
    QVariantList pending() const;
    void markAttempt(const QString& requestId);
    bool acknowledge(const QString& requestId, bool success, int code, const QString& error, const QString& entityId);

private:
    QSqlQuery run(const QString& sql, const QVariantList& values = {}) const;
    QVariantMap row(const QSqlQuery& query) const;
    QSqlDatabase m_db;
};

#endif
