import argparse
import os
import random
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="qlikai-sim-"))

os.environ.update({
    "QLIK_MODE": "enterprise",
    "QLIK_HOST": "qlik.sim.internal",
    "QLIK_CERT_DIR": str(WORK / "certs"),
    "QLIK_USER_DIRECTORY": "SIM",
    "QLIK_USER_ID": "svc_sim",
    "APP_NAME": "Deposits",
    "AUTH_ENABLED": "true",
    "COOKIE_SECURE": "false",
    "USERS_DB": str(WORK / "users.db"),
    "HISTORY_DIR": str(WORK / "history"),
    "LDAP_ENABLED": "true",
    "LDAP_SERVER": "dc01.sim.internal",
    "LDAP_PORT": "636",
    "LDAP_USE_SSL": "true",
    "LDAP_BASE_DN": "DC=sim,DC=internal",
    "LDAP_UPN_SUFFIX": "sim.internal",
    "LDAP_WINDOWS_DOMAIN": "SIM",
    "LDAP_QLIK_DIRECTORY": "SIM",
    "LOG_FILE": "",
    "LOG_LEVEL": "ERROR",
})

sys.path.insert(0, str(Path(__file__).parent))

import httpx
import ldap3
import uvicorn

import config
import directory
import history
import llm
import session
import users
import web_app
from qlik_engine import QlikConnectionError

for noisy in ("httpx", "httpcore", "uvicorn", "uvicorn.error", "users",
              "web_app", "session", "mcp.server.streamable_http_manager"):
    __import__("logging").getLogger(noisy).setLevel(__import__("logging").ERROR)

BASE_DN = "DC=sim,DC=internal"
PASSWORD = "Windows-Password-1"
GREEN, RED, GREY, BOLD, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"


class Qlik:
    reachable = True
    opened = []
    lock = threading.Lock()

    def __init__(self, user_directory=None, user_id=None, **kwargs):
        if not Qlik.reachable:
            raise QlikConnectionError(
                "Could not connect to the Qlik Engine at wss://qlik.sim.internal:4747/app/")
        self.identity = (user_directory, user_id)
        self.connected = True
        self.app_name = None
        self.mode = "enterprise"
        with Qlik.lock:
            Qlik.opened.append(self.identity)

    def _work(self):
        if not self.connected:
            raise QlikConnectionError("The engine closed the connection")
        time.sleep(random.uniform(0.002, 0.012))

    def open_app(self, name):
        self._work()
        self.app_name = name
        return 1

    def close(self):
        self.connected = False

    def die(self):
        self.connected = False

    def get_script(self):
        self._work()
        return "///$tab Main\r\nLOAD * INLINE [a,b\n1,2];\r\n"

    def set_script(self, script, validate=True):
        self._work()
        return script

    def list_connections(self):
        self._work()
        return [{"id": "1", "name": "deposits", "path": "\\\\nas\\data", "type": "folder"}]

    def list_sheets(self):
        self._work()
        return [{"qId": "SH1", "title": "Overview", "description": "", "chart_count": 4}]

    def list_apps(self):
        self._work()
        return ["Deposits", "Risk"]

    def get_fields(self):
        self._work()
        return [{"name": "Amount"}, {"name": "Branch"}]

    def get_tables(self):
        self._work()
        return [{"name": "Deposits", "rows": 1269, "fields": []}]

    def app_file_info(self):
        return {"path": "Deposits.qvf", "size_bytes": 1, "modified": "now"}

    def save(self):
        self._work()
        return True


class Model:
    calls = 0
    lock = threading.Lock()

    def chat(self, model=None, messages=None, tools=None, options=None, stream=False):
        with Model.lock:
            Model.calls += 1
        asked = next((m["content"] for m in reversed(messages or [])
                      if m.get("role") == "user"), "")
        reply = f"There are 1,269 deposits. You asked: {asked[:40]}"

        if not stream:
            time.sleep(random.uniform(0.05, 0.2))
            return {"message": {"role": "assistant", "content": reply,
                                "tool_calls": []}, "done": True}

        def word_by_word():
            for word in reply.split():
                time.sleep(random.uniform(0.004, 0.02))
                yield {"message": {"role": "assistant", "content": word + " ",
                                   "tool_calls": []}, "done": False}
            yield {"message": {"role": "assistant", "content": "",
                               "tool_calls": []}, "done": True}

        return word_by_word()

    def list(self):
        return {"models": [{"name": "sim-model", "size": 1}]}

    def show(self, model):
        return {"capabilities": ["tools"]}

    def close(self):
        pass


