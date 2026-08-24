"""Generate a large, realistic Kazakhstan deposit dataset for testing.

The shipped sample is 1,269 rows over six months, which hides almost
everything worth testing: no trend to speak of, no branch or client detail,
nothing for the cleaning tools to find, and every chart fits on one screen
whatever the assistant does. This builds the same domain at a size where
those things start to matter.

    python generate_test_data.py                  # ~1.1M rows, the default
    python generate_test_data.py --accounts 5000  # small, for a quick loop
    python generate_test_data.py --months 60      # five years of history

Four files, a star schema around one fact table:

    deposits.csv   one row per account per month-end - the fact table
    clients.csv    who holds the account
    branches.csv   where it is held, and which region that is
    products.csv   what the account actually is

Every field lives in exactly ONE table, which is what keeps the model clean:
Qlik associates tables by shared field NAMES, so a field present on both
sides joins the key rather than travelling with it, and Qlik then builds a
synthetic table to resolve the ambiguity. Region therefore lives only on
branches and TermMonths only on products - both still work as dimensions
anywhere in the app, reaching the fact through BranchID and ProductID.

Currency, deposit_type and agent stay on the fact table, so every chart
already built against those names keeps working.

The data has faults on purpose. Two constant columns, some 'N/A' and '-'
placeholders, a few percent of blanks, and text with stray whitespace: all
of it is what data_model's quality checks and build_load_script's
drop_fields / null_tokens exist to deal with, and none of it can be tested
against data that is already clean.
"""

import argparse
import csv
import os
import random
from datetime import date, timedelta

# Real regions and their real share of the deposit book, taken from the
# National Bank figures already loaded in the app. Invented weights would
# make every concentration insight fictional.
REGIONS = [
    ("г.Алматы", 39.1), ("г.Астана", 20.3), ("Карагандинская область", 5.0),
    ("г.Шымкент", 3.7), ("Восточно-Казахстанская область", 3.5),
    ("Костанайская область", 3.4), ("Павлодарская область", 2.9),
    ("Актюбинская область", 2.8), ("Западно-Казахстанская область", 2.7),
    ("Атырауская область", 2.6), ("Мангистауская область", 2.2),
    ("Акмолинская область", 2.0), ("Северо-Казахстанская область", 1.7),
    ("Жамбылская область", 1.6), ("Область Абай", 1.3),
    ("Кызылординская область", 1.2), ("Туркестанская область", 1.1),
    ("Область Жетісу", 1.0), ("Алматинская область", 1.4),
    ("Область Ұлытау", 0.5),
]

CURRENCIES = [("Национальная валюта", 81.1), ("Иностранная валюта", 18.9)]
DEPOSIT_TYPES = [
    ("срочные и условные", 90.8), ("сберегательные", 9.1),
    ("вклады до востребования", 0.1),
]
AGENTS = [("Физические лица", 68.2), ("Юридические лица", 31.8)]
CHANNELS = ["Отделение", "Мобильное приложение", "Интернет-банк", "Колл-центр"]
RISK = ["Низкий", "Средний", "Повышенный", "Высокий"]
SEGMENTS = ["Массовый", "Премиум", "Private", "Малый бизнес", "Корпоративный"]
STATUSES = ["Действующий", "Закрыт", "Пролонгирован"]

# Constant on every row, exactly as in the shipped file. data_model should
# spot it and the assistant should offer to drop it.
UNIT = "Остаток (млн. тенге)"

FIRST_NAMES = ["Айгуль", "Данияр", "Асель", "Нурлан", "Мадина", "Ерлан",
               "Гульнара", "Тимур", "Сауле", "Арман", "Жанна", "Бекзат"]
LAST_NAMES = ["Ахметов", "Оспанов", "Сериков", "Жумабаев", "Токтаров",
              "Абдуллин", "Нурпеисов", "Сагындыков", "Искаков", "Байжанов"]
LEGAL_FORMS = ["ТОО", "АО", "ИП", "ПК"]
LEGAL_NAMES = ["КазМунай", "АлматыСтрой", "СтепьАгро", "ТехноЛогистик",
               "АстанаТрейд", "КаспийОйл", "ЖолСервис", "АгроЭкспорт"]

PRODUCT_FAMILIES = ["Депозит", "Накопительный", "Сберегательный", "Текущий"]


def weighted(items):
    """(value, weight) pairs -> a picker that respects the weights."""
    values = [v for v, _ in items]
    weights = [w for _, w in items]
    return lambda: random.choices(values, weights=weights, k=1)[0]


pick_region = weighted(REGIONS)
pick_currency = weighted(CURRENCIES)
pick_type = weighted(DEPOSIT_TYPES)
pick_agent = weighted(AGENTS)


