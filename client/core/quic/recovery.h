// Schedules connection attempts and bounds the time spent waiting for login.
#ifndef MINI_IM_CORE_QUIC_RECOVERY_H_
#define MINI_IM_CORE_QUIC_RECOVERY_H_

#include <QObject>
#include <QTimer>

class MiniImConnectionRecovery final : public QObject
{
    Q_OBJECT
public:
    explicit MiniImConnectionRecovery(QObject* parent = nullptr);
    void start();
    void stop();
    void beginAttempt();
    void authenticated();
    void connectionLost();
    bool enabled() const;

signals:
    void attemptRequested();
    void loginTimedOut();
    void retryScheduled(int delayMs);

private:
    QTimer m_retryTimer;
    QTimer m_loginTimer;
    bool m_enabled = false;
    int m_nextDelayMs = 1000;
};

#endif  // MINI_IM_CORE_QUIC_RECOVERY_H_
