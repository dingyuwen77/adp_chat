"""企微网关 SQLite 结构和事务内的版本初始化。"""

from .messages import storage_error


SCHEMA_VERSION = 1
ACTIVE = "adp_state IN ('RECEIVED', 'ADP_SEND_ARMED')"
SEND_OPEN = "send_state IN ('PENDING', 'SEND_ARMED')"
UNFINISHED = f"({ACTIVE} OR {SEND_OPEN})"
RETENTION = (
    "adp_state IN ('ADP_SUCCEEDED', 'ADP_REJECTED', 'REJECTED_USER_BUSY') "
    "AND send_state IN ('SEND_ACCEPTED', 'SEND_SIMULATED', 'SEND_FAILED')"
)
DDL = (
    """CREATE TABLE gateway_owner (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        corp_id TEXT NOT NULL, agent_id INTEGER NOT NULL,
        adp_application TEXT NOT NULL, environment TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE gateway_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_key TEXT NOT NULL,
        corp_id TEXT NOT NULL, agent_id INTEGER NOT NULL,
        user_id TEXT NOT NULL, send_user TEXT NOT NULL, conversation_id TEXT NOT NULL,
        msg_id TEXT NOT NULL, create_time TEXT NOT NULL, content TEXT,
        adp_state TEXT NOT NULL CHECK (adp_state IN (
            'RECEIVED', 'ADP_SEND_ARMED', 'ADP_SUCCEEDED', 'ADP_REJECTED', 'ADP_UNKNOWN', 'REJECTED_USER_BUSY'
        )),
        send_state TEXT NOT NULL CHECK (send_state IN (
            'NONE', 'PENDING', 'SEND_ARMED', 'SEND_ACCEPTED', 'SEND_SIMULATED', 'SEND_FAILED', 'SEND_UNKNOWN'
        )),
        request_id TEXT, adp_result_json TEXT, adp_error_json TEXT,
        notification TEXT, notification_kind TEXT, send_result_json TEXT,
        admission_decision TEXT NOT NULL CHECK (admission_decision IN ('ACCEPTED', 'USER_BUSY')),
        environment TEXT NOT NULL, redacted INTEGER NOT NULL DEFAULT 0 CHECK (redacted IN (0, 1)),
        record_version INTEGER NOT NULL DEFAULT 1 CHECK (record_version >= 1),
        received_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    "CREATE UNIQUE INDEX uq_message_key ON gateway_messages(message_key)",
    f"CREATE UNIQUE INDEX uq_active_conversation ON gateway_messages(conversation_id) WHERE {ACTIVE}",
    "CREATE INDEX idx_adp_dispatch ON gateway_messages(adp_state, id)",
    "CREATE INDEX idx_send_dispatch ON gateway_messages(send_state, id)",
    f"CREATE INDEX idx_send_user ON gateway_messages(send_user, id) WHERE {SEND_OPEN}",
    f"CREATE INDEX idx_unfinished ON gateway_messages(id) WHERE {UNFINISHED}",
    f"CREATE INDEX idx_retention_received ON gateway_messages(received_at, id) WHERE {RETENTION}",
    f"CREATE INDEX idx_retention_updated ON gateway_messages(updated_at, id) WHERE {RETENTION} AND redacted = 0",
)


def initialize_schema(connection, config):
    """绑定企业、应用和环境。只初始化空库，发现其他结构或版本即失败。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    scope = (config.corp_id, config.agent_id, config.adp_application, config.gateway_environment)
    objects = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
    if version == 0 and not objects:
        for statement in DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO gateway_owner VALUES (1, ?, ?, ?, ?)", scope)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    expected = {statement.partition(" ON ")[0].split()[-1] if " INDEX " in statement
                else statement.split()[2] for statement in DDL}
    if version != SCHEMA_VERSION or not expected <= objects:
        raise storage_error("unsupported_schema")
    owner = connection.execute(
        "SELECT corp_id, agent_id, adp_application, environment FROM gateway_owner WHERE id = 1"
    ).fetchone()
    if owner is None or tuple(owner) != scope:
        raise storage_error("scope_mismatch")
