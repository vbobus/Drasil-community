"""Complete review events must survive PostgreSQL's former 32k limit."""
import json
import unittest
from unittest.mock import Mock

from sqlalchemy import Text
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.database import _migrate_postgres_agent_event_payload
from app.models.strategist_agent import StrategistAgentEvent
from app.models.strategist_chat import StrategistChatSession


class AgentEventStorageTests(unittest.TestCase):
    def test_postgres_event_column_is_unbounded_text(self):
        column = StrategistAgentEvent.__table__.c.payload_json
        self.assertIsInstance(column.type, Text)
        self.assertEqual(column.type.compile(dialect=postgresql.dialect()), 'TEXT')
        self.assertFalse(column.nullable)

    def test_large_continuation_event_round_trips_without_truncation(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        payload = json.dumps({'continuation_state': {'review': 'x' * 64000}, 'tail': 'complete'})
        with Session(engine) as session:
            chat = StrategistChatSession()
            session.add(chat)
            session.commit()
            session.add(StrategistAgentEvent(session_id=chat.id, event_type='final_answer', payload_json=payload))
            session.commit()
        with Session(engine) as session:
            saved = session.exec(select(StrategistAgentEvent)).one()
            self.assertEqual(saved.payload_json, payload)
            self.assertEqual(json.loads(saved.payload_json)['tail'], 'complete')

    def test_legacy_postgres_column_is_widened_and_current_text_is_skipped(self):
        for data_type, calls in [('character varying', 2), ('text', 1)]:
            with self.subTest(data_type=data_type):
                connection = Mock()
                connection.execute.return_value.scalar_one.return_value = data_type
                _migrate_postgres_agent_event_payload(connection)
                self.assertEqual(connection.execute.call_count, calls)
                if calls == 2:
                    self.assertEqual(str(connection.execute.call_args.args[0]),
                        'ALTER TABLE strategist_agent_events ALTER COLUMN payload_json TYPE TEXT')
