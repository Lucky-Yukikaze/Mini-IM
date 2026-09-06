from __future__ import annotations

import time
from dataclasses import dataclass

from protocol.pb import common_pb2, conversation_pb2, message_pb2
from storage.repo import ConversationRepo, StoredSyncEvent


@dataclass
class CreateConversationServiceResult:
    ack: message_pb2.Ack
    conversation_updated: conversation_pb2.ConversationUpdated | None
    sync_events: list[StoredSyncEvent]


class ConversationService:
    def __init__(self, conversation_repo: ConversationRepo) -> None:
        self.m_conversation_repo = conversation_repo

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def ensure_user(self, user_id: str) -> None:
        self.m_conversation_repo.ensure_user(user_id)

    def handle_create_conversation(
        self,
        user_id: str,
        request_id: str,
        create_conversation: conversation_pb2.CreateConversation,
    ) -> CreateConversationServiceResult:
        now_ms = self._now_ms()
        ack = message_pb2.Ack(
            request_id=request_id,
            success=False,
            code=400,
            message="invalid create_conversation",
            entity_id="",
            server_time_ms=now_ms,
        )
        conversation_type = int(create_conversation.type)
        if not create_conversation.client_conv_id.strip():
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        member_ids = [item for item in create_conversation.member_ids if item.strip()]
        if conversation_type == int(common_pb2.CONVERSATION_GROUP):
            if len({user_id, *member_ids}) < 2:
                ack.message = "group conversation requires at least 2 members"
                return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])
            try:
                result = self.m_conversation_repo.create_group_conversation(
                    owner_id=user_id,
                    client_conv_id=create_conversation.client_conv_id.strip(),
                    title=create_conversation.title.strip(),
                    member_ids=member_ids,
                )
            except ValueError:
                ack.message = "conversation member does not exist"
                return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])
        elif conversation_type == int(common_pb2.CONVERSATION_DIRECT):
            unique_members = [item for item in dict.fromkeys(member_ids) if item != user_id]
            if len(unique_members) != 1:
                ack.message = "direct conversation requires exactly 1 peer"
                return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])
            try:
                result = self.m_conversation_repo.create_direct_conversation(
                    owner_id=user_id,
                    client_conv_id=create_conversation.client_conv_id.strip(),
                    target_user_id=unique_members[0],
                )
            except ValueError:
                ack.message = "direct peer does not exist"
                return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])
        else:
            ack.message = "unsupported conversation type"
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        if result is None:
            ack.message = "group conversation requires at least 2 members"
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        ack.success = True
        ack.code = 0
        ack.message = "ok" if result.created else "ok(idempotent)"
        ack.entity_id = result.conversation.conversation_id
        ack.server_time_ms = self._now_ms()
        return CreateConversationServiceResult(
            ack=ack,
            conversation_updated=result.conversation,
            sync_events=result.sync_events,
        )

    def _make_ack(self, request_id: str, message: str) -> message_pb2.Ack:
        now_ms = self._now_ms()
        return message_pb2.Ack(
            request_id=request_id,
            success=False,
            code=400,
            message=message,
            entity_id="",
            server_time_ms=now_ms,
        )

    def _result_from_update(
        self,
        request_id: str,
        conversation_id: str,
        update_result,
        failure_message: str,
    ) -> CreateConversationServiceResult:
        ack = self._make_ack(request_id, failure_message)
        if update_result is None:
            ack.code = 403
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        ack.success = True
        ack.code = 0
        ack.message = "ok" if update_result.changed else "ok(idempotent)"
        ack.entity_id = conversation_id
        ack.server_time_ms = self._now_ms()
        return CreateConversationServiceResult(
            ack=ack,
            conversation_updated=update_result.conversation,
            sync_events=update_result.sync_events,
        )

    def handle_add_members(
        self,
        user_id: str,
        request_id: str,
        add_members: conversation_pb2.AddMembers,
    ) -> CreateConversationServiceResult:
        conversation_id = add_members.conversation_id.strip()
        member_ids = [item.strip() for item in add_members.member_ids if item.strip()]
        if not conversation_id or not member_ids:
            ack = self._make_ack(request_id, "invalid add_members")
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        result = self.m_conversation_repo.add_members(
            operator_id=user_id,
            conversation_id=conversation_id,
            member_ids=member_ids,
        )
        return self._result_from_update(request_id, conversation_id, result, "add_members rejected")

    def handle_remove_members(
        self,
        user_id: str,
        request_id: str,
        remove_members: conversation_pb2.RemoveMembers,
    ) -> CreateConversationServiceResult:
        conversation_id = remove_members.conversation_id.strip()
        member_ids = [item.strip() for item in remove_members.member_ids if item.strip()]
        if not conversation_id or not member_ids:
            ack = self._make_ack(request_id, "invalid remove_members")
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        result = self.m_conversation_repo.remove_members(
            operator_id=user_id,
            conversation_id=conversation_id,
            member_ids=member_ids,
        )
        return self._result_from_update(request_id, conversation_id, result, "remove_members rejected")

    def handle_leave_conversation(
        self,
        user_id: str,
        request_id: str,
        leave_conversation: conversation_pb2.LeaveConversation,
    ) -> CreateConversationServiceResult:
        conversation_id = leave_conversation.conversation_id.strip()
        if not conversation_id:
            ack = self._make_ack(request_id, "invalid leave_conversation")
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        result = self.m_conversation_repo.leave_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return self._result_from_update(request_id, conversation_id, result, "leave_conversation rejected")

    def handle_join_conversation(
        self,
        user_id: str,
        request_id: str,
        join_conversation: conversation_pb2.JoinConversation,
    ) -> CreateConversationServiceResult:
        conversation_id = join_conversation.conversation_id.strip()
        if not conversation_id:
            ack = self._make_ack(request_id, "invalid join_conversation")
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        result = self.m_conversation_repo.join_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return self._result_from_update(request_id, conversation_id, result, "join_conversation rejected")

    def handle_rename_conversation(
        self,
        user_id: str,
        request_id: str,
        rename_conversation: conversation_pb2.RenameConversation,
    ) -> CreateConversationServiceResult:
        conversation_id = rename_conversation.conversation_id.strip()
        title = rename_conversation.title.strip()
        if not conversation_id or not title:
            ack = self._make_ack(request_id, "invalid rename_conversation")
            return CreateConversationServiceResult(ack=ack, conversation_updated=None, sync_events=[])

        result = self.m_conversation_repo.rename_conversation(
            operator_id=user_id,
            conversation_id=conversation_id,
            title=title,
        )
        return self._result_from_update(request_id, conversation_id, result, "rename_conversation rejected")
