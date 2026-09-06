#ifndef MINI_IM_UI_MAINWINDOW_H_
#define MINI_IM_UI_MAINWINDOW_H_

#include <QMainWindow>
#include <QString>

class QWebChannel;
class QWebEngineView;
class QTextBrowser;
class ImBridge;

class MiniImMainWindow final : public QMainWindow
{
    Q_OBJECT

public:
    explicit MiniImMainWindow(QWidget* parent = nullptr);

private:
    bool tryLoadLocalWebDist();
    void showFallbackMessage(const QString& title, const QString& detail);

    QWebEngineView* m_web_view;
    QTextBrowser* m_fallback_view;
    QWebChannel* m_web_channel;
    ImBridge* m_bridge;
    QString m_target_web_url;
    bool m_local_fallback_attempted;
    bool m_render_process_crashed;
};

#endif  // MINI_IM_UI_MAINWINDOW_H_
