// Persists business projections and their continuous sync cursor in one SQLite transaction.
#ifndef MINI_IM_CORE_SYNC_STATESTORE_H_
#define MINI_IM_CORE_SYNC_STATESTORE_H_

#include <QSqlDatabase>
#include <QSqlQuery>
#include <QString>
#include <QVariantMap>
#include <QVector>
#include "core/message/outbox.h"

struct MiniImStateEvent
{
    quint64 position = 0;
    QString eventId;
    QString type;
    QVariantMap data;
};

class MiniImStateStore final
{
public:
    MiniImStateStore() = default;
    ~MiniImStateStore();
    MiniImStateStore(const MiniImStateStore&) = delete;
    MiniImStateStore& operator=(const MiniImStateStore&) = delete;

    bool open(const QString& root, const QString& endpoint, const QString& user, const QString& device);
    void close();
    bool apply(const QVector<MiniImStateEvent>& events, QVector<MiniImStateEvent>* applied);
    bool saveSession(const QString& session, const QString& ack);
    QString sessionId() const;
    QString lastAck() const;
    quint64 cursor() const;
    bool hasGap() const;
    QVariantMap snapshot() const;
    QString errorString() const;
    QString databasePath() const;
    MiniImMessageOutbox& outbox();

private:
    QSqlQuery run(const QString& sql, const QVariantList& values = {}) const;
    QString metadata(const QString& key) const;
    QVariantMap object(const QString& kind, const QString& id) const;
    void saveObject(const QString& kind, const QString& id, quint64 position, const QVariantMap& data);
    QVariantMap project(const MiniImStateEvent& event);
    QVariantMap protectMessage(QVariantMap data);

    QSqlDatabase m_db;
    MiniImMessageOutbox m_outbox;
    QString m_name;
    QString m_path;
    QString m_user;
    quint64 m_cursor = 0;
    mutable QString m_error;
};

#endif
