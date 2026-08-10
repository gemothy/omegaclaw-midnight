import pytest

from rpc import *

TEST_ADDRESS = (LOCALHOST, 9767)

class TestClass:

    def setup_class(cls):
        def thread_id_filter(record):
            record.thread_id = threading.get_native_id()
            return record

        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(levelname)s] [%(thread_id)d]: %(message)s'))
        handler.addFilter(thread_id_filter)
        logging.getLogger().handlers.clear()
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.DEBUG)

    @pytest.fixture
    def server(self):
        server = Rpc(IPCServer(TEST_ADDRESS))
        server.start()
        yield server
        server.stop(5)

    @pytest.fixture
    def client(self):
        client = Rpc(IPCClient(TEST_ADDRESS))
        client.start()
        yield client
        client.stop(5)

    def test_request_from_server_to_client(self, server, client):
        def foo(param):
            assert param == { 'arg': 'abcd' }
            return { 'result': 'dcba' }
        client.on_request('foo', foo)
        response = server.request('foo', { 'arg': 'abcd' })
        assert response.get(5) == { 'result': 'dcba' }


    def test_request_from_client_to_server(self, server, client):
        def foo(param):
            assert param == { 'arg': 'abcd' }
            return { 'result': 'dcba' }
        server.on_request('foo', foo)
        response = client.request('foo', { 'arg': 'abcd' })
        assert response.get(5) == { 'result': 'dcba' }

    def test_request_client_reconnect(self, server):
        def reverse(param):
            assert param.get('arg')
            return { 'result': param['arg'][::-1] }

        client = Rpc(IPCClient(TEST_ADDRESS))
        client.on_request('reverse', reverse)
        client.start()
        response = server.request('reverse', { 'arg': 'abcd' })
        assert response.get(5) == { 'result': 'dcba' }
        client.stop(5)

        client = Rpc(IPCClient(TEST_ADDRESS))
        client.on_request('reverse', reverse)
        client.start()
        response = server.request('reverse', { 'arg': 'cdef' })
        assert response.get(5) == { 'result': 'fedc' }
        client.stop(5)

    def test_request_server_reconnect(self, client):
        def reverse(param):
            assert param.get('arg')
            return { 'result': param['arg'][::-1] }

        server = Rpc(IPCServer(TEST_ADDRESS))
        server.on_request('reverse', reverse)
        server.start()
        response = client.request('reverse', { 'arg': 'abcd' })
        assert response.get(5) == { 'result': 'dcba' }
        server.stop(5)

        server = Rpc(IPCServer(TEST_ADDRESS))
        server.on_request('reverse', reverse)
        server.start()
        response = client.request('reverse', { 'arg': 'cdef' })
        assert response.get(5) == { 'result': 'fedc' }
        server.stop(5)
