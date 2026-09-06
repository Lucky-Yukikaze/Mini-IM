#include "ui/mainwindow.h"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QTextBrowser>
#include <QUrl>
#include <QVBoxLayout>
#include <QWebChannel>
#include <QWidget>

#ifdef MINI_IM_HAS_WEBENGINE
#include <QWebEngineLoadingInfo>
#include <QWebEnginePage>
#include <QWebEngineSettings>
#include <QWebEngineView>
#endif

#include "bridge/imbridge.h"

MiniImMainWindow::MiniImMainWindow(QWidget* parent)
    : QMainWindow(parent),
      m_web_view(nullptr),
      m_fallback_view(nullptr),
      m_web_channel(new QWebChannel(this)),
      m_bridge(new ImBridge(this)),
      m_target_web_url(),
      m_local_fallback_attempted(false),
      m_render_process_crashed(false)
{
    setWindowTitle(QStringLiteral("Mini-IM Client"));
    resize(1200, 800);

    auto* center_widget = new QWidget(this);
    auto* layout = new QVBoxLayout(center_widget);
    layout->setContentsMargins(0, 0, 0, 0);

#ifdef MINI_IM_HAS_WEBENGINE
    auto* web_view = new QWebEngineView(this);
    m_web_view = web_view;
    layout->addWidget(m_web_view);

    m_fallback_view = new QTextBrowser(this);
    m_fallback_view->hide();
    layout->addWidget(m_fallback_view);

    m_web_channel->registerObject(QStringLiteral("imBridge"), m_bridge);
    web_view->page()->setWebChannel(m_web_channel);
    web_view->settings()->setAttribute(QWebEngineSettings::LocalContentCanAccessFileUrls, true);
    web_view->settings()->setAttribute(QWebEngineSettings::LocalContentCanAccessRemoteUrls, true);

    QObject::connect(
        web_view->page(),
        &QWebEnginePage::loadingChanged,
        this,
        [this](const QWebEngineLoadingInfo& info)
        {
            if (info.status() != QWebEngineLoadingInfo::LoadFailedStatus)
            {
                return;
            }
            if (info.errorCode() == 0 && info.errorString().isEmpty())
            {
                return;
            }

            showFallbackMessage(
                QStringLiteral("页面加载失败"),
                QStringLiteral("url=%1\nerrorCode=%2\nerror=%3")
                    .arg(info.url().toString())
                    .arg(info.errorCode())
                    .arg(info.errorString()));
        });

    QObject::connect(
        web_view,
        &QWebEngineView::loadFinished,
        this,
        [this](bool ok)
        {
            if (ok)
            {
                m_render_process_crashed = false;
                if (m_fallback_view != nullptr)
                {
                    m_fallback_view->hide();
                }
                if (m_web_view != nullptr)
                {
                    m_web_view->show();
                }
                return;
            }

            if (m_render_process_crashed)
            {
                return;
            }

            if (!m_local_fallback_attempted)
            {
                m_local_fallback_attempted = true;
                if (tryLoadLocalWebDist())
                {
                    return;
                }
            }

            if (m_target_web_url.startsWith(QStringLiteral("http://127.0.0.1:5173")))
            {
                return;
            }

            showFallbackMessage(
                QStringLiteral("页面加载失败"),
                QStringLiteral("Web UI 地址无法访问：%1").arg(m_target_web_url));
        });

    QObject::connect(
        web_view->page(),
        &QWebEnginePage::renderProcessTerminated,
        this,
        [this](QWebEnginePage::RenderProcessTerminationStatus status, int exit_code)
        {
            m_render_process_crashed = true;
            showFallbackMessage(
                QStringLiteral("渲染进程已终止"),
                QStringLiteral("status=%1, exitCode=%2, url=%3")
                    .arg(static_cast<int>(status))
                    .arg(exit_code)
                    .arg(m_target_web_url));
        });

    if (qEnvironmentVariableIsSet("MINIIM_WEB_SMOKE_TEST"))
    {
        m_target_web_url = QStringLiteral("data:text/html;charset=utf-8,%3Ch1%3EMini-IM%20WebEngine%20OK%3C/h1%3E");
        web_view->load(QUrl(m_target_web_url));
        setCentralWidget(center_widget);
        return;
    }

    m_target_web_url = qEnvironmentVariable("MINIIM_WEB_URL");
    if (m_target_web_url.isEmpty() && tryLoadLocalWebDist())
    {
        setCentralWidget(center_widget);
        return;
    }
    if (m_target_web_url.isEmpty())
    {
        m_target_web_url = QStringLiteral("http://127.0.0.1:5173");
    }
    web_view->load(QUrl(m_target_web_url));
#else
    auto* placeholder = new QTextBrowser(this);
    placeholder->setText(
        QStringLiteral("QtWebEngineWidgets not found. Install Qt WebEngine dev package to enable embedded web UI."));
    layout->addWidget(placeholder);
#endif

    setCentralWidget(center_widget);
}

bool MiniImMainWindow::tryLoadLocalWebDist()
{
#ifdef MINI_IM_HAS_WEBENGINE
    if (m_web_view == nullptr)
    {
        return false;
    }

    const QDir app_dir(QCoreApplication::applicationDirPath());
    const QStringList candidates = {
        app_dir.filePath(QStringLiteral("../../../web/dist/index.html")),
        app_dir.filePath(QStringLiteral("../../web/dist/index.html")),
        app_dir.filePath(QStringLiteral("../web/dist/index.html")),
        app_dir.filePath(QStringLiteral("web/dist/index.html")),
    };

    for (const auto& candidate : candidates)
    {
        const QFileInfo file_info(candidate);
        if (!file_info.exists() || !file_info.isFile())
        {
            continue;
        }
        QUrl local_url = QUrl::fromLocalFile(file_info.absoluteFilePath());
        local_url.setQuery(QStringLiteral("v=%1").arg(file_info.lastModified().toMSecsSinceEpoch()));
        m_target_web_url = local_url.toString();
        m_web_view->load(local_url);
        return true;
    }
#else
    Q_UNUSED(m_target_web_url);
#endif
    return false;
}

void MiniImMainWindow::showFallbackMessage(const QString& title, const QString& detail)
{
    if (m_fallback_view == nullptr)
    {
        return;
    }
#ifdef MINI_IM_HAS_WEBENGINE
    if (m_web_view != nullptr)
    {
        m_web_view->hide();
    }
#endif
    const QString body = QStringLiteral(
        "%1\n\n%2\n\n建议检查：\n"
        "1. 前端是否监听 MINIIM_WEB_URL 端口\n"
        "2. 若端口可用，客户端会自动回退到本地 web/dist\n"
        "3. 是否使用 Release 客户端\n"
        "4. 是否设置 QT_OPENGL=software 和 --disable-gpu\n")
                             .arg(title)
                             .arg(detail);
    m_fallback_view->setPlainText(body);
    m_fallback_view->show();
}
