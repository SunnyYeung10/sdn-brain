import unittest
from unittest.mock import patch, MagicMock
from lab import traffic


class TrafficTests(unittest.TestCase):
    def test_congested_sender_still_exits_at_deadline(self):
        fake=MagicMock()
        fake.sendto.side_effect=BlockingIOError
        ticks=iter(i/5 for i in range(100))
        with patch('sys.argv',['traffic.py','send','--interface','10.0.0.1','--seconds','1']), \
             patch.object(traffic.socket,'socket',return_value=fake), \
             patch.object(traffic.time,'monotonic',side_effect=lambda:next(ticks)), \
             patch.object(traffic.time,'sleep'), patch('builtins.print'):
            traffic.main()
        fake.setblocking.assert_called_once_with(False)
        fake.close.assert_called_once()
