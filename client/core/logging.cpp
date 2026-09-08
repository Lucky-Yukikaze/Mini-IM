#include "core/logging.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QTextStream>

namespace
{
bool IsClientDebugLogEnabled()
{
    static const QString value = qEnvironmentVariable("MINIIM_DEBUG_LOG");
    static const bool enabled = value.isEmpty() || value != QStringLiteral("0");
    return enabled;
}

}

void AppendClientLog(const QString& message)
{
    if (!IsClientDebugLogEnabled())
    {
        return;
    }
    const QString base_dir = QCoreApplication::applicationDirPath();
    QFile file(QDir(base_dir).filePath(QStringLiteral("mini_im_client.log")));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text))
    {
        return;
    }

    QTextStream stream(&file);
    stream << QDateTime::currentDateTime().toString(QStringLiteral("yyyy-MM-dd HH:mm:ss.zzz"))
           << QStringLiteral(" | ")
           << message
           << Qt::endl;
}
