import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch, Mock

from graph_engineering.dashboard import make_server, serve_centre, DashboardServer
from graph_engineering.tickets import Tickets
from tests.test_core import make_repo


class ControlAPITests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name);self.home=self.root/'controller'
        self.server=make_server(self.home,0,control=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.csrf=json.load(urlopen(self.url+'/api/session'))['csrf']

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.folder.cleanup()

    def post(self,path,data,key='test-command-123'):
        return json.load(urlopen(Request(self.url+path,data=json.dumps(data).encode(),headers={
            'Origin':self.url,'X-GE-CSRF':self.csrf,'Idempotency-Key':key,'Content-Type':'application/json'})))

    def test_commands_require_origin_csrf_and_default_repository_to_read_only(self):
        repo=make_repo(self.root)
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(self.url+'/api/centre/register',data=json.dumps({'path':str(repo)}).encode()))
        self.assertEqual(caught.exception.code,403)
        registered=self.post('/api/centre/register',{'path':str(repo)})
        self.assertFalse(registered['trusted'])
        with self.assertRaises(HTTPError):
            self.post('/api/centre/start',{'repository_id':registered['id'],'mode':'assisted'})
        data={'repository_id':registered['id'],'mode':'read_only'}
        start=self.post('/api/centre/start',data,'start-command-123')
        self.assertEqual(start,self.post('/api/centre/start',data,'start-command-123'))
        snapshot=json.load(urlopen(self.url+'/api/centre'))
        self.assertEqual(len(snapshot['tickets']),1)
        ticket=snapshot['tickets'][0]
        self.assertEqual(ticket['id'],start['ticket_id'])
        self.assertEqual(ticket['mode'],'metadata')
        self.assertEqual(ticket['board_status'],'To Do')
        self.assertNotIn(self.csrf,json.dumps(snapshot))

    def test_stream_has_persistent_event_ids_and_resumes_after_cursor(self):
        tickets=Tickets(self.home)
        try:
            tickets.store.create_run({'repo':'test','models':{},'backend':'local'},'run1')
            ticket=tickets.admit('run1')
            cursor=tickets.db.execute('SELECT MAX(seq) FROM events').fetchone()[0]
            tickets.store.event('run1','repository_discovery','started',{'ticket_id':ticket['id']})
        finally:tickets.store.close()
        with urlopen(self.url+f'/api/centre/events?after={cursor}',timeout=3) as response:
            lines=[response.readline().decode().strip() for _ in range(4)]
        self.assertEqual(lines[0],f'id: {cursor+1}')
        self.assertEqual(lines[1],'event: execution')
        event=json.loads(lines[2].removeprefix('data: '))
        self.assertEqual(event['ticket_id'],ticket['id'])
        self.assertEqual(event['source_node'],'repository_discovery')
        self.assertEqual(event['event_type'],'started')

    def test_attached_ui_does_not_launch_or_own_another_controller(self):
        with patch.object(DashboardServer,'serve_forever',return_value=None), \
             patch('graph_engineering.dashboard.subprocess.Popen') as launch, \
             patch('builtins.print'):
            serve_centre(self.home,0,attach_controller=True)
        launch.assert_not_called()

    def test_existing_listener_cannot_be_silently_shadowed(self):
        with self.assertRaises(OSError):
            make_server(self.home,self.server.server_port,control=True)

    def test_client_disconnect_does_not_attempt_a_second_error_response(self):
        handler=object.__new__(self.server.RequestHandlerClass)
        for phase in ('headers','body'):
            with self.subTest(phase=phase),patch.object(handler,'send_response'), \
                 patch.object(handler,'send_header'),patch.object(handler,'end_headers') as headers:
                handler.wfile=Mock()
                if phase=='headers':headers.side_effect=ConnectionAbortedError('client reloaded')
                else:handler.wfile.write.side_effect=ConnectionAbortedError('client reloaded')
                handler._json(200,{'state':'persisted'})
