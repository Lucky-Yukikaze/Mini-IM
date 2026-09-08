// Schedules durable control writes independently after login and sync catch-up.
#ifndef MINI_IM_CORE_SESSION_WRITECOORDINATOR_H_
#define MINI_IM_CORE_SESSION_WRITECOORDINATOR_H_

#include "core/session/writestore.h"
#include <QObject>
#include <QTimer>
#include <QElapsedTimer>
#include <functional>
#include <exception>
#ifdef SendMessage
#undef SendMessage
#endif
#include "envelope.pb.h"

class MiniImControlWriteCoordinator final : public QObject
{
    Q_OBJECT
public:
    using Sender = std::function<bool(im::envelope::Envelope)>;
    using RequestIdFactory = std::function<QString()>;
    explicit MiniImControlWriteCoordinator(MiniImControlWriteStore& store, Sender sender,
        RequestIdFactory requestIdFactory, QObject* parent = nullptr);
    MiniImControlWriteCoordinator(const MiniImControlWriteCoordinator&) = delete;
    MiniImControlWriteCoordinator& operator=(const MiniImControlWriteCoordinator&) = delete;
    void start();
    void stop();
    void setSyncReady(bool ready);
    bool enqueue(const im::envelope::Envelope& body);
    void pump();
    void handleResult(const QString& requestId, bool success, int code, const QString& error, const QString& entityId);

signals:
    void writesChanged(const QVariantMap& payload);
    void errorRaised(const QString& message);
    void failed();

private:
    void publish();
    void fail(const std::exception& error);
    MiniImControlWriteStore& m_store;
    Sender m_sender;
    RequestIdFactory m_requestIdFactory;
    QTimer m_timer;
    QElapsedTimer m_attemptTime;
    QString m_activeRequest;
    bool m_running = false;
    bool m_syncReady = false;
};

#endif
