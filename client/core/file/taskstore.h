// Persists account-scoped file intents and their confirmation state.
#ifndef MINI_IM_CORE_FILE_TASKSTORE_H_
#define MINI_IM_CORE_FILE_TASKSTORE_H_

#include <QSqlDatabase>
#include <QSqlQuery>
#include <QVariantMap>

class MiniImFileTaskStore final
{
public:
    MiniImFileTaskStore() = default;
    MiniImFileTaskStore(const MiniImFileTaskStore&) = delete;
    MiniImFileTaskStore& operator=(const MiniImFileTaskStore&) = delete;
    void open(const QSqlDatabase& database);
    void close();
    void create(const QVariantMap& task);
    QVariantMap task(const QString& id) const;
    QVariantMap byFile(const QString& fileId) const;
    QVariantMap byRequest(const QString& requestId) const;
    QVariantList pending() const;
    void update(const QString& id, const QVariantMap& changes);

private:
    QSqlQuery run(const QString& sql, const QVariantList& values = {}) const;
    QVariantMap find(const QString& field, const QString& value) const;
    QSqlDatabase m_db;
};

#endif  // MINI_IM_CORE_FILE_TASKSTORE_H_
