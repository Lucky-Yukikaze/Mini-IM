#include <QApplication>
#include <QCoreApplication>
#include <QString>
#include <QStringList>

#include "ui/mainwindow.h"

int main(int argc, char* argv[])
{
    if (qEnvironmentVariableIsEmpty("QT_OPENGL"))
    {
        qputenv("QT_OPENGL", "software");
    }
    if (qEnvironmentVariableIsEmpty("QTWEBENGINE_DISABLE_SANDBOX"))
    {
        qputenv("QTWEBENGINE_DISABLE_SANDBOX", "1");
    }
    QString chromium_flags = qEnvironmentVariable(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--no-sandbox --disable-gpu --disable-gpu-compositing --disable-features=Vulkan");
    const QStringList required_flags = {
        QStringLiteral("--no-sandbox"),
        QStringLiteral("--disable-gpu"),
        QStringLiteral("--disable-gpu-compositing"),
        QStringLiteral("--disable-features=Vulkan"),
        QStringLiteral("--disable-3d-apis"),
        QStringLiteral("--disable-webgl"),
        QStringLiteral("--disable-webgl2"),
        QStringLiteral("--disable-gpu-rasterization"),
        QStringLiteral("--disable-zero-copy"),
        QStringLiteral("--disable-accelerated-2d-canvas"),
        QStringLiteral("--disable-accelerated-video-decode"),
        QStringLiteral("--allow-file-access"),
        QStringLiteral("--allow-file-access-from-files"),
    };
    for (const auto& flag : required_flags)
    {
        if (!chromium_flags.contains(flag))
        {
            chromium_flags.append(QLatin1Char(' '));
            chromium_flags.append(flag);
        }
    }
    qputenv("QTWEBENGINE_CHROMIUM_FLAGS", chromium_flags.toUtf8());
    QCoreApplication::setAttribute(Qt::AA_UseSoftwareOpenGL);

    QApplication app(argc, argv);
    MiniImMainWindow main_window;
    main_window.show();
    return app.exec();
}
