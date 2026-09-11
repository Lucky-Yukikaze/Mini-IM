#include "core/file/coordinator.h"
#include "core/file/downloadsink.h"
#include "core/quic/connection.h"
#include "core/logging.h"
#include "core/model/eventmapper.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QUuid>
#include <stdexcept>
#include <utility>

namespace
{
constexpr int kRequestRetryMs = 5000;

QString FileDigest(const QString& path, quint64 expectedSize)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || static_cast<quint64>(file.size()) != expectedSize)
    {
        return {};
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    while (!file.atEnd())
    {
        const auto bytes = file.read(65536);
        if (bytes.isEmpty())
        {
            return {};
        }
        hash.addData(bytes);
    }
    return QString::fromLatin1(hash.result().toHex());
}

}

MiniImFileCoordinator::MiniImFileCoordinator(
    MiniImFileTaskStore& tasks, MiniImQuicConnection& transport, EnvelopeFactory envelopeFactory,
    Sender sender, RequestIdFactory requestIdFactory, QObject* parent)
    : QObject(parent), m_tasks(tasks), m_transport(transport), m_envelopeFactory(std::move(envelopeFactory)),
      m_sender(std::move(sender)), m_requestIdFactory(std::move(requestIdFactory))
{
    m_retryTimer.setInterval(500);
    QObject::connect(&m_retryTimer, &QTimer::timeout, this, &MiniImFileCoordinator::pumpFileTasks);
    QObject::connect(&m_transport, &MiniImQuicConnection::fileData, this, &MiniImFileCoordinator::onFileData);
    QObject::connect(&m_transport, &MiniImQuicConnection::fileEnded, this, &MiniImFileCoordinator::onFileEnded);
    QObject::connect(&m_transport, &MiniImQuicConnection::fileClosed, this, &MiniImFileCoordinator::onFileClosed);
    QObject::connect(&m_transport, &MiniImQuicConnection::uploadFinished, this, &MiniImFileCoordinator::onUploadFinished);
}

void MiniImFileCoordinator::start(const QVariantList& cachedFiles)
{
    stop();
    m_running = true;
    for (const auto& value : m_tasks.pending())
    {
        const auto task = value.toMap();
        if (task.value("direction").toInt() == 1 && task.value("status") == "finishing")
        {
            // Recheck server storage before resuming an upload's completion after reconnect.
            m_tasks.update(task.value("clientFileId").toString(), {{"status", "pending"}});
        }
    }
    for (const auto& value : cachedFiles)
    {
        const auto file = value.toMap();
        m_latest_file_versions.insert(file.value("fileId").toString(), file.value("version").toULongLong());
    }
    m_retryTimer.start();
}

void MiniImFileCoordinator::stop()
{
    m_cleanup.reset();
    m_running = false;
    m_syncReady = false;
    m_retryTimer.stop();
    m_activeFileTasks.clear();
    m_fileControlAttempts.clear();
    m_download_stream_states.clear();
    m_latest_file_versions.clear();
    m_pending_file_init_requests.clear();
    m_pending_file_uploads.clear();
    m_pending_file_download_init_requests.clear();
    m_pending_file_downloads.clear();
    m_failed_file_downloads.clear();
    m_pending_file_finish_requests.clear();
}

void MiniImFileCoordinator::setSyncReady(bool ready)
{
    m_syncReady = ready;
}

