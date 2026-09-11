// Previews and removes only cancelled download staging files from the current account.
#ifndef MINI_IM_CORE_FILE_CLEANUP_H_
#define MINI_IM_CORE_FILE_CLEANUP_H_
#include <QVariantMap>

class MiniImFileCleanup final
{
public:
    MiniImFileCleanup() = default;
    MiniImFileCleanup(const MiniImFileCleanup&) = delete;
    MiniImFileCleanup& operator=(const MiniImFileCleanup&) = delete;
    QVariantMap preview(const QVariantList& tasks);
    QVariantMap apply(const QString& token, const QVariantList& tasks);
    void reset();
private:
    static QVariantList candidates(const QVariantList& tasks);
    QString m_token;
    QVariantList m_items;
};
#endif  // MINI_IM_CORE_FILE_CLEANUP_H_
