from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from protocol.pb import common_pb2, conversation_pb2
from storage.sqlite.db import MiniImSqliteDb
from storage.repo.sync_event import AppendSyncEvents, StoredSyncEvent


@dataclass
class ConversationRecord:
    conversation_id: str
    title: str
    type: int
    owner_id: str
    member_ids: list[str]


@dataclass
class CreateConversationResult:
    conversation: conversation_pb2.ConversationUpdated
    sync_events: list[StoredSyncEvent]
    created: bool


@dataclass
class UpdateConversationResult:
    conversation: conversation_pb2.ConversationUpdated
    sync_events: list[StoredSyncEvent]
    changed: bool


class ConversationRepo:
    def __init__(self, db: MiniImSqliteDb) -> None:
        self.m_db = db

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _normalize_member_ids(owner_id: str, member_ids: list[str]) -> list[str]:
        normalized = [owner_id]
        for member_id in member_ids:
            item = member_id.strip()
            if not item or item in normalized:
                continue
            normalized.append(item)
        return normalized

    @staticmethod
    def _unique_user_ids(user_ids: list[str]) -> list[str]:
        result: list[str] = []
        for user_id in user_ids:
            item = user_id.strip()
            if item and item not in result:
                result.append(item)
        return result

    def ensure_user(self, user_id: str) -> None:
        item = user_id.strip()
        if not item:
            return
        now_ms = self._now_ms()
        self.m_db.execute_write(
            """
            INSERT OR IGNORE INTO users(user_id, nickname, created_at_ms)
            VALUES(?, ?, ?)
            """,
            (item, item, now_ms),
        )

    def user_exists(self, user_id: str) -> bool:
        row = self.m_db.execute_fetchone(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user_id.strip(),),
        )
        return row is not None

    def all_users_exist(self, user_ids: list[str]) -> bool:
        return all(self.user_exists(user_id) for user_id in self._unique_user_ids(user_ids))

    def has_any_user(self) -> bool:
        row = self.m_db.execute_fetchone("SELECT 1 FROM users LIMIT 1")
        return row is not None

    def _build_conversation_updated_events(
        self,
        connection,
        record: ConversationRecord,
        target_user_ids: list[str],
        now_ms: int,
    ) -> tuple[conversation_pb2.ConversationUpdated, list[StoredSyncEvent]]:
        conversation_updated = conversation_pb2.ConversationUpdated(
            event_id=str(uuid.uuid4()),
            conversation_id=record.conversation_id,
            updated_at_ms=now_ms,
            title=record.title,
            type=record.type,
            owner_id=record.owner_id,
            member_ids=record.member_ids,
        )
        sync_events = AppendSyncEvents(
            connection, self._unique_user_ids(target_user_ids), record.conversation_id,
            "conversation_updated", conversation_updated, now_ms,
        )
        return conversation_updated, sync_events

    def create_group_conversation(
        self,
        owner_id: str,
        client_conv_id: str,
        title: str,
        member_ids: list[str],
    ) -> CreateConversationResult:
        return self.create_conversation(
            owner_id=owner_id,
            client_conv_id=client_conv_id,
            title=title,
            member_ids=member_ids,
            conversation_type=int(common_pb2.CONVERSATION_GROUP),
        )

    def create_direct_conversation(
        self,
        owner_id: str,
        client_conv_id: str,
        target_user_id: str,
    ) -> CreateConversationResult:
        return self.create_conversation(
            owner_id=owner_id,
            client_conv_id=client_conv_id,
            title="",
            member_ids=[target_user_id],
            conversation_type=int(common_pb2.CONVERSATION_DIRECT),
        )

    def create_conversation(
        self,
        owner_id: str,
        client_conv_id: str,
        title: str,
        member_ids: list[str],
        conversation_type: int,
    ) -> CreateConversationResult:
        now_ms = self._now_ms()
        normalized_members = self._normalize_member_ids(owner_id, member_ids)
        connection = self.m_db.m_connection

        with self.m_db.transaction():
            should_validate_users = self.has_any_user()
            if should_validate_users and not self.all_users_exist(normalized_members):
                raise ValueError("conversation member does not exist")
            if not should_validate_users:
                for user_id in normalized_members:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO users(user_id, nickname, created_at_ms)
                        VALUES(?, ?, ?)
                        """,
                        (user_id, user_id, now_ms),
                    )

            dedup_row = connection.execute(
                """
                SELECT conversation_id
                FROM conversation_create_requests
                WHERE user_id = ? AND client_conv_id = ?
                """,
                (owner_id, client_conv_id),
            ).fetchone()

            created = False
            if dedup_row is None:
                conversation_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO conversations(conversation_id, type, title, owner_id, created_at_ms)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        int(conversation_type),
                        title,
                        owner_id,
                        now_ms,
                    ),
                )
                for member_id in normalized_members:
                    role = "owner" if member_id == owner_id else "member"
                    connection.execute(
                        """
                        INSERT INTO conversation_members(
                          conversation_id,
                          user_id,
                          role,
                          joined_at_ms,
                          last_read_seq
                        ) VALUES(?, ?, ?, ?, 0)
                        """,
                        (conversation_id, member_id, role, now_ms),
                    )
                connection.execute(
                    """
                    INSERT INTO conversation_create_requests(
                      user_id,
                      client_conv_id,
                      conversation_id,
                      created_at_ms
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (owner_id, client_conv_id, conversation_id, now_ms),
                )
                created = True
            else:
                conversation_id = str(dedup_row["conversation_id"])

            record = self.get_conversation(conversation_id)
            if record is None:
                raise RuntimeError("conversation must exist after creation")

            conversation_updated = conversation_pb2.ConversationUpdated(
                event_id="",
                conversation_id=record.conversation_id,
                updated_at_ms=now_ms,
                title=record.title,
                type=record.type,
                owner_id=record.owner_id,
                member_ids=record.member_ids,
            )

            sync_events = AppendSyncEvents(
                connection, record.member_ids, record.conversation_id,
                "conversation_updated", conversation_updated, now_ms,
            )

        return CreateConversationResult(
            conversation=conversation_updated,
            sync_events=sync_events,
            created=created,
        )

    def rename_conversation(
        self,
        operator_id: str,
        conversation_id: str,
        title: str,
    ) -> UpdateConversationResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection
        with self.m_db.transaction():
            record = self.get_conversation(conversation_id)
            if record is None or record.type != int(common_pb2.CONVERSATION_GROUP):
                return None
            if record.owner_id != operator_id:
                return None
            if record.title == title:
                updated, _ = self._build_conversation_updated_events(connection, record, [], now_ms)
                return UpdateConversationResult(conversation=updated, sync_events=[], changed=False)

            connection.execute(
                "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                (title, conversation_id),
            )
            updated_record = self.get_conversation(conversation_id)
            if updated_record is None:
                raise RuntimeError("conversation must exist after rename")
            updated, events = self._build_conversation_updated_events(
                connection,
                updated_record,
                updated_record.member_ids,
                now_ms,
            )
            return UpdateConversationResult(conversation=updated, sync_events=events, changed=True)

    def add_members(
        self,
        operator_id: str,
        conversation_id: str,
        member_ids: list[str],
    ) -> UpdateConversationResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection
        with self.m_db.transaction():
            record = self.get_conversation(conversation_id)
            if record is None or record.type != int(common_pb2.CONVERSATION_GROUP):
                return None
            if record.owner_id != operator_id:
                return None

            existing = set(record.member_ids)
            new_members = [
                item for item in self._unique_user_ids(member_ids)
                if item not in existing
            ]
            if not new_members:
                updated, _ = self._build_conversation_updated_events(connection, record, [], now_ms)
                return UpdateConversationResult(conversation=updated, sync_events=[], changed=False)
            if not self.all_users_exist(new_members):
                return None

            for user_id in new_members:
                connection.execute(
                    """
                    INSERT INTO conversation_members(conversation_id, user_id, role, joined_at_ms, last_read_seq)
                    VALUES(?, ?, 'member', ?, 0)
                    """,
                    (conversation_id, user_id, now_ms),
                )

            updated_record = self.get_conversation(conversation_id)
            if updated_record is None:
                raise RuntimeError("conversation must exist after add_members")
            updated, events = self._build_conversation_updated_events(
                connection,
                updated_record,
                updated_record.member_ids,
                now_ms,
            )
            return UpdateConversationResult(conversation=updated, sync_events=events, changed=True)

    def join_conversation(
        self,
        user_id: str,
        conversation_id: str,
    ) -> UpdateConversationResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection
        with self.m_db.transaction():
            record = self.get_conversation(conversation_id)
            if record is None or record.type != int(common_pb2.CONVERSATION_GROUP):
                return None
            if not self.user_exists(user_id):
                return None
            if user_id in record.member_ids:
                updated, _ = self._build_conversation_updated_events(connection, record, [], now_ms)
                return UpdateConversationResult(conversation=updated, sync_events=[], changed=False)

            connection.execute(
                """
                INSERT INTO conversation_members(conversation_id, user_id, role, joined_at_ms, last_read_seq)
                VALUES(?, ?, 'member', ?, 0)
                """,
                (conversation_id, user_id, now_ms),
            )

            updated_record = self.get_conversation(conversation_id)
            if updated_record is None:
                raise RuntimeError("conversation must exist after join_conversation")
            updated, events = self._build_conversation_updated_events(
                connection,
                updated_record,
                updated_record.member_ids,
                now_ms,
            )
            return UpdateConversationResult(conversation=updated, sync_events=events, changed=True)

    def remove_members(
        self,
        operator_id: str,
        conversation_id: str,
        member_ids: list[str],
    ) -> UpdateConversationResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection
        with self.m_db.transaction():
            record = self.get_conversation(conversation_id)
            if record is None or record.type != int(common_pb2.CONVERSATION_GROUP):
                return None
            if record.owner_id != operator_id:
                return None

            old_members = list(record.member_ids)
            targets = [
                item for item in self._unique_user_ids(member_ids)
                if item in old_members and item != record.owner_id
            ]
            if not targets:
                updated, _ = self._build_conversation_updated_events(connection, record, [], now_ms)
                return UpdateConversationResult(conversation=updated, sync_events=[], changed=False)

            for user_id in targets:
                connection.execute(
                    "DELETE FROM conversation_members WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, user_id),
                )

            updated_record = self.get_conversation(conversation_id)
            if updated_record is None:
                raise RuntimeError("conversation must exist after remove_members")
            updated, events = self._build_conversation_updated_events(
                connection,
                updated_record,
                old_members,
                now_ms,
            )
            return UpdateConversationResult(conversation=updated, sync_events=events, changed=True)

    def leave_conversation(
        self,
        user_id: str,
        conversation_id: str,
    ) -> UpdateConversationResult | None:
        now_ms = self._now_ms()
        connection = self.m_db.m_connection
        with self.m_db.transaction():
            record = self.get_conversation(conversation_id)
            if record is None or record.type != int(common_pb2.CONVERSATION_GROUP):
                return None
            if user_id not in record.member_ids:
                return None

            old_members = list(record.member_ids)
            remaining = [item for item in old_members if item != user_id]
            if not remaining:
                return None

            connection.execute(
                "DELETE FROM conversation_members WHERE conversation_id = ? AND user_id = ?",
                (conversation_id, user_id),
            )
            if record.owner_id == user_id:
                new_owner = remaining[0]
                connection.execute(
                    "UPDATE conversations SET owner_id = ? WHERE conversation_id = ?",
                    (new_owner, conversation_id),
                )
                connection.execute(
                    "UPDATE conversation_members SET role = 'owner' WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, new_owner),
                )

            updated_record = self.get_conversation(conversation_id)
            if updated_record is None:
                raise RuntimeError("conversation must exist after leave_conversation")
            updated, events = self._build_conversation_updated_events(
                connection,
                updated_record,
                old_members,
                now_ms,
            )
            return UpdateConversationResult(conversation=updated, sync_events=events, changed=True)

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        row = self.m_db.execute_fetchone(
            """
            SELECT conversation_id, type, title, owner_id
            FROM conversations
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        )
        if row is None:
            return None

        member_rows = self.m_db.execute_fetchall(
            """
            SELECT user_id
            FROM conversation_members
            WHERE conversation_id = ?
            ORDER BY joined_at_ms ASC, user_id ASC
            """,
            (conversation_id,),
        )
        return ConversationRecord(
            conversation_id=str(row["conversation_id"]),
            title=str(row["title"] or ""),
            type=int(row["type"]),
            owner_id=str(row["owner_id"] or ""),
            member_ids=[str(item["user_id"]) for item in member_rows],
        )

    def is_member(self, conversation_id: str, user_id: str) -> bool:
        row = self.m_db.execute_fetchone(
            """
            SELECT 1
            FROM conversation_members
            WHERE conversation_id = ? AND user_id = ?
            """,
            (conversation_id, user_id),
        )
        return row is not None

    def list_member_ids(self, conversation_id: str) -> list[str]:
        rows = self.m_db.execute_fetchall(
            """
            SELECT user_id
            FROM conversation_members
            WHERE conversation_id = ?
            ORDER BY joined_at_ms ASC, user_id ASC
            """,
            (conversation_id,),
        )
        return [str(row["user_id"]) for row in rows]