def month_ends(start_year, start_month, count):
    """The last day of each of `count` months, as date objects."""
    out = []
    year, month = start_year, start_month
    for _ in range(count):
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)
        out.append(nxt - timedelta(days=1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def qlik_date(d):
    """DD.MM.YYYY - the format the existing file and its load script use."""
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def build_branches(count):
    """Branches, spread across regions in proportion to their deposits."""
    rows = []
    for i in range(1, count + 1):
        region = pick_region()
        city = region.replace("г.", "").replace(" область", "")
        manager = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        rows.append({
            "BranchID": f"BR{i:04d}",
            "BranchName": f"Отделение №{i} {city}",
            "Region": region,
            "City": city,
            # ~8% have no manager on record - a realistic gap, and something
            # the quality report should mention rather than the model
            # inventing a name for.
            "Manager": "" if random.random() < 0.08 else manager,
            "OpenDate": qlik_date(date(random.randint(1998, 2023),
                                       random.randint(1, 12),
                                       random.randint(1, 28))),
            "StaffCount": random.randint(4, 60),
        })
    return rows


def build_products():
    """A small product catalogue - deliberately low cardinality."""
    rows = []
    pid = 1
    for family in PRODUCT_FAMILIES:
        for term in (3, 6, 12, 24, 36):
            if family == "Текущий" and term != 3:
                continue          # a current account has no term
            rows.append({
                "ProductID": f"P{pid:03d}",
                "ProductName": f"{family} {term} мес." if family != "Текущий"
                               else "Текущий счёт",
                "ProductFamily": family,
                "TermMonths": 0 if family == "Текущий" else term,
                "BaseRate": round(random.uniform(0.5, 3.0), 2) if family == "Текущий"
                            else round(11.0 + term * 0.12 + random.uniform(-0.8, 0.8), 2),
                "MinAmount": random.choice([0, 1000, 5000, 50000, 100000]),
            })
            pid += 1
    return rows


def build_clients(count):
    """Clients, plus the agent type of each so an account can inherit it.

    An account whose agent type disagrees with its client's is not a hard
    error anywhere - it just quietly makes every "individuals vs legal
    entities" chart meaningless, which is worse.
    """
    rows = []
    agents = {}
    for i in range(1, count + 1):
        agent = pick_agent()
        agents[f"C{i:06d}"] = agent
        if agent == "Физические лица":
            name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            segment = random.choices(
                ["Массовый", "Премиум", "Private"], weights=[80, 17, 3], k=1)[0]
            birth = random.randint(1955, 2006)
        else:
            name = (f"{random.choice(LEGAL_FORMS)} "
                    f'"{random.choice(LEGAL_NAMES)}"')
            segment = random.choices(
                ["Малый бизнес", "Корпоративный"], weights=[75, 25], k=1)[0]
            birth = ""
        # Stray whitespace on a few names, the way a real export has it.
        if random.random() < 0.03:
            name = f"  {name} "
        rows.append({
            "ClientID": f"C{i:06d}",
            "ClientName": name,
            "ClientSegment": segment,
            "BirthYear": birth,
            # '-' is a placeholder, not a rating: null_tokens territory.
            "RiskRating": "-" if random.random() < 0.05 else random.choices(
                RISK, weights=[62, 26, 9, 3], k=1)[0],
            "OnboardDate": qlik_date(date(random.randint(2015, 2026),
                                          random.randint(1, 12),
                                          random.randint(1, 28))),
            "IsVIP": 1 if random.random() < 0.04 else 0,
        })
    return rows, agents


def write_csv(path, rows, fieldnames):
    # utf-8-sig: the BOM is what makes Qlik pick UTF-8 rather than the
    # system codepage, without which every Russian value arrives as mojibake.
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return os.path.getsize(path)


def generate(out_dir, accounts, months, branches, clients, seed):
    random.seed(seed)
    os.makedirs(out_dir, exist_ok=True)

    branch_rows = build_branches(branches)
    product_rows = build_products()
    client_rows, client_agents = build_clients(clients)

    write_csv(os.path.join(out_dir, "branches.csv"), branch_rows,
              list(branch_rows[0]))
    write_csv(os.path.join(out_dir, "products.csv"), product_rows,
              list(product_rows[0]))
    write_csv(os.path.join(out_dir, "clients.csv"), client_rows,
              list(client_rows[0]))

    dates = month_ends(2024, 1, months)

    # Each account gets a fixed identity and a balance that then moves month
    # to month, so a trend chart shows a real trend rather than noise.
    # Branches are picked in proportion to their region's share of the book,
    # which is what puts ~39% of accounts in Almaty without Region ever being
    # written to the fact table.
    branch_ids = [b["BranchID"] for b in branch_rows]
    region_share = dict(REGIONS)
    branch_weights = [region_share.get(b["Region"], 1.0) for b in branch_rows]

    client_ids = list(client_agents)

    book = []
    for i in range(1, accounts + 1):
        currency = pick_currency()
        product = random.choice(product_rows)
        client_id = random.choice(client_ids)
        branch_id = random.choices(branch_ids, weights=branch_weights, k=1)[0]
        opened = random.randrange(months)
        # Log-normal-ish: a few very large accounts, most of them small.
        # A flat range would make every distribution chart a rectangle.
        base = random.lognormvariate(3.2, 1.15)
        if currency == "Иностранная валюта":
            base *= 1.6
        book.append({
            "AccountID": f"A{i:07d}",
            "ClientID": client_id,
            "BranchID": branch_id,
            "ProductID": product["ProductID"],
            "Currency": currency,
            "deposit_type": pick_type(),
            # Inherited, not re-rolled: the client is the one who decides
            # whether this is an individual's account or a company's.
            "agent": client_agents[client_id],
            "InterestRate": round(product["BaseRate"]
                                  + random.uniform(-1.2, 1.2), 2),
            "opened_at": opened,
            # A quarter of accounts close at some point - without churn the
            # account count is a flat line and "new vs closed" is untestable.
            "closes_at": (opened + random.randint(6, months + 12)
                          if random.random() < 0.25 else None),
            "balance": base,
            "drift": random.uniform(-0.015, 0.03),
        })

    # Region lives on branches and TermMonths on products, and neither is
    # repeated here. Qlik associates tables by shared field NAMES, so a field
    # present on both sides joins the key rather than travelling with it, and
    # Qlik builds a synthetic table to resolve the ambiguity. Region is still
    # usable as a dimension everywhere - it reaches the fact through BranchID.
    fields = [
        "Report Date", "SUM", "Type", "agent", "Currency",
        "deposit_type", "AccountID", "ClientID", "BranchID", "ProductID",
        "InterestRate", "Channel", "Status", "AccruedInterest",
        "IsNewAccount", "LoadedAt",
    ]

    path = os.path.join(out_dir, "deposits.csv")
    written = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)

        for index, snapshot in enumerate(dates):
            # Sector growth plus a December bump, so the trend has a shape
            # worth commenting on instead of a straight line.
            trend = 1.0 + index * 0.006
            seasonal = 1.05 if snapshot.month == 12 else 1.0

            for account in book:
                if index < account["opened_at"]:
                    continue
                closes = account["closes_at"]
                if closes is not None and index > closes:
                    continue

                account["balance"] *= (1 + account["drift"]
                                       + random.uniform(-0.05, 0.05))
                amount = account["balance"] * trend * seasonal
                if amount <= 0:
                    continue

                is_new = index == account["opened_at"]
                status = ("Закрыт" if closes is not None and index == closes
                          else random.choices(STATUSES[:1] + STATUSES[2:],
                                              weights=[92, 8], k=1)[0])

                writer.writerow([
                    qlik_date(snapshot),
                    f"{amount:.4f}",
                    UNIT,
                    account["agent"],
                    account["Currency"],
                    account["deposit_type"],
                    account["AccountID"],
                    account["ClientID"],
                    account["BranchID"],
                    account["ProductID"],
                    account["InterestRate"],
                    # 'N/A' rather than blank: a placeholder that looks like
                    # a real value until null_tokens turns it into one.
                    "N/A" if random.random() < 0.04 else random.choice(CHANNELS),
                    status,
                    "" if random.random() < 0.02
                    else f"{amount * account['InterestRate'] / 1200:.4f}",
                    1 if is_new else 0,
                    "2026-07-01 03:00:00",
                ])
                written += 1

    return {
        "deposits.csv": (written, os.path.getsize(path)),
        "clients.csv": (len(client_rows), 0),
        "branches.csv": (len(branch_rows), 0),
        "products.csv": (len(product_rows), 0),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=r"C:\Users\Hacker\Documents\1\bigdata",
                        help="folder to write into (must be reachable by a Qlik connection)")
    parser.add_argument("--accounts", type=int, default=45_000)
    parser.add_argument("--months", type=int, default=30,
                        help="month-ends from January 2024 onwards")
    parser.add_argument("--branches", type=int, default=120)
    parser.add_argument("--clients", type=int, default=40_000)
    parser.add_argument("--seed", type=int, default=20260821)
    args = parser.parse_args()

    print(f"writing to {args.out}")
    result = generate(args.out, args.accounts, args.months,
                      args.branches, args.clients, args.seed)

    for name, (rows, size) in result.items():
        note = f"  ({size / 2**20:.0f} MB)" if size else ""
        print(f"  {name:<14} {rows:>9,} rows{note}")


if __name__ == "__main__":
    main()
