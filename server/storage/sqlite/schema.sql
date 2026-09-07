PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  nickname TEXT,
  created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  platform TEXT,
  created_at_ms INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  status TEXT NOT NULL,
  last_acked_request_id TEXT,
  updated_at_ms INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  type INTEGER NOT NULL,
  title TEXT,
  owner_id TEXT,
  created_at_ms INTEGER NOT NULL,
  FOREIGN KEY (owner_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS conversation_create_requests (
  user_id TEXT NOT NULL,
  client_conv_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (user_id, client_conv_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);

CREATE TABLE IF NOT EXISTS conversation_members (
  conversation_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  joined_at_ms INTEGER NOT NULL,
  last_read_seq INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (conversation_id, user_id),
  FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS messages (
  server_msg_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  client_msg_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  conversation_seq INTEGER NOT NULL,
  type INTEGER NOT NULL,
  content BLOB NOT NULL,
  created_at_ms INTEGER NOT NULL,
  recalled INTEGER NOT NULL DEFAULT 0,
  burn_mode INTEGER NOT NULL DEFAULT 0,
  burn_ttl_sec INTEGER NOT NULL DEFAULT 0,
  content_purged_at_ms INTEGER NOT NULL DEFAULT 0,
  UNIQUE (conversation_id, sender_id, client_msg_id),
  UNIQUE (conversation_id, conversation_seq),
  FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
  FOREIGN KEY (sender_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS message_deliveries (
  server_msg_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  status TEXT NOT NULL,
  delivered_at_ms INTEGER,
  read_at_ms INTEGER,
  burn_started_at_ms INTEGER,
  burn_at_ms INTEGER,
  burned_at_ms INTEGER,
  failed_at_ms INTEGER,
  failure_reason TEXT,
  PRIMARY KEY (server_msg_id, user_id),
  FOREIGN KEY (server_msg_id) REFERENCES messages(server_msg_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);

CREATE TABLE IF NOT EXISTS attachments (
  attachment_id TEXT PRIMARY KEY,
  server_msg_id TEXT NOT NULL,
  file_name TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  path TEXT,
  status TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  FOREIGN KEY (server_msg_id) REFERENCES messages(server_msg_id)
);

CREATE TABLE IF NOT EXISTS file_transfers (
  file_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  client_file_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  file_name TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  direction INTEGER NOT NULL,
  source_file_id TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 0,
  received_bytes INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  UNIQUE (owner_id, conversation_id, client_file_id),
  FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
  FOREIGN KEY (owner_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_file_transfers_owner_intent
ON file_transfers(owner_id, client_file_id);

CREATE INDEX IF NOT EXISTS idx_file_transfers_owner_updated
ON file_transfers(owner_id, updated_at_ms);

CREATE TABLE IF NOT EXISTS sync_cursors (
  user_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  last_seq INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY (user_id, conversation_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS sync_events (
  event_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  conversation_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  entity_id TEXT NOT NULL DEFAULT '',
  payload BLOB,
  created_at_ms INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_events_user_seq
ON sync_events(user_id, seq);

CREATE INDEX IF NOT EXISTS idx_sync_events_entity
ON sync_events(entity_id, user_id) WHERE event_type = 'message';

CREATE INDEX IF NOT EXISTS idx_message_deliveries_burn_due
ON message_deliveries(burn_at_ms, burned_at_ms);

CREATE TABLE IF NOT EXISTS message_read_counters (
  server_msg_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  conversation_seq INTEGER NOT NULL,
  member_count INTEGER NOT NULL,
  read_count INTEGER NOT NULL,
  unread_count INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  FOREIGN KEY (server_msg_id) REFERENCES messages(server_msg_id),
  FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_message_read_counters_conversation_seq
ON message_read_counters(conversation_id, conversation_seq);
