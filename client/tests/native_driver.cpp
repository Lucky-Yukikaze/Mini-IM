// Drives the real desktop bridge and native core over a loopback-only test command socket.
#include "bridge/imbridge.h"

#include <QCoreApplication>
#include <QHostAddress>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPointer>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>
#include <iostream>

namespace
{
bool Dispatch(ImBridge& bridge, const QJsonObject& command)
{
    const auto value = [&command](const char* key)
    {
        return command.value(QLatin1String(key)).toString();
    };
    const QString operation = value("op");
    if (operation == QStringLiteral("connect"))
    {
        return bridge.connectToServer(value("endpoint"), value("token"), value("device"));
    }
    if (operation == QStringLiteral("disconnect"))
    {
        bridge.disconnectFromServer();
        return true;
    }
    if (operation == QStringLiteral("direct"))
    {
        return bridge.createDirectConversation(value("intent"), value("peer"));
    }
    if (operation == QStringLiteral("group"))
    {
        return bridge.createConversation(
            value("intent"), value("title"), command.value("members").toVariant().toList());
    }
    if (operation == QStringLiteral("rename"))
    {
        return bridge.renameConversation(value("conversation"), value("title"));
    }
    if (operation == QStringLiteral("add-members"))
    {
        return bridge.addMembers(value("conversation"), command.value("members").toVariant().toList());
    }
    if (operation == QStringLiteral("remove-members"))
    {
        return bridge.removeMembers(value("conversation"), command.value("members").toVariant().toList());
    }
    if (operation == QStringLiteral("leave"))
    {
        return bridge.leaveConversation(value("conversation"));
    }
    if (operation == QStringLiteral("join"))
    {
        return bridge.joinConversation(value("conversation"));
    }
    if (operation == QStringLiteral("message"))
    {
        return bridge.sendMessage(value("conversation"), value("intent"), value("text"),
            command.value("burnMode").toInt(), command.value("burnTtlSec").toInt());
    }
    if (operation == QStringLiteral("retry-message"))
    {
        return bridge.retryMessage(value("conversation"), value("intent"));
    }
    if (operation == QStringLiteral("receipt"))
    {
        return bridge.sendReceipt(value("conversation"), command.value("seq").toVariant().toULongLong());
    }
    if (operation == QStringLiteral("recall"))
    {
        return bridge.recallMessage(value("conversation"), value("message"));
    }
    if (operation == QStringLiteral("retry-file"))
    {
        return bridge.retryFile(value("intent"));
    }
    if (operation == QStringLiteral("cancel-file"))
    {
        return bridge.cancelFile(value("intent"));
    }
    if (operation == QStringLiteral("upload"))
    {
        return bridge.sendFile(value("conversation"), value("path"), 0);
    }
    if (operation == QStringLiteral("download"))
    {
        return bridge.downloadFile(value("conversation"), value("source"), value("path"), 0);
    }
    return false;
}
}

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    ImBridge bridge;
    QTcpServer server;
    QPointer<QTcpSocket> peer;
    const auto send = [&peer](const QString& event, const QVariantMap& data)
    {
        if (peer)
        {
            const QJsonObject packet{{"event", event}, {"data", QJsonObject::fromVariantMap(data)}};
            peer->write(QJsonDocument(packet).toJson(QJsonDocument::Compact) + '\n');
        }
    };
    const auto forward = [&bridge, &app, &send](auto signal, const QString& event)
    {
        QObject::connect(&bridge, signal, &app, [&send, event](const QVariantMap& data) { send(event, data); });
    };
    forward(&ImBridge::connectionChanged, QStringLiteral("connection"));
    forward(&ImBridge::initialStateLoaded, QStringLiteral("initial"));
    forward(&ImBridge::messagePushed, QStringLiteral("message"));
    forward(&ImBridge::messageUpdated, QStringLiteral("update"));
    forward(&ImBridge::conversationUpdated, QStringLiteral("conversation"));
    forward(&ImBridge::controlWritesChanged, QStringLiteral("control-writes"));
    forward(&ImBridge::fileTasksChanged, QStringLiteral("file-tasks"));
    forward(&ImBridge::fileProgress, QStringLiteral("file"));
    forward(&ImBridge::syncProgress, QStringLiteral("sync"));
    forward(&ImBridge::messageSendsChanged, QStringLiteral("message-sends"));
    QObject::connect(&bridge, &ImBridge::errorRaised, &app, [&send](const QString& message)
    {
        send(QStringLiteral("error"), {{"message", message}});
    });
    QObject::connect(&server, &QTcpServer::newConnection, &app, [&]()
    {
        peer = server.nextPendingConnection();
        server.close();
        QObject::connect(peer, &QTcpSocket::disconnected, &app, &QCoreApplication::quit);
        QObject::connect(peer, &QTcpSocket::readyRead, &app, [&]()
        {
            if (peer->bytesAvailable() > 1024 * 1024)
            {
                peer->disconnectFromHost();
                return;
            }
            while (peer->canReadLine())
            {
                const QJsonObject command = QJsonDocument::fromJson(peer->readLine()).object();
                const auto operation = command.value("op").toString();
                bool accepted = true;
                if (operation == "preview-file-cleanup")
                {
                    send(QStringLiteral("file-cleanup"), bridge.previewCancelledDownloads());
                }
                else if (operation == "apply-file-cleanup")
                {
                    send(QStringLiteral("file-cleanup"), bridge.cleanupCancelledDownloads(command.value("token").toString()));
                }
                else
                {
                    accepted = Dispatch(bridge, command);
                }
                send(QStringLiteral("result"), {{"id", command.value("id").toString()}, {"accepted", accepted}});
            }
        });
    });
    if (!server.listen(QHostAddress::LocalHost, 0))
    {
        std::cerr << "failed to listen for the native test controller\n";
        return 1;
    }
    std::cout << server.serverPort() << std::endl;
    QTimer::singleShot(120000, &app, [&app]() { app.exit(2); });
    return app.exec();
}
