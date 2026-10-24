"""Eventos: cierre automático, persistencia y reintento sin perder la entrega."""
import unittest
from datetime import datetime, timedelta, timezone
from delivery import validate_config
from event_source import EventSource


def make_event(name="15 Vicky Herrera", start="2026-10-17T12:00:00-03:00",
               end="2026-10-18T12:00:00-03:00", event_id="4"):
    return dict(id=event_id, name=name, start=start, end=end,
                album_slug="vicky-herrera")


class DummyDelivery:
    def __init__(self, config):
        self.config = config
        import threading
        self.lock = threading.RLock()
        self.saved = []
        self.reconciled = []
        self.message_writes = []
        self.gallery = None

    def load_config(self):
        return self.config

    def save_config(self, config):
        config = validate_config(config)
        self.config = config
        self.saved.append(dict(config))
        return config

    def reconcile(self, messages):
        self.reconciled.append(list(messages))
        return dict(verified=2, folder="archived")

    def on_messages(self, messages):
        self.message_writes.append(list(messages))


class EventClosureTests(unittest.TestCase):
    def setUp(self):
        self.event=make_event()
        self.previous=validate_config(dict(name=self.event["name"],
                start=self.event["start"],end=self.event["end"],enabled=True,
                source_id="4",album_slug=self.event["album_slug"]))
        self.delivery=DummyDelivery(self.previous)
        self.cleaned=[]
        self.now=datetime(2026,10,18,16,0,tzinfo=timezone.utc)

    def make_source(self, events=None, close_runtime=None):
        return EventSource(
            self.delivery, lambda config,prev: None,
            fetch=lambda: events if events is not None else [],
            close_runtime=close_runtime or (lambda config:self.cleaned.append(dict(config))))

    def test_closes_previous_event_without_needing_a_new_one(self):
        source=self.make_source()
        result=source.ensure([{"texto":"feliz cumple","ts":1}],now=self.now)
        self.assertIsNone(result)
        self.assertEqual(len(self.delivery.reconciled),1)
        self.assertEqual(len(self.cleaned),1)
        self.assertTrue(self.delivery.config['closed'])
        self.assertFalse(self.delivery.config['close_pending'])
        self.assertEqual([x['close_pending'] for x in self.delivery.saved],[True,False])
        source.ensure([],now=self.now+timedelta(hours=1))
        self.assertEqual(len(self.delivery.reconciled),1)
        self.assertEqual(len(self.cleaned),1)

    def test_does_not_cleanup_an_active_event(self):
        source=self.make_source([self.event])
        active=source.ensure([],now=datetime(2026,10,17,16,0,tzinfo=timezone.utc))
        self.assertEqual(active['source_id'],"4")
        self.assertEqual(self.delivery.reconciled,[])
        self.assertEqual(self.cleaned,[])

    def test_cleanup_failure_retries_without_rearchiving_emptied_greetings(self):
        count=[0]
        def flaky(config):
            count[0]+=1
            if count[0]==1:
                raise OSError('Drive temporarily unavailable')
            self.cleaned.append(dict(config))
        source=self.make_source(close_runtime=flaky)
        with self.assertRaises(OSError):
            source.ensure([{'texto':'mensaje original'}],now=self.now)
        self.assertTrue(self.delivery.config['close_pending'])
        self.assertFalse(self.delivery.config['closed'])
        self.assertEqual(len(self.delivery.reconciled),1)
        source.ensure([],now=self.now+timedelta(minutes=10))
        self.assertTrue(self.delivery.config['closed'])
        self.assertEqual(len(self.delivery.reconciled),1)
        self.assertEqual(count[0],2)

    def test_previous_event_closes_before_next_opens_without_replacing_greetings(self):
        following=make_event("15 Otra fiesta","2026-10-24T12:00:00-03:00",
                             "2026-10-25T12:00:00-03:00","5")
        source=self.make_source([following])
        result=source.ensure([{'texto':'saludo vieja fiesta'}],
                 now=datetime(2026,10,24,16,0,tzinfo=timezone.utc))
        self.assertEqual(result['source_id'],'5')
        self.assertFalse(result['closed'])
        self.assertEqual(len(self.delivery.reconciled),1)
        self.assertEqual(len(self.delivery.message_writes),0)
        self.assertEqual(len(self.cleaned),1)


if __name__ == '__main__':
    unittest.main()
