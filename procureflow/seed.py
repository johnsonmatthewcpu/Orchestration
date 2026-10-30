"""Demo organisation so the UI has something to click through."""
import datetime as dt


def seed(engine):
    if engine.db.scalar("SELECT COUNT(*) FROM users"):
        return False
    d = {name: engine.create_department(name, cc) for name, cc in
         [("Engineering", "CC-100"), ("Marketing", "CC-200"), ("Finance", "CC-300"), ("Operations", "CC-400")]}
    u = {}

    def user(key, name, role, dept, manager=None):
        u[key] = engine.create_user(name, f"{key}@example.com", role, d[dept], u.get(manager))

    user("admin", "Avery Admin", "admin", "Operations")
    user("adm", "Dana ADM", "adm", "Operations")
    user("cfo", "Casey CFO", "cfo", "Finance")
    user("finance", "Frankie Finance", "finance", "Finance", "cfo")
    user("ap", "Alex Payables", "ap", "Finance", "finance")
    user("procurement", "Parker Procurement", "procurement", "Operations", "admin")
    user("legal", "Logan Legal", "legal", "Operations", "admin")
    user("security", "Sam Security", "security", "Engineering", "admin")
    user("it", "Indy IT", "it", "Engineering", "admin")
    user("eng_mgr", "Morgan Manager", "employee", "Engineering", "admin")
    user("engineer", "Jordan Engineer", "employee", "Engineering", "eng_mgr")
    user("mkt_mgr", "Riley Marketing Lead", "employee", "Marketing", "admin")
    user("marketer", "Taylor Marketer", "employee", "Marketing", "mkt_mgr")

    year = engine.today().year
    for dept, cat, amt in [("Engineering", "Software", 250000), ("Engineering", "Hardware", 80000),
                           ("Marketing", "Marketing", 150000), ("Marketing", "Software", 40000),
                           ("Operations", "Facilities", 120000), ("Finance", "Professional Services", 60000)]:
        engine.set_budget(d[dept], cat, year, amt, u["finance"])

    acme = engine.create_vendor("Acme Cloud", "billing@acme.example", "11-1111111", "active", "low")
    engine.create_vendor("Globex Hardware", "ar@globex.example", "22-2222222", "active", "medium")
    engine.create_vendor("Initech Consulting", "ap@initech.example", "33-3333333", "active", "low")
    engine.create_vendor("Shady Data LLC", None, None, "blocked", "high")

    # An existing contract coming up for renewal so the renewal job has work to do.
    today = engine.today()
    engine.create_contract(acme, "Acme Cloud hosting", 36000, (today - dt.timedelta(days=300)).isoformat(),
                           (today + dt.timedelta(days=65)).isoformat(), notice_days=30, actor_id=u["procurement"])
    return True
