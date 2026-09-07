#include "core/quic/recovery.h"

MiniImConnectionRecovery::MiniImConnectionRecovery(QObject* parent) : QObject(parent)
{
    m_retryTimer.setSingleShot(true);
    m_retryTimer.setTimerType(Qt::PreciseTimer);
    m_loginTimer.setSingleShot(true);
    m_loginTimer.setTimerType(Qt::PreciseTimer);
    m_loginTimer.setInterval(10000);
    connect(&m_retryTimer, &QTimer::timeout, this, [this]()
    {
        if (m_enabled)
        {
            emit attemptRequested();
        }
    });
    connect(&m_loginTimer, &QTimer::timeout, this, [this]()
    {
        if (m_enabled)
        {
            emit loginTimedOut();
        }
    });
}

void MiniImConnectionRecovery::start()
{
    stop();
    m_enabled = true;
    m_nextDelayMs = 1000;
}

void MiniImConnectionRecovery::stop()
{
    m_enabled = false;
    m_retryTimer.stop();
    m_loginTimer.stop();
}

void MiniImConnectionRecovery::beginAttempt()
{
    m_retryTimer.stop();
    if (m_enabled)
    {
        m_loginTimer.start();
    }
}

void MiniImConnectionRecovery::authenticated()
{
    m_loginTimer.stop();
    m_nextDelayMs = 1000;
}

void MiniImConnectionRecovery::connectionLost()
{
    m_loginTimer.stop();
    if (!m_enabled || m_retryTimer.isActive())
    {
        return;
    }
    const int delayMs = m_nextDelayMs;
    m_nextDelayMs = qMin(m_nextDelayMs * 2, 30000);
    m_retryTimer.start(delayMs);
    emit retryScheduled(delayMs);
}

bool MiniImConnectionRecovery::enabled() const
{
    return m_enabled;
}