def staff(count):
    people = {}
    for n in range(count):
        name = f"emp{n:04d}"
        people[f"CN={name},OU=Staff,{BASE_DN}"] = {
            "sAMAccountName": name,
            "displayName": f"Employee {n:04d}",
            "mail": f"{name}@sim.internal",
            "userPrincipalName": f"{name}@sim.internal",
            "userPassword": PASSWORD,
            "objectClass": "person",
        }
    return people


def install_fake_domain(people):
    names = {}
    for dn, entry in people.items():
        names[f"{entry['sAMAccountName']}@sim.internal"] = dn
        names[f"SIM\\{entry['sAMAccountName']}"] = dn

    def connect(_ldap3, user, password, server=None):
        time.sleep(random.uniform(0.004, 0.02))
        connection = ldap3.Connection(
            ldap3.Server("fake-dc", get_info=ldap3.OFFLINE_AD_2012_R2),
            user=names.get(user, user), password=password,
            client_strategy=ldap3.MOCK_SYNC, raise_exceptions=False,
        )
        for dn, attributes in people.items():
            connection.strategy.add_entry(dn, dict(attributes))
        return connection, connection.bind()

    directory._connect = connect
    return names


class Employee:
    def __init__(self, name, port):
        self.name = name
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
        self.failures = []

    def sign_in(self, password=PASSWORD):
        started = time.perf_counter()
        response = self.http.post("/api/login",
                                  json={"username": self.name, "password": password})
        return response, time.perf_counter() - started

    def get(self, path):
        return self.http.get(path)

    def ask(self, question):
        return self.http.post("/api/chat", json={"message": question})

    def note(self, what, response):
        if response.status_code != 200:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))[:80]
            except Exception:
                pass
            self.failures.append(f"{what} -> {response.status_code} {detail}")
        return response

    def close(self):
        self.http.close()


def at_once(work, count, gap=0.0):
    ready = threading.Barrier(count)
    results = [None] * count
    errors = []

    def run(index):
        ready.wait()
        if gap:
            time.sleep(random.uniform(0, gap))
        try:
            results[index] = work(index)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    return results, errors, time.perf_counter() - started


class Report:
    def __init__(self):
        self.checks = []

    def check(self, passed, statement, detail=""):
        self.checks.append((bool(passed), statement, detail))
        mark = f"{GREEN}pass{OFF}" if passed else f"{RED}FAIL{OFF}"
        print(f"     [{mark}] {statement}")
        if detail:
            print(f"            {GREY}{detail}{OFF}")
        return passed

    @property
    def failed(self):
        return [c for c in self.checks if not c[0]]


def heading(text):
    print(f"\n{BOLD}{text}{OFF}")
    print("  " + "-" * (len(text) + 2))


