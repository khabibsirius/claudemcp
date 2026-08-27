import threading
import time

import anyio
import anyio.to_thread
import pytest

import llm
import session
import web_app
from config import WORKER_THREADS


class TestTheRequestThreadCeiling:

    def test_the_default_would_block_at_forty(self):
        async def measure():
            return anyio.to_thread.current_default_thread_limiter().total_tokens

        assert anyio.run(measure) == 40

    def test_startup_raises_it(self):
        async def measure():
            before, after = web_app.widen_the_threadpool()
            return before, after, anyio.to_thread.current_default_thread_limiter().total_tokens

        before, after, live = anyio.run(measure)
        assert before == 40
        assert after == WORKER_THREADS
        assert live == WORKER_THREADS

    def test_it_is_configured_well_above_the_default(self):
        assert WORKER_THREADS >= 64

    def test_raising_is_idempotent(self):
        async def twice():
            web_app.widen_the_threadpool()
            first = anyio.to_thread.current_default_thread_limiter().total_tokens
            web_app.widen_the_threadpool()
            return first, anyio.to_thread.current_default_thread_limiter().total_tokens

        first, second = anyio.run(twice)
        assert first == second == WORKER_THREADS

    def test_it_never_lowers_a_higher_ceiling(self, monkeypatch):
        monkeypatch.setattr(web_app, "WORKER_THREADS", 8)

        async def attempt():
            limiter = anyio.to_thread.current_default_thread_limiter()
            limiter.total_tokens = 256
            web_app.widen_the_threadpool()
            return limiter.total_tokens

        assert anyio.run(attempt) == 256


class TestTheModelConnectionPool:
    @pytest.fixture(autouse=True)
    def hosted(self, monkeypatch):
        monkeypatch.setattr(llm, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(llm, "OPENAI_BASE_URL", "https://api.example.com/v1")
        llm.close_shared()
        yield
        llm.close_shared()

    def test_everybody_shares_one_client(self):
        assert llm.build_client() is llm.build_client()

    def test_two_sessions_share_it(self):
        one, other = session.Session("u1"), session.Session("u2")
        assert one.client() is other.client()

    def test_the_pool_is_sized_for_concurrent_turns(self):
        from config import LLM_MAX_CONNECTIONS

        pool = llm.build_client()._http
        limits = pool._transport._pool._max_connections
        assert limits == LLM_MAX_CONNECTIONS
        assert LLM_MAX_CONNECTIONS >= WORKER_THREADS or LLM_MAX_CONNECTIONS >= 100

    def test_building_it_from_many_threads_makes_one(self):
        made = []
        barrier = threading.Barrier(8)

        def build():
            barrier.wait()
            made.append(llm.build_client())

        threads = [threading.Thread(target=build) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len({id(c) for c in made}) == 1

    def test_closing_it_is_safe_twice(self):
        llm.build_client()
        llm.close_shared()
        llm.close_shared()
        assert llm.build_client() is not None

    def test_a_local_model_is_not_shared(self, monkeypatch):
        monkeypatch.setattr(llm, "LLM_PROVIDER", "ollama")

        class FakeOllama:
            def __init__(self, host=None):
                pass

        monkeypatch.setitem(__import__("sys").modules, "ollama",
                            type("m", (), {"Client": FakeOllama}))
        assert llm.build_client() is not llm.build_client()


class TestSessionsDoNotSerialiseOnEachOther:
    @pytest.fixture(autouse=True)
    def enterprise(self, monkeypatch):
        monkeypatch.setattr(session, "per_user_engines", lambda: True)

    def make(self, key, directory="BANK", user_id=None):
        return session.Session(key, {
            "id": int(key[1:]), "username": key,
            "qlik_directory": directory, "qlik_user_id": user_id or key,
        })

    def test_impersonating_sessions_hold_different_locks(self):
        assert self.make("u1").lock is not self.make("u2").lock

    def test_one_turn_does_not_block_another(self):
        one, other = self.make("u1"), self.make("u2")
        ran = threading.Event()

        with one.lock:
            def second():
                with other.lock:
                    ran.set()

            thread = threading.Thread(target=second)
            thread.start()
            thread.join(timeout=2)

        assert ran.is_set(), "a second user waited on the first user's lock"

    def test_a_user_without_a_qlik_identity_falls_back_to_the_shared_lock(self):
        nobody = session.Session("u9", {"id": 9, "username": "u9"})
        also_nobody = session.Session("u8", {"id": 8, "username": "u8"})
        assert nobody.lock is also_nobody.lock
        assert nobody.lock is not self.make("u1").lock


class TestTheSessionRegistry:
    def test_the_cap_is_soft_not_a_wall(self, monkeypatch):
        monkeypatch.setattr(session, "MAX_USER_SESSIONS", 3)
        monkeypatch.setattr(session, "USER_SESSION_IDLE_MINUTES", 60)

        for i in range(101, 109):
            session.for_user({"id": i, "username": f"u{i}"})

        live = [s for s in session.sessions()
                if s.key != session.SYSTEM_KEY and s.key.startswith("u1")]
        assert len(live) == 8

    def test_idle_sessions_are_closed_once_over_the_cap(self, monkeypatch):
        monkeypatch.setattr(session, "MAX_USER_SESSIONS", 2)
        monkeypatch.setattr(session, "USER_SESSION_IDLE_MINUTES", 0)

        for i in range(201, 206):
            sess = session.for_user({"id": i, "username": f"u{i}"})
            sess.touched = time.monotonic() - 3600

        session.for_user({"id": 299, "username": "u299"})
        live = [s for s in session.sessions()
                if s.key != session.SYSTEM_KEY and s.key.startswith("u2")]
        assert len(live) <= 3

    def test_the_configured_cap_suits_hundreds_of_people(self):
        from config import MAX_USER_SESSIONS

        assert MAX_USER_SESSIONS >= 100
