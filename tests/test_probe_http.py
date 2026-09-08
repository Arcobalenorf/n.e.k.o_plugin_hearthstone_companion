from http.client import IncompleteRead
from urllib.error import HTTPError

import neko_answer_probe as probe
import pytest


@pytest.mark.parametrize("error_response", [False, True])
def test_partial_http_read_becomes_a_recoverable_probe_error(monkeypatch, error_response):
    class BrokenResponse:
        status = 200
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            raise IncompleteRead(b"private-response-fragment")

        def close(self):
            pass

    def open_response(*args, **kwargs):
        if error_response:
            raise HTTPError("http://127.0.0.1/health", 503, "unavailable", {}, BrokenResponse())
        return BrokenResponse()

    monkeypatch.setattr(probe, "urlopen", open_response)
    with pytest.raises(probe.ProbeSkip, match="^neko_unavailable$"):
        probe._http_json("GET", "http://127.0.0.1/health")