def serve(port):
    server = uvicorn.Server(uvicorn.Config(
        web_app.app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("the server did not start")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Put a crowd through the real server and see what breaks.")
    parser.add_argument("--users", type=int, default=40,
                        help="how many employees (default: 40)")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--questions", type=int, default=2,
                        help="questions each person asks (default: 2)")
    args = parser.parse_args(argv)

    print(f"{BOLD}Simulating {args.users} employees against the real server{OFF}")
    print(f"{GREY}  Qlik, Active Directory and the model are stood in for - "
          f"everything above them is the shipping code.{OFF}")
    print(f"{GREY}  working directory: {WORK}{OFF}")

    people = staff(args.users)
    install_fake_domain(people)
    session.QlikEngine = Qlik
    llm.build_client = lambda: Model()
    session.system().state["model"] = "sim-model"

    admin_password = users.bootstrap()
    server = serve(args.port)
    report = Report()
    names = [entry["sAMAccountName"] for entry in people.values()]

    try:
        run_scenarios(args, names, report, admin_password)
    finally:
        server.should_exit = True
        time.sleep(0.5)

    heading("Verdict")
    passed = len(report.checks) - len(report.failed)
    print(f"     {passed}/{len(report.checks)} checks passed")
    for _, statement, detail in report.failed:
        print(f"     {RED}FAILED{OFF} {statement}  {GREY}{detail}{OFF}")
    if not report.failed:
        print(f"\n     {GREEN}Everything held.{OFF}")
    print(f"\n{GREY}  Not proven here: a real Qlik Engine, a real domain "
          f"controller, a real model endpoint.\n"
          f"  Run check_connection.py against those before go-live.{OFF}")
    return 1 if report.failed else 0


def run_scenarios(args, names, report, admin_password):
    port = args.port
    staff_clients = [Employee(name, port) for name in names]

    admin = Employee(config.ADMIN_USERNAME, port)
    if admin.sign_in(password=admin_password)[0].status_code != 200:
        admin = None

    heading("1. Day one: nobody has an account yet")

    before = users.listing()
    outcomes, errors, elapsed = at_once(
        lambda i: staff_clients[i].sign_in(), len(staff_clients))

    good = [o for o in outcomes if o and o[0].status_code == 200]
    times = [o[1] for o in outcomes if o]
    report.check(not errors, "no thread raised", "; ".join(errors[:3]))
    report.check(len(good) == len(staff_clients),
                 f"all {len(staff_clients)} signed in with their Windows password",
                 f"{len(good)} succeeded in {elapsed:.1f}s")
    if times:
        print(f"            {GREY}sign-in: median {statistics.median(times) * 1000:.0f}ms, "
              f"slowest {max(times) * 1000:.0f}ms{OFF}")

    made = users.listing()
    report.check(len(made) - len(before) == len(staff_clients),
                 "an account was created for each of them, with nobody doing it",
                 f"{len(before)} accounts before, {len(made)} after")

    from_ad = [u for u in made if u.get("username", "").startswith("emp")]
    identities = {(u["qlik_directory"], u["qlik_user_id"]) for u in from_ad}
    report.check(all(d == "SIM" for d, _ in identities),
                 "each account carries the Qlik identity taken from the directory",
                 f"example: {sorted(identities)[0] if identities else 'none'}")
    report.check(len(identities) == len(from_ad),
                 "no two people were given the same Qlik identity")

    heading("2. Everyone works at the same time")

    def work(index):
        person = staff_clients[index]
        person.note("state", person.get("/api/state"))
        for n in range(args.questions):
            person.note("chat", person.ask(f"{person.name} question {n}"))
        person.note("chats", person.get("/api/chats"))
        return person

    _, errors, elapsed = at_once(work, len(staff_clients), gap=0.05)
    broken = [f"{p.name}: {p.failures[0]}" for p in staff_clients if p.failures]

    turns = len(staff_clients) * args.questions
    report.check(not errors, "no thread raised", "; ".join(errors[:3]))
    report.check(not broken,
                 f"{turns} chat turns and {len(staff_clients) * 2} page loads all answered",
                 f"{len(broken)} people hit an error" if broken else
                 f"{elapsed:.1f}s wall clock, {turns / elapsed:.1f} turns/second")
    for line in broken[:4]:
        print(f"            {RED}{line}{OFF}")

    heading("3. Did anybody see anybody else's work?")

    leaks = []
    for person in staff_clients:
        listing = person.get("/api/chats")
        if listing.status_code != 200:
            leaks.append(f"{person.name}: could not read their own list")
            continue
        titles = " ".join(c.get("title", "") for c in listing.json().get("chats", []))
        others = [n for n in names if n != person.name and n in titles]
        if others:
            leaks.append(f"{person.name} can see {others[0]}'s conversation")

    report.check(not leaks,
                 "every person's sidebar holds only their own conversations",
                 leaks[0] if leaks else f"{len(staff_clients)} sidebars checked")

    owners = history.owners()
    report.check(len(owners) >= len(staff_clients),
                 "each person's conversations are filed under their own owner",
                 f"{len(owners)} owner directories on disk")

    sockets = [i for i in Qlik.opened if i[1] and i[1].startswith("emp")]
    report.check(len(set(sockets)) == len(staff_clients),
                 "each person got their own Qlik connection, opened as them",
                 f"{len(set(sockets))} distinct identities across {len(sockets)} sockets")

    heading("4. Qlik goes down in the middle of the day")

    Qlik.reachable = False
    for sess in session.sessions():
        engine = sess.state.get("engine")
        if engine is not None:
            engine.die()
    session._shared["engine"] = None

    sample = staff_clients[:min(10, len(staff_clients))]
    results, _, _ = at_once(lambda i: (
        sample[i].get("/api/state"), sample[i].get("/api/me"),
        sample[i].get("/api/chats"), sample[i].get("/api/health"),
    ), len(sample))

    qlik_calls = [r[0] for r in results if r]
    report.check(all(r.status_code == 503 for r in qlik_calls),
                 "requests that need Qlik say so, rather than hanging or crashing",
                 f"{sum(1 for r in qlik_calls if r.status_code == 503)}/{len(qlik_calls)} got 503")
    report.check(all(r.headers.get("X-Qlik-Reconnect") == "1" for r in qlik_calls),
                 "each one is marked as something the Reconnect button can fix")
    report.check(all(r[1].status_code == 200 and r[2].status_code == 200
                     and r[3].status_code == 200 for r in results if r),
                 "signing in, saved conversations and the health view keep working")

    attempts_before = len(Qlik.opened)
    for _ in range(5):
        sample[0].get("/api/state")
    report.check(len(Qlik.opened) == attempts_before,
                 "a downed Qlik is not hammered with a connection attempt per request",
                 f"{len(Qlik.opened) - attempts_before} new attempts across 5 requests")

    heading("5. Qlik comes back")

    Qlik.reachable = True

    pressed = sample[0].http.post("/api/reconnect")
    report.check(pressed.status_code == 200,
                 "pressing Reconnect works the moment Qlik is back",
                 "the cooldown does not apply to a person who can see it is up")

    waiting = sample[1].get("/api/state")
    report.check(waiting.status_code == 503,
                 "somebody who presses nothing is still held off briefly",
                 f"the {config.RECONNECT_COOLDOWN_SECONDS:g}s cooldown is what stops a "
                 f"downed Qlik being stormed - it also delays the recovery")

    report.check(admin is not None, "the administrator can sign in")
    if admin is not None:
        healed = admin.http.post("/api/admin/reconnect-all")
        body = healed.json() if healed.status_code == 200 else {}
        report.check(healed.status_code == 200,
                     "an administrator puts everyone back on in one press",
                     f"{len(body.get('reconnected', []))} sessions reconnected, "
                     f"{len(body.get('failed', []))} failed")

    results, _, _ = at_once(lambda i: sample[i].get("/api/state"), len(sample))
    recovered = [r for r in results if r is not None and r.status_code == 200]
    report.check(len(recovered) == len(sample),
                 "everyone is working again",
                 f"{len(recovered)}/{len(sample)}")

    still_in = [p for p in sample if p.get("/api/me").status_code == 200]
    report.check(len(still_in) == len(sample),
                 "nobody was signed out by the outage",
                 f"{len(still_in)}/{len(sample)} still hold the session they logged in with")

    heading("6. Somebody types their password wrong five times")

    victim = Employee(names[0], port)
    for _ in range(config.LOGIN_MAX_ATTEMPTS):
        victim.sign_in(password="wrong-password")
    locked = victim.sign_in(password=PASSWORD)[0]
    report.check(locked.status_code == 429,
                 "that account locks after the configured number of attempts",
                 f"{config.LOGIN_MAX_ATTEMPTS} wrong tries -> {locked.status_code}")

    neighbour = Employee(names[1], port)
    fine = neighbour.sign_in()[0]
    report.check(fine.status_code == 200,
                 "everybody else is unaffected")

    record = users.by_username(names[0])
    cleared = (admin.http.post(f"/api/admin/users/{record['id']}/unlock")
               if admin is not None and record else None)
    report.check(cleared is not None and cleared.status_code == 200,
                 "an administrator can clear the lockout without a restart")
    back = Employee(names[0], port).sign_in()[0]
    report.check(back.status_code == 200,
                 "and they are straight back in",
                 f"{names[0]} -> {back.status_code}")
    victim.close()
    neighbour.close()

    heading("7. A new starter arrives while everyone is working")

    newcomer_name = "newstarter"
    people = staff(0)
    entry = {
        "sAMAccountName": newcomer_name, "displayName": "New Starter",
        "mail": f"{newcomer_name}@sim.internal",
        "userPrincipalName": f"{newcomer_name}@sim.internal",
        "userPassword": PASSWORD, "objectClass": "person",
    }
    joined = staff(args.users)
    joined[f"CN={newcomer_name},OU=Staff,{BASE_DN}"] = entry
    install_fake_domain(joined)

    busy = staff_clients[:min(20, len(staff_clients))]
    newcomer = Employee(newcomer_name, port)
    outcome = {}

    def everyone(index):
        if index == 0:
            response, took = newcomer.sign_in()
            outcome["response"] = response
            outcome["took"] = took
            outcome["state"] = newcomer.get("/api/state")
        else:
            busy[index % len(busy)].get("/api/state")

    at_once(everyone, len(busy))

    report.check(outcome.get("response") is not None
                 and outcome["response"].status_code == 200,
                 "a person who has never used it signs in while the server is busy",
                 f"took {outcome.get('took', 0) * 1000:.0f}ms")
    report.check(outcome.get("state") is not None
                 and outcome["state"].status_code == 200,
                 "and lands on a working page with an app open",
                 (outcome["state"].json().get("app") if outcome.get("state") is not None
                  and outcome["state"].status_code == 200 else ""))

    record = users.by_username(newcomer_name)
    report.check(record is not None and record["qlik_user_id"] == newcomer_name,
                 "their account and Qlik identity were created for them",
                 f"{record['qlik_directory']}\\{record['qlik_user_id']}" if record else "no account")
    newcomer.close()

    heading("8. More people at once than there are worker threads")

    crowd = min(config.WORKER_THREADS + 40, 200)
    browsers = [Employee(names[i % len(names)], port) for i in range(crowd)]
    signed, _, _ = at_once(lambda i: browsers[i].sign_in(), crowd)
    in_hand = [s for s in signed if s and s[0].status_code == 200]
    report.check(len(in_hand) == crowd,
                 f"{crowd} browsers signed in at once",
                 f"{len(in_hand)}/{crowd} - one client and one cookie jar each, "
                 f"so none of them share a session")

    results, errors, elapsed = at_once(
        lambda i: browsers[i].get("/api/state"), crowd)
    answered = [r for r in results if r is not None and r.status_code == 200]
    report.check(not errors, "no thread raised", "; ".join(errors[:3]))
    report.check(len(answered) == crowd,
                 f"{crowd} simultaneous requests against {config.WORKER_THREADS} "
                 f"worker threads all answered",
                 f"{len(answered)}/{crowd} in {elapsed:.1f}s - they queue, they do not fail")

    live = len(session.sessions())
    report.check(live <= config.MAX_USER_SESSIONS + 1,
                 f"the session registry stays under its cap of "
                 f"{config.MAX_USER_SESSIONS}",
                 f"{live} sessions held")

    for person in browsers:
        person.close()

    heading("9. Streaming answers while the server is busy")

    talkers = staff_clients[:min(12, len(staff_clients))]
    readers = staff_clients[12:] or staff_clients

    def stream(index):
        person = talkers[index]
        with person.http.stream("POST", "/api/chat/stream",
                                json={"message": f"streamed from {person.name}"}) as r:
            return r.status_code, "".join(r.iter_text())

    def load(index):
        for _ in range(4):
            readers[index % len(readers)].get("/api/state")

    answers = [None] * len(talkers)
    ready = threading.Barrier(len(talkers) + len(readers))

    def both(index):
        ready.wait()
        if index < len(talkers):
            try:
                answers[index] = stream(index)
            except Exception as e:
                answers[index] = (0, f"{type(e).__name__}: {e}")
        else:
            load(index - len(talkers))

    threads = [threading.Thread(target=both, args=(i,))
               for i in range(len(talkers) + len(readers))]
    began = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    finished = [a for a in answers if a and a[0] == 200
                and ("\"type\": \"final\"" in a[1] or "\"type\":\"final\"" in a[1])]
    wedged = [a for a in answers if a and "un-acquired lock" in a[1]]
    report.check(not wedged,
                 "no streamed answer left a lock behind",
                 "a lock held across a yield is released by whichever pool "
                 "thread happens to resume the generator")
    report.check(len(finished) == len(talkers),
                 f"all {len(talkers)} streamed answers completed while "
                 f"{len(readers)} others hammered the page",
                 f"{len(finished)}/{len(talkers)} in {time.perf_counter() - began:.1f}s")

    leftover = [t.name for t in threading.enumerate() if t.name.startswith("chat-")]
    report.check(not leftover,
                 "no turn is still running once its answer is delivered",
                 f"{len(leftover)} left behind" if leftover else "")

    after = staff_clients[0].get("/api/state")
    report.check(after.status_code == 200,
                 "the server is still answering afterwards",
                 f"GET /api/state -> {after.status_code}")

    heading("10. One person, two devices, first ever sign-in, same instant")

    clashes = Counter()
    for round_number in range(8):
        name = f"twindev{round_number:02d}"
        joined = staff(args.users)
        joined[f"CN={name},OU=Staff,{BASE_DN}"] = {
            "sAMAccountName": name, "displayName": "Two Devices",
            "mail": f"{name}@sim.internal",
            "userPrincipalName": f"{name}@sim.internal",
            "userPassword": PASSWORD, "objectClass": "person",
        }
        install_fake_domain(joined)
        devices = [Employee(name, port) for _ in range(4)]
        got, _, _ = at_once(lambda i: devices[i].sign_in(), len(devices))
        clashes.update(str(o[0].status_code) for o in got if o)
        for device in devices:
            device.close()

    report.check(all(code == "200" for code in clashes),
                 "a phone, a laptop and two tabs all get in",
                 ", ".join(f"{code}: {count}" for code, count in sorted(clashes.items())))

    duplicated = [name for name, count in
                  Counter(u["username"] for u in users.listing()).items() if count > 1]
    report.check(not duplicated,
                 "and only one account was created for each of them",
                 f"duplicated: {duplicated[:3]}" if duplicated else "")

    heading("11. Nine o'clock tomorrow: everybody signs in again at once")

    returning = [Employee(name, port) for name in names]
    outcomes, errors, elapsed = at_once(
        lambda i: returning[i].sign_in(), len(returning))

    got_in = [o for o in outcomes if o and o[0].status_code == 200]
    waits = sorted(o[1] for o in outcomes if o)
    report.check(not errors, "no thread raised", "; ".join(errors[:3]))
    report.check(len(got_in) == len(returning),
                 f"all {len(returning)} are back in",
                 f"{len(got_in)} in {elapsed:.1f}s")

    if waits:
        median = waits[len(waits) // 2] * 1000
        worst = waits[-1] * 1000
        print(f"            {GREY}waited: median {median:.0f}ms, "
              f"slowest {worst:.0f}ms{OFF}")
        report.check(worst < 30_000,
                     "nobody waits anything like a timeout to get in",
                     "the accounts database takes one writer at a time, so a "
                     "simultaneous rush queues - it does not fail, and it does "
                     "not get worse as the audit trail grows")

    for person in returning:
        person.close()
    for person in staff_clients:
        person.close()


if __name__ == "__main__":
    raise SystemExit(main())
