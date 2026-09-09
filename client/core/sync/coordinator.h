// Applies live/replayed events and owns catch-up requests, retries, and write readiness.
#ifndef MINI_IM_CORE_SYNC_COORDINATOR_H_
#define MINI_IM_CORE_SYNC_COORDINATOR_H_

#include "core/sync/statestore.h"
#include "envelope.pb.h"
#include <QElapsedTimer>
#include <QObject>
#include <QTimer>
#include <functional>
#include <string>

class MiniImSyncCoordinator final : public QObject
{
    Q_OBJECT

public:
    using RequestFactory = std::function<im::envelope::Envelope(quint64, quint32)>;
    using Sender = std::function<bool(const std::string&)>;

    MiniImSyncCoordinator(MiniImStateStore& store, RequestFactory factory, Sender sender, QObject* parent = nullptr);
    MiniImSyncCoordinator(const MiniImSyncCoordinator&) = delete;
    MiniImSyncCoordinator& operator=(const MiniImSyncCoordinator&) = delete;

    void start();
    void stop();
    bool isReady() const;
    bool handleConfirmationResult(const QString& requestId, bool success, int code, const QString& entityId);
    bool handleEnvelope(const im::envelope::Envelope& envelope);

signals:
    void eventApplied(const MiniImStateEvent& event);
    void stateApplied();
    void progressChanged(const QVariantMap& payload);
    void fileUpdated(const im::file::FileUpdated& updated);
    void readinessChanged(bool ready);
    void ready();
    void errorRaised(const QString& error);
    void failed();

private:
    void requestNext();
    void retryRequest();
    void pumpConfirmation();
    void clearRequest();
    bool apply(const QVector<MiniImStateEvent>& events);
    void handleResponse(const im::envelope::Envelope& envelope);
    void handleFile(const im::envelope::Envelope& envelope);
    void handleMessage(const im::envelope::Envelope& envelope);
    void fail(const QString& error);

    MiniImStateStore& m_store;
    RequestFactory m_factory;
    Sender m_sender;
    QTimer m_retryTimer;
    QElapsedTimer m_attemptTime;
    QElapsedTimer m_confirmationAttempt;
    QString m_confirmationId;
    std::string m_confirmationPayload;
    quint64 m_confirmationCursor = 0;
    QString m_requestId;
    std::string m_requestPayload;
    quint64 m_requestCursor = 0;
    bool m_running = false;
    bool m_caughtUp = false;
    bool m_reportedGap = false;
};

#endif  // MINI_IM_CORE_SYNC_COORDINATOR_H_
