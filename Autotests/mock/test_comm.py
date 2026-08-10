import pytest

from comm import *
from rpc import LOCALHOST

TEST_ADDRESS = (LOCALHOST, 9767)

class TestCommMock:

    def setup_class(cls):
        import logging
        import threading

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
    def client(self):
        client = CommMockClient(TEST_ADDRESS)
        yield client
        client.stop(5)

    @pytest.fixture
    def server(self):
        server = CommMockServer(TEST_ADDRESS)
        yield server
        server.stop(5)

    def test_response(self, client, server):
        assert server.send_message("hello")
        assert client.getLastMessage() == "hello"
        assert client.send_message("world")
        assert server.getLastMessage() == "world"

    def test_test_restart(self, client):
        server = CommMockServer(TEST_ADDRESS)
        assert server.send_message("hello world")
        assert client.getLastMessage() == "hello world"
        server.stop(5)
        server = CommMockServer(TEST_ADDRESS)
        assert server.send_message("hello earth")
        assert client.getLastMessage() == "hello earth"
        server.stop(5)

    def test_two_messages(self, client, server):
        assert server.send_message("hello")
        assert server.send_message("world")
        assert client.getLastMessage() == "hello"
        assert client.getLastMessage() == "world"

    def test_context_manager(self, client):
        with comm_mock_server(address=TEST_ADDRESS) as server:
            assert server.send_message("hello world")
            assert client.getLastMessage() == "hello world"

    def test_context_manager_timeout(self, client):
        address = (TEST_ADDRESS[0], TEST_ADDRESS[1] + 1)
        try:
            with comm_mock_server(address=address, timeout=2) as server:
                assert False
        except RuntimeError as e:
            assert e.args == ("Client didn't answered in 2 seconds",)
