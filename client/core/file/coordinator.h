// Owns persistent file intents, acknowledgements, scheduling and local transfer state.
#ifndef MINI_IM_CORE_FILE_COORDINATOR_H_
#define MINI_IM_CORE_FILE_COORDINATOR_H_

#include "core/file/taskstore.h"
#ifdef SendMessage
#undef SendMessage
#endif
#include "envelope.pb.h"
#include <QHash>
#include <QElapsedTimer>
#include <QTimer>
#include <QSet>
#include <functional>
#include <memory>

class MiniImDownloadSink;
class MiniImQuicConnection;

class MiniImFileCoordinator final : public QObject
{
    Q_OBJECT

public:
    using EnvelopeFactory = std::function<im::envelope::Envelope(const QString&)>;
    using Sender = std::function<bool(const std::string&)>;
    using RequestIdFactory = std::function<QString(const QString&)>;

    MiniImFileCoordinator(MiniImFileTaskStore& tasks, MiniImQuicConnection& transport,
        EnvelopeFactory envelopeFactory, Sender sender, RequestIdFactory requestIdFactory, QObject* parent = nullptr);
    MiniImFileCoordinator(const MiniImFileCoordinator&) = delete;
    MiniImFileCoordinator& operator=(const MiniImFileCoordinator&) = delete;

    void start(const QVariantList& cachedFiles);
    void stop();
    void setSyncReady(bool ready);
    void handleAck(const im::message::Ack& ack);
    bool sendFile(const QString& conversation_id, const QString& file_path, quint32 priority);
    bool downloadFile(
        const QString& conversation_id,
        const QString& source_file_id,
        const QString& save_path,
        quint32 priority);
    void handleFileResult(
        const QString& requestId, bool success, int code, const QString& error, const QString& fileId);
    bool retryFile(const QString& clientFileId);
    bool cancelFile(const QString& clientFileId);
    void pumpFileTasks();
    void handleFileUpdated(const im::file::FileUpdated& updated);

signals:
    void fileProgress(const QVariantMap& payload);
    void fileTasksChanged(const QVariantMap& payload);
    void errorRaised(const QString& error);
    void restartRequested(const QString& reason);
    void failed();

private:
    void onFileData(quint64 streamId, const QByteArray& payload);
    void onFileEnded(quint64 streamId);
    void onFileClosed(quint64 streamId, bool connectionShutdown);
    void onUploadFinished(const QString& fileId, bool success, const QString& error);
    void publishFileTasks();
    void failFileTask(const QString& fileId, const QString& error);
    void startFileTask(const QVariantMap& task);
    bool sendFileInitRequest(
        const QString& request_id,
        const QString& conversation_id,
        const QString& client_file_id,
        const QString& file_name,
        quint64 file_size,
        const QString& sha256,
        quint64 resume_offset,
        quint32 priority,
        int direction,
        const QString& source_file_id);
    bool sendFileFinish(
        const QString& file_id, bool success, quint64 transferredBytes = 0, const QString& sha256 = QString());
    bool sendFileStreamData(
        const QString& file_id, const QString& file_path, quint64 offset, quint64 fileSize);
    void handleIncomingFileStream(quint64 streamId, const QByteArray& payload);
    void flushPendingDownloadBuffers(const QString& file_id);
    void completeFileReceive(quint64 streamId);
    void finishDownload(const QString& file_id);

    struct PendingFileUpload
    {
        QString conversation_id;
        QString file_path;
        QString file_name;
        QString client_file_id;
        QString sha256;
        quint64 file_size = 0;
        quint32 priority = 0;
        QString file_id;
        bool stream_started = false;
    };

    struct PendingFileDownload
    {
        QString conversation_id;
        QString source_file_id;
        QString save_path;
        QString client_file_id;
        QString file_id;
        std::shared_ptr<MiniImDownloadSink> sink;
        bool finish_sent = false;
        quint64 resume_offset = 0;
    };

    struct DownloadStreamState
    {
        QByteArray buffer;
        QString file_id;
        bool header_parsed = false;
        bool finished = false;
    };

    MiniImFileTaskStore& m_tasks;
    MiniImQuicConnection& m_transport;
    EnvelopeFactory m_envelopeFactory;
    Sender m_sender;
    RequestIdFactory m_requestIdFactory;
    QTimer m_retryTimer;
    bool m_running = false;
    bool m_syncReady = false;
    QSet<QString> m_activeFileTasks;
    QHash<QString, QElapsedTimer> m_fileControlAttempts;
    QHash<QString, quint64> m_latest_file_versions;
    QHash<QString, PendingFileUpload> m_pending_file_init_requests;
    QHash<QString, PendingFileUpload> m_pending_file_uploads;
    QHash<QString, PendingFileDownload> m_pending_file_download_init_requests;
    QHash<QString, PendingFileDownload> m_pending_file_downloads;
    QSet<QString> m_failed_file_downloads;
    QHash<QString, QString> m_pending_file_finish_requests;
    QHash<quint64, DownloadStreamState> m_download_stream_states;
};

#endif  // MINI_IM_CORE_FILE_COORDINATOR_H_