void MiniImFileCoordinator::handleAck(const im::message::Ack& ack)
{
    if (!m_running)
    {
        return;
    }
    const QString request_id = QString::fromStdString(ack.request_id());
    const auto task = m_tasks.byRequest(request_id);
    if ((task.value("status") == "cancelling" || task.value("status") == "cancel_failed")
        && request_id != task.value("cancelRequestId").toString())
    {
        return;
    }
    if (task.value("status") == "cancelled")
    {
        return;
    }
    handleFileResult(request_id, ack.success(), ack.code(), QString::fromStdString(ack.message()),
        QString::fromStdString(ack.entity_id()));
    const QString finishedFileId = m_pending_file_finish_requests.take(request_id);
    if (!finishedFileId.isEmpty())
    {
        if (ack.success())
        {
            m_pending_file_uploads.remove(finishedFileId);
            m_pending_file_downloads.remove(finishedFileId);
            for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end();)
            {
                it = it->file_id == finishedFileId ? m_download_stream_states.erase(it) : ++it;
            }
        }
        else if (m_pending_file_downloads.contains(finishedFileId))
        {
            m_pending_file_downloads[finishedFileId].finish_sent = false;
        }
    }
    auto pending_init = m_pending_file_init_requests.find(request_id);
    if (pending_init != m_pending_file_init_requests.end())
    {
        if (ack.success())
        {
            PendingFileUpload pending = pending_init.value();
            pending.file_id = QString::fromStdString(ack.entity_id());
            pending.stream_started = false;
            m_pending_file_uploads.insert(pending.file_id, pending);
        }
        m_pending_file_init_requests.erase(pending_init);
    }
    auto pending_download_init = m_pending_file_download_init_requests.find(request_id);
    if (pending_download_init != m_pending_file_download_init_requests.end())
    {
        if (ack.success())
        {
            PendingFileDownload pending = pending_download_init.value();
            pending.file_id = QString::fromStdString(ack.entity_id());
            m_failed_file_downloads.remove(pending.file_id);
            m_pending_file_downloads.insert(pending.file_id, pending);
            AppendClientLog(
                QStringLiteral("download ack ready fileId=%1 savePath=%2")
                    .arg(pending.file_id, pending.save_path));
            flushPendingDownloadBuffers(pending.file_id);
        }
        m_pending_file_download_init_requests.erase(pending_download_init);
    }
}

void MiniImFileCoordinator::onFileData(quint64 streamId, const QByteArray& payload)
{
    if (!m_running)
    {
        m_transport.completeFileReceive(streamId, true);
        return;
    }

    handleIncomingFileStream(streamId, payload);
    const auto state = m_download_stream_states.constFind(streamId);
    if (state == m_download_stream_states.cend())
    {
        m_transport.completeFileReceive(streamId, true);
    }
    else if (!state->header_parsed || state->buffer.isEmpty() || m_failed_file_downloads.contains(state->file_id))
    {
        completeFileReceive(streamId);
    }
}

void MiniImFileCoordinator::onFileEnded(quint64 streamId)
{
    if (!m_running)
    {
        return;
    }

    auto& state = m_download_stream_states[streamId];
    state.finished = true;
    flushPendingDownloadBuffers(state.file_id);
}

void MiniImFileCoordinator::onFileClosed(quint64 streamId, bool connectionShutdown)
{
    if (!m_running)
    {
        return;
    }

    const auto state = m_download_stream_states.constFind(streamId);
    if (state == m_download_stream_states.cend())
    {
        return;
    }
    const QString fileId = state->file_id;
    if (!state->finished || connectionShutdown)
    {
        m_failed_file_downloads.insert(fileId);
        m_download_stream_states.remove(streamId);
        if (connectionShutdown)
        {
            emit errorRaised(QStringLiteral("download stream interrupted"));
        }
        else
        {
            failFileTask(fileId, QStringLiteral("download stream interrupted"));
        }
        return;
    }
    flushPendingDownloadBuffers(fileId);
    if (m_failed_file_downloads.contains(fileId))
    {
        m_download_stream_states.remove(streamId);
    }
}

void MiniImFileCoordinator::onUploadFinished(const QString& fileId, bool success, const QString& error)
{
    if (!m_running)
    {
        return;
    }

    if (!success)
    {
        emit restartRequested(error);
    }
    else if (!sendFileFinish(fileId, true))
    {
        emit errorRaised(QStringLiteral("failed to send file finish"));
    }
}

