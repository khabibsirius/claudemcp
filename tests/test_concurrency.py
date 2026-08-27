"""How many people can use this at once.

The ceiling that mattered was not in this codebase at all: every endpoint is
a plain `def`, Starlette runs those in a worker thread, and anyio's default
pool is forty. A chat turn holds its thread for the whole turn, so the
forty-first concurrent chat did not queue - it blocked the whole server,
login page included.

That is exactly the kind of limit that comes back silently, so it is pinned
here rather than left as a comment.
"""

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
        """The number this exists to raise. If anyio's default ever changes,
        this says so rather than the comment quietly going stale."""
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
        """Sized for people mid-question at once, not for accounts."""
        assert WORKER_THREADS >= 64

    def test_raising_is_idempotent(self):
        """Called again - a second app instance in one process, as the tests
        do - it must not lower a ceiling somebody already raised."""
        async def twice():
            web_app.widen_the_threadpool()
            first = anyio.to_thread.current_default_thread_limiter().total_tokens
            web_app.widen_the_threadpool()
            return first, anyio.to_thread.current_default_thread_limiter().total_tokens

        first, second = anyio.run(twice)
        assert first == second == WORKER_THREADS

    def test_it_never_lowers_a_higher_ceiling(self, monkeypatch):
        """An operator who set it higher by hand keeps their setting."""
        monkeypatch.setattr(web_app, "WORKER_THREADS", 8)

        async def attempt():
            limiter = anyio.to_thread.current_default_thread_limiter()
            limiter.total_tokens = 256
            web_app.widen_the_threadpool()
            return limiter.total_tokens

        assert anyio.run(attempt) == 256


class TestTheModelConnectionPool:
    """A pool per user would mean hundreds of pools, each paying its own TLS
    handshake and none reusing a connection anybody else opened."""

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
        """Two first requests arriving together must not each install their
        own pool."""
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
        # And it comes back for the next caller rather than staying dead.
        assert llm.build_client() is not None

    def test_a_local_model_is_not_shared(self, monkeypatch):
        """Ollama's client is cheap and local; there is nothing to share, and
        pretending otherwise would hand every user one connection."""
        monkeypatch.setattr(llm, "LLM_PROVIDER", "ollama")

        class FakeOllama:
            def __init__(self, host=None):
                pass

        monkeypatch.setitem(__import__("sys").modules, "ollama",
                            type("m", (), {"Client": FakeOllama}))
        assert llm.build_client() is not llm.build_client()


class TestSessionsDoNotSerialiseOnEachOther:
    """The whole point of per-user engines: two people asking at once are two
    people asking at once, not a queue."""

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
        """Held locks are per session, so a long turn for one person leaves
        everybody else's requests free to run."""
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
        """Worth knowing at scale: on Enterprise, anyone left without a Qlik
        identity serialises against everybody else in the same position."""
        nobody = session.Session("u9", {"id": 9, "username": "u9"})
        also_nobody = session.Session("u8", {"id": 8, "username": "u8"})
        assert nobody.lock is also_nobody.lock
        assert nobody.lock is not self.make("u1").lock


class TestTheSessionRegistry:
    """Ids start high on purpose: the shared fixture seeds an administrator
    as user 1 and points its session at the system one, so a test using low
    ids would be counting that alias rather than its own sessions."""

    def test_the_cap_is_soft_not_a_wall(self, monkeypatch):
        """A server genuinely busy with more people than the cap keeps them
        all - only sessions idle past the timeout are closed, so nobody is
        evicted mid-question."""
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
