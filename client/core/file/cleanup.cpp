#include "core/file/cleanup.h"
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QSet>
#include <QUuid>

namespace
{
QString StagingPath(const QVariantMap& task)
{
    const QString id = task.value("fileId").toString();
    static const QRegularExpression validId(QStringLiteral("^[A-Za-z0-9_-]+$"));
    const QString path = task.value("path").toString();
    if (!validId.match(id).hasMatch() || !QFileInfo(path).isAbsolute())
    {
        return {};
    }
    return QDir::cleanPath(path + QStringLiteral(".miniim-") + id + QStringLiteral(".part"));
}

bool PlainPath(const QString& path)
{
    QFileInfo current(path);
    while (true)
    {
        if (current.isSymLink() || current.isJunction())
        {
            return false;
        }
        const auto parent = current.dir().absolutePath();
        if (parent == current.absoluteFilePath())
        {
            return true;
        }
        current.setFile(parent);
    }
}

QString PathKey(const QString& path)
{
    const QFileInfo info(path);
    const QString canonical = info.canonicalFilePath();
    const QString result = canonical.isEmpty() ? info.absoluteFilePath() : canonical;
#ifdef Q_OS_WIN
    return result.toCaseFolded();
#else
    return result;
#endif
}
}

QVariantList MiniImFileCleanup::candidates(const QVariantList& tasks)
{
    QVariantList result;
    QSet<QString> selected;
    for (const auto& value : tasks)
    {
        const auto task = value.toMap();
        if (task.value("status") != "cancelled" || task.value("direction").toInt() != 2
            || task.value("cancelRequestId").toString().isEmpty() || task.value("cleanupBusy").toBool())
        {
            continue;
        }
        const QString path = StagingPath(task);
        const QFileInfo info(path);
        if (path.isEmpty() || !info.isFile() || !PlainPath(path))
        {
            continue;
        }
        const QString key = PathKey(path);
        bool referenced = false;
        for (const auto& otherValue : tasks)
        {
            const auto other = otherValue.toMap();
            referenced = referenced || PathKey(other.value("path").toString()) == key;
            if (other.value("clientFileId") != task.value("clientFileId"))
            {
                const auto staging = StagingPath(other);
                referenced = referenced || (!staging.isEmpty() && PathKey(staging) == key);
            }
        }
        if (referenced || selected.contains(key))
        {
            continue;
        }
        selected.insert(key);
        result.append(QVariantMap{{"clientFileId", task.value("clientFileId")}, {"path", path},
            {"fileName", task.value("fileName")}, {"bytes", info.size()},
            {"modifiedMs", info.lastModified().toMSecsSinceEpoch()}});
    }
    return result;
}

QVariantMap MiniImFileCleanup::preview(const QVariantList& tasks)
{
    m_items = candidates(tasks);
    m_token = QUuid::createUuid().toString(QUuid::WithoutBraces);
    return {{"ok", true}, {"token", m_token}, {"items", m_items}};
}

QVariantMap MiniImFileCleanup::apply(const QString& token, const QVariantList& tasks)
{
    if (token.isEmpty() || token != m_token)
    {
        return {{"ok", false}, {"error", "cleanup preview expired; preview again"}};
    }
    const auto approved = m_items;
    reset();
    const auto current = candidates(tasks);
    QVariantList outcomes;
    int removed = 0;
    bool ok = true;
    for (const auto& value : approved)
    {
        auto item = value.toMap();
        if (!current.contains(value))
        {
            item.insert("error", "file or task changed; preview again");
            ok = false;
        }
        else
        {
            QFile file(item.value("path").toString());
            if (file.remove())
            {
                ++removed;
                item.insert("removed", true);
            }
            else
            {
                item.insert("error", file.errorString());
                ok = false;
            }
        }
        outcomes.append(item);
    }
    return {{"ok", ok}, {"removed", removed}, {"items", outcomes}};
}

void MiniImFileCleanup::reset()
{
    m_token.clear();
    m_items.clear();
}