bool MiniImFileCoordinator::sendFile(const QString& conversation_id, const QString& file_path, quint32 priority)
{
    if (!m_running || conversation_id.trimmed().isEmpty() || file_path.trimmed().isEmpty())
    {
        return false;
    }

    QFile file(file_path);
    if (!file.exists() || !file.open(QIODevice::ReadOnly))
    {
        emit errorRaised(QStringLiteral("failed to open file"));
        return false;
    }

    QCryptographicHash hash(QCryptographicHash::Sha256);
    while (!file.atEnd())
    {
        const QByteArray chunk = file.read(64 * 1024);
        if (chunk.isEmpty() && file.error() != QFileDevice::NoError)
        {
            emit errorRaised(QStringLiteral("failed to read file"));
            return false;
        }
        if (!chunk.isEmpty())
        {
            hash.addData(chunk);
        }
    }
    const quint64 file_size = static_cast<quint64>(file.size());
    file.close();
    if (file_size == 0)
    {
        emit errorRaised(QStringLiteral("empty file is not supported"));
        return false;
    }

    const QFileInfo info(file_path);
    const QString file_name = info.fileName();
    const QString sha256 = QString::fromLatin1(hash.result().toHex());
    const QString client_file_id = QStringLiteral("intent-%1")
        .arg(QUuid::createUuid().toString(QUuid::WithoutBraces));
    const QString request_id = m_requestIdFactory(QStringLiteral("fileinit"));

    try
    {
        m_tasks.create({{"clientFileId", client_file_id}, {"requestId", request_id},
            {"finishRequestId", m_requestIdFactory(QStringLiteral("filefinish"))}, {"conversationId", conversation_id},
            {"path", info.absoluteFilePath()}, {"fileName", file_name}, {"fileSize", QVariant::fromValue(file_size)},
            {"sha256", sha256}, {"priority", priority}, {"direction", 1}, {"sourceFileId", ""},
            {"metadataReady", true}, {"status", "pending"}, {"fileId", ""}, {"error", ""}});
        publishFileTasks();
        pumpFileTasks();
        return true;
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
}

bool MiniImFileCoordinator::downloadFile(
    const QString& conversation_id,
    const QString& source_file_id,
    const QString& save_path,
    quint32 priority)
{
    if (!m_running || conversation_id.trimmed().isEmpty() || source_file_id.trimmed().isEmpty() || save_path.trimmed().isEmpty())
    {
        return false;
    }

    const QString client_file_id = QStringLiteral("intent-%1")
        .arg(QUuid::createUuid().toString(QUuid::WithoutBraces));
    const QString request_id = m_requestIdFactory(QStringLiteral("filedl"));
    QString normalized_save_path = save_path.trimmed();
    const QFileInfo save_info(normalized_save_path);
    if (save_info.fileName().isEmpty() || save_info.isDir())
    {
        QString safe_name = source_file_id.trimmed();
        safe_name.replace(QChar('/'), QChar('_'));
        safe_name.replace(QChar('\\'), QChar('_'));
        safe_name.replace(QChar(':'), QChar('_'));
        if (safe_name.isEmpty())
        {
            safe_name = QStringLiteral("download");
        }
        normalized_save_path = QDir(normalized_save_path).filePath(safe_name + QStringLiteral(".bin"));
    }
    try
    {
        m_tasks.create({{"clientFileId", client_file_id}, {"requestId", request_id},
            {"finishRequestId", m_requestIdFactory(QStringLiteral("filefinish"))}, {"conversationId", conversation_id},
            {"path", QFileInfo(normalized_save_path).absoluteFilePath()}, {"fileName", QFileInfo(normalized_save_path).fileName()},
            {"fileSize", 1}, {"sha256", "na"}, {"priority", priority}, {"direction", 2},
            {"sourceFileId", source_file_id}, {"metadataReady", false}, {"status", "pending"},
            {"fileId", ""}, {"error", ""}});
        publishFileTasks();
        pumpFileTasks();
        return true;
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
}

void MiniImFileCoordinator::publishFileTasks()
{
    emit fileTasksChanged({{"items", m_tasks.pending()}});
}

void MiniImFileCoordinator::failFileTask(const QString& fileId, const QString& error)
{
    const auto task = m_tasks.byFile(fileId);
    if (task.value("status") == "cancelling" || task.value("status") == "cancel_failed"
        || task.value("status") == "cancelled")
    {
        return;
    }
    if (!task.isEmpty())
    {
        m_tasks.update(task.value("clientFileId").toString(), {{"status", "failed"}, {"error", error},
             {"restartFromZero", error.contains(QStringLiteral("sha256 mismatch"))
                 || error.contains(QStringLiteral("staging size"))}});
        m_activeFileTasks.remove(task.value("clientFileId").toString());
        m_fileControlAttempts.remove(task.value("requestId").toString());
        m_fileControlAttempts.remove(task.value("finishRequestId").toString());
        publishFileTasks();
    }
    emit errorRaised(error);
}

void MiniImFileCoordinator::handleFileResult(
    const QString& requestId, bool success, int code, const QString& error, const QString& fileId)
{
    if (!m_running)
    {
        return;
    }

    const auto task = m_tasks.byRequest(requestId);
    if (task.isEmpty())
    {
        return;
    }
    const QString id = task.value("clientFileId").toString();
    if (requestId == task.value("cancelRequestId").toString())
    {
        if (task.value("status") != "cancelling")
        {
            return;
        }
        if (success)
        {
            if (fileId != id)
            {
                throw std::runtime_error("file cancellation confirmation intent mismatch");
            }
            m_tasks.update(id, {{"status", "cancelled"}, {"error", ""}});
        }
        else
        {
            const bool retryable = code == 401 || code == 408 || code == 429 || code >= 500;
            m_tasks.update(id, {{"status", retryable ? "cancelling" : "cancel_failed"}, {"error", error}});
            if (!retryable)
            {
                emit errorRaised(error);
            }
        }
        if (success || m_tasks.task(id).value("status") == "cancel_failed")
        {
            m_activeFileTasks.remove(id);
            m_fileControlAttempts.remove(requestId);
        }
        publishFileTasks();
        return;
    }
    if (task.value("status") == "cancelling" || task.value("status") == "cancel_failed"
        || task.value("status") == "cancelled")
    {
        return;
    }
    if (success)
    {
        if (fileId.isEmpty())
        {
            throw std::runtime_error("file confirmation has no entity id");
        }
        const bool finishing = requestId == task.value("finishRequestId").toString();
        m_tasks.update(id,
            {{"fileId", fileId}, {"status", finishing ? "completed" : "transferring"}, {"error", ""}});
        if (finishing)
        {
            m_activeFileTasks.remove(id);
            m_fileControlAttempts.remove(requestId);
        }
    }
    else if (code != 401 && code != 408 && code != 429 && code < 500)
    {
        m_tasks.update(id, {{"status", "failed"}, {"error", error},
            {"finishRejected", requestId == task.value("finishRequestId").toString()}});
        m_activeFileTasks.remove(id);
        m_fileControlAttempts.remove(requestId);
    }
    publishFileTasks();
}

QVariantList MiniImFileCoordinator::cleanupTasks() const
{
    QVariantList result;
    for (const auto& value : m_tasks.all())
    {
        auto task = value.toMap();
        task.insert("cleanupBusy", m_activeFileTasks.contains(task.value("clientFileId").toString())
            || m_pending_file_downloads.contains(task.value("fileId").toString()));
        result.append(task);
    }
    return result;
}

QVariantMap MiniImFileCoordinator::previewCancelledDownloads()
{
    try
    {
        return m_running ? m_cleanup.preview(cleanupTasks())
            : QVariantMap{{"ok", false}, {"error", "connect before previewing cleanup"}};
    }
    catch (const std::exception& error)
    {
        m_cleanup.reset();
        return {{"ok", false}, {"error", QString::fromUtf8(error.what())}};
    }
}

QVariantMap MiniImFileCoordinator::cleanupCancelledDownloads(const QString& token)
{
    try
    {
        return m_running ? m_cleanup.apply(token, cleanupTasks())
            : QVariantMap{{"ok", false}, {"error", "cleanup preview expired; connect and preview again"}};
    }
    catch (const std::exception& error)
    {
        m_cleanup.reset();
        return {{"ok", false}, {"error", QString::fromUtf8(error.what())}};
    }
}

bool MiniImFileCoordinator::retryFile(const QString& clientFileId)
{
    try
    {
        const auto task = m_tasks.task(clientFileId);
        if (!m_running || task.value("status").toString() != QStringLiteral("failed"))
        {
            return false;
        }
        QVariantMap changes{{"status", "pending"}, {"error", ""}};
        if (task.value("finishRejected").toBool())
        {
            changes.insert("finishRequestId", m_requestIdFactory(QStringLiteral("filefinish")));
            changes.insert("finishRejected", false);
        }
        m_tasks.update(clientFileId, changes);
        publishFileTasks();
        // Releasing the old stream prevents duplicate writes when retrying the same intent.
        emit restartRequested(QStringLiteral("resuming file task"));
        return true;
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
}

bool MiniImFileCoordinator::cancelFile(const QString& clientFileId)
{
    try
    {
        const auto task = m_tasks.task(clientFileId);
        if (task.isEmpty() || task.value("status") == "completed" || task.value("status") == "cancelled")
        {
            return false;
        }
        if (task.value("status") == "cancelling")
        {
            return true;
        }
        m_tasks.update(clientFileId, {{"status", "cancelling"}, {"error", ""},
            {"cancelRequestId", m_requestIdFactory(QStringLiteral("filecancel"))}});
        publishFileTasks();
        if (m_running)
        {
            emit restartRequested(QStringLiteral("file task cancelled"));
        }
        return true;
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
}

void MiniImFileCoordinator::pumpFileTasks()
{
    if (!m_running || !m_syncReady)
    {
        return;
    }
    try
    {
        for (const auto& value : m_tasks.pending())
        {
            const auto task = value.toMap();
            const QString id = task.value("clientFileId").toString();
            if (task.value("status") == "failed" || task.value("status") == "cancel_failed")
            {
                continue;
            }
            if (task.value("status") == "cancelling")
            {
                const auto attempt = m_fileControlAttempts.constFind(task.value("cancelRequestId").toString());
                if (attempt == m_fileControlAttempts.constEnd() || attempt->elapsed() >= kRequestRetryMs)
                {
                    startFileTask(task);
                }
                continue;
            }
            const bool finishing = task.value("status") == "finishing";
            const QString request = task.value(finishing ? "finishRequestId" : "requestId").toString();
            if (m_activeFileTasks.contains(id))
            {
                const auto attempt = m_fileControlAttempts.constFind(request);
                if (attempt == m_fileControlAttempts.constEnd() || attempt->elapsed() < kRequestRetryMs)
                {
                    continue;
                }
                if (!finishing)
                {
                    emit restartRequested(QStringLiteral("file initialization timed out"));
                    return;
                }
            }
            else
            {
                if (m_activeFileTasks.size() >= 8)
                {
                    continue;
                }
                m_activeFileTasks.insert(id);
            }
            startFileTask(task);
        }
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        emit failed();
    }
}

void MiniImFileCoordinator::startFileTask(const QVariantMap& task)
{
    const QString id = task.value("clientFileId").toString();
    const QString requestId = task.value("requestId").toString();
    const QString fileId = task.value("fileId").toString();
    const QString path = task.value("path").toString();
    const bool download = task.value("direction").toInt() == 2;
    const quint64 size = task.value("fileSize").toULongLong();
    const QString hash = task.value("sha256").toString();
    if (task.value("status") == "cancelling")
    {
        const QString cancelRequestId = task.value("cancelRequestId").toString();
        auto envelope = m_envelopeFactory(cancelRequestId);
        envelope.set_channel(im::common::CHANNEL_FILE);
        auto* cancel = envelope.mutable_file_cancel();
        cancel->set_client_file_id(id.toStdString());
        cancel->set_file_id(fileId.toStdString());
        m_fileControlAttempts[cancelRequestId].start();
        m_sender(envelope.SerializeAsString());
        return;
    }
    if (task.value("status") == "finishing")
    {
        if (download && FileDigest(path, size) != hash)
        {
            failFileTask(fileId, QStringLiteral("completed download is missing or changed"));
            return;
        }
        sendFileFinish(fileId, true, task.value("transferredBytes").toULongLong(),
            task.value("verifiedSha256").toString());
        return;
    }
    quint64 offset = 0;
    if (download)
    {
        if (!fileId.isEmpty() && task.value("metadataReady").toBool() && FileDigest(path, size) == hash)
        {
            sendFileFinish(fileId, true, size, hash);
            return;
        }
        if (!fileId.isEmpty() && !task.value("restartFromZero").toBool())
        {
            const MiniImDownloadSink sink(path, fileId);
            const auto stagingSize = QFileInfo(sink.stagingPath()).size();
            if (stagingSize < 0 || static_cast<quint64>(stagingSize) > size)
            {
                failFileTask(fileId, QStringLiteral("download staging size is invalid"));
                return;
            }
            offset = static_cast<quint64>(stagingSize);
        }
        PendingFileDownload pending;
        pending.conversation_id = task.value("conversationId").toString();
        pending.source_file_id = task.value("sourceFileId").toString();
        pending.save_path = path;
        pending.client_file_id = id;
        pending.resume_offset = offset;
        m_pending_file_download_init_requests.insert(requestId, pending);
    }
    else
    {
        PendingFileUpload pending;
        pending.conversation_id = task.value("conversationId").toString();
        pending.file_path = path;
        pending.file_name = task.value("fileName").toString();
        pending.client_file_id = id;
        pending.sha256 = hash;
        pending.file_size = size;
        pending.priority = task.value("priority").toUInt();
        m_pending_file_init_requests.insert(requestId, pending);
    }
    m_fileControlAttempts[requestId].start();
    sendFileInitRequest(requestId, task.value("conversationId").toString(), id,
        task.value("fileName").toString(), size, hash, offset, task.value("priority").toUInt(),
        download ? 2 : 1, task.value("sourceFileId").toString());
}

bool MiniImFileCoordinator::sendFileInitRequest(
    const QString& request_id,
    const QString& conversation_id,
    const QString& client_file_id,
    const QString& file_name,
    quint64 file_size,
    const QString& sha256,
    quint64 resume_offset,
    quint32 priority,
    int direction,
    const QString& source_file_id)
{
    if (!m_running || request_id.trimmed().isEmpty())
    {
        return false;
    }

    auto envelope = m_envelopeFactory(request_id);
    auto* file_init = envelope.mutable_file_init();
    file_init->set_conversation_id(conversation_id.toStdString());
    file_init->set_client_file_id(client_file_id.toStdString());
    file_init->set_file_name(file_name.toStdString());
    file_init->set_file_size(file_size);
    file_init->set_sha256(sha256.toStdString());
    file_init->set_direction(static_cast<im::common::FileTransferDirection>(direction));
    file_init->set_resume_offset(resume_offset);
    file_init->set_priority(priority);
    file_init->set_source_file_id(source_file_id.toStdString());

    return m_sender(envelope.SerializeAsString());
}

bool MiniImFileCoordinator::sendFileFinish(
    const QString& file_id, bool success, quint64 transferredBytes, const QString& sha256)
{
    if (!m_running || file_id.isEmpty())
    {
        return false;
    }
    try
    {
        auto task = m_tasks.byFile(file_id);
        if (task.isEmpty())
        {
            return false;
        }
        if (task.value("status") == "cancelling" || task.value("status") == "cancel_failed"
            || task.value("status") == "cancelled")
        {
            return false;
        }
        const QString requestId = task.value("finishRequestId").toString();
        m_tasks.update(task.value("clientFileId").toString(),
            {{"status", "finishing"}, {"finishSuccess", success},
             {"transferredBytes", QVariant::fromValue(transferredBytes)}, {"verifiedSha256", sha256}});
        auto envelope = m_envelopeFactory(requestId);
        auto* finish = envelope.mutable_file_finish();
        finish->set_file_id(file_id.toStdString());
        finish->set_success(success);
        finish->set_transferred_bytes(transferredBytes);
        finish->set_sha256(sha256.toStdString());
        m_pending_file_finish_requests.insert(requestId, file_id);
        m_fileControlAttempts[requestId].start();
        publishFileTasks();
        return m_sender(envelope.SerializeAsString());
    }
    catch (const std::exception& error)
    {
        emit errorRaised(QString::fromUtf8(error.what()));
        return false;
    }
}

bool MiniImFileCoordinator::sendFileStreamData(
    const QString& file_id, const QString& file_path, quint64 offset, quint64 fileSize)
{
    return m_transport.sendFile(file_id, file_path, offset, fileSize);
}

void MiniImFileCoordinator::handleFileUpdated(const im::file::FileUpdated& updated)
{
    if (!m_running)
    {
        return;
    }

    const QString fileId = QString::fromStdString(updated.file_id());
    if (updated.version() < m_latest_file_versions.value(fileId, 0))
    {
        return;
    }
    m_latest_file_versions.insert(fileId, updated.version());
    emit fileProgress(miniim::BuildFileProgressPayload(
        updated.event_id(), updated.file_id(), updated.conversation_id(), updated.transferred_bytes(),
        updated.completed(), updated.version(), updated.updated_at_ms()));

    auto task = m_tasks.byFile(fileId);
    if (task.value("status") == "cancelling" || task.value("status") == "cancel_failed"
        || task.value("status") == "cancelled")
    {
        return;
    }
    if (!task.isEmpty() && updated.status() == "cancelled")
    {
        m_tasks.update(task.value("clientFileId").toString(), {{"status", "cancelled"}, {"error", ""}});
        publishFileTasks();
        emit restartRequested(QStringLiteral("file task cancelled remotely"));
        return;
    }
    if (!task.isEmpty() && updated.file_size() > 0)
    {
        m_tasks.update(task.value("clientFileId").toString(),
            {{"fileSize", QVariant::fromValue(updated.file_size())},
             {"sha256", QString::fromStdString(updated.sha256())}, {"metadataReady", true}});
        if (m_pending_file_uploads.contains(fileId) || m_pending_file_downloads.contains(fileId))
        {
            m_fileControlAttempts.remove(task.value("requestId").toString());
        }
        if (task.value("direction").toInt() == 1 && updated.completed())
        {
            m_fileControlAttempts.remove(task.value("requestId").toString());
            m_tasks.update(task.value("clientFileId").toString(), {{"status", "completed"}});
            m_activeFileTasks.remove(task.value("clientFileId").toString());
            publishFileTasks();
        }
    }
    if (task.value("status") == "failed" || task.value("status") == "cancelled")
    {
        return;
    }
    auto download = m_pending_file_downloads.find(fileId);
    if (download != m_pending_file_downloads.end() && !download->sink && updated.file_size() > 0)
    {
        download->sink = std::make_shared<MiniImDownloadSink>(download->save_path, fileId);
        if (!download->sink->open(updated.file_size(), QString::fromStdString(updated.sha256()), download->resume_offset))
        {
            m_failed_file_downloads.insert(fileId);
            failFileTask(fileId, download->sink->errorString());
            for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end(); ++it)
            {
                if (it->file_id == fileId)
                {
                    completeFileReceive(it.key());
                }
            }
            return;
        }
        flushPendingDownloadBuffers(fileId);
    }
    auto upload = m_pending_file_uploads.find(fileId);
    if (upload == m_pending_file_uploads.end() || upload->stream_started || updated.completed())
    {
        return;
    }
    if (FileDigest(upload->file_path, upload->file_size) != upload->sha256)
    {
        failFileTask(fileId, QStringLiteral("upload source has changed or is unavailable"));
        return;
    }
    if (sendFileStreamData(fileId, upload->file_path, updated.transferred_bytes(), upload->file_size))
    {
        upload->stream_started = true;
    }
    else
    {
        failFileTask(fileId, QStringLiteral("failed to send file stream"));
    }
}

void MiniImFileCoordinator::handleIncomingFileStream(quint64 streamId, const QByteArray& payload)
{
    const quint64 key = streamId;
    AppendClientLog(
        QStringLiteral("handleIncomingFileStream stream=%1 append=%2")
            .arg(key)
            .arg(payload.size()));
    auto state_it = m_download_stream_states.find(key);
    if (state_it == m_download_stream_states.end())
    {
        state_it = m_download_stream_states.insert(key, DownloadStreamState());
    }
    DownloadStreamState& state = state_it.value();
    state.buffer.append(payload);

    if (!state.header_parsed)
    {
        const int split = state.buffer.indexOf('\n');
        if (split < 0)
        {
            if (state.buffer.size() >= 512)
            {
                AppendClientLog(QStringLiteral("handleIncomingFileStream header too large stream=%1").arg(key));
                m_download_stream_states.remove(key);
            }
            return;
        }
        if (split >= 512)
        {
            m_download_stream_states.remove(key);
            return;
        }
        const QByteArray header = state.buffer.left(split);
        state.buffer.remove(0, split + 1);
        const QByteArray prefix("MINIIMFILE1 ");
        if (!header.startsWith(prefix))
        {
            AppendClientLog(QStringLiteral("handleIncomingFileStream invalid header stream=%1").arg(key));
            m_download_stream_states.remove(key);
            return;
        }
        state.file_id = QString::fromUtf8(header.mid(prefix.size())).trimmed();
        AppendClientLog(
            QStringLiteral("handleIncomingFileStream header parsed stream=%1 fileId=%2")
                .arg(key)
                .arg(state.file_id));
        state.header_parsed = true;
    }

    if (state.file_id.isEmpty())
    {
        AppendClientLog(QStringLiteral("handleIncomingFileStream empty file id stream=%1").arg(key));
        m_download_stream_states.remove(key);
        return;
    }
    flushPendingDownloadBuffers(state.file_id);
}

void MiniImFileCoordinator::flushPendingDownloadBuffers(const QString& file_id)
{
    auto pending = m_pending_file_downloads.find(file_id);
    if (pending == m_pending_file_downloads.end() || !pending->sink || m_failed_file_downloads.contains(file_id))
    {
        return;
    }
    bool finished = false;
    for (auto it = m_download_stream_states.begin(); it != m_download_stream_states.end(); ++it)
    {
        auto& state = it.value();
        if (state.file_id != file_id)
        {
            continue;
        }
        if (!state.buffer.isEmpty() && !pending->sink->append(state.buffer))
        {
            m_failed_file_downloads.insert(file_id);
            failFileTask(file_id, pending->sink->errorString());
            completeFileReceive(it.key());
            return;
        }
        state.buffer.clear();
        completeFileReceive(it.key());
        finished = finished || state.finished;
    }
    if (finished)
    {
        finishDownload(file_id);
    }
}

void MiniImFileCoordinator::completeFileReceive(quint64 streamId)
{
    auto state = m_download_stream_states.find(streamId);
    if (state == m_download_stream_states.end())
    {
        return;
    }
    const bool abort = m_failed_file_downloads.contains(state->file_id);
    if (abort)
    {
        state->buffer.clear();
    }
    m_transport.completeFileReceive(streamId, abort);
}

void MiniImFileCoordinator::finishDownload(const QString& file_id)
{
    auto pending = m_pending_file_downloads.find(file_id);
    if (pending == m_pending_file_downloads.end() || pending->finish_sent || !pending->sink)
    {
        return;
    }
    if (!pending->sink->finish())
    {
        m_failed_file_downloads.insert(file_id);
        failFileTask(file_id, pending->sink->errorString());
        return;
    }
    pending->finish_sent = sendFileFinish(file_id, true, pending->sink->receivedBytes(), pending->sink->sha256());
    if (!pending->finish_sent)
    {
        emit errorRaised(QStringLiteral("failed to confirm downloaded file"));
    }
}
