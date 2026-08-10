# Support both layouts: imported as a package (Autotests.mock.llm in
# the container's loop.metta), and as a plain directory (host-side
# pytest collecting mock/ without __init__.py).
try:
    from .rpc import Rpc, IPCClient, IPCServer
except ImportError:
    from rpc import Rpc, IPCClient, IPCServer
from contextlib import contextmanager
import queue

COMM_MOCK_PORT = 9766

class CommMockClient:

    def __init__(self, address):
        self._queue = queue.Queue()
        self._rpc = Rpc(IPCClient(address))
        self._rpc.on_request('message', lambda args: self.on_message(args))
        self._rpc.on_request('ping', lambda args: self.on_ping(args))
        self._rpc.start()

    def stop(self, timeout=None):
        self._rpc.stop(timeout)

    def send_message(self, text, timeout=10):
        result = self._rpc.request('message', { 'text': text })
        if result.get(timeout) != True:
            print(f'[CommMockClient] Cannot set answer to the mock, error: {result.error()}')
            return False
        return True

    def on_message(self, args):
        text = args['text']
        print(f'[CommMockClient] Message received: "{text}"')
        try:
            self._queue.put(text, block=False)
        except Exception as e:
            print(f'[CommMockClient] Could not add received message into the queue: {e}')
            return False
        return True

    def getLastMessage(self):
        try:
            return self._queue.get(block=False)
        except queue.Empty as e:
            return ""

    def on_ping(self, args):
        print(f'[CommMockClient] Mock ping request processed')
        return True

class CommMockServer:

    def __init__(self, address):
        self._queue = queue.Queue()
        self._received = []
        self._rpc = Rpc(IPCServer(address))
        self._rpc.on_request('message', lambda args: self.on_message(args))
        self._rpc.start()

    def stop(self, timeout=None):
        self._rpc.stop(timeout)

    def send_message(self, text, timeout=10):
        result = self._rpc.request('message', { 'text': text })
        if result.get(timeout) != True:
            print(f'[CommMockServer] Cannot set answer to the mock, error: {result.error()}')
            return False
        return True

    def on_message(self, args):
        text = args['text']
        print(f'[CommMockServer] Message received: "{text}"')
        try:
            self._queue.put(text, block=False)
        except Exception as e:
            print(f'[CommMockServer] Could not add received message into the queue: {e}')
            return False
        return True

    def getLastMessage(self):
        try:
            return self._queue.get(block=False)
        except queue.Empty as e:
            return ""

    def ping(self, timeout=None):
        print(f'[CommMockServer] Ping agent')
        result = self._rpc.request('ping', {})
        if result.get(timeout) != True:
            print(f'[CommMockServer] Did not get answer on ping in {timeout} seconds')
            return False
        else:
            return True

@contextmanager
def comm_mock_server(*args, **kwargs) -> CommMockServer:
    timeout = kwargs.pop("timeout", 30)
    server = CommMockServer(*args, **kwargs)
    try:
        if not server.ping(timeout):
            raise RuntimeError(f"Client didn't answered in {timeout} seconds")
        yield server
    finally:
        server.stop(5)
